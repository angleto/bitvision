"""CPU cross-encoder re-ranking for the RAG / agent retrieval path.

RRF fuses the sparse + dense lists by rank — robust, but coarse: it never
reads the passage text. A small cross-encoder scores each (query,
passage) pair with true semantic relevance and re-orders the top
candidates. It is scoped to the chunk-search / agent path (``rerank``
flag), NOT the interactive study grid: ~30 pairs cost roughly 0.5-1.2s on
ARM CPU, which is invisible behind an LLM turn but too slow for a human
typing in a grid.

Lazy-loaded and ai-gated. If ``sentence-transformers`` is not installed
(no ``ai`` extra) the reranker degrades to a no-op — :func:`rerank_order`
returns ``None`` and the caller keeps the RRF order — so retrieval still
works everywhere.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Small, CPU-friendly cross-encoder. Multilingual quality is modest; the
# model id is the one knob the eval harness pins when tuning.
_MODEL_NAME = "cross-encoder/ms-marco-MiniLM-L-6-v2"

_model: Any | None = None
_load_failed = False


def _ensure_model() -> Any | None:
    """Load the cross-encoder once. Returns None if the ai extra is absent
    (cached so a missing dependency is not retried on every call)."""
    global _model, _load_failed
    if _model is not None:
        return _model
    if _load_failed:
        return None
    try:
        from sentence_transformers import CrossEncoder  # type: ignore[import-not-found]
    except (ImportError, ModuleNotFoundError):
        _load_failed = True
        logger.info("reranker: sentence-transformers not installed, cross-encoder disabled")
        return None
    try:
        _model = CrossEncoder(_MODEL_NAME)
    except Exception as exc:  # pragma: no cover — model download / init failure
        _load_failed = True
        logger.warning("reranker: failed to load %s: %s", _MODEL_NAME, exc)
        return None
    return _model


def _score_sync(query: str, passages: list[str]) -> list[float] | None:
    model = _ensure_model()
    if model is None:
        return None
    scores = model.predict([(query, p) for p in passages])
    return [float(s) for s in scores]


async def rerank_order(
    query: str, passages: list[str], *, top_n: int | None = None
) -> list[int] | None:
    """Return passage *indices* reordered best-first by the cross-encoder.

    Returns ``None`` when the cross-encoder is unavailable so the caller
    can keep its existing order. The blocking forward pass runs in a
    thread so the event loop stays responsive.
    """
    if not passages:
        return []
    scores = await asyncio.to_thread(_score_sync, query, passages)
    if scores is None:
        return None
    order = sorted(range(len(passages)), key=lambda i: scores[i], reverse=True)
    return order[:top_n] if top_n is not None else order
