"""Embedding coverage + error-tracking API — admin-only.

Powers the admin dashboard view at ``/admin/embeddings`` in the frontend:

- ``GET /api/embeddings/coverage`` — for every (model_id, target_kind)
  pair that has at least one row in ``embeddings`` *or* one row in
  ``embedding_errors``, return ``(total, done, failed, pending,
  percentage)`` plus the last 10 failures so the admin can drill in.

- ``POST /api/embeddings/retry-failed`` — re-enqueue every target that
  currently has a row in ``embedding_errors`` (and no matching
  ``embeddings`` row) for the given model / kind.

- ``POST /api/embeddings/embed-missing`` — enqueue every target that
  has no ``embeddings`` row (regardless of whether it previously failed).

All three are gated behind :func:`bvphoenix.auth.require_admin` — agent
tokens are rejected there, so this surface is strictly human-gated.
"""

from __future__ import annotations

from typing import Annotated, Literal

from arq import create_pool
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.auth import require_admin
from bvphoenix.config import get_settings
from bvphoenix.db.models import User
from bvphoenix.db.session import get_db
from bvphoenix.services.arq_redis import redis_settings
from bvphoenix.services.embeddable import embeddable_modality_clause
from bvphoenix.services.embedding_models import get_default_model
from bvphoenix.services.text_models import DEFAULT_TEXT_MODEL_ID, TEXT_MODELS

router = APIRouter(prefix="/embeddings", tags=["embeddings-admin"])

# Map (target_kind, model_id) → arq task name. We only ship the series /
# BiomedCLIP path today; the map makes it trivial to add study-level
# workers later without touching the coverage code.
_TASK_FOR_KIND: dict[str, str] = {
    "series": "embed_series",
}


def _total_sql_for_kind(target_kind: str) -> str:
    """Return the SQL that counts all targets of this kind.

    Split out so coverage stays a single round trip per row even when we
    add ``study`` / ``instance`` kinds. Returns a scalar-producing
    ``SELECT`` with no parameters.
    """
    if target_kind == "series":
        # Denominator counts only EMBEDDABLE series — non-image series
        # (SR / PR / SEG) are not eligible for BiomedCLIP, so including them
        # would permanently mask the percentage. Source: services.embeddable.
        return f"SELECT COUNT(*) FROM series WHERE {embeddable_modality_clause('modality')}"
    if target_kind == "study":
        # v3 (0073): table renamed studies → imaging_studies.
        return "SELECT COUNT(*) FROM imaging_studies"
    if target_kind == "instance":
        return "SELECT COUNT(*) FROM instances"
    return "SELECT 0"


class LastFailureOut(BaseModel):
    target_id: str
    error_message: str
    error_class: str | None
    failed_at: str
    retry_count: int


class CoverageRow(BaseModel):
    model_id: str
    target_kind: Literal["study", "series", "instance"]
    total: int
    done: int
    failed: int
    pending: int
    percentage: float
    last_failures: list[LastFailureOut]


class CoverageOut(BaseModel):
    items: list[CoverageRow]


