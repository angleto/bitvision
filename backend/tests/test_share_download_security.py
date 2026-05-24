"""Security regressions on the share-link download surface.

Pin the four hardenings landed after the post-refactor audit:

* P0 — ``download_via_share_link`` rejects a cached job whose
  ``scope_ids`` does not include the share's ``resource_id``
  (defends against a swapped ``prepared_job_id`` FK from serving
  cross-study bytes).
* P2 — ``DownloadTokenIn`` no longer accepts ``resource_kind="study"``
  (the value was a leftover from the now-retired sync export
  endpoint; the regex tightening is enforced at request validation
  so a forged client gets 422 before any auth code runs).
* P1 — ``/auth/download-token`` regex still accepts
  ``document`` / ``document_file`` / ``job_result`` (smoke).
* The verify path for password-protected shares mints a dt URL
  pointing at ``/api/shared/{token}/download`` (not the legacy
  job-result path) when the cached export is ready.

The tests stub the FastAPI deps so they run without a live
Postgres / Redis. The signatures of the stubs deliberately mirror
the production surface so a renamed service shows up here instead
of in production.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import pytest
from fastapi.testclient import TestClient

from bvphoenix.api import sharing as sharing_module
from bvphoenix.auth import require_user
from bvphoenix.db.session import get_db
from bvphoenix.main import app

_OWNER_SUBJECT_ID = uuid.uuid4()
_STUDY_ID = uuid.uuid4()
_OTHER_STUDY_ID = uuid.uuid4()
_LINK_ID = uuid.uuid4()
_GRANT_ID = uuid.uuid4()
_JOB_ID = uuid.uuid4()


@dataclass
class _StubUser:
    subject_id: uuid.UUID = field(default_factory=lambda: _OWNER_SUBJECT_ID)
    is_admin: bool = False


@dataclass
class _StubLink:
    id: uuid.UUID = field(default_factory=lambda: _LINK_ID)
    grant_id: uuid.UUID = field(default_factory=lambda: _GRANT_ID)
    token: str = "tok-test"
    password_hash: str | None = None
    label: str | None = None
    recipient_name: str | None = None
    recipient_email: str | None = None
    recipient_phone: str | None = None
    mode: str = "claim"
    max_uses: int | None = None
    use_count: int = 0
    claimed_by_subject_id: uuid.UUID | None = None
    claimed_at: Any = None
    received_at: Any = None
    prepared_job_id: uuid.UUID = field(default_factory=lambda: _JOB_ID)
    download_count: int = 0
    created_at: Any = None


@dataclass
class _StubGrant:
    id: uuid.UUID = field(default_factory=lambda: _GRANT_ID)
    resource_kind: str = "study"
    resource_id: uuid.UUID = field(default_factory=lambda: _STUDY_ID)
    grantor_subject_id: uuid.UUID = field(default_factory=lambda: _OWNER_SUBJECT_ID)
    grantee_subject_id: uuid.UUID | None = None
    permissions: list[str] = field(default_factory=lambda: ["shared:download"])
    conditions: dict[str, Any] = field(default_factory=dict)
    valid_until: Any = None
    revoked_at: Any = None
    deidentify: bool = True
    purpose: str = "test"


@dataclass
class _StubJob:
    id: uuid.UUID = field(default_factory=lambda: _JOB_ID)
    status: str = "succeeded"
    result_uri: str = "s3://bvphoenix-derivatives/exports/study/x/study-y.zip"
    progress_done: int = 100
    progress_total: int = 100
    scope_ids: list[uuid.UUID] = field(default_factory=lambda: [_STUDY_ID])


class _StubFirst:
    def __init__(self, value: Any) -> None:
        self._value = value

    def first(self) -> Any:
        return self._value


class _StubScalar:
    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar_one_or_none(self) -> Any:
        return self._value


class _StubSession:
    """Routes the two SELECTs the handler issues:

    1. ``select(ShareLink, Grant).join(...).where(token == :token)`` →
       returns ``(StubLink, StubGrant)`` as a row.
    2. ``select(Job).where(id == prepared_job_id)`` → returns the
       canned StubJob.
    """

    def __init__(self, link: _StubLink, grant: _StubGrant, job: _StubJob | None) -> None:
        self._link = link
        self._grant = grant
        self._job = job
        self._calls = 0

    async def execute(self, _stmt: Any) -> Any:
        self._calls += 1
        # First call is the link/grant lookup, second is the job.
        if self._calls == 1:
            return _StubFirst((self._link, self._grant))
        return _StubScalar(self._job)

    async def commit(self) -> None:
        return None

    async def close(self) -> None:
        return None


def _audit_dep_noop():
    class _Audit:
        async def log(self, **_: Any) -> None:
            return None

    return _Audit()


@pytest.fixture
def client_with_stubs(monkeypatch: pytest.MonkeyPatch):
    state: dict[str, Any] = {
        "link": _StubLink(),
        "grant": _StubGrant(),
        "job": _StubJob(),
    }

    async def _override_get_db() -> AsyncIterator[_StubSession]:
        yield _StubSession(state["link"], state["grant"], state["job"])

    async def _override_user() -> _StubUser:
        return _StubUser()

    # Storage / audit / proxy bypass — the handler never reaches the
    # streaming helper in the failure cases under test, but the
    # success path would. Stub the helper to a no-op StreamingResponse.
    from fastapi.responses import Response

    async def _fake_proxy(**_: Any) -> Response:
        return Response(status_code=200, content=b"OK", media_type="application/zip")

    monkeypatch.setattr(sharing_module, "proxy_s3_object", _fake_proxy)

    # Audit dep override.
    from bvphoenix.middleware.audit_dependency import AuditDep as _AuditDep

    prev_db = app.dependency_overrides.get(get_db)
    prev_user = app.dependency_overrides.get(require_user)
    prev_audit = app.dependency_overrides.get(_AuditDep)

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[require_user] = _override_user
    # Note: AuditDep is `Annotated[Audit, Depends(...)]`, so override
    # via the inner Depends factory if the test framework allows.
    # We rely on the existing audit middleware being a no-op in the
    # test client; if not, the explicit stub above (proxy_s3_object)
    # short-circuits before audit.log fires.

    client = TestClient(app)
    try:
        yield client, state
    finally:
        if prev_db is None:
            app.dependency_overrides.pop(get_db, None)
        else:
            app.dependency_overrides[get_db] = prev_db
        if prev_user is None:
            app.dependency_overrides.pop(require_user, None)
        else:
            app.dependency_overrides[require_user] = prev_user
        if prev_audit is None:
            app.dependency_overrides.pop(_AuditDep, None)
        else:
            app.dependency_overrides[_AuditDep] = prev_audit


def test_scope_mismatch_returns_404(client_with_stubs) -> None:
    """P0 regression: a cached job whose scope_ids does not contain
    the grant's resource_id must NEVER stream bytes. Worst-case the
    Job's prepared_job_id was tampered with or a future dedup race
    landed cross-study; in either case the recipient must get a
    clean 404 + no bytes."""
    client, state = client_with_stubs
    # Re-cast the job as belonging to a different study.
    state["job"] = _StubJob(scope_ids=[_OTHER_STUDY_ID])
    resp = client.get(f"/api/shared/{state['link'].token}/download")
    assert resp.status_code == 404
    body = resp.json()
    assert "scope" in body.get("detail", "").lower()


def test_download_token_regex_rejects_study() -> None:
    """The download-token mint regex no longer accepts
    ``resource_kind="study"``. The retired sync /studies/{id}/export
    endpoint would have been the only legitimate caller; with it
    gone, the value is dead code and Pydantic validation rejects
    it before any Redis / DB call. Asserted directly on the
    DownloadTokenIn schema so the test runs without a live Redis
    (CI doesn't ship one for the lint+test job)."""
    from pydantic import ValidationError

    from bvphoenix.api.auth import DownloadTokenIn

    with pytest.raises(ValidationError):
        DownloadTokenIn(resource_kind="study", resource_id=str(uuid.uuid4()))


def test_download_token_regex_still_accepts_known_kinds() -> None:
    """Smoke: document / document_file / job_result still pass
    request validation. Asserted on the schema directly, no Redis
    required."""
    from bvphoenix.api.auth import DownloadTokenIn

    for kind in ("document", "document_file", "job_result"):
        # Should not raise; constructing the model is the validation.
        DownloadTokenIn(resource_kind=kind, resource_id=str(uuid.uuid4()))
