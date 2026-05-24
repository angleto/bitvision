"""Reciprocal Rank Fusion (RRF).

Combines several ranked result lists into a single score using the
classic Cormack / Clarke / Buettcher 2009 formula:

    score(d) = Σ_i  weight_i * 1 / (k + rank_i(d))

where ``rank_i(d)`` is the 1-based position of document ``d`` in the
i-th ranked list, or ∞ (contribution 0) if ``d`` doesn't appear there.
``k`` is a damping constant — 60 is the value used in the original
paper and gives good behaviour when the per-signal scores are on
wildly different scales, which is exactly our case (ts_rank,
cosine-similarity, integer tag-match count).

The fuse function is pure and synchronous so callers can test it
without a database.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable


def rrf_fuse(
    ranked_lists: Iterable[tuple[list[uuid.UUID], float]],
    k: int = 60,
) -> dict[uuid.UUID, float]:
    """Fuse multiple ranked id lists into a single id→score map.

    Parameters
    ----------
    ranked_lists
        Iterable of ``(ids, weight)`` pairs. ``ids`` is ordered best
        first; ``weight`` is the caller-supplied importance for this
        signal (negative weights are legal but unusual).
    k
        RRF damping constant. 60 is the literature default.

    Returns
    -------
    dict[UUID, float]
        Map from id to combined score. Items absent from every list
        are absent from the result.
    """
    if k <= 0:
        # k=0 would divide by zero for rank=0, which isn't possible
        # here (rank is 1-based) but guard against pathological callers.
        raise ValueError("k must be > 0")

    scores: dict[uuid.UUID, float] = {}
    for ids, weight in ranked_lists:
        for rank, doc_id in enumerate(ids, start=1):
            scores[doc_id] = scores.get(doc_id, 0.0) + weight * (1.0 / (k + rank))
    return scores


def rrf_signal_contribution(rank: int | None, weight: float, k: int = 60) -> float:
    """Per-signal contribution for a single document.

    Returns 0 if ``rank`` is None (document not in that signal's list).
    Used by callers that want to expose a breakdown alongside the
    fused score.
    """
    if rank is None:
        return 0.0
    return weight * (1.0 / (k + rank))
