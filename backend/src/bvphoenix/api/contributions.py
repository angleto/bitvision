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
from bvphoenix.services.pixel_deid import PixelDeidResult, clean_pixel_data, render_instance_png
from bvphoenix.services.pixel_deid_eval import GtBox, RedactionScore, score_redaction
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


# ---- ground-truth PHI box labeling (M6c) -----------------------------------
# The reviewer draws the burned-in-PHI boxes on a staged instance; these are the
# answer key the automatic pixel redaction's recall is scored against. Shape is
# the GtBox of services.pixel_deid_eval (intrinsic pixel XYWH, top-left origin).

# Presidio-compatible PHI categories the synthesizer/answer-key use, plus the
# escape hatches the corpus loader already tolerates.
PHI_CATEGORIES: frozenset[str] = frozenset(
    {
        "name",
        "codice_fiscale",
        "date",
        "address",
        "phone",
        "email",
        "mrn",
        "other",
        "unknown",
    }
)


class GtBoxIn(BaseModel):
    x: int = Field(ge=0)
    y: int = Field(ge=0)
    w: int = Field(ge=1)
    h: int = Field(ge=1)
    text: str = ""
    category: str = "unknown"


class SaveGtBoxesIn(BaseModel):
    boxes: list[GtBoxIn]


class GtBoxesOut(BaseModel):
    instance_id: str
    boxes: list[GtBoxIn]
    etag: uuid.UUID


class GtScoreOut(BaseModel):
    instance_id: str
    recall: float
    covered: int
    total: int
    missed: list[str]
    risk_level: str


class DetectedBoxesOut(BaseModel):
    instance_id: str
    width: int
    height: int
    risk_level: str
    residual_suspect: bool
    boxes: list[dict]  # [{x, y, w, h, text, conf}] — the auto-redaction masks


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
    """Stream the de-identified DICOM of one staged instance so the reviewer can
    inspect it: header scrub + burned-in-pixel redaction (M4). Storage-isolated
    (bucket/key never leave the backend). The reviewer sees what publishing would
    expose, then accepts or rejects."""
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

    def _deidentify_for_preview(blob: bytes) -> bytes:
        # Header scrub first; on an SR/verify exception fall back to the raw
        # bytes (the reviewer is an authorised admin) so the burned-in-pixel
        # redaction can still run and show the masked image.
        try:
            scrubbed = deidentify_dicom_bytes(blob)
        except Exception:
            scrubbed = blob
        return clean_pixel_data(scrubbed).out_bytes

    served = await asyncio.to_thread(_deidentify_for_preview, raw)
    return Response(
        content=served,
        media_type="application/dicom",
        headers={
            "x-deidentified": "true",
            "x-pixel-redacted": "true",
            "cache-control": "no-store",
        },
    )


# ---- GT box labeling (M6c): render + detected boxes + GT store + score ------


def _instance_entry(sub: Submission, instance_id: str) -> dict:
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
    return entry


async def _fetch_instance_raw(entry: dict) -> bytes:
    storage = get_s3_storage()
    return await asyncio.to_thread(
        storage.get_object_bytes, bucket=entry["s3_bucket"], key=entry["s3_key"]
    )


@router.get("/{submission_id}/instances/{instance_id}/render.png")
async def render_instance(
    submission_id: uuid.UUID,
    instance_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    variant: Annotated[str, Query(pattern="^(original|redacted)$")] = "original",
    frame: Annotated[int, Query(ge=0)] = 0,
) -> Response:
    """Render one frame of a staged instance to a PNG at native pixel resolution
    so the reviewer can draw ground-truth PHI boxes over it.

    ``variant=original`` shows the burned-in PHI (the labeling surface — this
    exposes pixel PHI to the authorised reviewer, admin-gated + no-store; the
    raster carries NO header PHI). ``variant=redacted`` shows the automatic
    redaction so the reviewer sees what publishing would expose. Coordinates are
    intrinsic pixels, aligned 1:1 with the GT / detected boxes."""
    _require_admin(user)
    sub = await _load(db, submission_id)
    entry = _instance_entry(sub, instance_id)
    raw = await _fetch_instance_raw(entry)

    def _render() -> tuple[bytes, int, int]:
        blob = raw
        if variant == "redacted":
            try:
                blob = deidentify_dicom_bytes(raw)
            except Exception:
                blob = raw
            blob = clean_pixel_data(blob).out_bytes
        return render_instance_png(blob, frame=frame)

    try:
        png, width, height = await asyncio.to_thread(_render)
    except Exception as exc:  # decode/render failure — not a labelable instance
        raise HTTPException(status_code=422, detail=f"cannot render instance: {exc}") from exc
    return Response(
        content=png,
        media_type="image/png",
        headers={
            "x-image-width": str(width),
            "x-image-height": str(height),
            "x-variant": variant,
            "cache-control": "no-store",
        },
    )


