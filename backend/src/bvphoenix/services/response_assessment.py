"""RECIST / volumetric response math + the patient-level aggregation.

The classifiers are pure and unit-tested; ``compute_response_assessment``
gathers the patient's target lesions from the lesion tracks and feeds them
in. The API persists the returned dict as a ``ResponseAssessment`` record.

RECIST 1.1 (single-timepoint overall response over TARGET lesions):

* new lesions (or unequivocal non-target progression) → PD;
* all target lesions disappeared → CR;
* sum ≥ 20% above the NADIR *and* ≥ 5 mm absolute increase → PD;
* sum ≥ 30% below BASELINE → PR;
* otherwise → SD.

(% decrease is measured against baseline, % increase against the nadir —
the smallest sum recorded so far — which is the RECIST 1.1 rule.)

The ``volumetric`` variant uses total volume with thresholds that are NOT
a standardised criterion (labelled research): default +25% (nadir) → PD,
-65% (baseline) → PR.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any
from uuid import UUID

VOLUMETRIC_PD_PCT = 0.25
VOLUMETRIC_PR_PCT = -0.65


@dataclass(frozen=True)
class TargetMeasurement:
    """One target/new lesion measured at one study (timepoint)."""

    track_id: UUID
    label: str
    study_id: UUID
    study_date: date | None
    longest_diameter_mm: float | None
    volume_ml: float | None
    recist_role: str  # 'target' | 'new' | ...
    short_axis_mm: float | None = None
    is_nodal: bool = False  # lymph-node target → contributes short axis to the SoD
    anatomy_key: str | None = None  # organ key, for the per-organ target cap


def _recist_diameter(m: TargetMeasurement) -> float | None:
    """The diameter a target contributes to the RECIST 1.1 sum-of-diameters:
    short axis for nodal targets, longest diameter otherwise (Eisenhauer et
    al., EJC 2009)."""
    return m.short_axis_mm if m.is_nodal else m.longest_diameter_mm


def _pct(ref: float | None, new: float | None) -> float | None:
    if ref is None or new is None or ref == 0:
        return None
    return (new - ref) / ref * 100.0


def classify_recist_1_1(
    *,
    current_sum: float | None,
    baseline_sum: float | None,
    nadir_sum: float | None,
    has_new_lesions: bool,
    non_target_pd: bool = False,
) -> str:
    if current_sum is None or baseline_sum is None:
        return "NE"
    if has_new_lesions or non_target_pd:
        return "PD"
    if current_sum == 0:
        return "CR"
    if (
        nadir_sum is not None
        and nadir_sum > 0
        and (current_sum - nadir_sum) / nadir_sum >= 0.20
        and (current_sum - nadir_sum) >= 5.0
    ):
        return "PD"
    if baseline_sum > 0 and (current_sum - baseline_sum) / baseline_sum <= -0.30:
        return "PR"
    return "SD"


def classify_volumetric(
    *,
    current_vol: float | None,
    baseline_vol: float | None,
    nadir_vol: float | None,
    has_new_lesions: bool,
    pd_pct: float = VOLUMETRIC_PD_PCT,
    pr_pct: float = VOLUMETRIC_PR_PCT,
) -> str:
    if current_vol is None or baseline_vol is None:
        return "NE"
    if has_new_lesions:
        return "PD"
    if current_vol == 0:
        return "CR"
    if nadir_vol is not None and nadir_vol > 0 and (current_vol - nadir_vol) / nadir_vol >= pd_pct:
        return "PD"
    if baseline_vol > 0 and (current_vol - baseline_vol) / baseline_vol <= pr_pct:
        return "PR"
    return "SD"


def _study_order_key(d: date | None) -> tuple[int, date]:
    return (1, date.min) if d is None else (0, d)


def summarize_response(
    measurements: list[TargetMeasurement],
    *,
    baseline_study_id: UUID | None,
    current_study_id: UUID,
    criterion: str = "recist_1_1",
) -> dict[str, Any]:
    """Aggregate per-study sums and classify. ``measurements`` may include
    ``recist_role='new'`` lesions (used only for new-lesion detection)."""
    targets = [m for m in measurements if m.recist_role == "target"]

    # Per-study sums over target lesions.
    sums: dict[UUID, dict[str, float]] = {}
    dates: dict[UUID, date | None] = {}
    for m in targets:
        s = sums.setdefault(m.study_id, {"diam": 0.0, "vol": 0.0})
        diam = _recist_diameter(m)
        if diam is not None:
            s["diam"] += diam
        if m.volume_ml is not None:
            s["vol"] += m.volume_ml
        dates[m.study_id] = m.study_date

    current = sums.get(current_study_id)
    current_sum = current["diam"] if current else None
    current_vol = current["vol"] if current else None
    baseline = sums.get(baseline_study_id) if baseline_study_id else None
    baseline_sum = baseline["diam"] if baseline else None
    baseline_vol = baseline["vol"] if baseline else None

    # Nadir = smallest sum among studies up to (and including) the current
    # timepoint (by date; undated studies always count).
    cur_date = dates.get(current_study_id)
    eligible = []
    for sid, ssum in sums.items():
        sdate = dates.get(sid)
        if cur_date is None or sdate is None or sdate <= cur_date:
            eligible.append(ssum)
    nadir_sum = min((e["diam"] for e in eligible), default=None)
    nadir_vol = min((e["vol"] for e in eligible), default=None)

    # New lesions: a 'new'-role lesion present at the current study.
    has_new = any(m.recist_role == "new" and m.study_id == current_study_id for m in measurements)

    if criterion == "volumetric":
        category = classify_volumetric(
            current_vol=current_vol,
            baseline_vol=baseline_vol,
            nadir_vol=nadir_vol,
            has_new_lesions=has_new,
        )
    else:
        category = classify_recist_1_1(
            current_sum=current_sum,
            baseline_sum=baseline_sum,
            nadir_sum=nadir_sum,
            has_new_lesions=has_new,
        )

    # Per-track basis (baseline vs current diameter — short axis for nodal
    # targets, longest diameter otherwise, matching the summed SoD above).
    by_track: dict[UUID, dict[str, Any]] = {}
    for m in targets:
        t = by_track.setdefault(
            m.track_id,
            {
                "label": m.label,
                "baseline_mm": None,
                "current_mm": None,
                "is_nodal": m.is_nodal,
                "anatomy": m.anatomy_key,
            },
        )
        if m.study_id == baseline_study_id:
            t["baseline_mm"] = _recist_diameter(m)
        if m.study_id == current_study_id:
            t["current_mm"] = _recist_diameter(m)

    # Why NE, so the UI can explain it and point at the missing input
    # instead of rendering a bare dash.
    ne_reason: str | None = None
    if category == "NE":
        if len(by_track) == 0:
            ne_reason = "no_target_lesions"
        elif current_sum is None:
            ne_reason = "current_missing"
        elif baseline_sum is None:
            ne_reason = "baseline_missing"
        else:
            ne_reason = "unknown"

    # RECIST 1.1 reading caps: <=5 target lesions total, <=2 per organ.
    # Surfaced as a soft warning (radiologists sometimes exceed it
    # deliberately), counted over distinct tracks not measurements.
    track_anatomy: dict[UUID, str | None] = {}
    for m in targets:
        track_anatomy.setdefault(m.track_id, m.anatomy_key)
    per_organ: dict[str, int] = {}
    for akey in track_anatomy.values():
        if akey:
            per_organ[akey] = per_organ.get(akey, 0) + 1

    basis = {
        "criterion": criterion,
        "n_target_lesions": len(by_track),
        "ne_reason": ne_reason,
        "has_baseline": baseline_sum is not None,
        "has_current": current_sum is not None,
        "caps": {
            "n_targets": len(by_track),
            "max_targets": 5,
            "over_limit": len(by_track) > 5,
            "per_organ": per_organ,
            "per_organ_over_limit": [k for k, c in per_organ.items() if c > 2],
        },
        "lesions": [
            {
                "track_id": str(tid),
                "label": v["label"],
                "baseline_mm": v["baseline_mm"],
                "current_mm": v["current_mm"],
                "is_nodal": v["is_nodal"],
                "anatomy": v["anatomy"],
                "delta_mm": (
                    None
                    if v["baseline_mm"] is None or v["current_mm"] is None
                    else v["current_mm"] - v["baseline_mm"]
                ),
            }
            for tid, v in by_track.items()
        ],
    }

    return {
        "criterion": criterion,
        "category": category,
        "target_sum_mm": current_sum,
        "baseline_sum_mm": baseline_sum,
        "nadir_sum_mm": nadir_sum,
        "target_sum_pct_change": _pct(baseline_sum, current_sum),
        "volume_total_ml": current_vol,
        "volume_pct_change": _pct(baseline_vol, current_vol),
        "new_lesions": has_new,
        "basis": basis,
    }


async def compute_response_assessment(
    db: Any,
    *,
    patient_id: UUID,
    current_study_id: UUID,
    baseline_study_id: UUID | None = None,
    criterion: str = "recist_1_1",
) -> dict[str, Any]:
    """Gather the patient's target/new lesions from the lesion tracks and
    summarise the response at ``current_study_id``. Patient-scoped (never
    reads another patient's tracks). Returns a dict ready to persist."""
    from sqlalchemy import select

    from bvphoenix.db.models import (
        AnatomySite,
        Finding,
        FindingType,
        ImagingStudy,
        LesionTrack,
        LesionTrackPoint,
    )

    rows = (
        await db.execute(
            select(
                LesionTrack.id,
                LesionTrack.label,
                LesionTrack.recist_role,
                Finding.study_id,
                ImagingStudy.study_date,
                Finding.longest_diameter_mm,
                Finding.short_axis_mm,
                Finding.volume_ml,
                FindingType.key,
                AnatomySite.key,
            )
            .join(LesionTrackPoint, LesionTrackPoint.lesion_track_id == LesionTrack.id)
            .join(Finding, Finding.id == LesionTrackPoint.finding_id)
            .join(ImagingStudy, ImagingStudy.id == Finding.study_id)
            .join(FindingType, FindingType.id == Finding.finding_type_id)
            .outerjoin(AnatomySite, AnatomySite.id == LesionTrack.anatomy_site_id)
            .where(
                LesionTrack.patient_id == patient_id,
                LesionTrack.deleted_at.is_(None),
                LesionTrack.recist_role.in_(("target", "new")),
                Finding.deleted_at.is_(None),
            )
        )
    ).all()

    measurements = [
        TargetMeasurement(
            track_id=r[0],
            label=r[1],
            recist_role=r[2],
            study_id=r[3],
            study_date=r[4],
            longest_diameter_mm=r[5],
            short_axis_mm=r[6],
            volume_ml=r[7],
            is_nodal=(r[8] == "lymph_node"),
            anatomy_key=r[9],
        )
        for r in rows
    ]

    # Infer baseline as the earliest target timepoint when not given.
    if baseline_study_id is None:
        target_studies = {
            (m.study_id, m.study_date) for m in measurements if m.recist_role == "target"
        }
        if target_studies:
            baseline_study_id = min(target_studies, key=lambda s: _study_order_key(s[1]))[0]

    summary = summarize_response(
        measurements,
        baseline_study_id=baseline_study_id,
        current_study_id=current_study_id,
        criterion=criterion,
    )
    cur_date = next((m.study_date for m in measurements if m.study_id == current_study_id), None)
    summary["baseline_study_id"] = baseline_study_id
    summary["current_study_id"] = current_study_id
    summary["assessment_date"] = cur_date
    return summary
