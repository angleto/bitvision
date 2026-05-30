"""Unit tests for the IR metric math (no DB, runs everywhere).

These guard the formulas the relevance harness asserts against, so a
metric bug can't silently turn a relevance regression green.
"""

from __future__ import annotations

import math

import pytest

from tests.eval.metrics import (
    dcg_at_k,
    mrr,
    ndcg_at_k,
    percentile,
    precision_at_k,
    recall_at_k,
)


def test_percentile() -> None:
    assert percentile([], 95) == 0.0
    assert percentile([5.0], 50) == pytest.approx(5.0)
    vals = [10.0, 20.0, 30.0, 40.0]
    assert percentile(vals, 0) == pytest.approx(10.0)
    assert percentile(vals, 100) == pytest.approx(40.0)
    assert percentile(vals, 50) == pytest.approx(25.0)  # interpolated midpoint


def test_recall_at_k_basic() -> None:
    ranked = ["a", "b", "c", "d"]
    assert recall_at_k(ranked, {"a", "c"}, 2) == pytest.approx(0.5)  # only a in top-2
    assert recall_at_k(ranked, {"a", "c"}, 4) == pytest.approx(1.0)
    assert recall_at_k(ranked, {"a", "c"}, 3) == pytest.approx(1.0)


def test_recall_at_k_more_relevant_than_k_caps_below_one() -> None:
    # |relevant| > k → perfect ranking still cannot reach 1.0 (standard).
    assert recall_at_k(["a", "b", "c"], {"a", "b", "c"}, 2) == pytest.approx(2 / 3)


def test_recall_at_k_no_relevant_is_zero() -> None:
    assert recall_at_k(["a", "b"], set(), 5) == 0.0


def test_precision_at_k() -> None:
    assert precision_at_k(["a", "b", "c"], {"a", "c"}, 2) == pytest.approx(0.5)
    assert precision_at_k(["a", "b", "c"], {"a", "c"}, 3) == pytest.approx(2 / 3)
    assert precision_at_k(["a"], {"a"}, 0) == 0.0


def test_mrr() -> None:
    assert mrr(["x", "a", "b"], {"a"}) == pytest.approx(0.5)  # first hit at rank 2
    assert mrr(["a", "x"], {"a"}) == pytest.approx(1.0)
    assert mrr(["x", "y"], {"a"}) == 0.0


def test_dcg_and_ndcg_perfect_order_is_one() -> None:
    grades = {"a": 3.0, "b": 2.0, "c": 1.0}
    expected_dcg = 3 / math.log2(2) + 2 / math.log2(3) + 1 / math.log2(4)
    assert dcg_at_k(["a", "b", "c"], grades, 3) == pytest.approx(expected_dcg)
    assert ndcg_at_k(["a", "b", "c"], grades, 3) == pytest.approx(1.0)


def test_ndcg_worst_order_below_one() -> None:
    grades = {"a": 3.0, "b": 2.0, "c": 1.0}
    score = ndcg_at_k(["c", "b", "a"], grades, 3)
    assert 0.0 < score < 1.0
    assert score == pytest.approx(0.7899, abs=1e-3)


def test_ndcg_no_positive_grades_is_zero() -> None:
    assert ndcg_at_k(["a", "b"], {"a": 0.0}, 3) == 0.0
    assert ndcg_at_k(["a", "b"], {}, 3) == 0.0