@router.get("/{submission_id}/instances/{instance_id}/detected-boxes", response_model=DetectedBoxesOut)
async def detected_boxes(
    submission_id: uuid.UUID,
    instance_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
) -> DetectedBoxesOut:
    """The boxes the automatic pixel-redaction pipeline masked (OCR/regex/VLM),
    so the labeling UI can prefill + the reviewer can see what was auto-caught vs
    what they must still mark. Coordinates are intrinsic pixels."""
    _require_admin(user)
    sub = await _load(db, submission_id)
    entry = _instance_entry(sub, instance_id)
    raw = await _fetch_instance_raw(entry)

    def _detect() -> tuple[int, int, PixelDeidResult]:
        try:
            scrubbed = deidentify_dicom_bytes(raw)
        except Exception:
            scrubbed = raw
        _png, w, h = render_instance_png(raw)
        return w, h, clean_pixel_data(scrubbed)

    try:
        width, height, result = await asyncio.to_thread(_detect)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"cannot analyse instance: {exc}") from exc
    return DetectedBoxesOut(
        instance_id=instance_id,
        width=width,
        height=height,
        risk_level=result.risk.level,
        residual_suspect=result.residual_suspect,
        boxes=list(result.redactions),
    )


def _clamp_boxes(boxes: list[GtBoxIn], width: int, height: int) -> list[dict]:
    """Clip each GT box to the image bounds and drop degenerate ones. Keeps the
    stored GT always valid + in-bounds so score_redaction never sees garbage."""
    out: list[dict] = []
    for b in boxes:
        x = min(b.x, width - 1)
        y = min(b.y, height - 1)
        w = min(b.w, width - x)
        h = min(b.h, height - y)
        if w < 1 or h < 1:
            continue
        cat = b.category if b.category in PHI_CATEGORIES else "unknown"
        out.append({"x": x, "y": y, "w": w, "h": h, "text": b.text, "category": cat})
    return out


@router.put("/{submission_id}/instances/{instance_id}/gt-boxes", response_model=GtBoxesOut)
async def save_gt_boxes(
    submission_id: uuid.UUID,
    instance_id: str,
    body: SaveGtBoxesIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> GtBoxesOut:
    """Persist the reviewer's ground-truth PHI boxes for one instance (the answer
    key). If-Match on the submission etag (optimistic concurrency); the write
    bumps the etag so a concurrent editor / a pending accept re-reads first.
    Boxes are clipped to the image bounds and validated against the GtBox schema.
    """
    _require_admin(user)
    sub = await _load(db, submission_id)
    enforce_if_match_value(if_match, str(sub.etag))
    entry = _instance_entry(sub, instance_id)
    raw = await _fetch_instance_raw(entry)
    try:
        _png, width, height = await asyncio.to_thread(render_instance_png, raw)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"cannot render instance: {exc}") from exc

    cleaned = _clamp_boxes(body.boxes, width, height)
    store = dict(sub.gt_boxes or {})
    if cleaned:
        store[instance_id] = cleaned
    else:
        store.pop(instance_id, None)
    sub.gt_boxes = store
    sub.etag = uuid.uuid4()
    await db.commit()
    await db.refresh(sub)
    return GtBoxesOut(
        instance_id=instance_id,
        boxes=[GtBoxIn(**b) for b in (sub.gt_boxes or {}).get(instance_id, [])],
        etag=sub.etag,
    )


@router.get("/{submission_id}/instances/{instance_id}/gt-boxes", response_model=GtBoxesOut)
async def get_gt_boxes(
    submission_id: uuid.UUID,
    instance_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
) -> GtBoxesOut:
    _require_admin(user)
    sub = await _load(db, submission_id)
    _instance_entry(sub, instance_id)  # 404 if the instance isn't in the submission
    return GtBoxesOut(
        instance_id=instance_id,
        boxes=[GtBoxIn(**b) for b in (sub.gt_boxes or {}).get(instance_id, [])],
        etag=sub.etag,
    )


@router.get("/{submission_id}/instances/{instance_id}/gt-score", response_model=GtScoreOut)
async def gt_score(
    submission_id: uuid.UUID,
    instance_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    coverage: Annotated[float, Query(ge=0.0, le=1.0)] = 0.8,
) -> GtScoreOut:
    """Recall of the automatic pixel redaction against the reviewer's GT boxes:
    the fraction of GT PHI boxes the auto-masks cover by >= ``coverage``. This is
    the per-instance recall-monitoring signal (M6c) — a miss means residual PHI
    the pipeline would have shipped."""
    _require_admin(user)
    sub = await _load(db, submission_id)
    entry = _instance_entry(sub, instance_id)
    gt_raw = (sub.gt_boxes or {}).get(instance_id, [])
    gt = [
        GtBox(
            x=int(b["x"]),
            y=int(b["y"]),
            w=int(b["w"]),
            h=int(b["h"]),
            text=str(b.get("text", "")),
            category=str(b.get("category", "unknown")),
        )
        for b in gt_raw
    ]
    raw = await _fetch_instance_raw(entry)

    def _score() -> tuple[str, RedactionScore]:
        try:
            scrubbed = deidentify_dicom_bytes(raw)
        except Exception:
            scrubbed = raw
        res = clean_pixel_data(scrubbed)
        masked = [(m["x"], m["y"], m["w"], m["h"]) for m in res.redactions]
        return res.risk.level, score_redaction(gt, masked, coverage=coverage)

    try:
        risk_level, s = await asyncio.to_thread(_score)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"cannot score instance: {exc}") from exc
    return GtScoreOut(
        instance_id=instance_id,
        recall=s.recall,
        covered=s.covered,
        total=s.total,
        missed=s.missed,
        risk_level=risk_level,
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
