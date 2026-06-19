"""Unit tests for the RECIST / volumetric response math (no DB).

Pins the clinical thresholds: PR is >=30% below baseline, PD is >=20% above
the nadir AND >=5 mm absolute, new lesions force PD, all-gone is CR.
"""

from __future__ import annotations

from datetime import date
from uuid import uuid4

from bvphoenix.services.response_assessment import (
    TargetMeasurement,
    classify_recist_1_1,
    summarize_response,
)


def test_partial_response_vs_baseline() -> None:
    # 100 -> 70 is exactly -30% -> PR.
    assert (
        classify_recist_1_1(
            current_sum=70.0, baseline_sum=100.0, nadir_sum=70.0, has_new_lesions=False
        )
        == "PR"
    )


def test_progressive_disease_vs_nadir_needs_both_pct_and_abs() -> None:
    # 50 -> 60: +20% AND +10mm -> PD.
    assert (
        classify_recist_1_1(
            current_sum=60.0, baseline_sum=100.0, nadir_sum=50.0, has_new_lesions=False
        )
        == "PD"
    )
    # 10 -> 12: +20% but only +2mm (<5mm) -> NOT PD.
    assert (
        classify_recist_1_1(
            current_sum=12.0, baseline_sum=12.0, nadir_sum=10.0, has_new_lesions=False
        )
        != "PD"
    )


def test_new_lesions_force_pd() -> None:
    assert (
        classify_recist_1_1(
            current_sum=50.0, baseline_sum=100.0, nadir_sum=50.0, has_new_lesions=True
        )
        == "PD"
    )


def test_complete_response_and_stable_and_ne() -> None:
    assert (
        classify_recist_1_1(
            current_sum=0.0, baseline_sum=100.0, nadir_sum=0.0, has_new_lesions=False
        )
        == "CR"
    )
    assert (
        classify_recist_1_1(
            current_sum=90.0, baseline_sum=100.0, nadir_sum=90.0, has_new_lesions=False
        )
        == "SD"
    )
    assert (
        classify_recist_1_1(
            current_sum=None, baseline_sum=100.0, nadir_sum=90.0, has_new_lesions=False
        )
        == "NE"
    )


def _m(
    track,
    study,
    study_date,
    diam,
    *,
    role="target",
    vol=None,
    short_axis=None,
    is_nodal=False,
    anatomy=None,
):
    return TargetMeasurement(
        track_id=track,
        label=f"L{track.int % 1000}",
        study_id=study,
        study_date=study_date,
        longest_diameter_mm=diam,
        volume_ml=vol,
        recist_role=role,
        short_axis_mm=short_axis,
        is_nodal=is_nodal,
        anatomy_key=anatomy,
    )


def test_summarize_two_target_lesions_partial_response() -> None:
    a, b = uuid4(), uuid4()
    base, cur = uuid4(), uuid4()
    rows = [
        _m(a, base, date(2026, 1, 1), 50.0),
        _m(b, base, date(2026, 1, 1), 30.0),
        _m(a, cur, date(2026, 4, 1), 35.0),
        _m(b, cur, date(2026, 4, 1), 20.0),
    ]
    out = summarize_response(rows, baseline_study_id=base, current_study_id=cur)
    assert out["baseline_sum_mm"] == 80.0
    assert out["target_sum_mm"] == 55.0
    assert out["nadir_sum_mm"] == 55.0
    assert abs(out["target_sum_pct_change"] - (-31.25)) < 1e-6
    assert out["category"] == "PR"
    assert out["basis"]["n_target_lesions"] == 2


def test_summarize_new_lesion_is_pd() -> None:
    a = uuid4()
    new = uuid4()
    base, cur = uuid4(), uuid4()
    rows = [
        _m(a, base, date(2026, 1, 1), 50.0),
        _m(a, cur, date(2026, 4, 1), 30.0),  # target shrank...
        _m(new, cur, date(2026, 4, 1), 8.0, role="new"),  # ...but a new lesion appeared
    ]
    out = summarize_response(rows, baseline_study_id=base, current_study_id=cur)
    assert out["new_lesions"] is True
    assert out["category"] == "PD"


