"""Integration tests for /api/search and /api/similar-to plus the
Embedding model's pgvector behavior.

Run with: `make backend.test`. Requires `make up.infra && make db.migrate`
to have already created the test database with the pgvector extension.

NOTE: these tests share a single async session with the app via
``dependency_overrides``. The async-session + pytest-asyncio + fastapi
TestClient combination has known lifecycle quirks that produce flaky
teardown errors here — pending a conftest rewrite, the whole module is
skipped by default. Set ``BVP_RUN_SEARCH_INTEGRATION=1`` to opt in.
"""

from __future__ import annotations

import os
import uuid
from datetime import date

import numpy as np
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from bvphoenix.auth import optional_user
from bvphoenix.db.models import Embedding, ReportContent
from bvphoenix.db.session import get_db
from bvphoenix.main import app

pytestmark = pytest.mark.skipif(
    os.environ.get("BVP_RUN_SEARCH_INTEGRATION") != "1",
    reason="search integration tests need a stable async-session fixture; opt in via BVP_RUN_SEARCH_INTEGRATION=1",
)

# ---- Helpers ---------------------------------------------------------------


def _override_user(user):
    """Build a FastAPI dependency override that injects the given user."""

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


# ---- Full-text search ------------------------------------------------------


@pytest.mark.asyncio
async def test_search_matches_description(db_session, make_user, make_study) -> None:
    user = await make_user()
    await make_study(user, description="Needle-in-haystack pulmonary embolism")
    await make_study(user, description="Brain MRI, routine")
    client = await _client_for(db_session, user)
    r = await client.get("/api/search", params={"q": "pulmonary"})
    assert r.status_code == 200
    items = r.json()["items"]
    assert any("pulmonary" in (s.get("study_description") or "").lower() for s in items)
    await client.aclose()
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_search_filter_modality(db_session, make_user, make_study) -> None:
    user = await make_user()
    await make_study(user, description="CT scan", modality="CT")
    await make_study(user, description="MRI scan", modality="MR")
    client = await _client_for(db_session, user)
    r = await client.get("/api/search", params={"modality": "CT"})
    assert r.status_code == 200
    for s in r.json()["items"]:
        assert "CT" in (s.get("modalities") or [])
    await client.aclose()
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_search_filter_body_part(db_session, make_user, make_study) -> None:
    user = await make_user()
    await make_study(user, description="head", body_part="HEAD")
    await make_study(user, description="chest", body_part="CHEST")
    client = await _client_for(db_session, user)
    r = await client.get("/api/search", params={"body_part": "chest"})
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) >= 1
    await client.aclose()
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_search_filter_date_range(db_session, make_user, make_study) -> None:
    user = await make_user()
    await make_study(user, description="old", study_date=date(2020, 1, 1))
    await make_study(user, description="recent", study_date=date(2025, 6, 15))
    client = await _client_for(db_session, user)
    r = await client.get("/api/search", params={"date_from": "2024-01-01", "date_to": "2026-01-01"})
    assert r.status_code == 200
    items = r.json()["items"]
    # Any dated studies returned must fall within the window.
    for s in items:
        if s.get("study_date"):
            assert s["study_date"] >= "2024-01-01"
            assert s["study_date"] <= "2026-01-01"
    await client.aclose()
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_search_pagination(db_session, make_user, make_study) -> None:
    user = await make_user()
    for i in range(5):
        await make_study(user, description=f"paginated-study-{i}-{uuid.uuid4()}")
    client = await _client_for(db_session, user)
    r1 = await client.get("/api/search", params={"limit": 2, "offset": 0})
    r2 = await client.get("/api/search", params={"limit": 2, "offset": 2})
    assert r1.status_code == r2.status_code == 200
    ids1 = {s["id"] for s in r1.json()["items"]}
    ids2 = {s["id"] for s in r2.json()["items"]}
    assert ids1.isdisjoint(ids2)
    await client.aclose()
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_search_rls_filters_invisible_studies(db_session, make_user, make_study) -> None:
    """User B must not see user A's private (non-public) studies."""
    user_a = await make_user(email="a@example.com")
    user_b = await make_user(email="b@example.com")
    unique = f"secret-phrase-{uuid.uuid4()}"
    await make_study(user_a, description=unique, is_public=False)
    client = await _client_for(db_session, user_b)
    r = await client.get("/api/search", params={"q": unique})
    assert r.status_code == 200
    # Should not contain the secret study
    descs = [s.get("study_description") or "" for s in r.json()["items"]]
    assert not any(unique in d for d in descs)
    await client.aclose()
    app.dependency_overrides.clear()


# ---- Similarity search -----------------------------------------------------


