"""BGE-M3 (BAAI/bge-m3) text embeddings — dense, sparse, ColBERT.

Two Arq tasks:

* ``embed_bge_m3_all`` (primary) — DENSE + SPARSE (lexical) + ColBERT
  (multi-vector) from ONE ``BGEM3FlagModel`` forward pass (FlagEmbedding),
  upserted into the three bge-m3-v1 stores in one transaction. The encode +
  serialization live in ``bvphoenix.services.bge_m3`` (shared with the backend
  query path, no duplication). Degrades to ``embed_bge_m3_dense`` (dense only)
  when FlagEmbedding is unavailable, so a build regression keeps populating the
  dense store rather than failing.
* ``embed_bge_m3_dense`` (fallback) — the 1024-d DENSE vector via
  ``sentence-transformers`` only. Kept registered as the explicit dense-only
  path; its dense output matches FlagEmbedding's (same weights, CLS pooling,
  L2-normalized).

The model is pre-baked into the worker image (HF_HOME): a runtime HF download
is slow/rate-limited and times out on CPU ARM. Requires the ``ai`` extra.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

MODEL_NAME = "BAAI/bge-m3"
MODEL_ID = "bge-m3-v1"
EMBEDDING_DIM = 1024

ALLOWED_TARGET_KINDS: frozenset[str] = frozenset(
    {
        "series",
        "report",
        "report_content",
        "annotation",
        "consultation",
        "document",
        "patient",
        "document_chunk",
        "finding",
        "study",
    }
)

_model: Any | None = None  # lazy: loaded once per worker on first call


def _ensure_model() -> Any:
    """Load BGE-M3 (sentence-transformers) on first use (module singleton)."""
    global _model
    if _model is not None:
        return _model

    from sentence_transformers import SentenceTransformer

    # Weights are pre-baked into the image (HF_HOME), so this loads from
    # local cache rather than the network.
    _model = SentenceTransformer(MODEL_NAME)
    return _model


def _compute_dense(text_value: str) -> list[float]:
    """Run BGE-M3 on a string, return the 1024-d dense vector.

    L2-normalized so dot-product equals cosine similarity and matches the
    ``vector_cosine_ops`` HNSW index used at query time.
    """
    model = _ensure_model()
    vector = model.encode(
        text_value,
        normalize_embeddings=True,
        convert_to_numpy=True,
        show_progress_bar=False,
    )
    return vector.tolist()


async def embed_bge_m3_dense(
    ctx: dict,  # type: ignore[type-arg]
    target_kind: str,
    target_id: str,
    text_value: str,
) -> dict:
    """Arq task: BGE-M3 dense-embed ``text_value`` and upsert into
    ``text_embeddings_bge_m3``.

    Mirrors ``embed_text_ml`` (MiniLM) so both stores are populated during
    the transition. Idempotent on ``(target_kind, target_id, model_id)``.
    """
    if target_kind not in ALLOWED_TARGET_KINDS:
        return {
            "status": "invalid_target_kind",
            "target_kind": target_kind,
            "allowed": sorted(ALLOWED_TARGET_KINDS),
        }

    if not text_value or not text_value.strip():
        return {
            "status": "empty_text",
            "target_kind": target_kind,
            "target_id": target_id,
        }

    tid = uuid.UUID(target_id)

    # CPU-bound encode → offload to a thread so we don't block the loop.
    vector = await asyncio.to_thread(_compute_dense, text_value)

    # pgvector literal format: "[v1,v2,...,vN]"
    vec_str = "[" + ",".join(str(v) for v in vector) + "]"

    async with AsyncSession(ctx["db_engine"]) as db:
        await db.execute(
            text(
                "INSERT INTO text_embeddings_bge_m3 "
                "(target_kind, target_id, model_id, vector) "
                "VALUES (:kind, :tid, :model, :vec) "
                "ON CONFLICT (target_kind, target_id, model_id) DO UPDATE SET "
                "vector = EXCLUDED.vector, created_at = NOW()"
            ),
            {"kind": target_kind, "tid": tid, "model": MODEL_ID, "vec": vec_str},
        )
        await db.commit()

    return {
        "status": "embedded",
        "target_kind": target_kind,
        "target_id": target_id,
        "model_id": MODEL_ID,
        "dim": len(vector),
    }


async def embed_bge_m3_all(
    ctx: dict,  # type: ignore[type-arg]
    target_kind: str,
    target_id: str,
    text_value: str,
) -> dict:
    """Arq task: BGE-M3 DENSE + SPARSE + ColBERT in ONE FlagEmbedding forward,
    upserted into the three bge-m3-v1 stores in a single transaction.

    Degrades to ``embed_bge_m3_dense`` (sentence-transformers, dense only) when
    FlagEmbedding is unavailable, so a build regression keeps the dense store
    populated rather than failing the chunk. Idempotent on
    ``(target_kind, target_id, model_id)`` per store.
    """
    if target_kind not in ALLOWED_TARGET_KINDS:
        return {
            "status": "invalid_target_kind",
            "target_kind": target_kind,
            "allowed": sorted(ALLOWED_TARGET_KINDS),
        }

    if not text_value or not text_value.strip():
        return {
            "status": "empty_text",
            "target_kind": target_kind,
            "target_id": target_id,
        }

    tid = uuid.UUID(target_id)

    # One forward pass -> dense + sparse + colbert. Shared encode/serialization
    # lives in bvphoenix.services.bge_m3 (also used by the query path).
    try:
        from bvphoenix.services.bge_m3 import flag_encode_sync

        full = await asyncio.to_thread(flag_encode_sync, text_value)
    except ImportError:
        # FlagEmbedding not in this image -> dense-only (today's behavior).
        return await embed_bge_m3_dense(ctx, target_kind, target_id, text_value)

    dense_str = "[" + ",".join(str(v) for v in full["dense"]) + "]"

    async with AsyncSession(ctx["db_engine"]) as db:
        await db.execute(
            text(
                "INSERT INTO text_embeddings_bge_m3 "
                "(target_kind, target_id, model_id, vector) "
                "VALUES (:kind, :tid, :model, :vec) "
                "ON CONFLICT (target_kind, target_id, model_id) DO UPDATE SET "
                "vector = EXCLUDED.vector, created_at = NOW()"
            ),
            {"kind": target_kind, "tid": tid, "model": MODEL_ID, "vec": dense_str},
        )
        await db.execute(
            text(
                "INSERT INTO text_embeddings_bge_m3_sparse "
                "(target_kind, target_id, model_id, sparse) "
                "VALUES (:kind, :tid, :model, (:sparse)::sparsevec) "
                "ON CONFLICT (target_kind, target_id, model_id) DO UPDATE SET "
                "sparse = EXCLUDED.sparse, created_at = NOW()"
            ),
            {"kind": target_kind, "tid": tid, "model": MODEL_ID, "sparse": full["sparse_text"]},
        )
        await db.execute(
            text(
                "INSERT INTO text_embeddings_bge_m3_colbert "
                "(target_kind, target_id, model_id, n_tokens, token_dim, colbert) "
                "VALUES (:kind, :tid, :model, :n, 1024, :blob) "
                "ON CONFLICT (target_kind, target_id, model_id) DO UPDATE SET "
                "colbert = EXCLUDED.colbert, n_tokens = EXCLUDED.n_tokens, created_at = NOW()"
            ),
            {
                "kind": target_kind,
                "tid": tid,
                "model": MODEL_ID,
                "n": full["n_tokens"],
                "blob": full["colbert_blob"],
            },
        )
        await db.commit()

    return {
        "status": "embedded",
        "target_kind": target_kind,
        "target_id": target_id,
        "model_id": MODEL_ID,
        "signals": ["dense", "sparse", "colbert"],
        "n_tokens": full["n_tokens"],
    }
