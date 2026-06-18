"""The medical-write half of propagation, against a real Postgres.

``persist_propagated_finding`` creates the follow-up Finding (inheriting
the baseline's coded type, ``author_kind='system'``, status ``candidate``),
links its mask, writes a create revision, and adds the track timepoint —
all same-patient by construction.
"""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import select

from bvphoenix.db.models import (
    Finding,
    FindingRevision,
    FindingType,
    LesionTrack,
    LesionTrackPoint,
    Patient,
)
from bvphoenix.services.lesion_propagation import persist_propagated_finding
from tests.conftest import skip_if_no_db

pytestmark = skip_if_no_db


async def _ftype_id(db) -> uuid.UUID:
    return (
        await db.execute(select(FindingType.id).where(FindingType.key == "nodule"))
    ).scalar_one()


async def test_persist_propagated_finding(db_session, make_user, make_study) -> None:
    owner = await make_user()
    base_study, base_series = await make_study(owner, study_date=date(2026, 1, 1))
    patient_id = base_study.patient_id
    patient = (
        await db_session.execute(select(Patient).where(Patient.id == patient_id))
    ).scalar_one()
    follow_study, follow_series = await make_study(
        owner, patient=patient, study_date=date(2026, 4, 1)
    )
    ftype = await _ftype_id(db_session)

    baseline = Finding(
        patient_id=patient_id,
        study_id=base_study.id,
        series_id=base_series.id,
        finding_type_id=ftype,
        laterality="right",
        morphology_keys=["spiculated"],
        volume_ml=4.0,
        longest_diameter_mm=20.0,
        author_kind="human",
        etag=uuid.uuid4(),
    )
    db_session.add(baseline)
    track = LesionTrack(
        patient_id=patient_id,
        label="Nodulo LSD",
        finding_type_id=ftype,
        author_kind="human",
        etag=uuid.uuid4(),
    )
    db_session.add(track)
    await db_session.flush()

    try:
        finding, point = await persist_propagated_finding(
            db_session,
            track=track,
            baseline_finding=baseline,
            followup_study_id=follow_study.id,
            followup_series_id=follow_series.id,
            frame_of_reference_uid="1.2.3.follow",
            measurements={
                "volume_ml": 8.0,
                "longest_diameter_mm": 25.0,
                "bbox_lps": {"min": [0.0, 0.0, 0.0], "max": [2.0, 2.0, 2.0]},
                "hu_mean": -50.0,
            },
            segmentation_id=None,
            registration_id=None,
            timepoint_date=date(2026, 4, 1),
            subject_id=owner.subject_id,
        )
        await db_session.commit()

        # Follow-up finding inherits semantics, carries the new measurement,
        # and is an attributable, reversible draft.
        assert finding.author_kind == "system"
        assert finding.status == "candidate"
        assert finding.finding_type_id == ftype
        assert finding.laterality == "right"
        assert list(finding.morphology_keys) == ["spiculated"]
        assert finding.volume_ml == 8.0
        assert finding.study_id == follow_study.id
        assert finding.series_id == follow_series.id
        assert finding.frame_of_reference_uid == "1.2.3.follow"

        rev = (
            await db_session.execute(
                select(FindingRevision).where(FindingRevision.finding_id == finding.id)
            )
        ).scalar_one()
        assert rev.revision_no == 1 and rev.change_kind == "create"
        assert rev.author_kind == "system"

        assert point.lesion_track_id == track.id
        assert point.finding_id == finding.id
        assert point.linked_by_kind == "system"
        assert point.is_baseline is False
        assert point.timepoint_date == date(2026, 4, 1)

        # The point is queryable on the track.
        pts = (
            (
                await db_session.execute(
                    select(LesionTrackPoint).where(LesionTrackPoint.lesion_track_id == track.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(pts) == 1
    finally:
        await db_session.rollback()
        # Clean committed rows (rollback can't undo the service commit).
        from sqlalchemy import text

        await db_session.execute(
            text("DELETE FROM lesion_tracks WHERE patient_id = :p"), {"p": str(patient_id)}
        )
        await db_session.execute(
            text("DELETE FROM findings WHERE patient_id = :p"), {"p": str(patient_id)}
        )
        await db_session.commit()
