"""Information-retrieval metrics for the search evaluation harness.

Pure, dependency-free functions over ranked id lists + relevance
judgements. Keeping the metric math here (rather than inline in the
DB-backed harness) means it is unit-tested without a database and the
two never drift.

Conventions
-----------
* ``ranked``     — ids in result order, best first (rank 1 == ``ranked[0]``).
* ``relevant``   — iterable/set of ids judged relevant (binary metrics).
* ``grades``     — mapping ``{id: grade}`` with ``grade >= 0`` for graded
                   metrics (nDCG). Ids absent from the map count as 0.

All functions return a float in ``[0, 1]`` (nDCG, recall, precision are
ratios; MRR is the reciprocal of a 1-based rank).
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence

__all__ = [
    "dcg_at_k",
    "mrr",
    "ndcg_at_k",
    "percentile",
    "precision_at_k",
    "recall_at_k",
]


def percentile(values: Sequence[float], p: float) -> float:
    """Linear-interpolated ``p``-th percentile (0..100). 0.0 on empty.

    Used for the latency budget: a generous p95 ceiling catches a
    pathological regression (e.g. a dropped index forcing a seq scan)
    without flaking on CI jitter, since healthy FTS latency is orders of
    magnitude below the bound.
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    rank = (max(0.0, min(100.0, p)) / 100.0) * (len(ordered) - 1)
    lo = math.floor(rank)
    hi = math.ceil(rank)
    if lo == hi:
        return float(ordered[lo])
    frac = rank - lo
    return float(ordered[lo] * (1.0 - frac) + ordered[hi] * frac)


def recall_at_k[T](ranked: Sequence[T], relevant: Iterable[T], k: int) -> float:
    """Fraction of the relevant set retrieved within the top ``k``.

    Returns 0.0 when nothing is relevant (recall is undefined there, and
    a 0 keeps a query with no gold from inflating an average). Note that
    if ``len(relevant) > k`` even a perfect ranking caps below 1.0 — that
    is the standard recall@k definition, so size your golden queries with
    ``|relevant| <= k`` when you want to assert ``== 1.0``.
    """
    rel = set(relevant)
    if not rel:
        return 0.0
    hits = sum(1 for x in ranked[: max(0, k)] if x in rel)
    return hits / len(rel)


def precision_at_k[T](ranked: Sequence[T], relevant: Iterable[T], k: int) -> float:
    """Fraction of the top ``k`` that is relevant (divides by ``k``)."""
    if k <= 0:
        return 0.0
    rel = set(relevant)
    hits = sum(1 for x in ranked[:k] if x in rel)
    return hits / k


def mrr[T](ranked: Sequence[T], relevant: Iterable[T]) -> float:
    """Reciprocal rank of the first relevant hit (0.0 if none present)."""
    rel = set(relevant)
    for i, x in enumerate(ranked, start=1):
        if x in rel:
            return 1.0 / i
    return 0.0


def dcg_at_k[T](ranked: Sequence[T], grades: Mapping[T, float], k: int) -> float:
    """Discounted cumulative gain over the top ``k`` (log2 discount)."""
    total = 0.0
    for i, x in enumerate(ranked[: max(0, k)], start=1):
        g = float(grades.get(x, 0.0))
        if g:
            total += g / math.log2(i + 1)
    return total


def ndcg_at_k[T](ranked: Sequence[T], grades: Mapping[T, float], k: int) -> float:
    """Normalised DCG@k: ``dcg / ideal_dcg``, 0.0 when no positive grade."""
    dcg = dcg_at_k(ranked, grades, k)
    ideal = sorted((g for g in grades.values() if g > 0), reverse=True)[: max(0, k)]
    idcg = sum(g / math.log2(i + 1) for i, g in enumerate(ideal, start=1))
    if idcg == 0.0:
        return 0.0
    return dcg / idcg
