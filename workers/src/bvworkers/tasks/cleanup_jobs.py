"""Arq task: prune expired ``jobs`` rows + their S3 artifacts.

Runs on a cron schedule (configured in ``bvworkers.main.WorkerSettings``)
so the table does not grow indefinitely. The default per-row TTL is
7 days for export consumers (see ``BVP_JOB_DEFAULT_EXPIRES_HOURS``),
but each consumer can override per-kind. The cleanup worker treats
``expires_at`` as authoritative regardless of kind.

Order of operations per row:

1. Read ``result_uri``.
2. If it points at ``s3://<bucket>/<key>``, drop the object (best-
   effort; missing objects do not fail the cleanup).
3. Hard-delete the ``jobs`` row.

S3 deletion failures are logged and tolerated; the row gets deleted
either way. The alternative (leave the row to retry storage cleanup
on the next pass) would let a permanently-broken artifact pin a row
forever, defeating the cleanup. Storage drift is a smaller incident
than a stuck cleanup loop.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from bvworkers.config import get_settings

log = logging.getLogger(__name__)

# Bound the per-tick batch so a giant backlog after a long downtime
# does not trigger a 30-minute job timeout. The cron fires hourly, so
# at this batch size we drain ~4800 rows/hour which dwarfs the
# realistic enqueue rate for a single-tenant deploy.
_BATCH_SIZE = 200

# Reaper window: a job in ``queued`` or ``running`` whose
# ``updated_at`` hasn't moved in this much time is considered abandoned
# (the worker died, an import broke, the pod was rescheduled mid-run,
# etc) and gets flipped to ``failed`` so the UI stops showing it as
# "in progress".
#
# 5 min on purpose. The export streaming pipeline ticks
# ``updated_at`` every 250 ms via the progress callback; the bulk
# ingest path checkpoints per-batch (~30 s); even the longest worker
# tasks call ``update_progress`` periodically. Any 5-minute silence
# means the runner is genuinely dead, not just busy. Pre-fix the
# window was 15 min, leaving orphan-job indicators visible in the
# user's panel for that whole window after a rolling restart — the
# cron firing at every-5-min (see ``main.CRON_JOBS``) plus this
# reaper window means the UI clears within ~10 min of death worst
# case (5 min stale-detect + up to 5 min until next cron tick).
_STALE_AFTER = timedelta(minutes=5)


def _parse_s3_uri(uri: str | None) -> tuple[str, str] | None:
    if not uri or not uri.startswith("s3://"):
        return None
    rest = uri[len("s3://") :]
    if "/" not in rest:
        return None
    bucket, key = rest.split("/", 1)
    if not bucket or not key:
        return None
    return bucket, key


async def _reap_stale(db: AsyncSession) -> int:
    """Flip stale ``queued``/``running`` rows to ``failed``.

    Stale = last DB write older than ``_STALE_AFTER``. Active
    workers checkpoint via ``services.jobs.update_progress`` (touches
    ``updated_at``) every few files, so any longer silence means the
    runner died or the import path was broken before
    ``mark_running`` ever fired. Without this sweep those rows stay
    visible in the user's "operations in progress" panel forever.
    """
    cutoff = datetime.now(UTC) - _STALE_AFTER
    result = await db.execute(
        text(
            """
            UPDATE jobs
            SET status = 'failed',
                error = jsonb_build_object(
                    'code', 'reaper_stale',
                    'message', 'job had no updates for >' || :window || '; worker likely died'
                ),
                finished_at = now(),
                updated_at = now()
            WHERE status IN ('queued', 'running')
              AND updated_at < :cutoff
            RETURNING id
            """
        ),
        {"cutoff": cutoff, "window": str(_STALE_AFTER)},
    )
    rows = list(result)
    if rows:
        log.warning(
            "reaper: flipped %d stale jobs to failed: %s",
            len(rows),
            [str(r[0]) for r in rows[:10]],
        )
    return len(rows)


async def cleanup_expired_jobs(
    ctx: dict,  # type: ignore[type-arg]
) -> dict[str, Any]:
    """Cron entry point. Returns a small summary dict for logging."""
    settings = get_settings()

    try:
        from bvphoenix.db.session import SERVICE_SUBJECT, set_current_subject
        from bvphoenix.services import jobs as jobs_service
        from bvphoenix.storage import get_s3_storage
    except ImportError as exc:
        log.exception("bvphoenix not importable from worker: %s", exc)
        return {"status": "error", "reason": f"bvphoenix import: {exc}"}

    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    deleted = 0
    artifacts_dropped = 0
    artifact_failures = 0
    reaped_stale = 0
    try:
        async with AsyncSession(engine, expire_on_commit=False) as db:
            await set_current_subject(db, SERVICE_SUBJECT)

            # Reap first — a stuck job won't tick its ``expires_at``
            # forward, but the user can see the failed status in the
            # UI as soon as we flip it.
            reaped_stale = await _reap_stale(db)
            if reaped_stale:
                await db.commit()

            rows = await jobs_service.expired_jobs(db, batch_size=_BATCH_SIZE)
            if not rows:
                return {
                    "status": "ok",
                    "deleted": 0,
                    "artifacts_dropped": 0,
                    "reaped_stale": reaped_stale,
                }

            storage = get_s3_storage()
            for row in rows:
                target = _parse_s3_uri(row.result_uri)
                if target is None:
                    continue
                bucket, key = target
                try:
                    storage.delete_object(bucket=bucket, key=key)
                    artifacts_dropped += 1
                except Exception as exc:
                    artifact_failures += 1
                    log.warning(
                        "cleanup: failed to drop s3://%s/%s for job %s: %s",
                        bucket,
                        key,
                        row.id,
                        exc,
                    )

            deleted = await jobs_service.delete_jobs(db, [r.id for r in rows])
            await db.commit()
    finally:
        await engine.dispose()

    log.info(
        "cleanup_expired_jobs: deleted=%d artifacts_dropped=%d failures=%d reaped_stale=%d",
        deleted,
        artifacts_dropped,
        artifact_failures,
        reaped_stale,
    )
    return {
        "status": "ok",
        "deleted": deleted,
        "artifacts_dropped": artifacts_dropped,
        "artifact_failures": artifact_failures,
        "reaped_stale": reaped_stale,
    }
