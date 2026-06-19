"""Accept / reject side-effects for public-contribution submissions."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.db.models import ImagingStudy, Submission
from bvphoenix.services.review_queue.actor import ReviewActor


async def promote_submission(
    db: AsyncSession, *, item: Submission, actor: ReviewActor
) -> dict[str, Any]:
    """Publish the approved study to the OpenData tier.

    Flips the source study to the target contribution tier (T4 -> public). Runs
    inside the engine's ``promoting`` transition, so a raise aborts the publish
    and the item stays ``accepted`` for the maintenance sweep to retry. The
    burned-in-pixel egress gate (M0) + per-download de-id still apply to what is
    actually served, so this flip never bypasses pixel screening.
    """
    refs: dict[str, Any] = {"target_tier": item.target_tier}
    if item.source_study_id is not None:
        study = await db.get(ImagingStudy, item.source_study_id)
        if study is not None:
            study.contribution_tier = item.target_tier
            study.is_public = item.target_tier == "t4"
            refs["study_id"] = str(study.id)
            refs["is_public"] = study.is_public
    item.promoted_refs = refs
    return refs


async def purge_submission_staged(item: Submission) -> None:
    """Delete staged (redacted-preview) blobs on reject.

    Staged redacted previews are produced by the automated pixel-redaction tier
    (M4); until that lands there is nothing to purge, so this is a no-op when
    ``staged_prefix`` is unset. (M4 wires the S3 prefix deletion here.)
    """
    if not item.staged_prefix:
        return


__all__ = ["promote_submission", "purge_submission_staged"]
