"""Maximal Marginal Relevance (MMR) re-ranking + per-group diversity cap.

Nearest-neighbour image search floods the top-k with near-duplicates:
adjacent slices of the same series, or several studies that are visually
almost identical. MMR trades a little relevance for diversity by, at
each step, picking the candidate that maximises::

    score = lambda * relevance(c) - (1 - lambda) * max_sim(c, already_selected)

``relevance`` is the query similarity already computed by pgvector;
``sim`` between two candidates is the cosine of their (L2-normalised)
embedding vectors, i.e. a plain dot product. A hard ``max_per_group``
cap (e.g. <= 2 series per study) runs alongside so a single study cannot
dominate even if its slices are individually diverse.

Pure + synchronous: it operates on vectors already fetched from the DB,
so it is unit-tested without one and adds no query cost beyond pulling
the candidate vectors.
"""

from __future__ import annotations

from collections.abc import Hashable, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class MMRCandidate:
    """One ranked candidate for MMR.

    ``vector`` must be L2-normalised (pgvector cosine embeddings are), so
    candidate-to-candidate similarity is a dot product. ``relevance`` is
    in roughly ``[0, 1]`` (e.g. ``1 - cosine_distance``). ``group`` is the
    diversity bucket (the study id, for series candidates).
    """

    id: Hashable
    vector: Sequence[float]
    relevance: float
    group: Hashable


def _dot(a: Sequence[float], b: Sequence[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=False))


def mmr_rerank(
    candidates: Sequence[MMRCandidate],
    *,
    k: int,
    lambda_: float = 0.7,
    max_per_group: int | None = None,
) -> list[MMRCandidate]:
    """Return up to ``k`` candidates reordered by MMR with a group cap.

    ``lambda_`` in ``[0, 1]``: 1.0 is pure relevance (no diversification),
    0.0 is pure novelty. ``max_per_group`` bounds how many selected items
    may share a ``group`` (None = unbounded). Input order is assumed
    best-relevance-first and is used as the tie-breaker, so ``lambda_=1``
    with no cap reproduces the input ordering.
    """
    if k <= 0 or not candidates:
        return []
    if not 0.0 <= lambda_ <= 1.0:
        raise ValueError(f"lambda_ must be in [0, 1], got {lambda_}")

    remaining = list(candidates)
    selected: list[MMRCandidate] = []
    group_counts: dict[Hashable, int] = {}

    while remaining and len(selected) < k:
        best_idx = -1
        best_score = float("-inf")
        for i, cand in enumerate(remaining):
            if max_per_group is not None and group_counts.get(cand.group, 0) >= max_per_group:
                continue
            if selected:
                max_sim = max(_dot(cand.vector, s.vector) for s in selected)
            else:
                max_sim = 0.0
            score = lambda_ * cand.relevance - (1.0 - lambda_) * max_sim
            if score > best_score:
                best_score = score
                best_idx = i
        if best_idx == -1:
            # Every remaining candidate is capped out by its group.
            break
        chosen = remaining.pop(best_idx)
        selected.append(chosen)
        group_counts[chosen.group] = group_counts.get(chosen.group, 0) + 1

    return selected
