"""Unit tests for the lesion-track trajectory math (no DB).

The arithmetic that answers "has the tumour grown?" — volume / diameter
deltas, doubling time, direction — exercised directly with clean numbers
and the longitudinal edge cases. The synthetic-phantom regression
(``test_lesion_phantom.py``, Phase 5) reuses ``compute_trajectory`` over a
sphere scaled by a known factor.
"""

from __future__ import annotations

from datetime import date
from uuid import uuid4

from bvphoenix.services.lesion_tracks import TrackTimepoint, compute_trajectory


def _tp(
    vol: float | None,
    diam: float | None,
    d: date | None,
    *,
    baseline: bool = False,
    suv: float | None = None,
) -> TrackTimepoint:
    return TrackTimepoint(
        point_id=uuid4(),
        finding_id=uuid4(),
        measured_on=d,
        is_baseline=baseline,
        volume_ml=vol,
        longest_diameter_mm=diam,
        suv_max=suv,
    )


def test_growth_doubling_and_pct() -> None:
    # Volume 4 → 8 ml over exactly 90 days: +100%, doubling time = span.
    b = _tp(4.0, 20.0, date(2026, 1, 1), baseline=True)
    f = _tp(8.0, 25.0, date(2026, 4, 1))
    out = compute_trajectory([f, b])  # unordered input → sorted by date

    assert out["baseline"]["finding_id"] == str(b.finding_id)
    assert out["latest"]["finding_id"] == str(f.finding_id)
    s = out["summary"]
    assert s["n_timepoints"] == 2
    assert s["span_days"] == 90.0
    assert abs(s["volume_pct_change_total"] - 100.0) < 1e-9
    assert abs(s["diameter_pct_change_total"] - 25.0) < 1e-9
    assert abs(s["doubling_time_days"] - 90.0) < 1e-6
    assert s["overall_direction"] == "increase"

    follow = out["timepoints"][-1]
    assert abs(follow["delta_from_baseline"]["volume_pct"] - 100.0) < 1e-9
    assert abs(follow["delta_from_baseline"]["volume_ml_abs"] - 4.0) < 1e-9
    assert abs(follow["delta_from_previous"]["doubling_time_days"] - 90.0) < 1e-6
    assert follow["direction"] == "increase"


def test_single_timepoint_has_no_deltas() -> None:
    b = _tp(4.0, 20.0, date(2026, 1, 1), baseline=True)
    out = compute_trajectory([b])
    assert out["summary"]["n_timepoints"] == 1
    assert out["summary"]["volume_pct_change_total"] is None
    assert out["baseline"]["finding_id"] == out["latest"]["finding_id"]
    assert out["timepoints"][0]["delta_from_baseline"] is None
    assert out["timepoints"][0]["delta_from_previous"] is None


def test_empty_trajectory() -> None:
    out = compute_trajectory([])
    assert out["timepoints"] == []
    assert out["summary"] is None
    assert out["baseline"] is None and out["latest"] is None


def test_shrink_is_decrease() -> None:
    b = _tp(8.0, 25.0, date(2026, 1, 1), baseline=True)
    f = _tp(4.0, 20.0, date(2026, 4, 1))
    out = compute_trajectory([b, f])
    assert out["summary"]["overall_direction"] == "decrease"
    assert out["summary"]["volume_pct_change_total"] < 0


def test_small_change_is_stable() -> None:
    # +5% volume is below the noise tolerance → "stable", not "increase".
    b = _tp(10.0, 20.0, date(2026, 1, 1), baseline=True)
    f = _tp(10.5, 20.0, date(2026, 4, 1))
    out = compute_trajectory([b, f])
    assert out["summary"]["overall_direction"] == "stable"


def test_orders_by_acquisition_date() -> None:
    p1 = _tp(4.0, 20.0, date(2026, 1, 1), baseline=True)
    p2 = _tp(6.0, 22.0, date(2026, 2, 1))
    p3 = _tp(8.0, 25.0, date(2026, 3, 1))
    out = compute_trajectory([p3, p1, p2])
    assert [t["measured_on"] for t in out["timepoints"]] == [
        "2026-01-01",
        "2026-02-01",
        "2026-03-01",
    ]


def test_no_doubling_time_when_volume_missing() -> None:
    b = _tp(None, 20.0, date(2026, 1, 1), baseline=True)
    f = _tp(None, 30.0, date(2026, 4, 1))
    out = compute_trajectory([b, f])
    assert out["summary"]["doubling_time_days"] is None
    assert out["summary"]["volume_pct_change_total"] is None
    # diameter still drives direction (+50%).
    assert out["summary"]["overall_direction"] == "increase"
    assert abs(out["summary"]["diameter_pct_change_total"] - 50.0) < 1e-9
