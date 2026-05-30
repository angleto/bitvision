"""Unit tests for MMR re-ranking (pure, no DB)."""

from __future__ import annotations

import pytest

from bvphoenix.services.mmr import MMRCandidate, mmr_rerank


def _c(id_, vec, rel, group):
    return MMRCandidate(id=id_, vector=vec, relevance=rel, group=group)


def test_empty_and_nonpositive_k() -> None:
    assert mmr_rerank([], k=5) == []
    assert mmr_rerank([_c("a", [1.0, 0.0], 0.9, "g")], k=0) == []


def test_lambda_one_preserves_relevance_order() -> None:
    cands = [
        _c("a", [1.0, 0.0], 0.9, "A"),
        _c("b", [0.0, 1.0], 0.8, "B"),
        _c("c", [0.0, 1.0], 0.7, "C"),
    ]
    out = [c.id for c in mmr_rerank(cands, k=3, lambda_=1.0)]
    assert out == ["a", "b", "c"]


def test_diversity_prefers_novel_over_near_duplicate() -> None:
    # c1 and c2 are identical vectors (near-duplicates); c3 is orthogonal
    # and slightly less relevant. MMR should pick c1 then jump to c3.
    cands = [
        _c("c1", [1.0, 0.0], 0.90, "A"),
        _c("c2", [1.0, 0.0], 0.85, "B"),
        _c("c3", [0.0, 1.0], 0.80, "C"),
    ]
    out = [c.id for c in mmr_rerank(cands, k=2, lambda_=0.5)]
    assert out == ["c1", "c3"]


def test_max_per_group_caps_a_dominant_group() -> None:
    cands = [
        _c("a", [1.0, 0.0], 0.9, "study1"),
        _c("b", [1.0, 0.0], 0.8, "study1"),
        _c("c", [1.0, 0.0], 0.7, "study1"),
    ]
    out = mmr_rerank(cands, k=5, lambda_=1.0, max_per_group=2)
    assert [c.id for c in out] == ["a", "b"]  # third dropped by the cap


def test_max_per_group_lets_other_groups_through() -> None:
    cands = [
        _c("a", [1.0, 0.0], 0.9, "A"),
        _c("b", [1.0, 0.0], 0.8, "A"),
        _c("c", [0.0, 1.0], 0.7, "B"),
    ]
    out = [c.id for c in mmr_rerank(cands, k=3, lambda_=1.0, max_per_group=1)]
    assert out == ["a", "c"]  # b capped (A full), c admitted (B fresh)


def test_invalid_lambda_raises() -> None:
    with pytest.raises(ValueError, match="lambda_"):
        mmr_rerank([_c("a", [1.0], 0.5, "g")], k=1, lambda_=1.5)
