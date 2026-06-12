"""Background-job glue for the review queue (arq).

The heavy auto-checks (ClamAV over multi-hundred-MB staged blobs) never
run inside an HTTP handler: ingress adapters enqueue
``run_review_checks`` and the worker drives ``received -> processing ->
needs_review|blocked``.

Dedup follows the deterministic ``_job_id`` convention: arq refuses a
second enqueue while a job with the same id is pending/running, so the
id embeds the item's current ``etag`` — re-enqueueing the *same staged
state* twice is a no-op, while a state that legitimately moved (etag
bumped on every transition) gets a fresh id and can re-run.

Cross-session recovery (the ``embed_series`` lesson): if a worker dies
mid-``processing``, nothing re-enqueues the item by itself. Consumers
run :func:`requeue_stale_processing` from a periodic worker sweep —
it finds items stuck in ``received``/``processing`` beyond a deadline
and re-enqueues their check run.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

REVIEW_CHECKS_TASK = "run_review_checks"


def review_checks_job_id(profile_name: str, item_id: uuid.UUID, etag: uuid.UUID) -> str:
    return f"review-checks:{profile_name}:{item_id}:{etag}"


async def enqueue_review_checks(
    redis,
    *,
    profile_name: str,
    item_id: uuid.UUID,
    etag: uuid.UUID,
):
    """Enqueue the auto-check run for one staged item.

    ``redis`` is an already-connected arq pool (caller owns lifecycle).
    Returns the arq job handle, or ``None`` when an identical run is
    already queued/in-flight (dedup hit).
    """
    return await redis.enqueue_job(
        REVIEW_CHECKS_TASK,
        profile_name,
        str(item_id),
        _job_id=review_checks_job_id(profile_name, item_id, etag),
    )


async def requeue_stale_processing(
    db: AsyncSession,
    redis,
    *,
    model: type,
    profile_name: str,
    stale_after: timedelta = timedelta(minutes=30),
    limit: int = 100,
) -> int:
    """Re-enqueue items stuck before the auto-check outcome.

    ``model`` is the consumer's ORM class (must include
    ``ReviewableItemMixin`` and an ``updated_at`` column — see the mixin
    docstring). Items in ``received``/``processing`` whose last write is
    older than ``stale_after`` get their check run re-enqueued; the
    etag-keyed job id makes this idempotent against a run that is in
    fact still alive. Returns the number of items re-enqueued.
    """
    if not hasattr(model, "updated_at"):
        raise TypeError(
            f"{model.__name__} has no updated_at column; the recovery sweep "
            "requires TimestampMixin + UpdatedAtMixin alongside ReviewableItemMixin"
        )
    cutoff = datetime.now(UTC) - stale_after
    rows = (
        (
            await db.execute(
                select(model)
                .where(
                    model.status.in_(("received", "processing")),
                    model.updated_at < cutoff,
                )
                .order_by(model.updated_at)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    requeued = 0
    for item in rows:
        handle = await enqueue_review_checks(
            redis, profile_name=profile_name, item_id=item.id, etag=item.etag
        )
        requeued += int(handle is not None)
    return requeued


__all__ = [
    "REVIEW_CHECKS_TASK",
    "enqueue_review_checks",
    "requeue_stale_processing",
    "review_checks_job_id",
]
