"""F11.2: transparency endpoints — public/admin split + access gating.

We stub ``get_db`` and the four aggregation helpers so the test does
not require a live Postgres; the endpoints still exercise their payload
assembly, the Pydantic schemas, the per-audience cache, and the FastAPI
router + auth wiring.

The central invariant under test: the public endpoint never exposes the
community / sharing / LLM-activity counts (they are not even in its
response schema, and its code path doesn't compute them), while the
admin endpoint is gated behind ``require_admin`` and returns the full
superset.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from bvphoenix.api import transparency as transparency_module
from bvphoenix.auth import require_admin, require_user
from bvphoenix.db.session import get_db
from bvphoenix.main import app

# Sections that must NEVER appear on the public payload.
_ADMIN_ONLY_SECTIONS = ("users", "sharing", "llm")


class _StubSession:
    async def execute(self, *_: Any, **__: Any) -> Any:  # pragma: no cover
        raise AssertionError("DB should be bypassed in these tests")

    async def close(self) -> None:
        return None


async def _override_get_db() -> AsyncIterator[_StubSession]:
    yield _StubSession()


class _AdminStub:
    """Minimal stand-in for the ``require_admin`` return value.

    The admin endpoint declares ``admin: User`` but never reads it, so
    any object satisfies the dependency override."""

    is_admin = True


class _NonAdminStub:
    """Authenticated but non-admin user, used to drive the *real*
    ``require_admin`` gate (we override ``require_user`` underneath it,
    not ``require_admin`` itself, so the gate logic actually runs)."""

    is_admin = False


def _public_cache_dict(total: int) -> dict[str, Any]:
    """A valid PublicTransparencyOut model_dump with a sentinel total."""
    return transparency_module.PublicTransparencyOut(
        generated_at="2026-01-01T00:00:00+00:00",
        version="sentinel",
        studies=transparency_module.StudiesStats(
            total=total, by_tier={"t1": 0, "t2": 0, "t3": 0, "t4": 0}, public=0, by_modality={}
        ),
        governance=transparency_module.GovernanceLinks(),
    ).model_dump()


def _admin_cache_dict(total: int) -> dict[str, Any]:
    """A valid TransparencyOut (admin superset) model_dump with sentinels."""
    return transparency_module.TransparencyOut(
        generated_at="2026-01-01T00:00:00+00:00",
        version="sentinel",
        studies=transparency_module.StudiesStats(
            total=total, by_tier={"t1": 0, "t2": 0, "t3": 0, "t4": 0}, public=0, by_modality={}
        ),
        governance=transparency_module.GovernanceLinks(),
        users=transparency_module.UsersStats(total=777),
        sharing=transparency_module.SharingStats(
            grants_active=0, grants_deidentified=0, grants_commercial=0
        ),
        llm=transparency_module.LLMStats(consultations_total=0, summaries_total=0),
    ).model_dump()


@pytest.fixture(autouse=True)
def _reset_transparency_cache() -> None:
    """Force a cache miss on every test so the stubbed aggregation
    helpers actually run. Without this, the first test populates the
    5-minute cache and every subsequent test reads back whichever
    fake the first fixture installed. Clears both audience entries."""
    cache = transparency_module._cache
    cache._mem = {}
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


# --- public endpoint -------------------------------------------------------


def test_transparency_public_returns_expected_shape() -> None:
    response = client.get("/api/transparency")
    assert response.status_code == 200, response.text
    body = response.json()

    assert "generated_at" in body
    assert body["version"]

    for section in ("studies", "governance"):
        assert section in body, section

    studies = body["studies"]
    assert studies["total"] == 42
    assert studies["by_tier"] == {"t1": 30, "t2": 5, "t3": 4, "t4": 3}
    assert studies["public"] == 3
    assert studies["by_modality"]["CT"] == 15


def test_transparency_public_omits_admin_only_sections() -> None:
    """The whole point of the split: community / sharing / LLM activity
    are not present on the anonymous payload, at all."""
    body = client.get("/api/transparency").json()
    for section in _ADMIN_ONLY_SECTIONS:
        assert section not in body, f"{section} leaked onto the public payload"


def test_transparency_is_unauthenticated() -> None:
    """The public endpoint is intentionally anonymous — no Authorization header."""
    response = client.get("/api/transparency")
    assert response.status_code == 200


def test_transparency_reports_agpl_license() -> None:
    """Locks in the license string so a silent rename is caught."""
    body = client.get("/api/transparency").json()
    assert body["governance"]["license"] == "AGPL-3.0-or-later"


def test_transparency_serves_cache_on_second_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """First call runs the (public) builder; second call within the
    TTL window should bypass it entirely."""
    call_count = {"n": 0}

    original = transparency_module._build_public_payload

    async def counting_build(db: Any) -> transparency_module.PublicTransparencyOut:
        call_count["n"] += 1
        return await original(db)

    monkeypatch.setattr(transparency_module, "_build_public_payload", counting_build)

    r1 = client.get("/api/transparency")
    r2 = client.get("/api/transparency")
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert call_count["n"] == 1
    # Payloads are identical bit-for-bit from the cache (generated_at
    # is frozen on the cached row).
    assert r1.json()["generated_at"] == r2.json()["generated_at"]


# --- admin endpoint --------------------------------------------------------


def test_transparency_admin_requires_authentication() -> None:
    """Anonymous callers cannot reach the admin slice."""
    response = client.get("/api/transparency/admin")
    assert response.status_code == 401, response.text


def test_transparency_admin_returns_full_shape() -> None:
    """An authenticated admin sees the public slice plus the non-public
    community / sharing / LLM-activity counts."""
    app.dependency_overrides[require_admin] = lambda: _AdminStub()
    try:
        response = client.get("/api/transparency/admin")
    finally:
        app.dependency_overrides.pop(require_admin, None)

    assert response.status_code == 200, response.text
    body = response.json()

    # Public slice still present.
    assert body["studies"]["total"] == 42
    assert body["governance"]["license"] == "AGPL-3.0-or-later"

    # Admin-only slice present with the asymmetric fakes.
    assert body["users"]["total"] == 12
    assert body["sharing"]["grants_active"] == 6
    assert body["sharing"]["grants_deidentified"] == 2
    assert body["sharing"]["grants_commercial"] == 1
    assert body["llm"]["consultations_total"] == 55
    assert body["llm"]["summaries_total"] == 33


def test_transparency_admin_rejects_non_admin() -> None:
    """An authenticated *non-admin* hits the real ``require_admin`` gate
    (we override ``require_user`` beneath it, so the is_admin check runs)
    and is refused with 403 — the gate semantic the design depends on."""
    app.dependency_overrides[require_user] = lambda: _NonAdminStub()
    try:
        response = client.get("/api/transparency/admin")
    finally:
        app.dependency_overrides.pop(require_user, None)
    assert response.status_code == 403, response.text


def test_caches_are_keyed_per_audience() -> None:
    """Each endpoint must read only its OWN cache key. We seed both keys
    with distinct sentinel study totals and assert the public endpoint
    returns the public sentinel (not the admin one) and the admin
    endpoint returns the admin sentinel (not the public one). This proves
    independence in BOTH directions — a single shared/aliased key would
    cross the sentinels."""
    cache = transparency_module._cache
    soon = time.time() + 300
    cache._mem[transparency_module._PUBLIC_CACHE_KEY] = (_public_cache_dict(101), soon)
    cache._mem[transparency_module._ADMIN_CACHE_KEY] = (_admin_cache_dict(202), soon)

    pub = client.get("/api/transparency").json()
    assert pub["studies"]["total"] == 101  # read the PUBLIC key, not the admin's 202
    for section in _ADMIN_ONLY_SECTIONS:
        assert section not in pub

    app.dependency_overrides[require_admin] = lambda: _AdminStub()
    try:
        admin = client.get("/api/transparency/admin").json()
    finally:
        app.dependency_overrides.pop(require_admin, None)
    assert admin["studies"]["total"] == 202  # read the ADMIN key, not the public's 101
    assert admin["users"]["total"] == 777
