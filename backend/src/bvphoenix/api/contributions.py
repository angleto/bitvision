"""Public-contribution review API — the OpenData publish quarantine.

A study owner *offers* a study (``POST /contributions``); it is staged as a
``Submission`` and screened by the ``public_contribution`` review profile
(header de-id, burned-in-pixel risk, malware, CSAM). A reviewer then accepts
(publish) or rejects it. Publishing PHI-bearing imaging to the public web is
irreversible, so the decision gate is **human-only** and admin-gated; the engine
refuses an agent actor by construction.

Importing :mod:`bvphoenix.services.public_contribution.profile` here IS the
profile registration for the API process. Mutations require ``If-Match`` (the
etag the engine bumps on every transition). Storage isolation: the manifest's
S3 bucket/key never cross the response boundary; the redacted preview is
streamed through the backend.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime
from typing import Annotated

from arq import create_pool
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.auth.deps import require_user
from bvphoenix.config import get_settings
from bvphoenix.db.models import SUBMISSION_TARGET_TIERS, ImagingStudy, Submission, User
from bvphoenix.db.session import get_db
from bvphoenix.services.arq_redis import redis_settings
from bvphoenix.services.deidentify import deidentify_dicom_bytes
from bvphoenix.services.etag import enforce_if_match_value
from bvphoenix.services.public_contribution.profile import PROFILE_NAME, PUBLIC_CONTRIBUTION_PROFILE
from bvphoenix.services.public_contribution.staging import create_submission
from bvphoenix.services.review_queue import ReviewDecisionError, ReviewTransitionError
from bvphoenix.services.review_queue import engine as review_engine
from bvphoenix.services.review_queue.actor import ReviewActor
from bvphoenix.storage import get_s3_storage

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/contributions", tags=["contributions"])

_LISTABLE = ("received", "processing", "needs_review", "blocked", "promoted", "rejected", "failed")


def _require_admin(user: User) -> None:
    # Reviewing public contributions is platform-level (publishing to the public
    # library), gated on admin today — see permissions.REVIEW_PUBLIC_CONTRIBUTION.
    if not getattr(user, "is_admin", False):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="public-contribution review is restricted to administrators",
        )


def _map_decision_errors(exc: Exception) -> HTTPException:
    if isinstance(exc, ReviewDecisionError):
        code = status.HTTP_422_UNPROCESSABLE_ENTITY
        if getattr(exc, "code", "") in ("decision.human_only", "decision.not_authorized"):
            code = status.HTTP_403_FORBIDDEN
        return HTTPException(
            status_code=code, detail={"code": getattr(exc, "code", "decision"), "message": str(exc)}
        )
    if isinstance(exc, ReviewTransitionError):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "review.invalid_transition", "from": exc.current, "to": exc.requested},
        )
    raise exc


# ---- schemas ---------------------------------------------------------------


class CreateSubmissionIn(BaseModel):
    study_id: uuid.UUID
    target_tier: str = Field(description="t3 (anonymised training pool) or t4 (public CC)")


class RejectIn(BaseModel):
    reason: str = Field(min_length=1)


class AcceptIn(BaseModel):
    reason: str = Field(min_length=1, description="Why this is safe to publish (audited)")


class SubmissionOut(BaseModel):
    id: uuid.UUID
    status: str
    auto_verdict: str | None
    auto_checks: dict | None  # per-check verdicts/details — no S3 keys (storage isolation)
    target_tier: str
    source_study_id: uuid.UUID | None
    contributor_subject_id: uuid.UUID | None
    instance_count: int
    # Per-instance ids + name + pixel risk for the review UI. S3 bucket/key are
    # deliberately NOT included (storage isolation) — preview goes through the
    # backend by instance_id.
    instances: list[dict]
    created_at: datetime | None
    reviewed_at: datetime | None
    review_note: str | None
    etag: uuid.UUID

    @classmethod
    def from_row(cls, sub: Submission) -> SubmissionOut:
        manifest = sub.manifest or {}
        raw_instances = manifest.get("instances", [])
        return cls(
            id=sub.id,
            status=sub.status,
            auto_verdict=sub.auto_verdict,
            auto_checks=sub.auto_checks,
            target_tier=sub.target_tier,
            source_study_id=sub.source_study_id,
            contributor_subject_id=sub.contributor_subject_id,
            instance_count=len(raw_instances),
            instances=[
                {
                    "instance_id": i.get("instance_id"),
                    "name": i.get("name"),
                    "pixel_phi_risk": i.get("pixel_phi_risk"),
                }
                for i in raw_instances
            ],
            created_at=getattr(sub, "created_at", None),
            reviewed_at=sub.reviewed_at,
            review_note=sub.review_note,
            etag=sub.etag,
        )


class DecisionOut(BaseModel):
    submission: SubmissionOut
    dry_run: bool = False


async def _load(db: AsyncSession, submission_id: uuid.UUID) -> Submission:
    sub = (
        await db.execute(select(Submission).where(Submission.id == submission_id))
    ).scalar_one_or_none()
    if sub is None:
        raise HTTPException(status_code=404, detail="submission not found")
    return sub


# ---- endpoints -------------------------------------------------------------


@router.post("", response_model=SubmissionOut, status_code=201)
async def offer_submission(
    body: CreateSubmissionIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
) -> SubmissionOut:
    """Offer a study to the OpenData library. The owner (or an admin) submits;
    the auto-check pass runs in the worker before any human review."""
    if body.target_tier not in SUBMISSION_TARGET_TIERS:
        raise HTTPException(
            status_code=422, detail=f"target_tier must be one of {SUBMISSION_TARGET_TIERS}"
        )
    study = await db.get(ImagingStudy, body.study_id)
    if study is None:
        raise HTTPException(status_code=404, detail="study not found")
    if not (getattr(user, "is_admin", False) or study.owner_subject_id == user.subject_id):
        raise HTTPException(
            status_code=403, detail="only the study owner can offer it for contribution"
        )

    sub = await create_submission(
        db,
        study_id=body.study_id,
        target_tier=body.target_tier,
        contributor_subject_id=user.subject_id,
    )
    await db.commit()

    settings = get_settings()
    try:
        redis = await create_pool(redis_settings(settings.redis_url))
        try:
            await redis.enqueue_job(
                "run_review_checks",
                PROFILE_NAME,
                str(sub.id),
                _job_id=f"contrib-checks:{sub.id}:{sub.etag}",
            )
        finally:
            await redis.close()
    except Exception:  # pragma: no cover - the maintenance sweep recovers a lost enqueue
        logger.exception("failed to enqueue run_review_checks for submission %s", sub.id)

    return SubmissionOut.from_row(sub)


@router.get("/queue", response_model=list[SubmissionOut])
async def list_queue(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    sub_status: Annotated[str | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[SubmissionOut]:
    _require_admin(user)
    stmt = select(Submission).order_by(Submission.created_at.desc()).limit(limit).offset(offset)
    if sub_status is not None:
        if sub_status not in _LISTABLE:
            raise HTTPException(status_code=422, detail=f"status must be one of {_LISTABLE}")
        stmt = stmt.where(Submission.status == sub_status)
    rows = (await db.execute(stmt)).scalars().all()
    return [SubmissionOut.from_row(s) for s in rows]


@router.get("/{submission_id}", response_model=SubmissionOut)
async def get_submission(
    submission_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
) -> SubmissionOut:
    _require_admin(user)
    return SubmissionOut.from_row(await _load(db, submission_id))


@router.get("/{submission_id}/instances/{instance_id}/preview")
async def preview_instance(
    submission_id: uuid.UUID,
    instance_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
) -> Response:
    """Stream the header-de-identified DICOM of one staged instance so the
    reviewer can inspect it. Storage-isolated: the bucket/key never leave the
    backend. (Pixel-redacted preview lands with the M4 redaction tier.)"""
    _require_admin(user)
    sub = await _load(db, submission_id)
    entry = next(
        (
            i
            for i in (sub.manifest or {}).get("instances", [])
            if str(i.get("instance_id")) == instance_id
        ),
        None,
    )
    if entry is None or not entry.get("s3_key"):
        raise HTTPException(status_code=404, detail="instance not found in submission")
    storage = get_s3_storage()
    raw = await asyncio.to_thread(
        storage.get_object_bytes, bucket=entry["s3_bucket"], key=entry["s3_key"]
    )
    scrubbed = await asyncio.to_thread(deidentify_dicom_bytes, raw)
    return Response(
        content=scrubbed,
        media_type="application/dicom",
        headers={"x-deidentified": "true", "cache-control": "no-store"},
    )


@router.post("/{submission_id}/accept", response_model=DecisionOut, status_code=202)
async def accept_submission(
    submission_id: uuid.UUID,
    body: AcceptIn,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> DecisionOut:
    """Approve publication. Human-only (the engine refuses agent actors); the
    promotion (publish to the OpenData tier) runs in the worker."""
    _require_admin(user)
    sub = await _load(db, submission_id)
    enforce_if_match_value(if_match, str(sub.etag))
    actor = ReviewActor.from_request(user, request)
    try:
        await review_engine.decide(
            db,
            PUBLIC_CONTRIBUTION_PROFILE,
            sub,
            decision="accepted",
            actor=actor,
            reason=body.reason,
        )
    except (ReviewDecisionError, ReviewTransitionError) as exc:
        await db.rollback()
        raise _map_decision_errors(exc) from exc
    await db.commit()

    settings = get_settings()
    try:
        redis = await create_pool(redis_settings(settings.redis_url))
        try:
            await redis.enqueue_job(
                "promote_submission", str(sub.id), _job_id=f"contrib-promote:{sub.id}:{sub.etag}"
            )
        finally:
            await redis.close()
    except Exception:  # pragma: no cover
        logger.exception("failed to enqueue promote_submission for %s", sub.id)

    return DecisionOut(submission=SubmissionOut.from_row(sub))


@router.post("/{submission_id}/reject", response_model=DecisionOut)
async def reject_submission(
    submission_id: uuid.UUID,
    body: RejectIn,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> DecisionOut:
    _require_admin(user)
    sub = await _load(db, submission_id)
    enforce_if_match_value(if_match, str(sub.etag))
    actor = ReviewActor.from_request(user, request)
    try:
        await review_engine.decide(
            db,
            PUBLIC_CONTRIBUTION_PROFILE,
            sub,
            decision="rejected",
            actor=actor,
            reason=body.reason,
        )
    except (ReviewDecisionError, ReviewTransitionError) as exc:
        await db.rollback()
        raise _map_decision_errors(exc) from exc
    await db.commit()
    return DecisionOut(submission=SubmissionOut.from_row(sub))


__all__ = ["router"]