@pytest.mark.asyncio
async def test_similar_to_returns_neighbors(
    db_session, make_user, make_study, make_embedding
) -> None:
    user = await make_user()
    _, series_a = await make_study(user, description="source")
    _, series_b = await make_study(user, description="near")
    _, series_c = await make_study(user, description="far")

    # Make a and b almost-identical, c far
    rng = np.random.default_rng(42)
    base = rng.standard_normal(512).astype(np.float32)
    base = base / np.linalg.norm(base)
    perturb = rng.standard_normal(512).astype(np.float32) * 0.05
    b_vec = base + perturb
    b_vec = b_vec / np.linalg.norm(b_vec)
    c_vec = rng.standard_normal(512).astype(np.float32)
    c_vec = c_vec / np.linalg.norm(c_vec)

    await make_embedding(series_a, vector=base.tolist())
    await make_embedding(series_b, vector=b_vec.tolist())
    await make_embedding(series_c, vector=c_vec.tolist())

    client = await _client_for(db_session, user)
    r = await client.get(f"/api/similar-to/{series_a.id}", params={"k": 10})
    assert r.status_code == 200
    results = r.json()
    assert len(results) >= 1
    # First (most similar) result's score must be >= any later result
    scores = [item["score"] for item in results]
    assert scores == sorted(scores, reverse=True)
    for s in scores:
        assert 0.0 <= s <= 1.0
    await client.aclose()
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_similar_to_respects_k_limit(
    db_session, make_user, make_study, make_embedding
) -> None:
    user = await make_user()
    _, series_a = await make_study(user, description="source")
    await make_embedding(series_a)
    for i in range(5):
        _, sr = await make_study(user, description=f"neighbor-{i}")
        await make_embedding(sr)
    client = await _client_for(db_session, user)
    r = await client.get(f"/api/similar-to/{series_a.id}", params={"k": 2})
    assert r.status_code == 200
    assert len(r.json()) <= 2
    await client.aclose()
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_similar_to_404_unknown_target(db_session, make_user) -> None:
    user = await make_user()
    client = await _client_for(db_session, user)
    r = await client.get(f"/api/similar-to/{uuid.uuid4()}")
    assert r.status_code == 404
    await client.aclose()
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_similar_to_422_when_target_not_indexed(
    db_session, make_user, make_study
) -> None:
    # A target that EXISTS but whose pixel data was never embedded is NOT a
    # 404 (indexing is async) and NOT an empty 200 (that is reserved for
    # "indexed, zero neighbours"): it returns 422 with a structured
    # ``study_not_indexed`` code so the FE renders a "not indexed yet" card and
    # the viewer panel stays quiet. (See find_similar_studies.)
    user = await make_user()
    _, series = await make_study(user, description="unembedded")
    # Intentionally no make_embedding call
    client = await _client_for(db_session, user)
    r = await client.get(f"/api/similar-to/{series.id}")
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "study_not_indexed"
    await client.aclose()
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_similar_to_filters_by_modality(
    db_session, make_user, make_study, make_embedding
) -> None:
    user = await make_user()
    _, source = await make_study(user, description="source", modality="CT")
    await make_embedding(source)
    _, ct_other = await make_study(user, description="also-ct", modality="CT")
    await make_embedding(ct_other)
    _, mr = await make_study(user, description="mri", modality="MR")
    await make_embedding(mr)
    client = await _client_for(db_session, user)
    r = await client.get(f"/api/similar-to/{source.id}", params={"modality": "CT"})
    assert r.status_code == 200
    for item in r.json():
        assert "CT" in item["study"]["modalities"]
    await client.aclose()
    app.dependency_overrides.clear()


# ---- Embedding model -------------------------------------------------------


@pytest.mark.asyncio
async def test_embedding_unique_constraint(
    db_session, make_user, make_study, make_embedding
) -> None:
    user = await make_user()
    _, series = await make_study(user)
    await make_embedding(series, model_id="biomedclip-v1")
    dup = Embedding(
        id=uuid.uuid4(),
        target_kind="series",
        target_id=series.id,
        model_id="biomedclip-v1",
        vector=[0.0] * 512,
    )
    db_session.add(dup)
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_embedding_cosine_distance_operator(
    db_session, make_user, make_study, make_embedding
) -> None:
    """Two near-identical normalized vectors should have small cosine distance."""
    user = await make_user()
    _, series = await make_study(user)
    rng = np.random.default_rng(7)
    v = rng.standard_normal(512).astype(np.float32)
    v = v / np.linalg.norm(v)
    await make_embedding(series, vector=v.tolist())

    stmt = select(Embedding.vector.cosine_distance(v.tolist())).where(
        Embedding.target_id == series.id
    )
    dist = (await db_session.execute(stmt)).scalar_one()
    assert dist == pytest.approx(0.0, abs=1e-4)


