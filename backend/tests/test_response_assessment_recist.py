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


def _m(track, study, study_date, diam, *, role="target", vol=None):
    return TargetMeasurement(
        track_id=track,
        label=f"L{track.int % 1000}",
        study_id=study,
        study_date=study_date,
        longest_diameter_mm=diam,
        volume_ml=vol,
        recist_role=role,
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
