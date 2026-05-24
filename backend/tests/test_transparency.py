"""F11.2: transparency endpoint — shape + public-access sanity.

We stub ``get_db`` and the four aggregation helpers so the test does
not require a live Postgres; the endpoint still exercises its payload
assembly, the Pydantic schema, and the FastAPI router wiring.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from bvphoenix.api import transparency as transparency_module
from bvphoenix.db.session import get_db
from bvphoenix.main import app


class _StubSession:
    async def execute(self, *_: Any, **__: Any) -> Any:  # pragma: no cover
        raise AssertionError("DB should be bypassed in these tests")

    async def close(self) -> None:
        return None


async def _override_get_db() -> AsyncIterator[_StubSession]:
    yield _StubSession()


@pytest.fixture(autouse=True)
def _reset_transparency_cache() -> None:
    """Force a cache miss on every test so the stubbed aggregation
    helpers actually run. Without this, the first test populates the
    5-minute cache and every subsequent test reads back whichever
    fake the first fixture installed."""
    cache = transparency_module._cache
    cache._mem_value = None
    cache._mem_expires_at = 0.0
    cache._use_memory = True
    cache._client = None


@pytest.fixture(autouse=True)
def _stub_aggregations(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the four aggregation helpers with deterministic fakes.
    Fake numbers are asymmetrical so the test can distinguish which
    field ended up where."""

    async def fake_studies(_db: Any) -> transparency_module.StudiesStats:
        return transparency_module.StudiesStats(
            total=42,
            by_tier={"t1": 30, "t2": 5, "t3": 4, "t4": 3},
            public=3,
            by_modality={"CT": 15, "MR": 10, "XR": 7},
        )

    async def fake_users(_db: Any) -> transparency_module.UsersStats:
        return transparency_module.UsersStats(total=12)

    async def fake_sharing(_db: Any) -> transparency_module.SharingStats:
        return transparency_module.SharingStats(
            grants_active=6, grants_deidentified=2, grants_commercial=1
        )

    async def fake_llm(_db: Any) -> transparency_module.LLMStats:
        return transparency_module.LLMStats(consultations_total=55, summaries_total=33)

    monkeypatch.setattr(transparency_module, "_studies_stats", fake_studies)
    monkeypatch.setattr(transparency_module, "_users_stats", fake_users)
    monkeypatch.setattr(transparency_module, "_sharing_stats", fake_sharing)
    monkeypatch.setattr(transparency_module, "_llm_stats", fake_llm)


@pytest.fixture(autouse=True)
def _stub_db() -> Iterator[None]:
    """Install the get_db override only for this module's tests, then
    remove it. Without the cleanup the override leaks into integration
    tests collected before this module's runtime (e.g. the
    care-phase E2E HTTP calls would hit the AssertionError instead of
    the real Postgres). Symptom previously: random failure of
    test_care_phase_full_pipeline_no_anthropic depending on collection
    order."""
    app.dependency_overrides[get_db] = _override_get_db
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_db, None)


client = TestClient(app)


def test_transparency_returns_expected_shape() -> None:
    response = client.get("/api/transparency")
    assert response.status_code == 200, response.text
    body = response.json()

    assert "generated_at" in body
    assert body["version"]

    for section in ("studies", "users", "sharing", "llm", "governance"):
        assert section in body, section

    studies = body["studies"]
    assert studies["total"] == 42
    assert studies["by_tier"] == {"t1": 30, "t2": 5, "t3": 4, "t4": 3}
    assert studies["public"] == 3
    assert studies["by_modality"]["CT"] == 15

    assert body["users"]["total"] == 12

    sharing = body["sharing"]
    assert sharing["grants_active"] == 6
    assert sharing["grants_deidentified"] == 2
    assert sharing["grants_commercial"] == 1

    llm = body["llm"]
    assert llm["consultations_total"] == 55
    assert llm["summaries_total"] == 33


def test_transparency_is_unauthenticated() -> None:
    """The endpoint is intentionally public — no Authorization header."""
    response = client.get("/api/transparency")
    assert response.status_code == 200


def test_transparency_reports_agpl_license() -> None:
    """Locks in the license string so a silent rename is caught."""
    body = client.get("/api/transparency").json()
    assert body["governance"]["license"] == "AGPL-3.0-or-later"


def test_transparency_serves_cache_on_second_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """First call runs the aggregation helpers; second call within the
    TTL window should bypass them entirely."""
    call_count = {"n": 0}

    original = transparency_module._build_payload

    async def counting_build(db: Any) -> transparency_module.TransparencyOut:
        call_count["n"] += 1
        return await original(db)

    monkeypatch.setattr(transparency_module, "_build_payload", counting_build)

    r1 = client.get("/api/transparency")
    r2 = client.get("/api/transparency")
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert call_count["n"] == 1
    # Payloads are identical bit-for-bit from the cache (generated_at
    # is frozen on the cached row).
    assert r1.json()["generated_at"] == r2.json()["generated_at"]