def test_nodal_target_contributes_short_axis_not_longest() -> None:
    # A lymph-node target measured 30mm long / 18mm short at baseline,
    # 30/12 at follow-up. RECIST 1.1 sums the SHORT axis for nodes, so the
    # SoD goes 18 -> 12 (-33% -> PR), NOT 30 -> 30 (stable).
    n = uuid4()
    base, cur = uuid4(), uuid4()
    rows = [
        _m(n, base, date(2026, 1, 1), 30.0, short_axis=18.0, is_nodal=True),
        _m(n, cur, date(2026, 4, 1), 30.0, short_axis=12.0, is_nodal=True),
    ]
    out = summarize_response(rows, baseline_study_id=base, current_study_id=cur)
    assert out["baseline_sum_mm"] == 18.0
    assert out["target_sum_mm"] == 12.0
    assert out["category"] == "PR"
    lesion = out["basis"]["lesions"][0]
    assert lesion["is_nodal"] is True
    assert lesion["baseline_mm"] == 18.0
    assert lesion["current_mm"] == 12.0


def test_mixed_nodal_and_parenchymal_sum() -> None:
    # Parenchymal lesion uses long axis (40 -> 30); node uses short axis
    # (16 -> 14). SoD: 56 -> 44.
    lesion, node = uuid4(), uuid4()
    base, cur = uuid4(), uuid4()
    rows = [
        _m(lesion, base, date(2026, 1, 1), 40.0, short_axis=22.0, is_nodal=False),
        _m(node, base, date(2026, 1, 1), 20.0, short_axis=16.0, is_nodal=True),
        _m(lesion, cur, date(2026, 4, 1), 30.0, short_axis=18.0, is_nodal=False),
        _m(node, cur, date(2026, 4, 1), 18.0, short_axis=14.0, is_nodal=True),
    ]
    out = summarize_response(rows, baseline_study_id=base, current_study_id=cur)
    assert out["baseline_sum_mm"] == 56.0  # 40 + 16
    assert out["target_sum_mm"] == 44.0  # 30 + 14


def test_ne_reason_no_target_lesions() -> None:
    cur = uuid4()
    out = summarize_response([], baseline_study_id=None, current_study_id=cur)
    assert out["category"] == "NE"
    assert out["basis"]["ne_reason"] == "no_target_lesions"
    assert out["basis"]["n_target_lesions"] == 0
    assert out["basis"]["has_baseline"] is False
    assert out["basis"]["has_current"] is False


def test_ne_reason_current_missing() -> None:
    # A target measured only at baseline; the current study has no target.
    a = uuid4()
    base, cur = uuid4(), uuid4()
    rows = [_m(a, base, date(2026, 1, 1), 40.0)]
    out = summarize_response(rows, baseline_study_id=base, current_study_id=cur)
    assert out["category"] == "NE"
    assert out["basis"]["ne_reason"] == "current_missing"
    assert out["basis"]["has_current"] is False


def test_ne_reason_baseline_missing() -> None:
    # A target measured only at the current study; no baseline timepoint.
    a = uuid4()
    base, cur = uuid4(), uuid4()
    rows = [_m(a, cur, date(2026, 4, 1), 40.0)]
    out = summarize_response(rows, baseline_study_id=base, current_study_id=cur)
    assert out["category"] == "NE"
    assert out["basis"]["ne_reason"] == "baseline_missing"
    assert out["basis"]["has_baseline"] is False
    assert out["basis"]["has_current"] is True


def test_target_caps_over_limit() -> None:
    # 6 target lesions, 3 of them in the liver -> exceeds both the global
    # (<=5) and per-organ (<=2) RECIST 1.1 caps.
    base, cur = uuid4(), uuid4()
    rows = []
    for i in range(6):
        organ = "liver" if i < 3 else f"organ_{i}"
        tid = uuid4()
        rows.append(_m(tid, base, date(2026, 1, 1), 20.0, anatomy=organ))
        rows.append(_m(tid, cur, date(2026, 4, 1), 18.0, anatomy=organ))
    out = summarize_response(rows, baseline_study_id=base, current_study_id=cur)
    caps = out["basis"]["caps"]
    assert caps["n_targets"] == 6
    assert caps["over_limit"] is True
    assert caps["per_organ"]["liver"] == 3
    assert "liver" in caps["per_organ_over_limit"]
