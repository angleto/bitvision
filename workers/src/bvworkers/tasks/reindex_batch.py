"""Reindex one batch of targets under a new embedding model.

This task is the worker half of unit E5 (reindex orchestrator). The
companion CLI (``bvphoenix-reembed``) creates a ``reindex_jobs`` row
and enqueues ``reindex_batch(job_id, offset, limit)`` calls to cover
every candidate target. Each call:

1. Re-reads the job row so a ``paused``/``failed``/``rolled_back``
   state causes the in-flight batch to exit early. This is the cheap
   cooperative-cancel hook the CLI's ``cancel`` subcommand relies on.
2. Loads up to ``limit`` candidate targets starting at ``offset``.
   Candidate set depends on ``from_model_id``:
     * NULL ⇒ every target row of the given kind that does NOT already
       have an embedding under ``to_model_id`` (fresh embed).
     * set ⇒ every target row that has an embedding under
       ``from_model_id`` but NOT under ``to_model_id`` (migration).
3. Dispatches per-target embedding via the Model Registry (unit E4).
   If the registry import fails we fall back to the hard-coded
   BiomedCLIP provider already living in ``embed_series.py`` — this is
   the "E4 not yet landed" escape hatch the task spec calls out.
4. On each successful target, advances ``processed_items`` by 1 in a
   separate short transaction so a later crash still reports a
   durable, monotonically-increasing progress counter.

Key property: the task never touches existing embeddings. Old and new
models coexist; only the admin's explicit ``activate --default-for-kind``
flips which one serves search traffic.
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog
from bvphoenix.db.engine import make_async_engine
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from bvworkers.config import get_settings

log = structlog.get_logger(__name__)

# Bounded upper limit regardless of what the caller asks for — protects
# the worker from "embed 1M rows in one task" accidents.
MAX_BATCH_LIMIT = 1000

# Statuses that mean "stop what you're doing and don't run this batch".
# Checked on entry so ``cancel`` / external failures drain in-flight work
# without producing new rows.
_HALT_STATUSES = frozenset({"paused", "failed", "rolled_back", "completed"})


def _load_registry_embed():
    """Return ``(fn, source)`` where ``fn(target_kind, target_id,
    model_id, db) -> list[float] | None`` computes an embedding.

    Tries the unit-E4 Model Registry first; falls back to the hard-coded
    BiomedCLIP helper in ``embed_series.py`` if the registry module is
    not yet available in this worker. This is explicit, one-shot
    wiring — the fallback is logged on first use so it's visible in
    operations but never silently wrong.
    """
    try:
        # Expected shape once E4 lands; keep the name generic so small
        # renames in E4 don't break us.
        from bvworkers.tasks.model_registry import (  # type: ignore[import-not-found]
            embed_target,
        )

        return embed_target, "registry"
    except Exception:
        from bvworkers.tasks.embed_series import (
            MODEL_ID as _FALLBACK_MODEL_ID,
        )
        from bvworkers.tasks.embed_series import (
            _compute_embedding,
            _dicom_to_pil,
        )

        async def _fallback(
            target_kind: str,
            target_id: uuid.UUID,
            model_id: str,
            db: AsyncSession,
            s3: Any,
        ) -> list[float] | None:
            # The legacy path only knows how to embed a DICOM ``series``
            # with BiomedCLIP. Anything else in fallback mode is a hard
            # error so the operator notices E4 is required.
            if target_kind != "series":
                raise RuntimeError(
                    f"fallback embed supports target_kind='series' only, got {target_kind!r}"
                )
            if model_id != _FALLBACK_MODEL_ID:
                raise RuntimeError(
                    f"fallback embed is pinned to {_FALLBACK_MODEL_ID!r}, cannot run {model_id!r}"
                )
            result = await db.execute(
                text(
                    "SELECT s3_bucket, s3_key FROM instances WHERE series_id = :sid "
                    "ORDER BY instance_number ASC NULLS LAST"
                ),
                {"sid": target_id},
            )
            rows = result.all()
            if not rows:
                return None
            mid_bucket, mid_key = rows[len(rows) // 2]
            import asyncio

            def _do_embed() -> list[float]:
                resp = s3.get_object(Bucket=mid_bucket, Key=mid_key)
                dcm_bytes = resp["Body"].read()
                pil_img = _dicom_to_pil(dcm_bytes)
                return _compute_embedding(pil_img)

            return await asyncio.to_thread(_do_embed)

        return _fallback, "fallback"


async def _get_job(db: AsyncSession, job_id: uuid.UUID) -> dict | None:
    row = (
        await db.execute(
            text(
                "SELECT id, target_kind, from_model_id, to_model_id, status, "
                "processed_items, failed_items, batch_size "
                "FROM reindex_jobs WHERE id = :jid"
            ),
            {"jid": job_id},
        )
    ).first()
    if row is None:
        return None
    return {
        "id": row[0],
        "target_kind": row[1],
        "from_model_id": row[2],
        "to_model_id": row[3],
        "status": row[4],
        "processed_items": row[5],
        "failed_items": row[6],
        "batch_size": row[7],
    }


async def _candidate_ids(
    db: AsyncSession,
    target_kind: str,
    from_model_id: str | None,
    to_model_id: str,
    offset: int,
    limit: int,
) -> list[uuid.UUID]:
    """Return up to ``limit`` candidate target ids starting at ``offset``.

    Uses ``ORDER BY id`` so a crashed batch resumes at a deterministic
    position when re-enqueued with the same offset.
    """
    table = {
        "study": "studies",
        "series": "series",
        "instance": "instances",
    }[target_kind]

    if from_model_id is None:
        # Fresh embed: pick targets that lack a vector under to_model_id.
        sql = text(
            f"""
            SELECT t.id FROM {table} t
            WHERE NOT EXISTS (
                SELECT 1 FROM embeddings e
                WHERE e.target_kind = :kind
                  AND e.target_id = t.id
                  AND e.model_id = :to_model
            )
            ORDER BY t.id
            OFFSET :offset LIMIT :limit
            """
        )
        rows = await db.execute(
            sql,
            {
                "kind": target_kind,
                "to_model": to_model_id,
                "offset": offset,
                "limit": limit,
            },
        )
    else:
        # Migration: targets that already have from_model but not to_model.
        sql = text(
            """
            SELECT e_from.target_id FROM embeddings e_from
            WHERE e_from.target_kind = :kind
              AND e_from.model_id = :from_model
              AND NOT EXISTS (
                  SELECT 1 FROM embeddings e_to
                  WHERE e_to.target_kind = :kind
                    AND e_to.target_id = e_from.target_id
                    AND e_to.model_id = :to_model
              )
            ORDER BY e_from.target_id
            OFFSET :offset LIMIT :limit
            """
        )
        rows = await db.execute(
            sql,
            {
                "kind": target_kind,
                "from_model": from_model_id,
                "to_model": to_model_id,
                "offset": offset,
                "limit": limit,
            },
        )
    return [r[0] for r in rows.all()]


async def reindex_batch(
    ctx: dict,  # type: ignore[type-arg]
    job_id: str,
    offset: int,
    limit: int,
) -> dict:
    """Arq task: embed up to ``limit`` targets for reindex job ``job_id``.

    Returns a small summary dict — arq keeps results briefly in Redis so
    operators can spot-check individual batch outcomes without trawling
    the DB.
    """
    jid = uuid.UUID(job_id)
    limit = max(1, min(int(limit), MAX_BATCH_LIMIT))
    offset = max(0, int(offset))

    settings = get_settings()
    engine = make_async_engine(settings.database_url, pool_pre_ping=True)

    import boto3
    from botocore.client import Config

    s3 = boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint_url,
        region_name=settings.s3_region,
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key,
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )

    embed_fn, embed_source = _load_registry_embed()
    if embed_source == "fallback":
        log.info("reindex_batch.embed_fallback", job_id=job_id)

    processed = 0
    failed = 0
    last_error: str | None = None

    try:
        async with AsyncSession(engine) as db:
            job = await _get_job(db, jid)
            if job is None:
                return {"status": "missing_job", "job_id": job_id}
            if job["status"] in _HALT_STATUSES:
                # Cooperative cancel: the orchestrator flipped our state
                # while we were queued. Exit without touching anything.
                return {
                    "status": "halted",
                    "job_status": job["status"],
                    "job_id": job_id,
                }

            # First batch flips pending → running and stamps started_at
            # (COALESCE keeps the first stamp). The WHERE clause makes it
            # a no-op once status has advanced past 'running'.
            await db.execute(
                text(
                    "UPDATE reindex_jobs SET status = 'running', "
                    "started_at = COALESCE(started_at, NOW()) "
                    "WHERE id = :jid AND status IN ('pending','running')"
                ),
                {"jid": jid},
            )
            await db.commit()

            ids = await _candidate_ids(
                db,
                target_kind=job["target_kind"],
                from_model_id=job["from_model_id"],
                to_model_id=job["to_model_id"],
                offset=offset,
                limit=limit,
            )

        # Each target embeds in its own session so a single failure
        # commits progress up to that point.
        for tid in ids:
            # Re-check halt status between targets for fast cancellation.
            async with AsyncSession(engine) as db:
                job = await _get_job(db, jid)
                if job is None or job["status"] in _HALT_STATUSES:
                    break

            try:
                async with AsyncSession(engine) as db:
                    vec = await embed_fn(
                        job["target_kind"],
                        tid,
                        job["to_model_id"],
                        db,
                        s3,
                    )
                    if vec is None:
                        # Target had no pixel data / no instances — count
                        # as processed so progress keeps moving, but log
                        # it for operator visibility.
                        log.info(
                            "reindex_batch.skip_empty",
                            job_id=job_id,
                            target_id=str(tid),
                        )
                    else:
                        vec_str = "[" + ",".join(str(v) for v in vec) + "]"
                        await db.execute(
                            text(
                                "INSERT INTO embeddings "
                                "(target_kind, target_id, model_id, vector) "
                                "VALUES (:kind, :tid, :model, :vec) "
                                "ON CONFLICT (target_kind, target_id, model_id) "
                                "DO UPDATE SET vector = EXCLUDED.vector, "
                                "created_at = NOW()"
                            ),
                            {
                                "kind": job["target_kind"],
                                "tid": tid,
                                "model": job["to_model_id"],
                                "vec": vec_str,
                            },
                        )
                    await db.execute(
                        text(
                            "UPDATE reindex_jobs "
                            "SET processed_items = processed_items + 1 "
                            "WHERE id = :jid"
                        ),
                        {"jid": jid},
                    )
                    await db.commit()
                    processed += 1
            except Exception as exc:
                failed += 1
                last_error = f"{type(exc).__name__}: {exc}"[:500]
                log.warning(
                    "reindex_batch.target_failed",
                    job_id=job_id,
                    target_id=str(tid),
                    error=last_error,
                )
                async with AsyncSession(engine) as db:
                    await db.execute(
                        text(
                            "UPDATE reindex_jobs "
                            "SET failed_items = failed_items + 1, "
                            "error_summary = :err "
                            "WHERE id = :jid"
                        ),
                        {"jid": jid, "err": last_error},
                    )
                    await db.commit()

        # Mark the job completed when the counters reach total_items.
        # Guarded by a CAS-style WHERE so a concurrent rollback/cancel
        # wins and we never resurrect a terminated job.
        async with AsyncSession(engine) as db:
            await db.execute(
                text(
                    "UPDATE reindex_jobs "
                    "SET status = 'completed', completed_at = NOW() "
                    "WHERE id = :jid "
                    "  AND status = 'running' "
                    "  AND total_items IS NOT NULL "
                    "  AND processed_items + failed_items >= total_items"
                ),
                {"jid": jid},
            )
            await db.commit()

    finally:
        await engine.dispose()

    return {
        "status": "ok",
        "job_id": job_id,
        "offset": offset,
        "limit": limit,
        "processed": processed,
        "failed": failed,
        "embed_source": embed_source,
        "last_error": last_error,
    }
