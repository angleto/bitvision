"""Unit tests for the long-running Jobs service (DESIGN.md §11).

Goes through the dev Postgres because the partial unique index on
``idempotency_key`` is the dedup primitive and we want to exercise
the actual constraint, not a Python-side stand-in. Each test creates
a fresh subject + user to avoid cross-test interference.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from bvphoenix.config import get_settings
from bvphoenix.db.models import Subject, User
from bvphoenix.db.models.jobs import Job
from bvphoenix.db.session import (
    SERVICE_SUBJECT,
    set_current_subject,
)
from bvphoenix.services.jobs import (
    EnqueueResult,
    JobAlreadyTerminalError,
    JobCapExceededError,
    JobNotFoundError,
    cleanup_expired,
    compute_idempotency_key,
    delete_jobs,
    enqueue_or_get,
    expired_jobs,
    get_active_count_for_user,
    get_job,
    list_active_for_user,
    mark_failed,
    mark_running,
    mark_succeeded,
    request_cancellation,
    set_arq_job_id,
    update_progress,
)

pytestmark = pytest.mark.skipif(
    not (os.getenv("BVP_DATABASE_URL") or os.getenv("DATABASE_URL")),
    reason="needs a Postgres with 0051_jobs applied",
)


@pytest_asyncio.fixture
async def session_with_user() -> AsyncIterator[tuple[AsyncSession, uuid.UUID]]:
    """Yields ``(db, owner_subject_id)``. The session is rolled back at
    teardown so test rows do not survive.

    Uses a per-test engine with ``NullPool`` so each test gets a fresh
    connection bound to its own event loop. Sharing the module-level
    ``SessionFactory`` across pytest-asyncio's per-test loops triggers
    "Event loop is closed" once a pooled connection is reused.
    """
    settings = get_settings()
    engine = create_async_engine(settings.database_url, echo=False, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    db = factory()
    sid = uuid.uuid4()
    try:
        await set_current_subject(db, SERVICE_SUBJECT)
        db.add(Subject(id=sid, kind="user", display_name=f"jobs-{sid}"))
        await db.flush()
        db.add(
            User(
                subject_id=sid,
                email=f"jobs-{sid}@example.com",
                password_hash=None,
                is_admin=False,
            )
        )
        await db.flush()
        yield db, sid
    finally:
        await db.rollback()
        await db.close()
        await engine.dispose()


def test_idempotency_key_is_deterministic() -> None:
    a = compute_idempotency_key(
        kind="x",
        owner_subject_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
        scope_ids=("p1", "s1"),
        canonical_input={"include": ["a", "b"]},
    )
    b = compute_idempotency_key(
        kind="x",
        owner_subject_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
        scope_ids=("s1", "p1"),  # order does not matter
        canonical_input={"include": ["a", "b"]},
    )
    assert a == b


def test_idempotency_key_changes_on_owner() -> None:
    base_kwargs = {
        "kind": "x",
        "scope_ids": ("p1",),
        "canonical_input": {"k": 1},
    }
    a = compute_idempotency_key(
        owner_subject_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
        **base_kwargs,  # type: ignore[arg-type]
    )
    b = compute_idempotency_key(
        owner_subject_id=uuid.UUID("00000000-0000-0000-0000-000000000002"),
        **base_kwargs,  # type: ignore[arg-type]
    )
    assert a != b


def test_idempotency_key_changes_on_input() -> None:
    a = compute_idempotency_key(
        kind="x",
        owner_subject_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
        canonical_input={"k": 1},
    )
    b = compute_idempotency_key(
        kind="x",
        owner_subject_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
        canonical_input={"k": 2},
    )
    assert a != b


def test_idempotency_key_ignores_underscore_prefixed_metadata() -> None:
    """Underscore-prefixed keys (``_display_label`` and friends) ride
    along on ``Job.input`` for FE consumption but must not affect dedup
    — otherwise editing a study description would silently invalidate
    an in-flight cached export."""
    base_owner = uuid.UUID("00000000-0000-0000-0000-000000000001")
    a = compute_idempotency_key(
        kind="study_export",
        owner_subject_id=base_owner,
        scope_ids=("study-uuid",),
        canonical_input={"_display_label": "TC torace 2024-12-01"},
    )
    b = compute_idempotency_key(
        kind="study_export",
        owner_subject_id=base_owner,
        scope_ids=("study-uuid",),
        canonical_input={"_display_label": "TC torace senza mdc · TC · 2024-12-01"},
    )
    c = compute_idempotency_key(
        kind="study_export",
        owner_subject_id=base_owner,
        scope_ids=("study-uuid",),
        canonical_input={},
    )
    assert a == b == c


@pytest.mark.asyncio
async def test_enqueue_creates_queued_job(
    session_with_user: tuple[AsyncSession, uuid.UUID],
) -> None:
    db, sid = session_with_user
    res = await enqueue_or_get(
        db,
        kind="fascicolo_export",
        owner_subject_id=sid,
        canonical_input={"patient_id": "p1"},
        scope_ids=("p1",),
    )
    assert isinstance(res, EnqueueResult)
    assert res.deduped is False
    assert res.job.status == "queued"
    assert res.job.owner_subject_id == sid
    assert res.job.kind == "fascicolo_export"
    assert res.job.progress_done == 0
    assert res.job.progress_total is None
    assert res.job.expires_at is not None


@pytest.mark.asyncio
async def test_enqueue_dedups_active_job(
    session_with_user: tuple[AsyncSession, uuid.UUID],
) -> None:
    db, sid = session_with_user
    first = await enqueue_or_get(
        db,
        kind="fascicolo_export",
        owner_subject_id=sid,
        canonical_input={"patient_id": "p1"},
    )
    second = await enqueue_or_get(
        db,
        kind="fascicolo_export",
        owner_subject_id=sid,
        canonical_input={"patient_id": "p1"},
    )
    assert second.deduped is True
    assert second.job.id == first.job.id


@pytest.mark.asyncio
async def test_enqueue_after_terminal_creates_new_job(
    session_with_user: tuple[AsyncSession, uuid.UUID],
) -> None:
    """Once the previous job is terminal, the same idempotency_key may
    enqueue again (user explicitly retried after completion)."""
    db, sid = session_with_user
    first = await enqueue_or_get(
        db,
        kind="fascicolo_export",
        owner_subject_id=sid,
        canonical_input={"patient_id": "p1"},
    )
    await mark_succeeded(db, first.job.id, result_uri="s3://test/done.zip")
    await db.flush()
    second = await enqueue_or_get(
        db,
        kind="fascicolo_export",
        owner_subject_id=sid,
        canonical_input={"patient_id": "p1"},
    )
    assert second.deduped is False
    assert second.job.id != first.job.id


@pytest.mark.asyncio
async def test_per_user_cap_raises(
    session_with_user: tuple[AsyncSession, uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db, sid = session_with_user
    settings = get_settings()
    monkeypatch.setattr(settings, "job_max_active_per_user", 2)
    monkeypatch.setattr(settings, "job_admin_bypass_cap", True)
    # Two different inputs ⇒ two different idem keys ⇒ two slots.
    await enqueue_or_get(db, kind="x", owner_subject_id=sid, canonical_input={"i": 1})
    await enqueue_or_get(db, kind="x", owner_subject_id=sid, canonical_input={"i": 2})
    with pytest.raises(JobCapExceededError) as excinfo:
        await enqueue_or_get(db, kind="x", owner_subject_id=sid, canonical_input={"i": 3})
    assert excinfo.value.scope == "per_user"
    assert excinfo.value.cap == 2
    assert excinfo.value.used == 2


@pytest.mark.asyncio
async def test_admin_bypasses_cap_when_configured(
    session_with_user: tuple[AsyncSession, uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db, sid = session_with_user
    settings = get_settings()
    monkeypatch.setattr(settings, "job_max_active_per_user", 1)
    monkeypatch.setattr(settings, "job_admin_bypass_cap", True)
    await enqueue_or_get(db, kind="x", owner_subject_id=sid, canonical_input={"i": 1})
    res = await enqueue_or_get(
        db,
        kind="x",
        owner_subject_id=sid,
        canonical_input={"i": 2},
        is_admin=True,
    )
    assert res.deduped is False


@pytest.mark.asyncio
async def test_admin_subject_to_cap_when_bypass_disabled(
    session_with_user: tuple[AsyncSession, uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db, sid = session_with_user
    settings = get_settings()
    monkeypatch.setattr(settings, "job_max_active_per_user", 1)
    monkeypatch.setattr(settings, "job_admin_bypass_cap", False)
    await enqueue_or_get(
        db,
        kind="x",
        owner_subject_id=sid,
        canonical_input={"i": 1},
        is_admin=True,
    )
    with pytest.raises(JobCapExceededError):
        await enqueue_or_get(
            db,
            kind="x",
            owner_subject_id=sid,
            canonical_input={"i": 2},
            is_admin=True,
        )


@pytest.mark.asyncio
async def test_dedup_does_not_consume_cap_slot(
    session_with_user: tuple[AsyncSession, uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retrying the same operation should never push the user over the
    cap. This is the contract that justifies frontend "click again"
    UX without throttling penalty."""
    db, sid = session_with_user
    settings = get_settings()
    monkeypatch.setattr(settings, "job_max_active_per_user", 1)
    first = await enqueue_or_get(db, kind="x", owner_subject_id=sid, canonical_input={"i": 1})
    # 100 retries of the same input; none should raise.
    for _ in range(100):
        again = await enqueue_or_get(db, kind="x", owner_subject_id=sid, canonical_input={"i": 1})
        assert again.deduped is True
        assert again.job.id == first.job.id
    assert (await get_active_count_for_user(db, sid)) == 1


