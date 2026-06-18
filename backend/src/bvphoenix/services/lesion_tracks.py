"""Lesion-track trajectory math — the "has the tumour grown?" answer.

Pure, DB-free functions over an ordered list of timepoints. The API
(``api/lesion_tracks.py``) builds ``TrackTimepoint`` inputs from the
track's points + their findings; the patient-level RECIST aggregation
(``services/response_assessment.py``, Phase 4) reuses the same primitives.

Keeping the arithmetic here (not in the route) makes it unit-testable
against the synthetic phantom: a sphere scaled by a known factor must
yield exactly that volume delta and doubling time.

Conventions:

* ``volume_ml`` is the primary growth metric (the user asked to compare
  *volumes*); ``longest_diameter_mm`` is the unidimensional RECIST proxy.
* percentage change is ``(new - ref) / ref * 100`` and is ``None`` when
  the reference is missing or zero.
* doubling time uses exponential-growth assumption
  ``Td = dt * ln(2) / ln(V1 / V0)`` (days); ``None`` unless both volumes
  are positive, differ, and ``dt > 0``.
* ``direction`` is a *descriptive* label (increase / decrease / stable)
  with a noise tolerance — NOT a RECIST response category (that is the
  job of the ResponseAssessment layer).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from typing import Any
from uuid import UUID

# Below this absolute percentage change we call a step "stable" rather
# than increase/decrease, to avoid labelling measurement noise as growth.
DIRECTION_TOLERANCE_PCT = 10.0


@dataclass(frozen=True)
class TrackTimepoint:
    """One measured timepoint on a track (a point + its finding)."""

    point_id: UUID
    finding_id: UUID
    measured_on: date | None
    is_baseline: bool
    volume_ml: float | None = None
    longest_diameter_mm: float | None = None
    short_axis_mm: float | None = None
    suv_max: float | None = None


def _pct(ref: float | None, new: float | None) -> float | None:
    if ref is None or new is None or ref == 0:
        return None
    return (new - ref) / ref * 100.0


def _doubling_days(v0: float | None, v1: float | None, dt_days: float | None) -> float | None:
    if v0 is None or v1 is None or dt_days is None:
        return None
    if v0 <= 0 or v1 <= 0 or dt_days <= 0 or v1 == v0:
        return None
    return dt_days * math.log(2.0) / math.log(v1 / v0)


def _span_days(a: date | None, b: date | None) -> float | None:
    if a is None or b is None:
        return None
    return float((b - a).days)


def _direction(volume_pct: float | None, diameter_pct: float | None) -> str:
    pct = volume_pct if volume_pct is not None else diameter_pct
    if pct is None:
        return "unknown"
    if pct > DIRECTION_TOLERANCE_PCT:
        return "increase"
    if pct < -DIRECTION_TOLERANCE_PCT:
        return "decrease"
    return "stable"


def _sort_timepoints(points: list[TrackTimepoint]) -> list[TrackTimepoint]:
    """Order by acquisition date, undated points last; stable so the
    caller's secondary ordering (e.g. created_at) is preserved on ties."""
    dated = [p for p in points if p.measured_on is not None]
    undated = [p for p in points if p.measured_on is None]
    dated.sort(key=lambda p: p.measured_on)  # type: ignore[arg-type,return-value]
    return dated + undated


def _measure_dict(p: TrackTimepoint) -> dict[str, Any]:
    return {
        "point_id": str(p.point_id),
        "finding_id": str(p.finding_id),
        "measured_on": p.measured_on.isoformat() if p.measured_on else None,
        "is_baseline": p.is_baseline,
        "volume_ml": p.volume_ml,
        "longest_diameter_mm": p.longest_diameter_mm,
        "short_axis_mm": p.short_axis_mm,
        "suv_max": p.suv_max,
    }


def compute_trajectory(points: list[TrackTimepoint]) -> dict[str, Any]:
    """Compute the longitudinal trajectory of a lesion: per-timepoint
    deltas vs the baseline and vs the previous timepoint, plus a summary.

    The baseline is the point flagged ``is_baseline`` if present, else the
    earliest timepoint. Returns a JSON-serialisable dict."""
    ordered = _sort_timepoints(points)
    if not ordered:
        return {"baseline": None, "latest": None, "timepoints": [], "summary": None}

    baseline = next((p for p in ordered if p.is_baseline), ordered[0])
    latest = ordered[-1]

    timepoints: list[dict[str, Any]] = []
    prev: TrackTimepoint | None = None
    for p in ordered:
        from_baseline = None
        if p is not baseline:
            from_baseline = {
                "volume_ml_abs": (
                    None
                    if p.volume_ml is None or baseline.volume_ml is None
                    else p.volume_ml - baseline.volume_ml
                ),
                "volume_pct": _pct(baseline.volume_ml, p.volume_ml),
                "diameter_mm_abs": (
                    None
                    if p.longest_diameter_mm is None or baseline.longest_diameter_mm is None
                    else p.longest_diameter_mm - baseline.longest_diameter_mm
                ),
                "diameter_pct": _pct(baseline.longest_diameter_mm, p.longest_diameter_mm),
                "suv_max_abs": (
                    None
                    if p.suv_max is None or baseline.suv_max is None
                    else p.suv_max - baseline.suv_max
                ),
            }
        from_previous = None
        if prev is not None:
            dt = _span_days(prev.measured_on, p.measured_on)
            from_previous = {
                "volume_pct": _pct(prev.volume_ml, p.volume_ml),
                "diameter_pct": _pct(prev.longest_diameter_mm, p.longest_diameter_mm),
                "doubling_time_days": _doubling_days(prev.volume_ml, p.volume_ml, dt),
                "interval_days": dt,
            }
        direction = _direction(
            from_previous["volume_pct"] if from_previous else None,
            from_previous["diameter_pct"] if from_previous else None,
        )
        timepoints.append(
            {
                **_measure_dict(p),
                "delta_from_baseline": from_baseline,
                "delta_from_previous": from_previous,
                "direction": direction,
            }
        )
        prev = p

    # With a single timepoint there is no change to report (baseline IS
    # latest); the totals are None rather than a spurious 0%.
    multi = latest is not baseline
    span = _span_days(baseline.measured_on, latest.measured_on)
    vol_total = _pct(baseline.volume_ml, latest.volume_ml) if multi else None
    diam_total = _pct(baseline.longest_diameter_mm, latest.longest_diameter_mm) if multi else None
    summary = {
        "n_timepoints": len(ordered),
        "span_days": span,
        "volume_pct_change_total": vol_total,
        "diameter_pct_change_total": diam_total,
        "doubling_time_days": (
            _doubling_days(baseline.volume_ml, latest.volume_ml, span) if multi else None
        ),
        "overall_direction": _direction(vol_total, diam_total) if multi else "unknown",
    }
    return {
        "baseline": _measure_dict(baseline),
        "latest": _measure_dict(latest),
        "timepoints": timepoints,
        "summary": summary,
    }
