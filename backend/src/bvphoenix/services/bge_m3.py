"""BGE-M3 (BAAI/bge-m3) DENSE query encoder for the backend search path.

Mirrors the worker's ``embed_bge_m3`` loader: encode a search query into
the 1024-d BGE-M3 dense space so it matches the vectors the worker writes
into ``text_embeddings_bge_m3``. Uses ``sentence-transformers`` (already a
backend dependency) rather than FlagEmbedding for Phase 1 — see the
worker module for the rationale (FlagEmbedding drags ir-datasets + a C
extension; sparse/ColBERT in Phase 2/3 will introduce it).

Imported lazily so environments without the ``ai`` extra (CI, light
tests) raise ImportError on first use and the caller degrades to
FTS-only rather than 500-ing. The model is pre-baked into the backend
image (HF_HOME); a runtime HF download is slow and times out on CPU ARM.
"""

from __future__ import annotations

import asyncio
from typing import Any

BGE_M3_MODEL_ID = "bge-m3-v1"
BGE_M3_MODEL_NAME = "BAAI/bge-m3"
BGE_M3_DENSE_DIM = 1024

_model: Any | None = None


def _ensure_model() -> Any:
    """Load BGE-M3 (sentence-transformers) on first use (module singleton)."""
    global _model
    if _model is not None:
        return _model
    from sentence_transformers import SentenceTransformer

    # Weights pre-baked into the image; loads from local HF cache.
    _model = SentenceTransformer(BGE_M3_MODEL_NAME)
    return _model


def _embed_sync(query: str) -> list[float]:
    """Blocking dense forward pass; run in a worker thread from async."""
    import numpy as np

    model = _ensure_model()
    arr = model.encode([query], normalize_embeddings=True, show_progress_bar=False)
    return np.asarray(arr[0], dtype=float).tolist()


async def embed_query_dense(query: str) -> list[float]:
    """Encode ``query`` into the 1024-d BGE-M3 dense vector (cosine-ready)."""
    return await asyncio.to_thread(_embed_sync, query)