@pytest.mark.asyncio
async def test_lifecycle_running_progress_succeeded(
    session_with_user: tuple[AsyncSession, uuid.UUID],
) -> None:
    db, sid = session_with_user
    res = await enqueue_or_get(db, kind="x", owner_subject_id=sid, canonical_input={"i": 1})
    job_id = res.job.id

    await mark_running(db, job_id)
    await update_progress(db, job_id, progress_done=10, progress_total=100, stage="packing")
    await update_progress(db, job_id, progress_done=50)
    await mark_succeeded(db, job_id, result_uri="s3://bucket/key")
    await db.flush()

    job = await get_job(db, job_id)
    await db.refresh(job)
    assert job.status == "succeeded"
    assert job.progress_done == 50
    assert job.progress_total == 100
    assert job.stage == "packing"
    assert job.result_uri == "s3://bucket/key"
    assert job.started_at is not None
    assert job.finished_at is not None


@pytest.mark.asyncio
async def test_failure_records_error_payload(
    session_with_user: tuple[AsyncSession, uuid.UUID],
) -> None:
    db, sid = session_with_user
    res = await enqueue_or_get(db, kind="x", owner_subject_id=sid, canonical_input={"i": 1})
    await mark_running(db, res.job.id)
    await mark_failed(
        db,
        res.job.id,
        error={"code": "permission_denied", "message": "no access"},
    )
    await db.flush()
    job = await get_job(db, res.job.id)
    await db.refresh(job)
    assert job.status == "failed"
    assert job.error == {"code": "permission_denied", "message": "no access"}


