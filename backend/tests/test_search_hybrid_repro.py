"""Integration coverage for ``GET /api/search/hybrid``.

The endpoint fuses three independent signals (tag, text, image) through
RRF and is expected to degrade gracefully: any individual signal that
errors out should be logged and contribute an empty list, never bubble
a 500 up to the caller. The historical bug — fixed in this same
commit — was that

  1. ``_text_signal`` produced SQL Postgres rejected with
     ``GroupingError`` (``series.series_description`` was not in
     ``GROUP BY`` nor aggregated), and
  2. the ``_safe`` wrapper caught the Python exception but did not roll
     back the asyncpg sub-transaction, so the hydrate query at the end
     of the endpoint inherited an aborted transaction and 500'd.

These tests cover both halves: a query the text signal must match
without 500ing (regression for bug #1), and the explicit assertion that
a deliberately-broken signal does not poison the response (regression
for bug #2).
"""

from __future__ import annotations

import os
import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from bvphoenix.api import search_hybrid as search_hybrid_mod
from bvphoenix.auth import optional_user
from bvphoenix.db.models import Tag
from bvphoenix.db.session import get_db
from bvphoenix.main import app

# Same opt-in gate as ``tests/test_search.py`` — the async-session +
# pytest-asyncio + FastAPI TestClient combination has a documented
# loop-affinity quirk that makes back-to-back tests flake on teardown.
# Run with ``BVP_RUN_SEARCH_INTEGRATION=1`` (each test passes when run
# individually under that flag).
pytestmark = pytest.mark.skipif(
    os.environ.get("BVP_RUN_SEARCH_INTEGRATION") != "1",
    reason="search integration tests need a stable async-session fixture; opt in via BVP_RUN_SEARCH_INTEGRATION=1",
)


def _override_user(user):
    async def _dep():
        return user

    return _dep


def _override_db(session):
    async def _dep():
        yield session

    return _dep


async def _client_for(session, user=None) -> AsyncClient:
    app.dependency_overrides[get_db] = _override_db(session)
    app.dependency_overrides[optional_user] = _override_user(user)
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


@pytest.mark.asyncio
async def test_hybrid_search_pet_matches_via_tag(db_session, make_user, make_study) -> None:
    user = await make_user()
    study, _ = await make_study(
        user,
        description="PET/CT torace addome con FDG",
        modality="PT",
        body_part="WHOLEBODY",
        series_description="PET whole-body, ricostruzione AC",
    )
    db_session.add(
        Tag(
            id=uuid.uuid4(),
            target_kind="study",
            target_id=study.id,
            namespace="modality",
            value="PET",
            created_by_subject_id=user.subject_id,
        )
    )
    await db_session.commit()

    client = await _client_for(db_session, user)
    try:
        r = await client.get("/api/search/hybrid", params={"q": "pet"})
        assert r.status_code == 200, f"unexpected 500: body={r.text}"
        body = r.json()
        ids = [item["study"]["id"] for item in body["items"]]
        assert str(study.id) in ids
    finally:
        await client.aclose()
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_hybrid_search_text_signal_matches_series_description(
    db_session, make_user, make_study
) -> None:
    """Regression for the GroupingError in ``_text_signal``.

    No tag is added, so the only way ``pet`` can surface this study is
    through the text signal (matching ``series.series_description``).
    Before the fix this returned 500 with ``column "series.series_description"
    must appear in the GROUP BY clause``.
    """
    user = await make_user()
    study, _ = await make_study(
        user,
        description="Esame whole body",
        modality="PT",
        body_part="WHOLEBODY",
        series_description="PET FDG with attenuation correction",
    )

    client = await _client_for(db_session, user)
    try:
        r = await client.get("/api/search/hybrid", params={"q": "pet"})
        assert r.status_code == 200, f"unexpected 500: body={r.text}"
        body = r.json()
        ids = [item["study"]["id"] for item in body["items"]]
        assert str(study.id) in ids
    finally:
        await client.aclose()
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_hybrid_search_empty_result_is_200(db_session, make_user, make_study) -> None:
    user = await make_user()
    await make_study(user, description="MRI brain routine", modality="MR")

    client = await _client_for(db_session, user)
    try:
        r = await client.get("/api/search/hybrid", params={"q": "xyznopematch"})
        assert r.status_code == 200, f"unexpected 500 on empty match: body={r.text}"
        assert r.json()["items"] == []
    finally:
        await client.aclose()
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_hybrid_search_isolates_failing_signal(
    db_session, make_user, make_study, monkeypatch
) -> None:
    """Regression for the ``InFailedSqlTransactionError`` cascade.

    A signal is monkeypatched to raise a DB error mid-flight. The
    endpoint must still return 200 with the surviving signal's
    contributions — proof that the ``_safe`` wrapper now scopes failures
    via ``SAVEPOINT`` instead of poisoning the outer transaction.
    """
    user = await make_user()
    study, _ = await make_study(
        user,
        description="PET/CT FDG whole body",
        modality="PT",
        body_part="WHOLEBODY",
    )

    real_text_signal = search_hybrid_mod._text_signal

    async def _broken_text_signal(db, **kwargs):
        # Force an SQL error that aborts the current sub-transaction.
        from sqlalchemy import text

        await db.execute(text("SELECT cast('not-an-int' AS integer)"))
        # Should never reach here — the cast above raises.
        return await real_text_signal(db, **kwargs)

    monkeypatch.setattr(search_hybrid_mod, "_text_signal", _broken_text_signal)

    db_session.add(
        Tag(
            id=uuid.uuid4(),
            target_kind="study",
            target_id=study.id,
            namespace="modality",
            value="PET",
            created_by_subject_id=user.subject_id,
        )
    )
    await db_session.commit()

    client = await _client_for(db_session, user)
    try:
        r = await client.get("/api/search/hybrid", params={"q": "pet"})
        assert r.status_code == 200, f"signal failure should not 500: body={r.text}"
        body = r.json()
        ids = [item["study"]["id"] for item in body["items"]]
        assert str(study.id) in ids
        # The broken signal contributed 0 — surviving signals carried it.
        item = next(it for it in body["items"] if it["study"]["id"] == str(study.id))
        assert item["signals"]["text"] == 0.0
        assert item["signals"]["tag"] > 0
    finally:
        await client.aclose()
        app.dependency_overrides.clear()
