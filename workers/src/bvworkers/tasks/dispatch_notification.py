"""Arq worker entry for outbound notification delivery.

Two entry points sharing the same dispatcher service:

* :func:`dispatch_notification` — one-shot send for a specific
  dispatch row. Enqueued by the safety-net cron when it finds a
  pending row past its scheduled_at.
* :func:`notification_safety_net` — cron task that scans
  ``notification_dispatches`` for ``status='pending'`` rows whose
  ``scheduled_at`` is in the past and dispatches them. This is the
  PRIMARY delivery path in this sprint — we skip per-row
  ``enqueue_at`` and let the cron handle scheduling, because:

  * worker restart resilience: a cron-driven scan rebuilds the
    queue from DB state on every tick, so a lost in-flight job is
    automatically retried
  * simpler audit trail: ``notification_dispatches`` is the single
    source of truth for what's scheduled, instead of an arq queue
    we'd have to query separately
  * grace window built-in: latency between scheduled_at and actual
    fire is bounded by the cron cadence (~5 min), perfectly fine
    for reminder use cases

The trade-off: a dispatch scheduled for 09:00 may fire anywhere
between 09:00 and 09:04. For appointment reminders that's
acceptable; if we ever need sub-minute precision (e.g. a "5-min
before" reminder where minute accuracy matters), we'd switch to
``arq.enqueue_at`` per row at scheduling time.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bvworkers.config import get_settings

logger = logging.getLogger(__name__)

# Grace window before the cron picks up a row. Set to zero so a row
# scheduled for exactly now() fires on the next tick; bump to a few
# seconds in production if clock skew between scheduler and worker
# becomes a problem.
_GRACE_SECONDS = 0


def _make_async_session_factory():
    """Lazy session factory bound to the worker's event loop. arq
    callbacks run inside the loop arq starts, so we create the
    AsyncSessionMaker on first use rather than at import time
    (avoiding "future attached to a different loop" errors)."""
    settings = get_settings()
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    return async_sessionmaker(engine, expire_on_commit=False)


_session_factory = None


def _get_session_factory():
    global _session_factory
    if _session_factory is None:
        _session_factory = _make_async_session_factory()
    return _session_factory


async def dispatch_notification(ctx: dict, dispatch_id: str) -> str:
    """Deliver one notification_dispatches row. Returns a short status
    string useful for arq job logs."""
    from bvphoenix.services.notifications.dispatcher import (
        dispatch_notification as run_dispatch,
    )

    factory = _get_session_factory()
    async with factory() as db:
        return await run_dispatch(db, uuid.UUID(dispatch_id))


async def notification_safety_net(ctx: dict) -> dict:
    """Cron entry. Find pending rows past their scheduled time and
    fire them. Returns a tiny stats dict for log aggregation."""
    from bvphoenix.db.models import NotificationDispatch
    from bvphoenix.services.notifications.dispatcher import (
        dispatch_notification as run_dispatch,
    )

    factory = _get_session_factory()
    now = datetime.now(UTC)
    sent = 0
    failed = 0
    cancelled = 0
    pending = 0
    async with factory() as db:
        rows = (
            (
                await db.execute(
                    select(NotificationDispatch.id)
                    .where(
                        NotificationDispatch.status == "pending",
                        NotificationDispatch.scheduled_at <= now,
                    )
                    .order_by(NotificationDispatch.scheduled_at)
                    .limit(200)
                )
            )
            .scalars()
            .all()
        )
        for dispatch_id in rows:
            try:
                outcome = await run_dispatch(db, dispatch_id)
            except Exception:
                logger.exception("safety-net dispatch crashed id=%s", dispatch_id)
                failed += 1
                continue
            if outcome == "sent":
                sent += 1
            elif outcome.startswith("cancelled"):
                cancelled += 1
            elif outcome in ("pending_retry",):
                pending += 1
            else:
                failed += 1
    return {"sent": sent, "failed": failed, "cancelled": cancelled, "pending": pending}


__all__ = ["dispatch_notification", "notification_safety_net"]