@pytest.mark.asyncio
async def test_progress_update_skipped_after_terminal(
    session_with_user: tuple[AsyncSession, uuid.UUID],
) -> None:
    """Terminal rows must not be mutated by stale worker checkpoints.
    The update is filtered by ``status IN active``."""
    db, sid = session_with_user
    res = await enqueue_or_get(db, kind="x", owner_subject_id=sid, canonical_input={"i": 1})
    await mark_succeeded(db, res.job.id, result_uri="s3://x")
    await update_progress(db, res.job.id, progress_done=999, stage="late")
    await db.flush()
    job = await get_job(db, res.job.id)
    await db.refresh(job)
    assert job.progress_done == 0
    assert job.stage is None


@pytest.mark.asyncio
async def test_cancel_by_owner(
    session_with_user: tuple[AsyncSession, uuid.UUID],
) -> None:
    db, sid = session_with_user
    res = await enqueue_or_get(db, kind="x", owner_subject_id=sid, canonical_input={"i": 1})
    await request_cancellation(db, res.job.id, sid)
    job = await get_job(db, res.job.id)
    await db.refresh(job)
    assert job.status == "cancelled"
    assert job.finished_at is not None


@pytest.mark.asyncio
async def test_cancel_by_other_user_raises_not_found(
    session_with_user: tuple[AsyncSession, uuid.UUID],
) -> None:
    """Non-owner cancellation must look like 'not found' to avoid
    leaking job existence to a third party."""
    db, sid = session_with_user
    res = await enqueue_or_get(db, kind="x", owner_subject_id=sid, canonical_input={"i": 1})
    other = uuid.uuid4()
    with pytest.raises(JobNotFoundError):
        await request_cancellation(db, res.job.id, other)


@pytest.mark.asyncio
async def test_cancel_already_terminal_raises(
    session_with_user: tuple[AsyncSession, uuid.UUID],
) -> None:
    db, sid = session_with_user
    res = await enqueue_or_get(db, kind="x", owner_subject_id=sid, canonical_input={"i": 1})
    await mark_succeeded(db, res.job.id, result_uri="s3://x")
    await db.flush()
    with pytest.raises(JobAlreadyTerminalError):
        await request_cancellation(db, res.job.id, sid)


