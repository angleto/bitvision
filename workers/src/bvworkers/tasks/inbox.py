"""Patient-inbox worker tasks: staging, promotion, maintenance.

Three responsibilities around the shared review engine:

* ``process_inbound_email`` — full MIME parse + component staging for
  one stored raw message, then the auto-check pass inline (we are
  already in a worker; a second hop through ``run_review_checks``
  would buy nothing) and, when an allowlist entry authorises it, the
  auto-accept + promotion;
* ``promote_inbox_item`` — execute the promotion of an accepted item
  (the accept endpoint only transitions and enqueues);
* ``inbox_maintenance`` — the recovery + retention sweep (cron): lost
  enqueues are re-issued, stale processing re-queued, undecided items
  past retention expired (staged blobs purged, held jobs cancelled),
  raw ``.eml`` blobs past retention deleted.

Tasks re-raise on failure so arq counts retries; every step is
idempotent (etag-keyed job ids, dedup on staging, UID-level dedup in
the DICOM ingestor).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta

from bvphoenix.db.engine import make_async_engine
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bvworkers.config import get_settings

logger = logging.getLogger(__name__)

PROFILE_NAME = "patient_inbox"

# Recovery windows. STALE_ENQUEUE covers the gap between an HTTP
# handler's commit and its (possibly lost) arq enqueue.
_STALE_ENQUEUE = timedelta(minutes=15)


def _session_engine():
    settings = get_settings()
    return make_async_engine(settings.database_url, pool_pre_ping=True)


async def process_inbound_email(ctx: dict, inbound_email_id: str) -> dict:
    from bvphoenix.db.models import InboundEmail
    from bvphoenix.services.inbox.checks import auto_accept_entry
    from bvphoenix.services.inbox.emails import stage_inbound_email
    from bvphoenix.services.inbox.profile import INBOX_PROFILE
    from bvphoenix.services.review_queue import engine as review_engine
    from bvphoenix.services.review_queue.actor import ReviewActor

    db_engine = _session_engine()
    try:
        async with AsyncSession(db_engine) as db:
            inbound = await db.get(InboundEmail, uuid.UUID(inbound_email_id))
            if inbound is None:
                return {"status": "not_found", "inbound_email_id": inbound_email_id}

            item = await stage_inbound_email(db, inbound=inbound)
            if item.status != "received":
                # Already past staging (retry after a crash mid-commit
                # or a concurrent run): never re-drive a decided item.
                await db.commit()
                return {"status": "already_staged", "item_status": item.status}

            await review_engine.start_processing(db, INBOX_PROFILE, item)
            verdict = await review_engine.run_auto_checks(db, INBOX_PROFILE, item)

            outcome: dict = {
                "status": "checked",
                "item_id": str(item.id),
                "auto_verdict": verdict,
            }

            entry = await auto_accept_entry(db, item)
            if entry is not None:
                # The allowlist entry IS the (prior, human) decision;
                # the worker executes it under that identity, and the
                # review note makes the mechanism explicit.
                actor = ReviewActor(kind="human", subject_id=entry.created_by_subject_id)
                await review_engine.decide(
                    db,
                    INBOX_PROFILE,
                    item,
                    decision="accepted",
                    actor=actor,
                    reason=f"auto-accepted: sender {entry.sender_email} allowlisted",
                )
                await review_engine.promote(db, INBOX_PROFILE, item)
                outcome["auto_accepted"] = True

            await db.commit()
            return outcome
    finally:
        await db_engine.dispose()


async def promote_inbox_item(ctx: dict, item_id: str) -> dict:
    from bvphoenix.db.models import InboxItem
    from bvphoenix.services.inbox.profile import INBOX_PROFILE
    from bvphoenix.services.review_queue import engine as review_engine

    db_engine = _session_engine()
    try:
        async with AsyncSession(db_engine) as db:
            item = (
                await db.execute(select(InboxItem).where(InboxItem.id == uuid.UUID(item_id)))
            ).scalar_one_or_none()
            if item is None:
                return {"status": "not_found", "item_id": item_id}
            if item.status != "accepted":
                # promoted already, or a reviewer raced us — terminal SKIP.
                return {"status": "skipped", "item_status": item.status}
            outcome = await review_engine.promote(db, INBOX_PROFILE, item)
            await db.commit()
            return {"status": "promoted", "item_id": item_id, "outcome": outcome or {}}
    finally:
        await db_engine.dispose()


async def inbox_maintenance(ctx: dict) -> dict:
    from bvphoenix.db.models import InboundEmail, InboxItem
    from bvphoenix.services.inbox.emails import purge_staged
    from bvphoenix.services.inbox.profile import INBOX_PROFILE
    from bvphoenix.services.inbox.promotion import reject_item_cleanup
    from bvphoenix.services.review_queue import engine as review_engine
    from bvphoenix.services.review_queue.jobs import requeue_stale_processing
    from bvphoenix.storage import get_s3_storage

    settings = get_settings()
    redis = ctx["redis"]
    now = datetime.now(UTC)
    stats = {"restaged": 0, "requeued": 0, "repromoted": 0, "expired": 0, "raw_purged": 0}

    db_engine = _session_engine()
    try:
        async with AsyncSession(db_engine) as db:
            # 1. Inbound emails whose staging enqueue was lost: no item
            #    references them and they are old enough to rule out an
            #    in-flight worker.
            cutoff = now - _STALE_ENQUEUE
            unstaged = (
                (
                    await db.execute(
                        select(InboundEmail)
                        .where(
                            InboundEmail.created_at < cutoff,
                            ~select(InboxItem.id)
                            .where(InboxItem.inbound_email_id == InboundEmail.id)
                            .exists(),
                        )
                        .limit(100)
                    )
                )
                .scalars()
                .all()
            )
            for inbound in unstaged:
                handle = await redis.enqueue_job(
                    "process_inbound_email",
                    str(inbound.id),
                    _job_id=f"inbound-email:{inbound.id}",
                )
                stats["restaged"] += int(handle is not None)

            # 2. Items stuck before the auto-check outcome.
            stats["requeued"] = await requeue_stale_processing(
                db, redis, model=InboxItem, profile_name=PROFILE_NAME
            )

            # 3. Accepted items whose promotion enqueue was lost.
            stuck_accepted = (
                (
                    await db.execute(
                        select(InboxItem)
                        .where(InboxItem.status == "accepted", InboxItem.updated_at < cutoff)
                        .limit(100)
                    )
                )
                .scalars()
                .all()
            )
            for item in stuck_accepted:
                handle = await redis.enqueue_job(
                    "promote_inbox_item",
                    str(item.id),
                    _job_id=f"inbox-promote:{item.id}:{item.etag}",
                )
                stats["repromoted"] += int(handle is not None)

            # 4. Retention: undecided items age out; their staged blobs
            #    are purged and a held upload job is cancelled.
            retention_cutoff = now - timedelta(days=settings.inbound_email_raw_retention_days)
            expirable = (
                (
                    await db.execute(
                        select(InboxItem)
                        .where(
                            InboxItem.status.in_(
                                ("received", "processing", "needs_review", "blocked")
                            ),
                            InboxItem.created_at < retention_cutoff,
                        )
                        .limit(100)
                    )
                )
                .scalars()
                .all()
            )
            for item in expirable:
                await review_engine.expire(
                    db, INBOX_PROFILE, item, reason="retention window elapsed"
                )
                await reject_item_cleanup(db, item=item)
                await purge_staged(item)
                stats["expired"] += 1

            # 5. Raw .eml past retention: delete the blob, keep the row
            #    (audit) with the subject blanked.
            storage = get_s3_storage()
            old_raw = (
                (
                    await db.execute(
                        select(InboundEmail)
                        .where(
                            InboundEmail.created_at < retention_cutoff,
                            InboundEmail.raw_s3_key != "",
                        )
                        .limit(100)
                    )
                )
                .scalars()
                .all()
            )
            for inbound in old_raw:
                try:
                    await asyncio.to_thread(
                        storage.delete_object,
                        bucket=settings.s3_bucket_raw,
                        key=inbound.raw_s3_key,
                    )
                except Exception:
                    logger.warning("raw purge failed for %s", inbound.id, exc_info=True)
                    continue
                inbound.raw_s3_key = ""
                inbound.subject = None
                stats["raw_purged"] += 1

            await db.commit()
    finally:
        await db_engine.dispose()
    return stats


__all__ = ["inbox_maintenance", "process_inbound_email", "promote_inbox_item"]
