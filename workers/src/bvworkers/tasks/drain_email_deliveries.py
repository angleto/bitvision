"""Arq cron that drains the outbound email ledger.

``email_deliveries`` rows are created by the request path *before* the
first send attempt. When the relay is reachable that first attempt
succeeds inline and this cron never sees the row. When it is not, the
row stays ``queued`` with a backoff-scheduled ``next_attempt_at`` and
this task is what eventually delivers it.

This is the piece that was missing on 2026-07-31: with the SMTP port
blackholed, three share invitations failed with nothing owning the
retry, so they were simply lost. A ledger without a drainer is just a
more detailed way to record loss.

Isolation properties that matter here:

* ``FOR UPDATE SKIP LOCKED`` in ``claim_due`` partitions the backlog
  across worker replicas instead of having them collide.
* Each row commits in its own transaction and a failure rolls back only
  that row. The reminder dispatcher shares one session across up to 200
  rows and ``continue``s past errors without rolling back, so a single
  DB error poisons the rest of the tick; that shape is not repeated.
* A wall-clock deadline bounds the batch. A blackholed relay costs the
  full connect timeout per row, so an unbounded batch would run past
  arq's ``job_timeout`` and be killed mid-flight.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from bvphoenix.db.engine import make_async_engine
from sqlalchemy.ext.asyncio import async_sessionmaker

from bvworkers.config import get_settings

logger = logging.getLogger(__name__)

# Upper bound on rows touched per tick. The cron runs every 5 minutes,
# so this is a rate limit as much as a batch size.
_BATCH_LIMIT = 50

# Stop claiming new rows past this much wall-clock in one tick, leaving
# headroom under arq's job_timeout for the row already in flight.
_DEADLINE_SECONDS = 120

_session_factory = None


def _get_session_factory():
    """Lazy session factory bound to the worker's event loop.

    arq callbacks run inside the loop arq starts, so the sessionmaker is
    built on first use rather than at import time (otherwise SQLAlchemy
    raises "future attached to a different loop").
    """
    global _session_factory
    if _session_factory is None:
        settings = get_settings()
        engine = make_async_engine(settings.database_url, pool_pre_ping=True)
        _session_factory = async_sessionmaker(engine, expire_on_commit=False)
    return _session_factory


async def drain_email_deliveries(ctx: dict) -> dict:
    """Attempt every due ledger row. Returns per-tick counters."""
    # Importing these API modules registers their ledger builders as a
    # side effect. Without them the worker has no way to rebuild a
    # message and would park every retriable row as dead_letter.
    import bvphoenix.api.auth
    import bvphoenix.api.sharing  # noqa: F401
    from bvphoenix.services.email_delivery import attempt, claim_due

    factory = _get_session_factory()
    started = datetime.now(UTC)
    counts = {"sent": 0, "requeued": 0, "failed": 0, "claimed": 0}

    async with factory() as db:
        try:
            due = await claim_due(db, limit=_BATCH_LIMIT)
        except Exception:
            logger.exception("email ledger: failed to claim due rows")
            await db.rollback()
            return counts

        counts["claimed"] = len(due)
        for row in due:
            elapsed = (datetime.now(UTC) - started).total_seconds()
            if elapsed > _DEADLINE_SECONDS:
                logger.info(
                    "email ledger: deadline hit after %d rows, %d left for the next tick",
                    counts["sent"] + counts["requeued"] + counts["failed"],
                    len(due) - (counts["sent"] + counts["requeued"] + counts["failed"]),
                )
                break
            try:
                outcome = await attempt(db, row.id)
                await db.commit()
            except Exception:
                # Roll back so the next row starts from a clean session.
                logger.exception("email ledger: row %s raised", row.id)
                await db.rollback()
                counts["failed"] += 1
                continue
            if outcome.ok:
                counts["sent"] += 1
            elif row.status == "queued":
                counts["requeued"] += 1
            else:
                counts["failed"] += 1

    if counts["claimed"]:
        logger.info("email ledger tick: %s", counts)
    return counts


__all__ = ["drain_email_deliveries"]
