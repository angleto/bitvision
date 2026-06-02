"""BGE-M3 (BAAI/bge-m3, FlagEmbedding) text embeddings.

BGE-M3 exposes three retrieval signals from a single forward pass:
1024-d **dense**, lexical **sparse** (token-id -> weight), and **ColBERT**
multi-vector (per-token). Phase 1 persists only the dense vector (into
``text_embeddings_bge_m3``, ``model_id='bge-m3-v1'``); the sparse +
ColBERT outputs are wired in Phase 2/3 — which is why the loader can
already return all three on request.

The model is lazy-loaded as a module-global singleton so jobs on the same
worker reuse it. It is PRE-BAKED into the worker image at build time:
downloading from the HF Hub at runtime is slow/rate-limited and times out
on CPU ARM (proven painful with BiomedCLIP), so the constructor must load
from the local HF cache.

Requires the ``ai`` extra: ``uv sync --extra ai`` (adds FlagEmbedding).
"""

from __future__ import annotations

import asyncio
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from bvworkers.config import get_settings

MODEL_NAME = "BAAI/bge-m3"
MODEL_ID = "bge-m3-v1"
EMBEDDING_DIM = 1024

ALLOWED_TARGET_KINDS: frozenset[str] = frozenset(
    {
        "series",
        "report",
        "annotation",
        "consultation",
        "document",
        "patient",
        "document_chunk",
    }
)

_model = None  # lazy: loaded once per worker on first call


def _ensure_model():
    """Load the BGE-M3 model on first use (module-global singleton)."""
    global _model
    if _model is not None:
        return _model

    from FlagEmbedding import BGEM3FlagModel

    # ``use_fp16`` only helps on CUDA; the prod workers are CPU-only (ARM),
    # so keep fp32 for correctness. Weights are pre-baked into the image
    # (HF_HOME) so this constructor loads from local cache, not the network.
    _model = BGEM3FlagModel(MODEL_NAME, use_fp16=False)
    return _model


def compute_bge_m3(
    text_value: str,
    *,
    dense: bool = True,
    sparse: bool = False,
    colbert: bool = False,
) -> dict:
    """Encode one string with BGE-M3; return only the requested signals.

    * ``dense``  -> ``result['dense']``: 1024-d list[float] (the model
      L2-normalizes, matching the ``vector_cosine_ops`` HNSW index).
    * ``sparse`` -> ``result['sparse']``: {token_id: weight} (Phase 2).
    * ``colbert``-> ``result['colbert']``: list[list[float]] per-token
      vectors (Phase 3).

    Unrequested signals are not computed, to save CPU on the dense-only
    Phase-1 path.
    """
    model = _ensure_model()
    out = model.encode(
        [text_value],
        return_dense=dense,
        return_sparse=sparse,
        return_colbert_vecs=colbert,
    )
    result: dict = {}
    if dense:
        result["dense"] = out["dense_vecs"][0].tolist()
    if sparse:
        # lexical_weights[0]: {token_id (str) -> weight (float)}
        result["sparse"] = {str(k): float(v) for k, v in out["lexical_weights"][0].items()}
    if colbert:
        result["colbert"] = [v.tolist() for v in out["colbert_vecs"][0]]
    return result


def _compute_dense(text_value: str) -> list[float]:
    return compute_bge_m3(text_value, dense=True)["dense"]


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

    settings = get_settings()
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)

    tid = uuid.UUID(target_id)

    # CPU-bound encode → offload to a thread so we don't block the loop.
    vector = await asyncio.to_thread(_compute_dense, text_value)

    # pgvector literal format: "[v1,v2,...,vN]"
    vec_str = "[" + ",".join(str(v) for v in vector) + "]"

    async with AsyncSession(engine) as db:
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

    await engine.dispose()
    return {
        "status": "embedded",
        "target_kind": target_kind,
        "target_id": target_id,
        "model_id": MODEL_ID,
        "dim": len(vector),
    }
