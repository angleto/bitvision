"""Training cohort export API (P5 of the annotation overhaul).

``POST /api/training-exports/manifest`` selects a cohort by a structured
Finding query and returns the de-identified, coded **labels manifest** for
it — the training-ready metadata the platform has accrued, gated by
training consent + tier + k-anonymity (see ``services.training_cohort``).
Admin / platform-owner only: building a training pool is a privileged
operation distinct from reading a study.

The synchronous manifest endpoint is the testable, immediately-useful
core. Streaming the actual image + mask byte bundle into a ZIP behind a
Job (reusing ``services.patient_export``'s stream-zip machinery) is the
remaining P5 slice.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.auth import require_user
from bvphoenix.db.models import User
from bvphoenix.db.session import get_db
from bvphoenix.services import k_anonymity, training_cohort

router = APIRouter(tags=["training-exports"])


class TrainingCohortQueryIn(BaseModel):
    """Structured Finding query selecting the training cohort (same axes
    as ``/findings/search``)."""

    type: str | None = Field(default=None, max_length=64)
    anatomy: str | None = Field(default=None, max_length=64)
    laterality: Literal["left", "right", "bilateral", "midline"] | None = None
    morphology: list[str] | None = None
    status: Literal["candidate", "confirmed", "retracted"] | None = None
    min_diameter_mm: float | None = Field(default=None, ge=0)
    max_diameter_mm: float | None = Field(default=None, ge=0)
    min_volume_ml: float | None = Field(default=None, ge=0)
    min_suv_max: float | None = Field(default=None, ge=0)
    scope: Literal["all", "mine", "public"] = "all"
    k_min: int = Field(default=k_anonymity.DEFAULT_K_MIN, ge=1, le=1000)


@router.post("/training-exports/manifest")
async def training_cohort_manifest(
    body: TrainingCohortQueryIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
) -> dict[str, Any]:
    """Build the de-identified labels manifest for the consented,
    k-anon-checked cohort matching the query. 422 when a quasi-identifier
    bucket falls below ``k_min`` (the offending bucket is named)."""
    if not getattr(user, "is_admin", False):
        raise HTTPException(status_code=403, detail="training cohort export requires admin")
    try:
        rows, kanon = await training_cohort.select_cohort(
            db,
            user,
            type=body.type,
            anatomy=body.anatomy,
            laterality=body.laterality,
            morphology=body.morphology,
            status_filter=body.status,
            min_diameter_mm=body.min_diameter_mm,
            max_diameter_mm=body.max_diameter_mm,
            min_volume_ml=body.min_volume_ml,
            min_suv_max=body.min_suv_max,
            scope=body.scope,
            k_min=body.k_min,
        )
    except k_anonymity.KAnonymityError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "k_anonymity_not_met",
                "title": "Cohort fails k-anonymity",
                "worst_bucket": list(exc.worst_bucket),
                "worst_size": exc.worst_size,
                "k_min": exc.k_min,
                "hint": "Broaden the query or raise the cohort until every "
                "(modality, body_part) bucket has at least k_min studies.",
            },
        ) from exc

    return training_cohort.build_labels_manifest(
        rows,
        dataset_id=str(uuid.uuid4()),
        generated_at=datetime.now(UTC).isoformat(),
        kanon=kanon,
    )