@router.get("/coverage", response_model=CoverageOut)
async def embedding_coverage(
    db: Annotated[AsyncSession, Depends(get_db)],
    _admin: Annotated[User, Depends(require_admin)],
) -> CoverageOut:
    """Per-(model × kind) coverage summary for the admin dashboard."""
    # Find every (model_id, target_kind) pair that's ever been touched,
    # from either side — UNIONing embeddings + embedding_errors means a
    # model that 100%-failed still shows up as a row the admin can act on.
    pair_rows = (
        await db.execute(
            text(
                "SELECT model_id, target_kind FROM embeddings "
                "UNION "
                "SELECT model_id, target_kind FROM embedding_errors "
                "ORDER BY target_kind, model_id"
            )
        )
    ).all()

    items: list[CoverageRow] = []
    for model_id, target_kind in pair_rows:
        if target_kind not in ("study", "series", "instance"):
            # Skip anything the CHECK constraint would have blocked —
            # defence in depth for a UNION over two tables.
            continue

        total = (await db.execute(text(_total_sql_for_kind(target_kind)))).scalar_one()

        done = (
            await db.execute(
                text(
                    "SELECT COUNT(*) FROM embeddings "
                    "WHERE model_id = :model AND target_kind = :kind"
                ),
                {"model": model_id, "kind": target_kind},
            )
        ).scalar_one()

        # A "failed" target is one that has an error row *and* no
        # successful embedding row for the same (model, kind, target_id).
        # Otherwise a retry that eventually succeeded would double-count.
        failed = (
            await db.execute(
                text(
                    "SELECT COUNT(DISTINCT ee.target_id) FROM embedding_errors ee "
                    "WHERE ee.model_id = :model AND ee.target_kind = :kind "
                    "AND NOT EXISTS ("
                    "    SELECT 1 FROM embeddings e "
                    "    WHERE e.model_id = ee.model_id "
                    "      AND e.target_kind = ee.target_kind "
                    "      AND e.target_id = ee.target_id"
                    ")"
                ),
                {"model": model_id, "kind": target_kind},
            )
        ).scalar_one()

        total_int = int(total)
        done_int = int(done)
        failed_int = int(failed)
        pending = max(total_int - done_int - failed_int, 0)
        pct = (done_int / total_int * 100.0) if total_int else 0.0

        failures = (
            await db.execute(
                text(
                    "SELECT DISTINCT ON (target_id) target_id, error_message, "
                    "error_class, failed_at, retry_count "
                    "FROM embedding_errors "
                    "WHERE model_id = :model AND target_kind = :kind "
                    "ORDER BY target_id, failed_at DESC"
                ),
                {"model": model_id, "kind": target_kind},
            )
        ).all()
        # Sort by most recent failure across targets, then take 10.
        failures_sorted = sorted(failures, key=lambda r: r[3], reverse=True)[:10]

        items.append(
            CoverageRow(
                model_id=model_id,
                target_kind=target_kind,  # type: ignore[arg-type]
                total=total_int,
                done=done_int,
                failed=failed_int,
                pending=pending,
                percentage=round(pct, 2),
                last_failures=[
                    LastFailureOut(
                        target_id=str(f[0]),
                        error_message=f[1],
                        error_class=f[2],
                        failed_at=f[3].isoformat(),
                        retry_count=int(f[4]),
                    )
                    for f in failures_sorted
                ],
            )
        )

    return CoverageOut(items=items)


class EnqueueResult(BaseModel):
    status: str
    model_id: str
    target_kind: str
    enqueued: int


async def _enqueue_targets(target_ids: list[str], task_name: str) -> int:
    """Push one job per target to arq. Returns the number enqueued."""
    if not target_ids:
        return 0
    settings = get_settings()
    redis = await create_pool(redis_settings(settings.redis_url))
    try:
        for tid in target_ids:
            await redis.enqueue_job(task_name, tid)
    finally:
        await redis.close()
    return len(target_ids)


@router.post("/retry-failed", response_model=EnqueueResult)
async def retry_failed(
    db: Annotated[AsyncSession, Depends(get_db)],
    _admin: Annotated[User, Depends(require_admin)],
    model_id: str = Query(..., max_length=128),
    target_kind: Literal["study", "series", "instance"] = Query(...),
) -> EnqueueResult:
    """Re-enqueue every target that has an error but no embedding yet.

    The worker is responsible for inserting / updating the
    ``embeddings`` row on success; on failure it will append another
    ``embedding_errors`` row and bump ``retry_count``.
    """
    task_name = _TASK_FOR_KIND.get(target_kind)
    if task_name is None:
        return EnqueueResult(
            status="unsupported_kind",
            model_id=model_id,
            target_kind=target_kind,
            enqueued=0,
        )

    # For the series kind, never re-enqueue a non-image series: its error
    # rows are non-actionable (it can never embed), so retrying would only
    # churn the worker. Source of truth: services.embeddable.
    series_filter = ""
    if target_kind == "series":
        series_filter = (
            " AND EXISTS (SELECT 1 FROM series s WHERE s.id = ee.target_id "
            f"AND {embeddable_modality_clause('s.modality')})"
        )

    rows = (
        await db.execute(
            text(
                "SELECT DISTINCT ee.target_id FROM embedding_errors ee "
                "WHERE ee.model_id = :model AND ee.target_kind = :kind "
                "AND NOT EXISTS ("
                "    SELECT 1 FROM embeddings e "
                "    WHERE e.model_id = ee.model_id "
                "      AND e.target_kind = ee.target_kind "
                "      AND e.target_id = ee.target_id"
                ")" + series_filter
            ),
            {"model": model_id, "kind": target_kind},
        )
    ).all()

    target_ids = [str(r[0]) for r in rows]
    n = await _enqueue_targets(target_ids, task_name)
    return EnqueueResult(
        status="enqueued",
        model_id=model_id,
        target_kind=target_kind,
        enqueued=n,
    )


