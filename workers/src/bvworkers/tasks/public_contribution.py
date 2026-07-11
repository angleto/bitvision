"""Public-contribution worker tasks: promote an accepted submission.

The auto-check pass is driven by the generic ``run_review_checks`` task (any
profile, resolved via ``BVP_REVIEW_PROFILE_MODULES``). This module adds:

* ``promote_submission`` — publish an ``accepted`` submission through the
  profile's ``on_accept`` hook (``accepted -> promoting -> promoted``). After
  the commit it purges the staged redacted blobs (never inside the hook: a
  later raise could roll back the DB but not un-delete S3) and enqueues the
  ingest post-processing (pack + embed) for the t4 public clone's series.
* ``contribution_maintenance`` — the recovery sweep (cron): re-queue stale
  auto-check runs and re-enqueue the promotion of ``accepted`` items whose
  enqueue was lost. Without it, a lost promote enqueue sticks forever.

Note on retries: the engine's ``promoting -> failed`` transition is rolled
back together with everything else when the hook raises (this task commits
only on success), which is exactly what keeps the item ``accepted`` and the
retry path alive — do not "fix" that by committing the failure.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import bvphoenix.services.review_queue.engine as review_engine
from bvphoenix.db.session import SERVICE_SUBJECT, set_current_subject
from bvphoenix.services.review_queue.profile import get_profile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from bvworkers.config import get_settings

log = logging.getLogger(__name__)

PROFILE_NAME = "public_contribution"

# Recovery window: covers the gap between an HTTP handler's commit and its
# (possibly lost) arq enqueue. Mirrors the inbox sweep.
_STALE_ENQUEUE = timedelta(minutes=15)


def _ensure_profile_registered() -> None:
    # Importing the profile module registers it (idempotent). The generic
    # checks task relies on BVP_REVIEW_PROFILE_MODULES; the promote path imports
    # directly so it works even if the env is unset in this worker.
    import bvphoenix.services.public_contribution.profile  # noqa: F401


async def promote_submission(ctx: dict[str, Any], submission_id: str) -> dict[str, Any]:
    """Publish an ``accepted`` submission to the OpenData tier."""
    from bvphoenix.services.ingest_jobs import enqueue_postprocess_jobs
    from bvphoenix.services.public_contribution.promotion import purge_submission_staged

    _ensure_profile_registered()
    settings = get_settings()
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    try:
        profile = get_profile(PROFILE_NAME)
        async with AsyncSession(engine, expire_on_commit=False) as db:
            await set_current_subject(db, SERVICE_SUBJECT)
            item = await profile.load_item(db, uuid.UUID(submission_id))
            if item is None:
                return {"status": "not_found", "submission_id": submission_id}
            if item.status != "accepted":
                # Idempotent: another runner already promoted it, or it was
                # rejected/expired. Never re-drive a terminal item.
                return {"status": "skipped", "submission_id": submission_id, "current": item.status}
            outcome = await review_engine.promote(db, profile, item)
            await db.commit()

            # Post-commit tail — both best-effort: the maintenance sweep and
            # the idempotent jobs recover a lost step, and the staged purge is
            # retried harmlessly (S3 deletes are idempotent).
            try:
                await purge_submission_staged(item)
            except Exception:  # pragma: no cover
                log.warning("staged purge failed for %s", submission_id, exc_info=True)
            series_pairs = [
                (sid, modality) for sid, modality in (outcome or {}).get("public_series", [])
            ]
            if series_pairs:
                try:
                    await enqueue_postprocess_jobs(ctx["redis"], series_pairs)
                except Exception:  # pragma: no cover
                    log.warning("postprocess enqueue failed for %s", submission_id, exc_info=True)
        return {"status": "promoted", "submission_id": submission_id, "outcome": outcome or {}}
    except Exception:
        log.exception("promote_submission failed for %s", submission_id)
        raise
    finally:
        await engine.dispose()


async def contribution_maintenance(ctx: dict[str, Any]) -> dict[str, Any]:
    """Recovery sweep for the contribution queue (cron).

    1. Items stuck in ``received``/``processing`` (lost check enqueue or a
       worker death mid-run) get their auto-check run re-enqueued.
    2. ``accepted`` items whose promotion enqueue was lost get re-promoted.
    3. Recently ``rejected`` items get their staged blobs re-purged — the
       safety net for a post-commit reject purge lost to a crash (the reject
       endpoint deletes them AFTER commit, deliberately outside the decision
       transaction; ``purge_submission_staged`` is idempotent).

    Bounded per tick and deduped by the etag-keyed job ids. No retention expiry
    here: submissions are owner-offered and reviewer-decided, an undecided one
    is a review-queue backlog item, not garbage.
    """
    from bvphoenix.db.models import Submission
    from bvphoenix.services.public_contribution.promotion import purge_submission_staged
    from bvphoenix.services.review_queue.jobs import requeue_stale_processing

    _ensure_profile_registered()
    settings = get_settings()
    redis = ctx["redis"]
    stats = {"requeued": 0, "repromoted": 0, "repurged": 0}

    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    try:
        async with AsyncSession(engine) as db:
            await set_current_subject(db, SERVICE_SUBJECT)
            stats["requeued"] = await requeue_stale_processing(
                db, redis, model=Submission, profile_name=PROFILE_NAME
            )

            cutoff = datetime.now(UTC) - _STALE_ENQUEUE
            stuck_accepted = (
                (
                    await db.execute(
                        select(Submission)
                        .where(Submission.status == "accepted", Submission.updated_at < cutoff)
                        .limit(100)
                    )
                )
                .scalars()
                .all()
            )
            for item in stuck_accepted:
                handle = await redis.enqueue_job(
                    "promote_submission",
                    str(item.id),
                    _job_id=f"contrib-promote:{item.id}:{item.etag}",
                )
                stats["repromoted"] += int(handle is not None)

            # Re-purge staged blobs of recently rejected items (bounded window:
            # catches a lost post-commit purge without re-scanning all history).
            recently_rejected = (
                (
                    await db.execute(
                        select(Submission)
                        .where(
                            Submission.status == "rejected",
                            Submission.updated_at >= cutoff - timedelta(hours=6),
                        )
                        .limit(100)
                    )
                )
                .scalars()
                .all()
            )
            for item in recently_rejected:
                stats["repurged"] += await purge_submission_staged(item)
            await db.commit()
    finally:
        await engine.dispose()
    return stats


__all__ = ["contribution_maintenance", "promote_submission"]
