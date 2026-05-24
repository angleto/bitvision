"""Generic Job orchestration service (DESIGN.md §11).

The service is the single source of truth for the lifecycle of a row
in ``jobs``: enqueue with idempotency + per-user cap, progress
updates, terminal transitions, cancellation, and cleanup. It is
intentionally Arq-agnostic — callers wire the worker enqueue
themselves and stamp ``arq_job_id`` via :func:`set_arq_job_id`. This
keeps the service usable from chained worker tasks (which already
have their own Redis pool) and from the API layer.

Concurrency contract:

* Idempotency is enforced by the partial unique index
  ``ix_jobs_idem_active_uniq``. Two concurrent enqueues for the same
  key race; the winner inserts, the loser gets ``IntegrityError``
  which we translate into a dedup hit by re-reading the existing
  row.
* The per-user cap check is a count + insert without explicit
  locking. A race can let the (cap+1)-th job through, but the
  overshoot is bounded by the number of concurrent enqueues and the
  next attempt fails. We accept that — moving the check into a
  SERIALIZABLE transaction would serialize every enqueue across the
  user, and the worst-case overshoot is dwarfed by the cap itself.

Caller responsibilities:

* Build a ``canonical_input`` dict that contains every field that
  affects the result, and only those fields. Different shape ⇒
  different idempotency_key ⇒ different job.
* For consumers that need a checkpoint contract (re-run from
  scratch is unsafe), document the resume semantics in the consumer
  module. The service does not enforce that.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.config import get_settings
from bvphoenix.db.models.jobs import (
    JOB_ACTIVE_STATUSES,
    JOB_TERMINAL_STATUSES,
    Job,
)

logger = logging.getLogger(__name__)


class JobError(Exception):
    """Base for service-level job errors. The API layer maps subclasses
    to HTTP status codes."""


class JobCapExceededError(JobError):
    """Raised when an enqueue would exceed the per-user or global
    active-jobs cap. API layer maps to 429."""

    def __init__(
        self,
        *,
        scope: str,
        used: int,
        cap: int,
        retry_after_seconds: int = 60,
    ) -> None:
        super().__init__(f"job cap exceeded: {scope}={used}/{cap}")
        self.scope = scope
        self.used = used
        self.cap = cap
        self.retry_after_seconds = retry_after_seconds


class JobNotFoundError(JobError):
    """Raised when a job lookup misses. API layer maps to 404."""


class JobAlreadyTerminalError(JobError):
    """Raised when a transition is attempted on a job already in a
    terminal state. API layer maps to 409."""


@dataclass(frozen=True, slots=True)
class EnqueueResult:
    """Return value of :func:`enqueue_or_get`.

    ``deduped`` is True when the call hit an existing active job,
    False when a new row was inserted. The API layer uses this to
    choose between HTTP 200 and 202.
    """

    job: Job
    deduped: bool


def compute_idempotency_key(
    *,
    kind: str,
    owner_subject_id: uuid.UUID | str,
    scope_ids: Sequence[str | uuid.UUID] = (),
    canonical_input: dict[str, Any] | None = None,
) -> str:
    """SHA-256 over a canonical, sorted JSON encoding of the inputs.

    The hash domain is ``(kind, owner, sorted(scope_ids), canonical_input)``.
    Same inputs ⇒ same key ⇒ dedup. Different owner ⇒ different
    key (jobs are not shared across users; see DESIGN.md §11.4).

    Keys starting with ``_`` in ``canonical_input`` are stripped
    before hashing. They are treated as denormalised display
    metadata (``_display_label``, future ``_render_hint``, ...) that
    rides along on ``Job.input`` so the FE can render a useful row
    label without a follow-up DB lookup, but must not influence dedup
    (otherwise editing a study description would silently invalidate
    an in-flight cached export).
    """
    filtered_input = (
        {k: v for k, v in canonical_input.items() if not k.startswith("_")}
        if canonical_input
        else {}
    )
    payload = {
        "kind": kind,
        "owner": str(owner_subject_id),
        "scope": sorted(str(s) for s in scope_ids),
        "input": filtered_input,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


async def get_active_count_for_user(db: AsyncSession, owner_subject_id: uuid.UUID) -> int:
    """Count of (queued + running) jobs for the user. Backed by
    ``ix_jobs_owner_active``."""
    q = (
        select(func.count())
        .select_from(Job)
        .where(
            Job.owner_subject_id == owner_subject_id,
            Job.status.in_(JOB_ACTIVE_STATUSES),
        )
    )
    return int((await db.execute(q)).scalar_one() or 0)


async def get_active_count_global(db: AsyncSession) -> int:
    """Count of (queued + running) jobs across all users. Cheap
    because the partial index covers active rows only."""
    q = select(func.count()).select_from(Job).where(Job.status.in_(JOB_ACTIVE_STATUSES))
    return int((await db.execute(q)).scalar_one() or 0)


async def _find_active_by_idem(db: AsyncSession, idempotency_key: str) -> Job | None:
    q = select(Job).where(
        Job.idempotency_key == idempotency_key,
        Job.status.in_(JOB_ACTIVE_STATUSES),
    )
    return (await db.execute(q)).scalar_one_or_none()


async def enqueue_or_get(
    db: AsyncSession,
    *,
    kind: str,
    owner_subject_id: uuid.UUID,
    canonical_input: dict[str, Any] | None = None,
    scope_ids: Sequence[str | uuid.UUID] = (),
    expires_in_hours: int | None = None,
    is_admin: bool = False,
) -> EnqueueResult:
    """Insert a new job or return the existing active one.

    Order (DESIGN.md §11.5):

    1. Compute idempotency key.
    2. If an active job with that key exists, return it (dedup hit).
       The cap is *not* consumed.
    3. Per-user cap check.
    4. Global cap check (admins bypass when configured).
    5. Insert. On unique-violation (a concurrent enqueue won), retry
       the dedup lookup once and return that.

    The returned Job has no ``arq_job_id`` yet; the caller enqueues
    the worker task and stamps the id via :func:`set_arq_job_id`.
    """
    settings = get_settings()
    canonical_input = canonical_input or {}
    idem = compute_idempotency_key(
        kind=kind,
        owner_subject_id=owner_subject_id,
        scope_ids=scope_ids,
        canonical_input=canonical_input,
    )

    existing = await _find_active_by_idem(db, idem)
    if existing is not None:
        logger.debug(
            "job dedup hit kind=%s owner=%s key=%s id=%s",
            kind,
            owner_subject_id,
            idem,
            existing.id,
        )
        return EnqueueResult(job=existing, deduped=True)

    if not (is_admin and settings.job_admin_bypass_cap):
        used = await get_active_count_for_user(db, owner_subject_id)
        # Per-user override (admin dashboard) wins over the platform
        # default. ``users.max_concurrent_jobs`` NULL → use settings.
        from bvphoenix.db.models import User

        per_user_cap = (
            await db.execute(
                select(User.max_concurrent_jobs).where(User.subject_id == owner_subject_id)
            )
        ).scalar_one_or_none()
        cap = int(per_user_cap) if per_user_cap is not None else settings.job_max_active_per_user
        if used >= cap:
            raise JobCapExceededError(
                scope="per_user",
                used=used,
                cap=cap,
            )

    if not is_admin:
        used_global = await get_active_count_global(db)
        if used_global >= settings.job_max_active_global:
            raise JobCapExceededError(
                scope="global",
                used=used_global,
                cap=settings.job_max_active_global,
                retry_after_seconds=120,
            )

    hours = expires_in_hours or settings.job_default_expires_hours
    expires_at = datetime.now(UTC) + timedelta(hours=hours)

    # Persist scope_ids alongside the hash so cross-device discovery
    # works (``GET /api/jobs?kind=X&scope_id=Y``). The list mirrors
    # exactly what was hashed, so the dedup contract stays intact.
    persisted_scope: list[uuid.UUID] | None = None
    if scope_ids:
        persisted_scope = [s if isinstance(s, uuid.UUID) else uuid.UUID(str(s)) for s in scope_ids]

    job = Job(
        kind=kind,
        owner_subject_id=owner_subject_id,
        idempotency_key=idem,
        status="queued",
        progress_done=0,
        input=dict(canonical_input),
        expires_at=expires_at,
        scope_ids=persisted_scope,
    )
    db.add(job)
    try:
        await db.flush()
    except IntegrityError as exc:
        # Concurrent enqueue won the race. Roll back the failed insert
        # and re-read the existing row.
        logger.debug(
            "job enqueue race lost kind=%s key=%s: %s",
            kind,
            idem,
            exc,
        )
        await db.rollback()
        existing = await _find_active_by_idem(db, idem)
        if existing is None:
            # Should not happen: the unique index fired but the row
            # vanished. Re-raise so the caller sees the integrity
            # error instead of silently mis-attributing it.
            raise
        return EnqueueResult(job=existing, deduped=True)

    return EnqueueResult(job=job, deduped=False)


async def get_job(db: AsyncSession, job_id: uuid.UUID) -> Job:
    job = await db.get(Job, job_id)
    if job is None:
        raise JobNotFoundError(str(job_id))
    return job


async def list_active_for_user(
    db: AsyncSession,
    owner_subject_id: uuid.UUID,
    *,
    kind: str | None = None,
    limit: int = 50,
) -> list[Job]:
    q = (
        select(Job)
        .where(
            Job.owner_subject_id == owner_subject_id,
            Job.status.in_(JOB_ACTIVE_STATUSES),
        )
        .order_by(Job.created_at.desc())
        .limit(limit)
    )
    if kind is not None:
        q = q.where(Job.kind == kind)
    return list((await db.execute(q)).scalars().all())


async def set_arq_job_id(db: AsyncSession, job_id: uuid.UUID, arq_job_id: str) -> None:
    """Stamp the Arq handle after the caller has enqueued the worker
    task. Idempotent; a second call overwrites only if the previous
    value was NULL (we never re-enqueue the same Job into Arq)."""
    await db.execute(
        update(Job).where(Job.id == job_id, Job.arq_job_id.is_(None)).values(arq_job_id=arq_job_id)
    )


async def mark_running(db: AsyncSession, job_id: uuid.UUID) -> None:
    """Worker entry point: flip queued ⇒ running and stamp
    ``started_at``. Safe to call repeatedly; only the first transition
    sets ``started_at``."""
    await db.execute(
        update(Job)
        .where(Job.id == job_id, Job.status == "queued")
        .values(status="running", started_at=datetime.now(UTC))
    )


async def update_progress(
    db: AsyncSession,
    job_id: uuid.UUID,
    *,
    progress_done: int | None = None,
    progress_total: int | None = None,
    stage: str | None = None,
) -> None:
    """Worker checkpoint. Each call is one DB roundtrip; consumers
    should batch (every N items) rather than every step."""
    values: dict[str, Any] = {"updated_at": datetime.now(UTC)}
    if progress_done is not None:
        values["progress_done"] = progress_done
    if progress_total is not None:
        values["progress_total"] = progress_total
    if stage is not None:
        values["stage"] = stage
    if len(values) == 1:
        return
    await db.execute(
        update(Job).where(Job.id == job_id, Job.status.in_(JOB_ACTIVE_STATUSES)).values(**values)
    )


async def mark_succeeded(
    db: AsyncSession,
    job_id: uuid.UUID,
    *,
    result_uri: str | None = None,
) -> None:
    # Explicit ``updated_at`` because SQLAlchemy's column-level
    # ``onupdate=func.now()`` does NOT auto-fire on Core
    # ``update(...).values(...)`` when the column isn't otherwise
    # touched in the same .values() call. Visible bug pre-fix:
    # succeeded jobs whose ``updated_at`` stayed frozen at the last
    # ``update_progress`` tick — making the row look "stale" to the
    # reaper for non-obvious reasons and making
    # "last touched" surfaces in the UI lie. Same fix in mark_failed
    # and request_cancellation.
    now = datetime.now(UTC)
    await db.execute(
        update(Job)
        .where(Job.id == job_id, Job.status.in_(JOB_ACTIVE_STATUSES))
        .values(
            status="succeeded",
            result_uri=result_uri,
            finished_at=now,
            updated_at=now,
        )
    )


async def mark_failed(
    db: AsyncSession,
    job_id: uuid.UUID,
    *,
    error: dict[str, Any],
) -> None:
    now = datetime.now(UTC)
    await db.execute(
        update(Job)
        .where(Job.id == job_id, Job.status.in_(JOB_ACTIVE_STATUSES))
        .values(
            status="failed",
            error=error,
            finished_at=now,
            updated_at=now,
        )
    )


async def request_cancellation(
    db: AsyncSession,
    job_id: uuid.UUID,
    requester_subject_id: uuid.UUID,
    *,
    is_admin: bool = False,
) -> Job:
    """User-initiated cancel. Only the owner (or an admin) may cancel.

    Sets the row to ``cancelled`` immediately for queued jobs (the
    worker will skip them when picked up). For running jobs the
    transition is also immediate at the DB level; the worker is
    expected to honour cancellation at its next checkpoint. Consumers
    that cannot interrupt mid-step should document that limitation.
    """
    job = await get_job(db, job_id)
    if job.owner_subject_id != requester_subject_id and not is_admin:
        raise JobNotFoundError(str(job_id))
    if job.status in JOB_TERMINAL_STATUSES:
        raise JobAlreadyTerminalError(job.status)
    now = datetime.now(UTC)
    await db.execute(
        update(Job)
        .where(Job.id == job_id, Job.status.in_(JOB_ACTIVE_STATUSES))
        .values(status="cancelled", finished_at=now, updated_at=now)
    )
    await db.refresh(job)
    return job


async def expired_jobs(db: AsyncSession, *, batch_size: int = 200) -> list[Job]:
    """Return up to ``batch_size`` rows past ``expires_at``, oldest
    first so a backlog drains in age order. The full row is returned
    (not just the id) so the caller can read ``result_uri`` to drop
    the S3 artifact before deleting the row.
    """
    q = (
        select(Job)
        .where(Job.expires_at < datetime.now(UTC))
        .order_by(Job.expires_at.asc())
        .limit(batch_size)
    )
    return list((await db.execute(q)).scalars().all())


async def delete_jobs(db: AsyncSession, job_ids: list[uuid.UUID]) -> int:
    """Hard-delete a batch of expired ``jobs`` rows. Returns the
    rowcount. Caller is responsible for already having deleted any
    S3 artifacts these rows pointed at."""
    if not job_ids:
        return 0
    res = await db.execute(delete(Job).where(Job.id.in_(job_ids)))
    return res.rowcount or 0


async def cleanup_expired(db: AsyncSession, *, batch_size: int = 200) -> list[uuid.UUID]:
    """Return the ids of rows past ``expires_at``. Kept for backward
    compatibility with callers that only need the ids; new callers
    should prefer :func:`expired_jobs` so they can resolve
    ``result_uri`` for storage cleanup before :func:`delete_jobs`."""
    rows = await expired_jobs(db, batch_size=batch_size)
    return [r.id for r in rows]


__all__ = [
    "EnqueueResult",
    "JobAlreadyTerminalError",
    "JobCapExceededError",
    "JobError",
    "JobNotFoundError",
    "cleanup_expired",
    "compute_idempotency_key",
    "delete_jobs",
    "enqueue_or_get",
    "expired_jobs",
    "get_active_count_for_user",
    "get_active_count_global",
    "get_job",
    "list_active_for_user",
    "mark_failed",
    "mark_running",
    "mark_succeeded",
    "request_cancellation",
    "set_arq_job_id",
    "update_progress",
]
