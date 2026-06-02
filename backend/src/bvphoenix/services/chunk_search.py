"""Hybrid sub-document search across all text-bearing patient sources.

Backs natural-language Q&A retrieval at chunk granularity. Two
independent ranking signals are computed and merged via Reciprocal
Rank Fusion (RRF):

* Vector cosine over per-chunk multilingual MiniLM embeddings stored
  in ``text_embeddings`` under ``target_kind='document_chunk'``.
* Postgres full-text rank over the chunk's generated ``text_tsv``
  column (Italian text-search config), via ``plainto_tsquery``.

RRF is preferred over score-blending because the two signals are on
different scales (cosine in [0,1], ts_rank unbounded) and rank-based
fusion is robust without per-corpus tuning.

Patient scoping is enforced server-side by binding ``patient_id`` to
the queried row set; callers cannot relax it. Cross-patient access is
already prevented at chunking time (the worker reads ``patient_id``
from the source row, never from the caller), and the explicit
predicate here is kept as defence in depth.

Filters surface every dimension the user can want to narrow on:
``source_kind`` (document / clinical_note / summary / report_content),
``author_kind`` (human / agent / system / unknown — supports the
"escludi note AI" UX), ``authority_id`` ("solo originali"), the
document subkind (``document_kind_id``), date bounds, single-source
restriction, and chunker version.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.db.models.text_chunks import (
    CHUNK_AUTHOR_KINDS,
    CHUNK_SOURCE_KINDS,
    DEFAULT_CHUNKER_VERSION,
)
from bvphoenix.services.bge_m3 import embed_query_dense as _embed_query_bge_m3
from bvphoenix.services.embedding_models import get_default_model
from bvphoenix.services.reranker import rerank_order
from bvphoenix.services.text_models import (
    BGE_M3_MODEL_ID,
    MULTILINGUAL_MODEL_ID,
    TEXT_MODELS,
)
from bvphoenix.services.vector_search import tune_vector_query

__all__ = [
    "CHUNK_AUTHOR_KINDS",
    "CHUNK_SOURCE_KINDS",
    "DEFAULT_CHUNKER_VERSION",
    "MULTILINGUAL_MODEL_ID",
    "ChunkHit",
    "search_chunks",
]


MULTILINGUAL_MODEL_NAME = "paraphrase-multilingual-MiniLM-L12-v2"
EMBEDDING_DIM = 384

# RRF constant per the original paper. Stable enough that we don't
# expose it to callers.
_RRF_K = 60.0
# Over-fetch so the union of the two top-k lists has enough candidates
# to fill k slots after RRF.
_OVERFETCH_FACTOR = 4
# When reranking, score this many RRF candidates with the cross-encoder
# before trimming to k. Wide enough to recover a passage RRF underranked,
# bounded so the per-pair CPU cost stays acceptable.
_RERANK_POOL = 30

_minilm_model: Any | None = None


@dataclass(frozen=True)
class ChunkHit:
    """One ranked chunk with provenance and a short excerpt.

    No S3 keys, no bucket names: ``source_kind`` + ``source_id`` +
    ``chunk_id`` are the only references the caller surfaces. The
    frontend resolves them via existing patient-scoped APIs to load
    the document viewer or note pane.
    """

    chunk_id: uuid.UUID
    source_kind: str
    source_id: uuid.UUID
    page: int | None
    char_start: int
    char_end: int
    excerpt: str
    score: float
    author_kind: str
    authority_id: str | None
    document_kind_id: str | None


def _ensure_multilingual_model() -> Any:
    """Load the multilingual MiniLM encoder on first call (cached)."""
    global _minilm_model
    if _minilm_model is not None:
        return _minilm_model
    from sentence_transformers import SentenceTransformer

    _minilm_model = SentenceTransformer(MULTILINGUAL_MODEL_NAME)
    return _minilm_model


def _embed_sync(query: str) -> list[float]:
    """Blocking forward pass; run in a worker thread from async code."""
    import numpy as np

    model = _ensure_multilingual_model()
    arr = model.encode([query], normalize_embeddings=True, show_progress_bar=False)
    return np.asarray(arr[0], dtype=float).tolist()


async def _embed_query(query: str) -> list[float]:
    return await asyncio.to_thread(_embed_sync, query)


def _vec_literal(vector: list[float]) -> str:
    return "[" + ",".join(repr(float(v)) for v in vector) + "]"


def _excerpt(text_value: str, *, max_chars: int = 320) -> str:
    """Single-line trimmed preview safe for transport."""
    flat = " ".join(text_value.split())
    if len(flat) <= max_chars:
        return flat
    return flat[: max_chars - 1].rstrip() + "…"


def _coerce_list(value: str | list[str] | None) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        return [value]
    return list(value)


def _validate_enum(values: list[str] | None, allowed: tuple[str, ...], name: str) -> None:
    if values is None:
        return
    for v in values:
        if v not in allowed:
            raise ValueError(f"{name}={v!r} not in {allowed}")


async def search_chunks(
    db: AsyncSession,
    *,
    patient_id: uuid.UUID,
    query: str,
    k: int = 8,
    source_kind: str | list[str] | None = None,
    author_kind: str | list[str] | None = None,
    exclude_ai: bool = False,
    authority_id: str | list[str] | None = None,
    document_kind_id: str | list[str] | None = None,
    since: date | None = None,
    until: date | None = None,
    source_id: uuid.UUID | None = None,
    chunker_version: str = DEFAULT_CHUNKER_VERSION,
    rerank: bool = False,
) -> list[ChunkHit]:
    """Return up to ``k`` ranked chunks for ``query`` in this patient's data.

    Filters:
        ``source_kind`` — restrict to a subset of {'document',
            'clinical_note', 'summary', 'report_content'}.
        ``author_kind`` — restrict to a subset of {'human','agent',
            'system','unknown'}.
        ``exclude_ai`` — convenience flag that adds ``author_kind !=
            'agent'`` (combined with any explicit ``author_kind``).
        ``authority_id`` — restrict to a subset of the document
            authority taxonomy (``original``/``derived``/...).
        ``document_kind_id`` — restrict by document kind (only
            meaningful for ``source_kind='document'``).
        ``since`` / ``until`` — bounds on the chunk's ``created_at``.
        ``source_id`` — restrict to a single source row.
        ``chunker_version`` — pin the chunker version.

    The function is patient-scoped at the query level: callers must
    supply ``patient_id`` and the SQL forces every candidate row to
    match it. There is no global / cross-patient mode.
    """
    if not query or not query.strip():
        return []
    if k <= 0:
        return []

    over = max(1, k * _OVERFETCH_FACTOR)

    source_kinds = _coerce_list(source_kind)
    author_kinds = _coerce_list(author_kind)
    authority_ids = _coerce_list(authority_id)
    document_kinds = _coerce_list(document_kind_id)
    _validate_enum(source_kinds, CHUNK_SOURCE_KINDS, "source_kind")
    _validate_enum(author_kinds, CHUNK_AUTHOR_KINDS, "author_kind")

    where_clauses = [
        "ch.patient_id = :patient_id",
        "ch.chunker_version = :chunker_version",
    ]
    params: dict[str, Any] = {
        "patient_id": patient_id,
        "chunker_version": chunker_version,
    }
    if source_kinds is not None:
        where_clauses.append("ch.source_kind = ANY(:source_kinds)")
        params["source_kinds"] = source_kinds
    if author_kinds is not None:
        where_clauses.append("ch.author_kind = ANY(:author_kinds)")
        params["author_kinds"] = author_kinds
    if exclude_ai:
        where_clauses.append("ch.author_kind <> 'agent'")
    if authority_ids is not None:
        where_clauses.append("ch.authority_id = ANY(:authority_ids)")
        params["authority_ids"] = authority_ids
    if document_kinds is not None:
        where_clauses.append("ch.document_kind_id = ANY(:document_kinds)")
        params["document_kinds"] = document_kinds
    if since is not None:
        where_clauses.append("ch.created_at >= :since")
        params["since"] = since
    if until is not None:
        where_clauses.append("ch.created_at <= :until")
        params["until"] = until
    if source_id is not None:
        where_clauses.append("ch.source_id = :source_id")
        params["source_id"] = source_id

    # Hide chunks belonging to a stale (superseded) report_content. The
    # canonical successor carries its own chunks reindexed at supersede
    # time, so the LLM still gets the equivalent content via the head
    # of the chain. Without this filter, retrieval can surface the same
    # clinical claim twice (once on the stale row, once on the head)
    # and the model ends up citing both versions of the same report.
    where_clauses.append(
        "NOT (ch.source_kind = 'report_content' AND EXISTS ("
        "  SELECT 1 FROM report_contents rc "
        "  WHERE rc.id = ch.source_id AND rc.status = 'stale'"
        "))"
    )

    where_sql = " AND ".join(where_clauses)

    # ---- resolve the active text model (registry default) ----
    # Both stores are populated during the MiniLM -> BGE-M3 transition;
    # the registry's default-for-kind decides which one search reads, so
    # flipping the default (post-backfill) switches retrieval with no code
    # change. Falls back to MiniLM if the registry can't be resolved.
    active_model_id = MULTILINGUAL_MODEL_ID
    try:
        active_model_id = (await get_default_model("text", db)).name
    except Exception:
        active_model_id = MULTILINGUAL_MODEL_ID
    if active_model_id == BGE_M3_MODEL_ID:
        _embed = _embed_query_bge_m3
    else:
        active_model_id = MULTILINGUAL_MODEL_ID
        _embed = _embed_query
    # Store table is the single routing fact, shared with the backfill CLI
    # and the write path via TEXT_MODELS, so a model/table change touches
    # one place. active_model_id is guaranteed to be a TEXT_MODELS key here.
    vec_table = TEXT_MODELS[active_model_id].store_table

    # ---- vector top-k ----
    # The active text encoder lives in the optional ``ai`` extra. When it
    # is not installed (CI without ``uv sync --extra ai``, lightweight
    # test envs) we degrade to FTS-only retrieval rather than 500-ing.
    try:
        vec = await _embed(query)
    except (ImportError, ModuleNotFoundError):
        vec = None
    vec_rows: list = []
    if vec is not None:
        # ``vec_table`` is one of two fixed literals (never user input).
        vec_sql = text(
            f"""
            SELECT ch.id AS chunk_id, te.vector <=> (:vec)::vector AS distance
            FROM text_chunks ch
            JOIN {vec_table} te
              ON te.target_id = ch.id
             AND te.target_kind = 'document_chunk'
             AND te.model_id = :model_id
            WHERE {where_sql}
            ORDER BY distance ASC
            LIMIT :limit
            """
        )
        # Per-patient is the most selective filter in the system (one
        # patient out of thousands), so this is exactly where plain HNSW
        # post-filtering under-returns; iterative scan keeps pulling until
        # the patient's own chunks fill the slab.
        await tune_vector_query(db, k=over, filtered=True)
        try:
            vec_rows = (
                await db.execute(
                    vec_sql,
                    {
                        **params,
                        "vec": _vec_literal(vec),
                        "model_id": active_model_id,
                        "limit": over,
                    },
                )
            ).all()
        except ProgrammingError:
            # text_embeddings or pgvector extension not provisioned —
            # degrade to FTS-only rather than 500-ing.
            await db.rollback()
            vec_rows = []

    # ---- FTS top-k ----
    fts_sql = text(
        f"""
        SELECT ch.id AS chunk_id,
               ts_rank_cd(ch.text_tsv, plainto_tsquery('italian', :q)) AS rank
        FROM text_chunks ch
        WHERE {where_sql}
          AND ch.text_tsv @@ plainto_tsquery('italian', :q)
        ORDER BY rank DESC
        LIMIT :limit
        """
    )
    fts_rows = (await db.execute(fts_sql, {**params, "q": query, "limit": over})).all()

    # ---- RRF ----
    rank_vec = {row[0]: i + 1 for i, row in enumerate(vec_rows)}
    rank_fts = {row[0]: i + 1 for i, row in enumerate(fts_rows)}
    candidates = set(rank_vec) | set(rank_fts)
    if not candidates:
        return []
    scored: list[tuple[uuid.UUID, float]] = []
    for cid in candidates:
        s = 0.0
        if cid in rank_vec:
            s += 1.0 / (_RRF_K + rank_vec[cid])
        if cid in rank_fts:
            s += 1.0 / (_RRF_K + rank_fts[cid])
        scored.append((cid, s))
    scored.sort(key=lambda x: x[1], reverse=True)
    # When reranking, enrich a wider RRF pool so the cross-encoder can
    # promote a passage the rank fusion underranked; otherwise just k.
    pool_size = max(k, _RERANK_POOL) if rerank else k
    pool = scored[:pool_size]
    top_ids = [cid for cid, _ in pool]
    if not top_ids:
        return []
    score_by_id = dict(pool)

    # ---- enrich with chunk metadata (no S3 / no bucket leakage) ----
    # Defence-in-depth: even though the candidate ids came from a
    # patient-scoped query above, we re-bind ``patient_id`` here. A
    # future bug in the ranker (e.g. a ``UNION ALL`` that forgets the
    # predicate) would then be caught at this last hop instead of
    # leaking foreign rows to the caller.
    enrich_sql = text(
        """
        SELECT id, source_kind, source_id, page, char_start, char_end,
               text, author_kind, authority_id, document_kind_id
        FROM text_chunks
        WHERE id = ANY(:ids) AND patient_id = :patient_id
        """
    )
    enrich_rows = (await db.execute(enrich_sql, {"ids": top_ids, "patient_id": patient_id})).all()
    by_id = {r[0]: r for r in enrich_rows}

    # Keep each hit's full chunk text alongside it so the cross-encoder
    # rescues quality on the full passage, not the truncated excerpt.
    pooled: list[tuple[ChunkHit, str]] = []
    for cid in top_ids:
        row = by_id.get(cid)
        if row is None:
            continue
        full_text = row[6]
        pooled.append(
            (
                ChunkHit(
                    chunk_id=row[0],
                    source_kind=row[1],
                    source_id=row[2],
                    page=row[3],
                    char_start=row[4],
                    char_end=row[5],
                    excerpt=_excerpt(full_text),
                    score=round(score_by_id[cid], 6),
                    author_kind=row[7],
                    authority_id=row[8],
                    document_kind_id=row[9],
                ),
                full_text or "",
            )
        )

    if rerank and len(pooled) > 1:
        # Cross-encoder reorders the pool by true (query, passage)
        # relevance; on no ai extra it returns None and we keep RRF order.
        order = await rerank_order(query, [text_ for _, text_ in pooled], top_n=k)
        if order is not None:
            pooled = [pooled[i] for i in order]

    return [hit for hit, _ in pooled[:k]]
