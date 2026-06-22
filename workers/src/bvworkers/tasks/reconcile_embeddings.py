"""Arq cron task: self-healing Visual Search indexer.

Enqueues ``embed_series`` for every embeddable diagnostic-image series that
still lacks a BiomedCLIP (``biomedclip-v1``) vector, so similarity search
(``/api/similar-to``) indexing CONVERGES on its own, with no manual
``bvphoenix-backfill embed`` / admin "Embed missing" step.

Why a standing reconciler and not only the import-time enqueue:
``services.ingest_jobs.enqueue_postprocess_jobs`` fires ``embed_series`` at
ingest time for every path (UI upload, STOW, bulk-import, inbox promotion),
but that enqueue is BEST-EFFORT — a Redis blip, or workers scaled to zero /
without the ``ai`` extra at that instant, silently drops it, and there was
no sweep to heal the miss (a study then answers ``study_not_indexed``
forever). This cron is that sweep.

Properties:
* Idempotent — the candidate query excludes series that already have a
  vector, and ``embed_series`` itself skips an already-embedded series.
* Bounded — at most ``_BATCH_SIZE`` per tick, so a large post-import
  backlog drains in chunks instead of dumping thousands of CPU-heavy
  embeds at once.
* Deduped — each enqueue carries a deterministic ``_job_id`` keyed on the
  series, so a series already queued/in-flight is never enqueued twice on
  the next tick (arq returns ``None`` for a duplicate id). Queue depth
  therefore stays ~``_BATCH_SIZE``, draining at worker throughput.

Candidate policy is the single source of truth in
``bvphoenix.services.embeddable`` (same clauses ``bvphoenix-backfill embed
--only-missing`` uses), so the reconciler never offers a series the worker
would only terminally skip (SR / SEG / RT / no-pixel SOP class).
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from bvworkers.config import get_settings

log = logging.getLogger(__name__)

# Per-tick cap. With a 5-minute cron + job-id dedup this tops up the queue
# continuously without piling duplicates; the drain rate is the worker's
# embed throughput, not this number.
_BATCH_SIZE = 500

# Mirrors workers/embed_series.py MODEL_ID (the vector the worker writes).
_IMAGE_MODEL_ID = "biomedclip-v1"


async def reconcile_missing_embeddings(ctx: dict) -> dict[str, Any]:  # type: ignore[type-arg]
    """Cron entry point. Returns a small summary dict for logging."""
    settings = get_settings()
    try:
        from bvphoenix.db.session import SERVICE_SUBJECT, set_current_subject
        from bvphoenix.services.embeddable import (
            embeddable_modality_clause,
            embeddable_sop_class_clause,
        )
    except ImportError as exc:
        log.exception("bvphoenix not importable from worker: %s", exc)
        return {"status": "error", "reason": f"bvphoenix import: {exc}"}

    # Embeddable image series (modality + ≥1 image-SOP instance) with no
    # biomedclip-v1 vector yet, oldest first. The blocklist clauses are
    # code-defined literals (no injection surface — see embeddable.py).
    sql = text(
        f"""
        SELECT s.id::text
        FROM series s
        WHERE {embeddable_modality_clause("s.modality")}
          AND EXISTS (
              SELECT 1 FROM instances i
              WHERE i.series_id = s.id
                AND {embeddable_sop_class_clause("i.sop_class_uid")}
          )
          AND NOT EXISTS (
              SELECT 1 FROM embeddings e
              WHERE e.target_kind = 'series'
                AND e.target_id = s.id
                AND e.model_id = :model
          )
        ORDER BY s.id
        LIMIT :limit
        """
    )

    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    enqueued = 0
    candidates = 0
    try:
        async with AsyncSession(engine, expire_on_commit=False) as db:
            await set_current_subject(db, SERVICE_SUBJECT)
            rows = (
                await db.execute(sql, {"model": _IMAGE_MODEL_ID, "limit": _BATCH_SIZE})
            ).all()
        sids = [r[0] for r in rows]
        candidates = len(sids)
        if not sids:
            return {"status": "ok", "enqueued": 0, "candidates": 0}

        redis = ctx["redis"]
        for sid in sids:
            # Deterministic id -> a series already queued / in-flight is not
            # re-enqueued (enqueue_job returns None for a duplicate id).
            job = await redis.enqueue_job("embed_series", sid, _job_id=f"embed_series:{sid}")
            if job is not None:
                enqueued += 1
    finally:
        await engine.dispose()

    if enqueued:
        log.info(
            "reconcile_missing_embeddings: enqueued %d/%d embed_series job(s)",
            enqueued,
            candidates,
        )
    return {"status": "ok", "enqueued": enqueued, "candidates": candidates}