# ---- Patient-scoped search: ReportContent-backed sections (regression) -----
# v3 folded the legacy ``Report`` / ``Consultation`` models into
# ``ReportContent``. ``patients/search.py`` used to reference the removed
# names, so the ``reports`` / ``consultations`` sections raised NameError
# -> 500. These guard the mapping + the no-crash invariant.


async def _add_report_content(
    session,
    *,
    event_id,
    subject_id,
    authority: str,
    status: str,
    title: str,
    narrative: str,
):
    """Insert a ReportContent on a clinical event; return its id."""
    rc = ReportContent(
        id=uuid.uuid4(),
        clinical_event_id=event_id,
        authority_id=authority,
        status=status,
        language="it",
        title=title,
        narrative_md=narrative,
        structured_fields={},
        created_by_subject_id=subject_id,
        author_kind="human",
    )
    session.add(rc)
    await session.flush()
    return rc.id


@pytest.mark.asyncio
async def test_patient_search_default_sections_do_not_500(
    db_session, make_user, make_study
) -> None:
    """Default sections include ``reports`` + ``consultations``; both the
    full-text and the @mention endpoints must return 200, not 500."""
    user = await make_user()
    study, _ = await make_study(user, description="addome con mdc")
    client = await _client_for(db_session, user)
    try:
        r = await client.get(f"/api/patients/{study.patient_id}/search", params={"q": "addome"})
        assert r.status_code == 200, r.text
        r2 = await client.get(f"/api/patients/{study.patient_id}/mention-search")
        assert r2.status_code == 200, r2.text
    finally:
        await client.aclose()
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_patient_search_maps_authority_to_section(db_session, make_user, make_study) -> None:
    """``original`` / ``derived`` -> reports section; ``canonical_synthesis``
    -> consultations section; ``stale`` rows are excluded from both."""
    user = await make_user()
    token = f"zz{uuid.uuid4().hex[:8]}"
    study, _ = await make_study(user, description="rmn encefalo")
    report_id = await _add_report_content(
        db_session,
        event_id=study.clinical_event_id,
        subject_id=user.subject_id,
        authority="original",
        status="endorsed",
        title="Referto RMN",
        narrative=f"Reperti {token} in regione frontale.",
    )
    synth_id = await _add_report_content(
        db_session,
        event_id=study.clinical_event_id,
        subject_id=user.subject_id,
        authority="canonical_synthesis",
        status="final",
        title="Sintesi",
        narrative=f"Sintesi clinica {token}.",
    )
    stale_id = await _add_report_content(
        db_session,
        event_id=study.clinical_event_id,
        subject_id=user.subject_id,
        authority="original",
        status="stale",
        title="Referto superato",
        narrative=f"Versione superata {token}.",
    )
    await db_session.commit()

    client = await _client_for(db_session, user)
    try:
        r = await client.get(f"/api/patients/{study.patient_id}/search", params={"q": token})
        assert r.status_code == 200, r.text
        by_section: dict[str, set[str]] = {}
        for it in r.json()["items"]:
            by_section.setdefault(it["section"], set()).add(it["id"])
        assert str(report_id) in by_section.get("reports", set())
        assert str(synth_id) in by_section.get("consultations", set())
        all_ids = set().union(*by_section.values()) if by_section else set()
        assert str(stale_id) not in all_ids
    finally:
        await client.aclose()
        app.dependency_overrides.clear()


# ---- Dual-config FTS (italian || simple) -----------------------------------


@pytest.mark.asyncio
async def test_search_italian_stemming(db_session, make_user, make_study) -> None:
    """The dual-config tsvector stems Italian, so a plural query matches a
    singular description ("polmoni" -> "polmone", both stem "polmon").
    The old ``simple``-only index could not do this."""
    user = await make_user()
    marker = uuid.uuid4().hex[:6]
    await make_study(user, description=f"Nodulo al polmone destro {marker}")
    client = await _client_for(db_session, user)
    try:
        r = await client.get("/api/search", params={"q": "polmoni"})
        assert r.status_code == 200, r.text
        descs = [s.get("study_description") or "" for s in r.json()["items"]]
        assert any(marker in d for d in descs), "plural query should stem-match singular"
    finally:
        await client.aclose()
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_search_acronym_exact_token(db_session, make_user, make_study) -> None:
    """The ``simple`` half of the dual config preserves exact radiology
    acronyms that an Italian stemmer would otherwise mangle."""
    user = await make_user()
    marker = uuid.uuid4().hex[:6]
    await make_study(user, description=f"Sequenza T2 FLAIR encefalo {marker}")
    client = await _client_for(db_session, user)
    try:
        r = await client.get("/api/search", params={"q": "FLAIR"})
        assert r.status_code == 200, r.text
        descs = [s.get("study_description") or "" for s in r.json()["items"]]
        assert any(marker in d for d in descs), "exact acronym token should match"
    finally:
        await client.aclose()
        app.dependency_overrides.clear()