@router.post("/embed-missing", response_model=EnqueueResult)
async def embed_missing(
    db: Annotated[AsyncSession, Depends(get_db)],
    _admin: Annotated[User, Depends(require_admin)],
    model_id: str = Query(..., max_length=128),
    target_kind: Literal["study", "series", "instance"] = Query(...),
) -> EnqueueResult:
    """Enqueue a job for every target that does not yet have an embedding.

    Unlike ``retry-failed``, this also picks up targets that were never
    attempted — the common path right after adding a brand-new model.
    """
    task_name = _TASK_FOR_KIND.get(target_kind)
    if task_name is None:
        return EnqueueResult(
            status="unsupported_kind",
            model_id=model_id,
            target_kind=target_kind,
            enqueued=0,
        )

    # Keep this as a single SQL round trip — "targets of this kind that
    # have no embeddings row for this model". We intentionally do NOT
    # exclude rows currently in embedding_errors; the worker itself
    # de-duplicates against the embeddings table on entry.
    if target_kind == "series":
        # Only embeddable series — exclude non-image (SR / PR / SEG) so the
        # admin "embed missing" button never enqueues a job that can only
        # no-op. Source of truth: services.embeddable.
        src_sql = f"SELECT id FROM series WHERE {embeddable_modality_clause('modality')}"
    elif target_kind == "study":
        # v3 (0073): table renamed studies → imaging_studies.
        src_sql = "SELECT id FROM imaging_studies"
    else:
        src_sql = "SELECT id FROM instances"

    rows = (
        await db.execute(
            text(
                f"SELECT t.id FROM ({src_sql}) AS t "
                "WHERE NOT EXISTS ("
                "    SELECT 1 FROM embeddings e "
                "    WHERE e.target_kind = :kind "
                "      AND e.model_id = :model "
                "      AND e.target_id = t.id"
                ")"
            ),
            {"kind": target_kind, "model": model_id},
        )
    ).all()

    target_ids = [str(r[0]) for r in rows]
    n = await _enqueue_targets(target_ids, task_name)
    return EnqueueResult(
        status="enqueued",
        model_id=model_id,
        target_kind=target_kind,
        enqueued=n,
    )


# ---------------------------------------------------------------------------
# Text-chunk coverage — distinct from BiomedCLIP which embeds DICOM series.
# text_chunks rows come from the chunk_and_embed_* workers; their vectors
# live in the active text model's store (text_embeddings for MiniLM,
# text_embeddings_bge_m3 for BGE-M3) under target_kind='document_chunk'.
# Store + task are resolved per-model from the shared TEXT_MODELS spec, so
# this surface tracks whatever model is active instead of hard-coding one.
# ---------------------------------------------------------------------------


async def _resolve_text_model(db: AsyncSession, model: str | None) -> str:
    """Resolve + validate a text ``model_id``, defaulting to the registry's
    active text default. 400 on an unknown model so a typo can't silently
    target a non-existent store."""
    if model is None:
        try:
            model = (await get_default_model("text", db)).name
        except Exception:
            model = DEFAULT_TEXT_MODEL_ID
    if model not in TEXT_MODELS:
        raise HTTPException(
            status_code=400,
            detail=f"unknown text model {model!r}; known: {sorted(TEXT_MODELS)}",
        )
    return model


