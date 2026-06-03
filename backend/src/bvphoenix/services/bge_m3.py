"""BGE-M3 (BAAI/bge-m3) encode service for the backend + workers.

Single home for the BGE-M3 encoders so the loader + serialization are not
duplicated across the backend query path and the worker write path:

* ``embed_query_dense`` — the 1024-d DENSE vector via ``sentence-transformers``
  (lean, ARM-tested). Used as the always-available dense path + the degrade
  fallback when FlagEmbedding is unavailable.
* ``flag_encode_sync`` / ``embed_query_all`` — DENSE + SPARSE (lexical) +
  ColBERT (multi-vector) in ONE ``BGEM3FlagModel`` forward pass, via
  FlagEmbedding (installed --no-deps in the image, Phase 2/3). The worker
  uses ``flag_encode_sync`` to populate the three stores; the query path uses
  ``embed_query_all`` to encode the query for the sparse RRF arm + the ColBERT
  MaxSim rerank.

Everything is imported lazily so a lean env (CI without the ``ai`` extra, or
an image where FlagEmbedding failed to install) raises ImportError on first
use and the caller degrades (query -> dense/FTS only; worker -> ST dense only)
rather than 500-ing. The model is pre-baked into the images (HF_HOME); a
runtime HF download is slow and times out on CPU ARM.
"""

from __future__ import annotations

import asyncio
from typing import Any

from bvphoenix.db.models.text_embeddings_bge_m3 import BGE_M3_SPARSE_DIM
from bvphoenix.services.text_models import BGE_M3_MODEL_ID

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


# --- FlagEmbedding: BGE-M3 sparse (lexical) + ColBERT (multi-vector) ---------
# Shared loader + serialization for the worker write path (embed_bge_m3_all)
# and the query path (embed_query_all). One forward pass yields all three
# signals. Lazy + isolated behind ImportError: a build without FlagEmbedding
# degrades to dense-only, never 500s.

_flag_model: Any | None = None


def _ensure_flag_model() -> Any:
    """Load the FlagEmbedding ``BGEM3FlagModel`` once per process (singleton).

    Raises ImportError if FlagEmbedding is not installed so callers can degrade.
    """
    global _flag_model
    if _flag_model is not None:
        return _flag_model
    from FlagEmbedding import BGEM3FlagModel

    # use_fp16=False: prod nodes are CPU-only; fp16 matmul is slower/unsupported
    # on CPU. Weights load from the pre-baked HF cache, not the network.
    _flag_model = BGEM3FlagModel(BGE_M3_MODEL_NAME, use_fp16=False)
    return _flag_model


def _l2_normalize(arr: Any) -> Any:
    """Row-wise L2 normalize a 2-D ndarray (zero rows left as zero)."""
    import numpy as np

    a = np.asarray(arr, dtype="float32")
    if a.ndim == 1:
        n = float(np.linalg.norm(a))
        return a / n if n else a
    norms = np.linalg.norm(a, axis=1, keepdims=True)
    norms[norms == 0.0] = 1.0
    return a / norms


def sparse_to_text(lexical_weights: dict) -> str:
    """Serialize FlagEmbedding ``lexical_weights`` ({token_id(str) -> weight})
    to a pgvector ``sparsevec`` literal.

    ``pgvector.SparseVector`` maps the 0-based BGE vocab ids to pgvector's
    1-based indices; the SAME mapping runs on the query side, so the inner
    product over shared tokens lines up. Non-positive weights are dropped.
    """
    from pgvector import SparseVector

    elements = {int(tid): float(w) for tid, w in lexical_weights.items() if float(w) > 0.0}
    return SparseVector(elements, BGE_M3_SPARSE_DIM).to_text()


def colbert_to_blob(colbert_vecs: Any) -> tuple[bytes, int]:
    """Pack the ColBERT token matrix as L2-normalized fp16 bytes for storage.

    Returns ``(blob, n_tokens)``; reassemble with
    ``np.frombuffer(blob, np.float16).reshape(n_tokens, 1024)``. Normalizing
    here means stored + query token vectors are unit vectors, so MaxSim dot
    products are cosine similarities.
    """
    arr = _l2_normalize(colbert_vecs)
    return arr.astype("float16").tobytes(), int(arr.shape[0])


def _dense_list(dense_vec: Any) -> list[float]:
    """L2-normalize the FlagEmbedding dense vector to match vector_cosine_ops
    + the existing sentence-transformers dense store."""
    return [float(x) for x in _l2_normalize(dense_vec).tolist()]


def flag_encode_sync(text_value: str) -> dict:
    """Blocking BGE-M3 full encode (dense + sparse + colbert) in ONE forward.

    Returns ``{dense: list[float], sparse_text: str, colbert_blob: bytes,
    n_tokens: int}`` ready for the three stores. Raises ImportError when
    FlagEmbedding is unavailable so the worker can degrade to ST dense-only.
    """
    model = _ensure_flag_model()
    out = model.encode(
        [text_value],
        return_dense=True,
        return_sparse=True,
        return_colbert_vecs=True,
    )
    blob, n_tokens = colbert_to_blob(out["colbert_vecs"][0])
    return {
        "dense": _dense_list(out["dense_vecs"][0]),
        "sparse_text": sparse_to_text(out["lexical_weights"][0]),
        "colbert_blob": blob,
        "n_tokens": n_tokens,
    }


async def embed_query_all(query: str) -> dict:
    """Encode ``query`` into BGE-M3 dense + sparse + ColBERT in one forward.

    Returns ``{dense: list[float], sparse_text: str, colbert: np.ndarray}``
    where ``colbert`` is the L2-normalized query token matrix kept in memory
    (not packed) for the MaxSim rerank. Raises ImportError when FlagEmbedding
    is unavailable so the query path can degrade to the dense + FTS arms.
    """

    def _run() -> dict:
        model = _ensure_flag_model()
        out = model.encode(
            [query],
            return_dense=True,
            return_sparse=True,
            return_colbert_vecs=True,
        )
        return {
            "dense": _dense_list(out["dense_vecs"][0]),
            "sparse_text": sparse_to_text(out["lexical_weights"][0]),
            "colbert": _l2_normalize(out["colbert_vecs"][0]),
        }

    return await asyncio.to_thread(_run)
