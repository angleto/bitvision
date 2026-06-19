"""Stage a study into a public-contribution Submission.

Builds the per-instance manifest (the bucket/key pointers the review checks read
to screen each DICOM) and creates the ``submissions`` row in ``received``. The
caller (API) enqueues ``run_review_checks`` to drive the auto-check pass.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.db.models import ImagingStudy, Instance, Series, Submission


async def create_submission(
    db: AsyncSession,
    *,
    study_id: uuid.UUID,
    target_tier: str,
    contributor_subject_id: uuid.UUID | None,
) -> Submission:
    """Create a Submission staging ``study_id`` for the given target tier.

    Raises ``ValueError`` if the study is unknown. The manifest lists every
    instance with its raw bucket/key so the profile's ``load_staged`` can fetch
    and screen the bytes without re-resolving the study.
    """
    study = await db.get(ImagingStudy, study_id)
    if study is None:
        raise ValueError("study not found")

    rows = (
        await db.execute(
            select(
                Instance.id,
                Series.id,
                Instance.s3_bucket,
                Instance.s3_key,
                Instance.sop_instance_uid,
                Instance.pixel_phi_risk,
            )
            .join(Series, Series.id == Instance.series_id)
            .where(Series.study_id == study_id)
        )
    ).all()
    instances = [
        {
            "instance_id": str(iid),
            "series_id": str(sid),
            "s3_bucket": bucket,
            "s3_key": key,
            "name": f"{sop}.dcm",
            "study_id": str(study_id),
            "pixel_phi_risk": risk,
        }
        for (iid, sid, bucket, key, sop, risk) in rows
    ]

    submission = Submission(
        source_study_id=study_id,
        source_patient_id=study.patient_id,
        contributor_subject_id=contributor_subject_id,
        target_tier=target_tier,
        manifest={"instances": instances},
    )
    db.add(submission)
    await db.flush()
    return submission


__all__ = ["create_submission"]
