"""PhaseEnhancementSet CRUD end-to-end (real Postgres, migration 0031).

Persist a wash-out measurement from per-phase samples (no S3: the indices
are recomputed purely), list/get/delete/restore it, and pin the
cross-study sample guard + soft-delete semantics.
"""

from __future__ import annotations

import uuid

from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.auth import optional_user, require_user
from bvphoenix.db.models import Series, User
from bvphoenix.db.session import get_db
from bvphoenix.main import app
from tests.conftest import skip_if_no_db

pytestmark = skip_if_no_db


def _client_as(session: AsyncSession, user: User | None) -> AsyncClient:
    async def _db():
        yield session

    async def _usr():
        if user is None:
            raise HTTPException(status_code=401, detail="not authenticated")
        return user

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[require_user] = _usr
    app.dependency_overrides[optional_user] = _usr
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _add_series(db: AsyncSession, study_id: uuid.UUID, desc: str, n: int) -> Series:
    s = Series(
        id=uuid.uuid4(),
        study_id=study_id,
        series_instance_uid=f"1.2.840.{uuid.uuid4().int}"[:64],
        series_number=n,
        modality="CT",
        body_part_examined="ABDOMEN",
        series_description=desc,
    )
    db.add(s)
    await db.flush()
    return s


async def test_phase_enhancement_set_crud(db_session, make_user, make_study) -> None:
    owner = await make_user()
    study, s0 = await make_study(owner, modality="CT", body_part="ABDOMEN")
    s0.acquisition_phase = "unenhanced"
    s1 = await _add_series(db_session, study.id, "portal venous", 2)
    s2 = await _add_series(db_session, study.id, "delayed", 3)
    await db_session.commit()

    samples = [
        {"series_id": str(s0.id), "acquisition_phase": "unenhanced", "hu_mean": 10.0},
        {"series_id": str(s1.id), "acquisition_phase": "portal_venous", "hu_mean": 100.0},
        {"series_id": str(s2.id), "acquisition_phase": "delayed", "hu_mean": 40.0},
    ]
    roi = {"center_lps": [1.0, 2.0, 3.0], "radius_mm": 8.0, "frame_of_reference_uid": "1.2.3"}

    try:
        async with _client_as(db_session, owner) as client:
            # Create: indices recomputed from samples (APW 66.7%, RPW 60%).
            r = await client.post(
                f"/api/studies/{study.id}/phase-enhancement-sets",
                json={
                    "roi_kind": "sphere",
                    "roi": roi,
                    "label": "Adrenal nodule",
                    "samples": samples,
                },
            )
            assert r.status_code == 200, r.text
            created = r.json()
            set_id = created["id"]
            assert created["author_kind"] == "human"
            assert created["enhanced_phase"] == "portal_venous"
            assert created["delayed_phase"] == "delayed"
            assert abs(created["apw"] - (100 * 60 / 90)) < 1e-6
            assert abs(created["rpw"] - 60.0) < 1e-6
            assert created["washout"]["apw_ge_60"] is True
            assert created["etag"] and created["etag"] != "dry-run"

            # dry_run does not persist.
            r = await client.post(
                f"/api/studies/{study.id}/phase-enhancement-sets",
                json={"roi_kind": "sphere", "roi": roi, "samples": samples, "dry_run": True},
            )
            assert r.status_code == 200
            assert r.json()["etag"] == "dry-run"

            # List has exactly the one persisted set.
            r = await client.get(f"/api/studies/{study.id}/phase-enhancement-sets")
            assert r.status_code == 200
            assert [s["id"] for s in r.json()] == [set_id]

            # Get by id.
            r = await client.get(f"/api/phase-enhancement-sets/{set_id}")
            assert r.status_code == 200
            assert r.json()["label"] == "Adrenal nodule"

            # Cross-study guard: a foreign series id -> 422.
            r = await client.post(
                f"/api/studies/{study.id}/phase-enhancement-sets",
                json={
                    "roi_kind": "sphere",
                    "roi": roi,
                    "samples": [
                        {
                            "series_id": str(uuid.uuid4()),
                            "acquisition_phase": "portal_venous",
                            "hu_mean": 100.0,
                        }
                    ],
                },
            )
            assert r.status_code == 422, r.text

            # Soft-delete -> excluded from default list, present with include_deleted.
            r = await client.delete(f"/api/phase-enhancement-sets/{set_id}?reason=mistake")
            assert r.status_code == 200
            assert r.json()["deleted_at"] is not None
            r = await client.get(f"/api/studies/{study.id}/phase-enhancement-sets")
            assert r.json() == []
            r = await client.get(
                f"/api/studies/{study.id}/phase-enhancement-sets?include_deleted=true"
            )
            assert [s["id"] for s in r.json()] == [set_id]

            # Restore.
            r = await client.post(f"/api/phase-enhancement-sets/{set_id}/restore")
            assert r.status_code == 200
            assert r.json()["deleted_at"] is None
    finally:
        app.dependency_overrides.clear()
