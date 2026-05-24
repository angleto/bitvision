"""POST /api/studies/{id}/export — async job enqueue contract.

The synchronous in-memory ZIP route was removed in favour of the
worker-mediated Job pipeline (memory ``streaming_zip_pattern``,
``feedback_long_ops_progress_recovery``). This test pins the new
contract so the FE's :func:`requestStudyExport` keeps working:

* 202 Accepted on a freshly enqueued job;
* response body is a ``JobOut`` with the right ``kind`` and
  caller-owned ``owner_subject_id``;
* dedup: a second call while the first is still active returns the
  same row instead of creating a duplicate;
* 404 when the caller doesn't have ``READ_PIXELS`` on the study
  (study doesn't appear to exist — the strict 404-not-403 contract
  the rest of the API uses).

The test stubs ``get_db``, ``require_user``, the permission gate, the
arq enqueue, and ``jobs_service.enqueue_or_get`` so it runs without a
live Postgres / Redis. The signature of the stubs deliberately mirrors
the production surface so a function-rename refactor in
``services/jobs.py`` shows up here instead of in production.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from bvphoenix.api import patient_export as patient_export_module
from bvphoenix.auth import require_user
from bvphoenix.db.session import get_db
from bvphoenix.main import app

_OWNER_SUBJECT_ID = uuid.uuid4()
_STUDY_ID = uuid.uuid4()
_PATIENT_ID = uuid.uuid4()


@dataclass
class _StubUser:
    subject_id: uuid.UUID = field(default_factory=lambda: _OWNER_SUBJECT_ID)
    is_admin: bool = False


@dataclass
class _StubStudy:
    id: uuid.UUID = field(default_factory=lambda: _STUDY_ID)
    patient_id: uuid.UUID = field(default_factory=lambda: _PATIENT_ID)
    # Added when ``api/patient_export.py`` started naming the export
    # filename from the study description; tests still pass a stub
    # study, so these fields have to exist (defaulting to empty/None
    # so ``_format_study_label`` falls back to the UUID prefix).
    study_description: str | None = None
    modalities: list[str] | None = None
    study_date: object | None = None


class _ScalarOne:
    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar_one_or_none(self) -> Any:
        return self._value


class _StubSession:
    """Returns a study on the only ``select(ImagingStudy)`` query the
    handler issues; commit/refresh are no-ops."""

    def __init__(self, study: _StubStudy | None) -> None:
        self._study = study

    async def execute(self, _stmt: Any) -> _ScalarOne:
        return _ScalarOne(self._study)

    async def commit(self) -> None:
        return None

    async def refresh(self, _obj: Any) -> None:
        return None

    async def close(self) -> None:
        return None


@dataclass
class _FakeJob:
    """Ducktypes the SQLAlchemy ``Job`` row enough for
    ``JobOut.model_validate`` to succeed."""

    id: uuid.UUID = field(default_factory=uuid.uuid4)
    kind: str = "study_export"
    owner_subject_id: uuid.UUID = field(default_factory=lambda: _OWNER_SUBJECT_ID)
    status: str = "queued"
    progress_total: int | None = None
    progress_done: int = 0
    stage: str | None = None
    input: dict[str, Any] = field(default_factory=dict)
    result_uri: str | None = None
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    started_at: datetime | None = None
    finished_at: datetime | None = None
    expires_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    arq_job_id: str | None = None
    idempotency_key: str = "stub-key"
    scope_ids: list[uuid.UUID] = field(default_factory=list)


@dataclass
class _FakeEnqueueResult:
    job: _FakeJob
    deduped: bool


class _FakeRedis:
    async def enqueue_job(self, *_args: Any, **_kwargs: Any) -> Any:
        class _H:
            job_id = "arq-handle-1"

        return _H()

    async def close(self) -> None:
        return None


@pytest.fixture
def client_with_stubs(monkeypatch: pytest.MonkeyPatch):
    study_ref: list[_StubStudy | None] = [_StubStudy()]
    enqueue_calls: list[dict[str, Any]] = []
    can_ref: list[bool] = [True]
    deduped_ref: list[bool] = [False]

    async def _override_get_db() -> AsyncIterator[_StubSession]:
        yield _StubSession(study_ref[0])

    async def _override_user() -> _StubUser:
        return _StubUser()

    async def _fake_can(*_args: Any, **_kwargs: Any) -> bool:
        return can_ref[0]

    def _fake_enforce_scope(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def _fake_enqueue_or_get(*args: Any, **kwargs: Any) -> _FakeEnqueueResult:
        enqueue_calls.append(kwargs)
        return _FakeEnqueueResult(job=_FakeJob(), deduped=deduped_ref[0])

    async def _fake_create_pool(*_args: Any, **_kwargs: Any) -> _FakeRedis:
        return _FakeRedis()

    async def _fake_set_arq(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(patient_export_module, "can", _fake_can)
    monkeypatch.setattr(
        patient_export_module,
        "enforce_agent_patient_scope",
        _fake_enforce_scope,
    )
    monkeypatch.setattr(
        patient_export_module.jobs_service,
        "enqueue_or_get",
        _fake_enqueue_or_get,
    )
    monkeypatch.setattr(
        patient_export_module.jobs_service,
        "set_arq_job_id",
        _fake_set_arq,
    )
    monkeypatch.setattr(patient_export_module, "create_pool", _fake_create_pool)

    prev_db = app.dependency_overrides.get(get_db)
    prev_user = app.dependency_overrides.get(require_user)
    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[require_user] = _override_user
    client = TestClient(app)
    try:
        yield (
            client,
            {
                "study_ref": study_ref,
                "enqueue_calls": enqueue_calls,
                "can_ref": can_ref,
                "deduped_ref": deduped_ref,
            },
        )
    finally:
        if prev_db is None:
            app.dependency_overrides.pop(get_db, None)
        else:
            app.dependency_overrides[get_db] = prev_db
        if prev_user is None:
            app.dependency_overrides.pop(require_user, None)
        else:
            app.dependency_overrides[require_user] = prev_user


def test_post_returns_202_with_job_descriptor(client_with_stubs) -> None:
    client, _state = client_with_stubs
    resp = client.post(f"/api/studies/{_STUDY_ID}/export", json={})
    assert resp.status_code == 202
    body = resp.json()
    assert body["kind"] == "study_export"
    assert body["status"] == "queued"
    assert body["owner_subject_id"] == str(_OWNER_SUBJECT_ID)


def test_post_404_when_study_missing(client_with_stubs) -> None:
    client, state = client_with_stubs
    state["study_ref"][0] = None
    resp = client.post(f"/api/studies/{uuid.uuid4()}/export", json={})
    assert resp.status_code == 404


def test_post_404_when_no_read_pixels(client_with_stubs) -> None:
    client, state = client_with_stubs
    state["can_ref"][0] = False
    resp = client.post(f"/api/studies/{_STUDY_ID}/export", json={})
    # 404 not 403: hide existence from callers without permission, matching
    # the rest of the studies API (memory ``cross_patient_links_forbidden``).
    assert resp.status_code == 404


def test_post_dedup_returns_existing_job(client_with_stubs) -> None:
    client, state = client_with_stubs
    state["deduped_ref"][0] = True
    resp = client.post(f"/api/studies/{_STUDY_ID}/export", json={})
    assert resp.status_code == 202
    # The handler skips the arq enqueue path on dedup but still returns
    # the existing job descriptor — the caller binds to it via the same
    # localStorage flow as a fresh enqueue.
    assert resp.json()["kind"] == "study_export"


def test_post_scope_is_study_id_for_dedup(client_with_stubs) -> None:
    """The dedup hash includes ``scope_ids``; for study export the scope
    is the study id (NOT the patient id), so concurrent exports of two
    different studies of the same patient don't collide."""
    client, state = client_with_stubs
    resp = client.post(f"/api/studies/{_STUDY_ID}/export", json={})
    assert resp.status_code == 202
    assert state["enqueue_calls"], "enqueue_or_get was never called"
    last = state["enqueue_calls"][-1]
    assert last["kind"] == "study_export"
    assert last["scope_ids"] == (_STUDY_ID,)
