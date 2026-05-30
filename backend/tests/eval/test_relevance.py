"""DB-backed search relevance gate (golden set).

Seeds the fixed corpus from ``golden.py`` into a real pgvector database,
replays each golden query through the *real* ``/api/search`` endpoint,
and asserts:

* **Exact retrieval** — the corpus studies matched equal the relevant
  set (recall == precision == 1.0). On this controlled corpus a
  softened "X of N" would be hiding a bug, so the bar is exact.
* **Perfect ranking** for free-text queries — every matched study is
  relevant, so nDCG@10 and MRR must be 1.0.
* **Cross-patient isolation** — a foreign user issuing a probe token
  unique to the corpus must receive ZERO corpus studies. This is a
  security invariant (asserted at recall@infinity), not a ranking nicety.

Gated behind ``BVP_RUN_SEARCH_INTEGRATION=1`` (needs a Postgres with
pgvector, migrated to head). FTS-only — no ``ai`` extra required, so it
runs in a lightweight CI gate. Vector/semantic eval is a separate,
``ai``-gated harness (future).
"""

from __future__ import annotations

import os
import time

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from bvphoenix.auth import optional_user
from bvphoenix.db.session import get_db
from bvphoenix.main import app
from bvphoenix.services.thesaurus import load_thesaurus
from tests.eval.golden import CORPUS, CROSS_PATIENT_PROBE, QUERIES
from tests.eval.metrics import mrr, ndcg_at_k, percentile, recall_at_k

pytestmark = pytest.mark.skipif(
    os.environ.get("BVP_RUN_SEARCH_INTEGRATION") != "1",
    reason="relevance gate needs a migrated Postgres; opt in via BVP_RUN_SEARCH_INTEGRATION=1",
)


def _client_for(session, user) -> AsyncClient:
    async def _db():
        yield session

    async def _user():
        return user

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[optional_user] = _user
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest_asyncio.fixture
async def seeded_corpus(db_session, make_user, make_study):
    """Seed the golden corpus under one owner; return (owner, marker->id)."""
    owner = await make_user()
    marker_to_id: dict[str, str] = {}
    for spec in CORPUS:
        study, _ = await make_study(
            owner,
            description=spec.description,
            modality=spec.modality,
            body_part=spec.body_part,
        )
        marker_to_id[spec.marker] = str(study.id)
    # Warm the thesaurus cache so synonym-expansion golden queries fire
    # (httpx ASGITransport does not run the app's startup events).
    await load_thesaurus(db_session)
    return owner, marker_to_id


@pytest.mark.asyncio
async def test_relevance_golden_set(seeded_corpus, db_session) -> None:
    owner, marker_to_id = seeded_corpus
    corpus_ids = set(marker_to_id.values())
    client = _client_for(db_session, owner)
    failures: list[str] = []
    try:
        for gq in QUERIES:
            params: dict[str, str | int] = {"limit": 50}
            if gq.q is not None:
                params["q"] = gq.q
            if gq.modality is not None:
                params["modality"] = gq.modality
            if gq.body_part is not None:
                params["body_part"] = gq.body_part
            r = await client.get("/api/search", params=params)
            assert r.status_code == 200, r.text
            # Restrict to our corpus (the owner sees nothing else, but be
            # defensive against shared-DB noise from parallel tests).
            ranked = [it["id"] for it in r.json()["items"] if it["id"] in corpus_ids]
            relevant = {marker_to_id[m] for m in gq.relevant_markers}
            grades = dict.fromkeys(relevant, 1.0)

            if set(ranked) != relevant:
                failures.append(
                    f"{gq.name}: matched {sorted(ranked)} != relevant {sorted(relevant)} "
                    f"(recall@10={recall_at_k(ranked, relevant, 10):.2f})"
                )
                continue
            if gq.q is not None:
                nd = ndcg_at_k(ranked, grades, 10)
                rr = mrr(ranked, relevant)
                if nd < 1.0 or rr < 1.0:
                    failures.append(f"{gq.name}: ranking nDCG@10={nd:.3f} MRR={rr:.3f} (want 1.0)")
    finally:
        await client.aclose()
        app.dependency_overrides.clear()

    assert not failures, "relevance regressions:\n  " + "\n  ".join(failures)


@pytest.mark.asyncio
async def test_cross_patient_isolation(seeded_corpus, make_user, db_session) -> None:
    """A foreign user must not retrieve any corpus study, even by a token
    that uniquely identifies one — the visibility filter is the security
    boundary, asserted at recall@infinity."""
    _owner, marker_to_id = seeded_corpus
    corpus_ids = set(marker_to_id.values())
    intruder = await make_user(email="intruder@example.com")
    client = _client_for(db_session, intruder)
    try:
        r = await client.get("/api/search", params={"q": CROSS_PATIENT_PROBE, "limit": 200})
        assert r.status_code == 200, r.text
        leaked = [it["id"] for it in r.json()["items"] if it["id"] in corpus_ids]
        assert leaked == [], f"cross-patient leak: intruder saw corpus studies {leaked}"
    finally:
        await client.aclose()
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_search_latency_budget(seeded_corpus, db_session) -> None:
    """Soft p95 latency ceiling. Healthy FTS is tens of ms; the bound sits
    well below the 3s statement_timeout, so it catches a pathological
    regression (a dropped index forcing a seq scan) without flaking on CI
    jitter. p50/p95 are printed for tracking."""
    owner, _ = seeded_corpus
    client = _client_for(db_session, owner)
    latencies: list[float] = []
    try:
        for gq in QUERIES:
            params: dict[str, str | int] = {"limit": 50}
            if gq.q is not None:
                params["q"] = gq.q
            if gq.modality is not None:
                params["modality"] = gq.modality
            for _ in range(3):
                started = time.perf_counter()
                r = await client.get("/api/search", params=params)
                latencies.append(time.perf_counter() - started)
                assert r.status_code == 200, r.text
    finally:
        await client.aclose()
        app.dependency_overrides.clear()

    p50 = percentile(latencies, 50)
    p95 = percentile(latencies, 95)
    print(f"search latency p50={p50 * 1000:.0f}ms p95={p95 * 1000:.0f}ms n={len(latencies)}")
    assert p95 < 2.0, f"search p95 latency {p95:.3f}s exceeds budget — possible index regression"
