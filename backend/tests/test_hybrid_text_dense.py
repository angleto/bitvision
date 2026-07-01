"""Phase 2 (fed1a35a): the multilingual dense-text arm of /api/search/hybrid.

Seeds a private study with a coarse ``report_content`` AND ``finding`` text
vector, then drives the endpoint with a monkeypatched encoder so the query
vector matches the seeded vectors exactly (cosine distance 0). Asserts:

* the study surfaces via the ``text_dense`` signal (signals.text_dense > 0),
  isolated by a nonsense query that matches no tag / description / image arm;
* a DIFFERENT owner's private study carrying the SAME matching vector does
  NOT leak (visibility is enforced in the projection);
* when the encoder is unavailable the arm degrades to empty (endpoint 200, no
  text_dense contribution) without failing the request.

DB-backed: skips in the no-DB ``backend-test`` job, runs in ``backend-db-test``.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text as sa_text

from bvphoenix.auth import optional_user
from bvphoenix.db.models import Finding, ReportContent, TextEmbedding
from bvphoenix.db.session import get_db
from bvphoenix.main import app
from bvphoenix.services import study_text_search
from tests.conftest import skip_if_no_db

pytestmark = [pytest.mark.asyncio, skip_if_no_db]

# 384-dim unit vector (MiniLM store dim). The monkeypatched encoder returns
# this same vector, so cosine distance to the seeded rows is 0 → rank 1.
_MATCH_VEC = [1.0] + [0.0] * 383
_TEXT_MODEL_ID = "minilm-multi-v1"
# A nonsense query so ONLY the text_dense arm (via the fake encoder) can
# surface the study — no tag / description / image match.
_NONSENSE_Q = "zzqxwvunon"


def _override_db(session):
    async def _dep():
        yield session

    return _dep


def _override_user(user):
    async def _dep():
        return user

    return _dep


async def _client_for(session, user) -> AsyncClient:
    app.dependency_overrides[get_db] = _override_db(session)
    app.dependency_overrides[optional_user] = _override_user(user)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest_asyncio.fixture
async def dense_fixture(db_session, make_user, make_study):
    """owner1 with a private study (report_content + finding, both vectored),
    plus owner2 with a private study carrying the same matching vector."""
    # canonical_synthesis + 'draft' is a valid (authority, status) combo per
    # ck_report_contents_authority_status.
    authority_id = "canonical_synthesis"
    finding_type_id = (
        await db_session.execute(sa_text("SELECT id FROM finding_types LIMIT 1"))
    ).scalar_one()

    seeded_targets: list[uuid.UUID] = []

    async def _study_with_text(owner):
        study, _series = await make_study(owner, is_public=False)
        rc = ReportContent(
            id=uuid.uuid4(),
            clinical_event_id=study.clinical_event_id,
            authority_id=authority_id,
            status="draft",
            author_kind="human",
            created_by_subject_id=owner.subject_id,
            narrative_md="referto di prova",
        )
        db_session.add(rc)
        finding = Finding(
            id=uuid.uuid4(),
            patient_id=study.patient_id,
            study_id=study.id,
            finding_type_id=finding_type_id,
            author_kind="human",
            description="lesione di prova",
        )
        db_session.add(finding)
        await db_session.flush()
        for kind, tid in (("report_content", rc.id), ("finding", finding.id)):
            db_session.add(
                TextEmbedding(
                    target_kind=kind,
                    target_id=tid,
                    model_id=_TEXT_MODEL_ID,
                    vector=list(_MATCH_VEC),
                )
            )
            seeded_targets.append(tid)
        await db_session.flush()
        return study

    owner1 = await make_user()
    owner2 = await make_user()
    study1 = await _study_with_text(owner1)
    study2 = await _study_with_text(owner2)
    await db_session.commit()

    yield {"owner1": owner1, "study1": study1, "study2": study2}

    # TextEmbedding is a loose store (no FK cascade) — delete explicitly.
    # ReportContent / Finding cascade when make_study drops the study/event.
    for tid in seeded_targets:
        await db_session.execute(
            TextEmbedding.__table__.delete().where(TextEmbedding.target_id == tid)
        )
    await db_session.commit()
    app.dependency_overrides.clear()


async def test_text_dense_arm_surfaces_own_study(
    dense_fixture, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_encode(_model_id: str, _q: str):
        return list(_MATCH_VEC)

    monkeypatch.setattr(study_text_search, "_embed_active_query", fake_encode)
    client = await _client_for(db_session, dense_fixture["owner1"])
    try:
        resp = await client.get("/api/search/hybrid", params={"q": _NONSENSE_Q, "k": 20})
    finally:
        await client.aclose()
        app.dependency_overrides.clear()

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "text_dense" in body["weights_used"]
    by_id = {item["study"]["id"]: item for item in body["items"]}
    sid1 = str(dense_fixture["study1"].id)
    assert sid1 in by_id, "owner's study should surface via the text_dense arm"
    assert by_id[sid1]["signals"]["text_dense"] > 0.0
    # The other owner's private study must NOT leak, despite the same vector.
    assert str(dense_fixture["study2"].id) not in by_id


async def test_text_dense_arm_degrades_when_encoder_unavailable(
    dense_fixture, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def no_encoder(_model_id: str, _q: str):
        return None

    monkeypatch.setattr(study_text_search, "_embed_active_query", no_encoder)
    client = await _client_for(db_session, dense_fixture["owner1"])
    try:
        resp = await client.get("/api/search/hybrid", params={"q": _NONSENSE_Q, "k": 20})
    finally:
        await client.aclose()
        app.dependency_overrides.clear()

    # Endpoint still succeeds; the arm simply contributed nothing.
    assert resp.status_code == 200, resp.text
    by_id = {item["study"]["id"]: item for item in resp.json()["items"]}
    sid1 = str(dense_fixture["study1"].id)
    # Encoder off + nonsense query → the study isn't found; if some other arm
    # surfaced it anyway, its text_dense contribution must be 0.
    if sid1 in by_id:
        assert by_id[sid1]["signals"]["text_dense"] == 0.0


async def test_legacy_weight_string_still_parses(
    dense_fixture, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_encode(_model_id: str, _q: str):
        return list(_MATCH_VEC)

    monkeypatch.setattr(study_text_search, "_embed_active_query", fake_encode)
    client = await _client_for(db_session, dense_fixture["owner1"])
    try:
        # Old three-signal weight string: text_dense inherits its default.
        resp = await client.get(
            "/api/search/hybrid",
            params={"q": _NONSENSE_Q, "k": 20, "weights": "tag:2,text:1,image:2"},
        )
    finally:
        await client.aclose()
        app.dependency_overrides.clear()
    assert resp.status_code == 200, resp.text
    assert resp.json()["weights_used"]["text_dense"] == 2.0


# ---------------------------------------------------------------------------
# 0ece383b — study-level coarse vector (the coverage that lights up the arm)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def study_vec_fixture(db_session, make_user, make_study):
    """owner1 with a private study carrying ONLY a target_kind='study' vector
    (no report_content / finding), plus owner2 with a private study carrying
    the same vector — the leak guard."""
    seeded: list[uuid.UUID] = []

    async def _study_with_vec(owner):
        study, _series = await make_study(owner, is_public=False)
        db_session.add(
            TextEmbedding(
                target_kind="study",
                target_id=study.id,
                model_id=_TEXT_MODEL_ID,
                vector=list(_MATCH_VEC),
            )
        )
        seeded.append(study.id)
        await db_session.flush()
        return study

    owner1 = await make_user()
    owner2 = await make_user()
    study1 = await _study_with_vec(owner1)
    study2 = await _study_with_vec(owner2)
    await db_session.commit()

    yield {"owner1": owner1, "study1": study1, "study2": study2}

    for tid in seeded:
        await db_session.execute(
            TextEmbedding.__table__.delete().where(TextEmbedding.target_id == tid)
        )
    await db_session.commit()
    app.dependency_overrides.clear()


async def test_text_dense_arm_surfaces_study_via_study_vector(
    study_vec_fixture, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_encode(_model_id: str, _q: str):
        return list(_MATCH_VEC)

    monkeypatch.setattr(study_text_search, "_embed_active_query", fake_encode)
    client = await _client_for(db_session, study_vec_fixture["owner1"])
    try:
        resp = await client.get("/api/search/hybrid", params={"q": _NONSENSE_Q, "k": 20})
    finally:
        await client.aclose()
        app.dependency_overrides.clear()

    assert resp.status_code == 200, resp.text
    by_id = {item["study"]["id"]: item for item in resp.json()["items"]}
    sid1 = str(study_vec_fixture["study1"].id)
    assert sid1 in by_id, "owner's study should surface via its target_kind='study' vector"
    assert by_id[sid1]["signals"]["text_dense"] > 0.0
    # The other owner's private study must NOT leak, despite the same vector.
    assert str(study_vec_fixture["study2"].id) not in by_id