@pytest.mark.asyncio
async def test_set_arq_job_id_sets_only_when_null(
    session_with_user: tuple[AsyncSession, uuid.UUID],
) -> None:
    db, sid = session_with_user
    res = await enqueue_or_get(db, kind="x", owner_subject_id=sid, canonical_input={"i": 1})
    await set_arq_job_id(db, res.job.id, "arq-1")
    await set_arq_job_id(db, res.job.id, "arq-2")  # should be a no-op
    await db.flush()
    job = await get_job(db, res.job.id)
    await db.refresh(job)
    assert job.arq_job_id == "arq-1"


@pytest.mark.asyncio
async def test_list_active_filters_by_status(
    session_with_user: tuple[AsyncSession, uuid.UUID],
) -> None:
    db, sid = session_with_user
    a = await enqueue_or_get(db, kind="x", owner_subject_id=sid, canonical_input={"i": 1})
    b = await enqueue_or_get(db, kind="x", owner_subject_id=sid, canonical_input={"i": 2})
    await mark_succeeded(db, a.job.id, result_uri="s3://x")
    await db.flush()
    active = await list_active_for_user(db, sid)
    ids = {j.id for j in active}
    assert b.job.id in ids
    assert a.job.id not in ids


@pytest.mark.asyncio
async def test_cleanup_returns_only_expired_rows(
    session_with_user: tuple[AsyncSession, uuid.UUID],
) -> None:
    db, sid = session_with_user
    fresh = await enqueue_or_get(
        db,
        kind="x",
        owner_subject_id=sid,
        canonical_input={"i": 1},
        expires_in_hours=24,
    )
    expired = await enqueue_or_get(
        db,
        kind="x",
        owner_subject_id=sid,
        canonical_input={"i": 2},
    )
    # Force-expire by hand.
    from datetime import UTC, datetime, timedelta

    expired_job = await db.get(Job, expired.job.id)
    assert expired_job is not None
    expired_job.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await db.flush()

    ids = await cleanup_expired(db)
    assert expired.job.id in ids
    assert fresh.job.id not in ids


@pytest.mark.asyncio
async def test_expired_jobs_returns_full_row_oldest_first(
    session_with_user: tuple[AsyncSession, uuid.UUID],
) -> None:
    """``expired_jobs`` must return the full Job (not just the id) so
    the cleanup worker can resolve ``result_uri`` for storage cleanup
    before deleting the row. Ordering must be oldest-first so a
    backlog drains in age order."""
    from datetime import UTC, datetime, timedelta

    db, sid = session_with_user
    a = await enqueue_or_get(db, kind="x", owner_subject_id=sid, canonical_input={"i": 1})
    b = await enqueue_or_get(db, kind="x", owner_subject_id=sid, canonical_input={"i": 2})
    # Mark both succeeded so they have result_uri values to inspect.
    await mark_succeeded(db, a.job.id, result_uri="s3://bucket/a")
    await mark_succeeded(db, b.job.id, result_uri="s3://bucket/b")
    # ``a`` expires 2 hours ago, ``b`` expires 1 hour ago.
    now = datetime.now(UTC)
    a_row = await db.get(Job, a.job.id)
    b_row = await db.get(Job, b.job.id)
    assert a_row is not None and b_row is not None
    a_row.expires_at = now - timedelta(hours=2)
    b_row.expires_at = now - timedelta(hours=1)
    await db.flush()

    rows = await expired_jobs(db)
    assert [r.id for r in rows] == [a.job.id, b.job.id]
    assert rows[0].result_uri == "s3://bucket/a"
    assert rows[1].result_uri == "s3://bucket/b"


@pytest.mark.asyncio
async def test_delete_jobs_removes_rows(
    session_with_user: tuple[AsyncSession, uuid.UUID],
) -> None:
    db, sid = session_with_user
    a = await enqueue_or_get(db, kind="x", owner_subject_id=sid, canonical_input={"i": 1})
    b = await enqueue_or_get(db, kind="x", owner_subject_id=sid, canonical_input={"i": 2})
    rowcount = await delete_jobs(db, [a.job.id, b.job.id])
    assert rowcount == 2
    assert (await db.get(Job, a.job.id)) is None
    assert (await db.get(Job, b.job.id)) is None


@pytest.mark.asyncio
async def test_delete_jobs_empty_input_is_noop(
    session_with_user: tuple[AsyncSession, uuid.UUID],
) -> None:
    db, _sid = session_with_user
    rowcount = await delete_jobs(db, [])
    assert rowcount == 0
