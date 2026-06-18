"""Persist a propagated lesion measurement (DB-only, no S3 / SimpleITK).

The medical-write half of the semi-automatic follow-up pipeline, split out
from the worker so it is unit-testable against a real Postgres: given a
lesion track, its baseline finding, and the measurements computed on the
follow-up's real voxels, create the follow-up ``Finding`` (inheriting the
baseline's coded semantics, ``author_kind='system'``, status ``candidate``
so a human confirms it), link it to its segmentation mask, and add it to
the track as a new timepoint.

The compute half (register → warp → re-segment → measure) lives in the
worker (``workers/.../tasks/propagate_lesion.py``) and the pure
SimpleITK primitives (``registration_core`` / ``lesion_measure``).
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.db.models import (
    Finding,
    FindingGeometry,
    FindingRevision,
    LesionTrack,
    LesionTrackPoint,
)

# Finding measurement columns a propagation may set (kept in sync with the
# findings API ``_MEASUREMENT_FIELDS``).
_MEASUREMENT_FIELDS = (
    "longest_diameter_mm",
    "short_axis_mm",
    "volume_ml",
    "suv_max",
    "suv_peak",
    "suv_mean",
    "hu_mean",
    "hu_std",
)

PROPAGATION_MODEL_ID = "propagate_lesion-v1"


def _finding_snapshot(f: Finding) -> dict[str, Any]:
    return {
        "id": str(f.id),
        "study_id": str(f.study_id),
        "finding_type_id": str(f.finding_type_id),
        "anatomy_site_id": str(f.anatomy_site_id) if f.anatomy_site_id else None,
        "laterality": f.laterality,
        "morphology_keys": list(f.morphology_keys or []),
        "measurements": {k: getattr(f, k) for k in _MEASUREMENT_FIELDS},
        "status": f.status,
        "author_kind": f.author_kind,
        "etag": str(f.etag),
    }


async def persist_propagated_finding(
    db: AsyncSession,
    *,
    track: LesionTrack,
    baseline_finding: Finding,
    followup_study_id: uuid.UUID,
    followup_series_id: uuid.UUID,
    frame_of_reference_uid: str | None,
    measurements: dict[str, Any],
    segmentation_id: uuid.UUID | None,
    registration_id: uuid.UUID | None,
    timepoint_date: Any | None,
    subject_id: uuid.UUID | None,
    model_id: str = PROPAGATION_MODEL_ID,
) -> tuple[Finding, LesionTrackPoint]:
    """Create the follow-up Finding (inheriting the baseline's coded
    semantics + the freshly-measured scalars), its mask geometry link, a
    create revision, and the new lesion-track point. Caller commits.

    ``author_kind='system'`` and ``status='candidate'`` make it a tracked,
    reversible draft the human confirms — never an un-attributable write.
    The composite FKs on the point still guarantee same-patient by
    construction; we also assert it here for a clear error.
    """
    if baseline_finding.patient_id != track.patient_id:
        raise ValueError("baseline finding and track belong to different patients")

    finding = Finding(
        patient_id=track.patient_id,
        study_id=followup_study_id,
        series_id=followup_series_id,
        frame_of_reference_uid=frame_of_reference_uid,
        finding_type_id=baseline_finding.finding_type_id,
        anatomy_site_id=baseline_finding.anatomy_site_id,
        laterality=baseline_finding.laterality,
        morphology_keys=list(baseline_finding.morphology_keys or []),
        bbox_lps=measurements.get("bbox_lps"),
        status="candidate",
        confidence=measurements.get("confidence"),
        description=(
            f"Propagated from baseline finding {baseline_finding.id} "
            "via registration; re-measured on the follow-up. Confirm or edit."
        ),
        author_subject_id=subject_id,
        author_kind="system",
        model_id=model_id,
        etag=uuid.uuid4(),
        **{k: measurements.get(k) for k in _MEASUREMENT_FIELDS},
    )
    db.add(finding)
    await db.flush()

    db.add(
        FindingRevision(
            finding_id=finding.id,
            patient_id=finding.patient_id,
            revision_no=1,
            snapshot=_finding_snapshot(finding),
            change_kind="create",
            author_kind="system",
            actor_id=subject_id,
            diff_summary=f"propagated from {baseline_finding.id}",
        )
    )
    if segmentation_id is not None:
        db.add(
            FindingGeometry(
                finding_id=finding.id,
                segmentation_id=segmentation_id,
                role="mask",
            )
        )

    point = LesionTrackPoint(
        lesion_track_id=track.id,
        finding_id=finding.id,
        patient_id=track.patient_id,
        is_baseline=False,
        timepoint_date=timepoint_date,
        registration_id=registration_id,
        linked_by_kind="system",
    )
    db.add(point)
    await db.flush()
    return finding, point
