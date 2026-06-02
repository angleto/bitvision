"""BGE-M3 query encoder for the backend search path.

Mirrors the worker's ``embed_bge_m3`` loader but query-side and
dense-only: encode a search query into the 1024-d BGE-M3 dense space so
it matches the vectors the worker writes into ``text_embeddings_bge_m3``.

FlagEmbedding is imported lazily so environments without the ``ai`` extra
(CI, lightweight tests) raise ImportError on first use and the caller
degrades to FTS-only rather than 500-ing. The model is pre-baked into the
backend image (HF_HOME) for the same reason as the workers: a runtime HF
download is slow/rate-limited and times out on CPU ARM.

Sparse + ColBERT query encoding (Phase 2/3) will live here too, behind
their own helpers, reusing the single loaded model.
"""

from __future__ import annotations

import asyncio
from typing import Any

BGE_M3_MODEL_ID = "bge-m3-v1"
BGE_M3_MODEL_NAME = "BAAI/bge-m3"
BGE_M3_DENSE_DIM = 1024

_model: Any | None = None


def _ensure_model() -> Any:
    """Load BGE-M3 on first use (module-global singleton)."""
    global _model
    if _model is not None:
        return _model
    from FlagEmbedding import BGEM3FlagModel

    # CPU-only (ARM) in prod -> fp32. Weights pre-baked into the image.
    _model = BGEM3FlagModel(BGE_M3_MODEL_NAME, use_fp16=False)
    return _model


def _embed_sync(query: str) -> list[float]:
    """Blocking dense forward pass; run in a worker thread from async."""
    model = _ensure_model()
    out = model.encode(
        [query],
        return_dense=True,
        return_sparse=False,
        return_colbert_vecs=False,
    )
    return out["dense_vecs"][0].tolist()


async def embed_query_dense(query: str) -> list[float]:
    """Encode ``query`` into the 1024-d BGE-M3 dense vector (cosine-ready)."""
    return await asyncio.to_thread(_embed_sync, query)
