"""Pathology deep-zoom serving — safety-critical API invariants.

The PHI-adjacent guarantees the plan flagged as must-test:

* anonymous callers see ONLY public slides (list + single-slide read);
* tile Cache-Control is ``public`` for public slides (CDN-cacheable) but
  ``private`` for private ones (a shared cache must not leak tissue);
* the descriptor / tiles 409 (not 404) while the pyramid is still being
  built, so the viewer can show a "tiling" state;
* the region endpoint rejects oversize crops (DoS guard on the MCP
  surface).

DB-touching + needs the FastAPI app; skipped without Postgres. S3 is
monkeypatched (no MinIO needed) for the byte-serving paths.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.auth import optional_user, require_user
from bvphoenix.db.models import PathologySlide, Patient, User
from bvphoenix.db.models.principals import Subject
from bvphoenix.db.session import SERVICE_SUBJECT, SessionFactory, get_db, set_current_subject
from bvphoenix.main import app
from tests.conftest import skip_if_no_db

pytestmark = skip_if_no_db


def _override_db(session: AsyncSession):
    async def _dep():
        yield session

    return _dep


def _override_optional(user: User | None):
    async def _dep():
        return user

    return _dep


def _override_require(user: User | None):
    async def _dep():
        if user is None:
            from fastapi import HTTPException

            raise HTTPException(status_code=401, detail="not authenticated")
        return user

    return _dep


def _client_for(session: AsyncSession, user: User | None) -> AsyncClient:
    app.dependency_overrides[get_db] = _override_db(session)
    app.dependency_overrides[optional_user] = _override_optional(user)
    app.dependency_overrides[require_user] = _override_require(user)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


class _FakeStorage:
    def get_object_bytes(self, *, bucket: str, key: str) -> bytes:
        return b"\xff\xd8\xff\xe0fake-jpeg"


def _mk_slide(
    patient_id: uuid.UUID,
    owner_id: uuid.UUID,
    *,
    is_public: bool,
    dzi_ready: bool,
) -> PathologySlide:
    ready_cols = (
        {
            "s3_dzi_key": f"pathology/{uuid.uuid4()}/dzi/image.dzi",
            "dzi_levels": 3,
            "dzi_tile_size": 512,
            "dzi_overlap": 0,
            "dzi_format": "jpeg",
            "dzi_generator_version": "tile-v1-noicc",
        }
        if dzi_ready
        else {}
    )
    return PathologySlide(
        id=uuid.uuid4(),
        patient_id=patient_id,
        owner_subject_id=owner_id,
        slide_instance_uid=str(uuid.uuid4()),
        source_format="svs",
        slide_class="wsi",
        s3_bucket="bvphoenix-raw",
        s3_source_key=f"patients/{patient_id}/pathology/x/source.svs",
        size_bytes=1000,
        content_sha256="0" * 64,
        base_width=2048,
        base_height=2048,
        is_public=is_public,
        ingestion_complete=True,
        dzi_ready=dzi_ready,
        **ready_cols,
    )


@pytest_asyncio.fixture
async def slides() -> AsyncIterator[tuple[AsyncSession, User, dict[str, PathologySlide]]]:
    """One owner + patient with three slides: public/ready, private/ready,
    private/not-ready. Rolled back at teardown."""
    session = SessionFactory()
    await set_current_subject(session, SERVICE_SUBJECT)
    owner_sub = Subject(id=uuid.uuid4(), kind="user", display_name="owner")
    session.add(owner_sub)
    await session.flush()
    user = User(id=uuid.uuid4(), email=f"{uuid.uuid4()}@t.test", subject_id=owner_sub.id)
    patient = Patient(id=uuid.uuid4(), managed_by_subject_id=None)
    session.add(user)
    session.add(patient)
    await session.flush()

    s = {
        "public_ready": _mk_slide(patient.id, owner_sub.id, is_public=True, dzi_ready=True),
        "private_ready": _mk_slide(patient.id, owner_sub.id, is_public=False, dzi_ready=True),
        "private_pending": _mk_slide(patient.id, owner_sub.id, is_public=False, dzi_ready=False),
    }
    for slide in s.values():
        session.add(slide)
    await session.flush()
    try:
        yield session, user, s
    finally:
        await session.rollback()
        await session.close()
        app.dependency_overrides.clear()


async def test_list_anonymous_sees_only_public(slides) -> None:
    session, _user, s = slides
    async with _client_for(session, None) as client:
        resp = await client.get("/api/pathology-slides", params={"public_only": True})
    assert resp.status_code == 200
    ids = {row["id"] for row in resp.json()}
    assert str(s["public_ready"].id) in ids
    assert str(s["private_ready"].id) not in ids
    assert str(s["private_pending"].id) not in ids


async def test_get_private_slide_anonymous_404(slides) -> None:
    session, _user, s = slides
    async with _client_for(session, None) as client:
        resp = await client.get(f"/api/pathology-slides/{s['private_ready'].id}")
    assert resp.status_code == 404


async def test_dzi_409_when_not_ready(slides) -> None:
    session, user, s = slides
    async with _client_for(session, user) as client:
        resp = await client.get(f"/api/pathology-slides/{s['private_pending'].id}/dzi")
    assert resp.status_code == 409


async def test_tile_cache_control_private_vs_public(slides, monkeypatch) -> None:
    session, user, s = slides
    monkeypatch.setattr("bvphoenix.api.pathology.get_s3_storage", lambda: _FakeStorage())

    # Private slide, owner: tiles must be browser-private (no shared cache).
    async with _client_for(session, user) as client:
        priv = await client.get(f"/api/pathology-slides/{s['private_ready'].id}/tiles/0/0/0")
    assert priv.status_code == 200
    assert priv.headers["cache-control"].startswith("private")
    assert "immutable" in priv.headers["cache-control"]

    # Public slide, anonymous: tiles may be shared-cached.
    async with _client_for(session, None) as client:
        pub = await client.get(f"/api/pathology-slides/{s['public_ready'].id}/tiles/0/0/0")
    assert pub.status_code == 200
    assert pub.headers["cache-control"].startswith("public")


async def test_region_rejects_oversize(slides) -> None:
    session, user, s = slides
    async with _client_for(session, user) as client:
        resp = await client.get(
            f"/api/pathology-slides/{s['private_ready'].id}/region",
            params={"x": 0, "y": 0, "w": 5000, "h": 5000, "level": 0},
        )
    assert resp.status_code == 413  # 25 MP > 16 MP cap