class TextChunkCoverageOut(BaseModel):
    total_chunks: int
    embedded_chunks: int
    pending_chunks: int
    pct: int
    by_source_kind: list[dict]
    model_id: str


@router.get("/text-chunks/coverage", response_model=TextChunkCoverageOut)
async def text_chunk_coverage(
    db: Annotated[AsyncSession, Depends(get_db)],
    _admin: Annotated[User, Depends(require_admin)],
    model: Annotated[str | None, Query(max_length=128)] = None,
) -> TextChunkCoverageOut:
    """Coverage of one text model's chunk embeddings over every
    ``text_chunks`` row (documents, clinical_notes, summaries,
    report_contents). ``model`` defaults to the registry's active text
    default; pass a model_id to inspect a specific store. The Q&A free /
    standard / premium paths consume these vectors via
    ``services.chunk_search``."""
    resolved = await _resolve_text_model(db, model)
    store = TEXT_MODELS[resolved].store_table
    total = (await db.execute(text("SELECT COUNT(*) FROM text_chunks"))).scalar_one()
    embedded = (
        await db.execute(
            # ``store`` comes only from the validated TEXT_MODELS spec, so
            # the f-string interpolation (table names can't be bound) is safe.
            text(
                f"SELECT COUNT(*) FROM {store} te "
                "JOIN text_chunks tc ON tc.id = te.target_id "
                "WHERE te.target_kind = 'document_chunk'"
            )
        )
    ).scalar_one()
    by_kind = (
        await db.execute(
            text(
                "SELECT tc.source_kind, "
                "  COUNT(*) AS total, "
                "  COUNT(te.target_id) AS embedded "
                "FROM text_chunks tc "
                f"LEFT JOIN {store} te ON te.target_id = tc.id "
                "  AND te.target_kind = 'document_chunk' "
                "GROUP BY tc.source_kind "
                "ORDER BY tc.source_kind"
            )
        )
    ).all()

    pending = max(0, int(total) - int(embedded))
    pct = round(100 * embedded / max(1, total))
    return TextChunkCoverageOut(
        total_chunks=int(total),
        embedded_chunks=int(embedded),
        pending_chunks=pending,
        pct=pct,
        by_source_kind=[
            {"source_kind": k, "total": int(t), "embedded": int(e), "pending": int(t) - int(e)}
            for (k, t, e) in by_kind
        ],
        model_id=resolved,
    )


@router.post("/text-chunks/embed-missing", response_model=EnqueueResult)
async def embed_missing_text_chunks(
    db: Annotated[AsyncSession, Depends(get_db)],
    _admin: Annotated[User, Depends(require_admin)],
    model: Annotated[str | None, Query(max_length=128)] = None,
) -> EnqueueResult:
    """Enqueue the text model's embed task for every ``text_chunks`` row
    with no vector in that model's store. ``model`` defaults to the
    registry's active text default. Useful right after rolling out a new
    text model (the chunks exist but their vectors do not)."""
    resolved = await _resolve_text_model(db, model)
    spec = TEXT_MODELS[resolved]
    rows = (
        await db.execute(
            # ``spec.store_table`` is from the validated TEXT_MODELS spec.
            text(
                "SELECT tc.id::text, tc.text FROM text_chunks tc "
                "WHERE NOT EXISTS ("
                f"    SELECT 1 FROM {spec.store_table} te "
                "    WHERE te.target_kind = 'document_chunk' "
                "      AND te.target_id = tc.id"
                ")"
            )
        )
    ).all()
    if not rows:
        return EnqueueResult(
            status="enqueued",
            model_id=resolved,
            target_kind="document_chunk",
            enqueued=0,
        )

    pool = await create_pool(redis_settings(get_settings().redis_url))
    enq = 0
    for cid, body in rows:
        try:
            await pool.enqueue_job(spec.arq_task, "document_chunk", cid, body)
            enq += 1
        except Exception:
            pass
    return EnqueueResult(
        status="enqueued",
        model_id=resolved,
        target_kind="document_chunk",
        enqueued=enq,
    )
