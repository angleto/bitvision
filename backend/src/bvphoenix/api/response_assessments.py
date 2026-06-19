"""Response assessments API — patient-level RECIST / volumetric response.

Aggregates the patient's target lesion tracks into an overall response
(CR/PR/SD/PD) at a follow-up study, persisted as an auditable, versioned
record. Conventions mirror the lesion-tracks / findings APIs (helpers
reused): patient-scoped RBAC, agent provenance, Idempotency-Key on create,
If-Match on mutate, soft-delete + restore, revision history. The category
is *computed* (``services/response_assessment``) but a clinician may edit
it; every change is tracked.

Endpoints (under ``/api``):

  POST   /patients/{id}/response-assessments
  GET    /patients/{id}/response-assessments
  GET    /response-assessments/{id}
  PATCH  /response-assessments/{id}
  DELETE /response-assessments/{id}
  POST   /response-assessments/{id}/restore
  POST   /response-assessments/{id}/recompute
  GET    /response-assessments/{id}/revisions
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.api.markers import (
    _agent_provenance,
    _patient_for_read,
    _patient_for_write,
    _study_or_404,
)
from bvphoenix.auth import require_user
from bvphoenix.db.models import ResponseAssessment, ResponseAssessmentRevision, User
from bvphoenix.db.session import get_db
from bvphoenix.middleware.audit_dependency import AuditDep
from bvphoenix.middleware.idempotency import IdempotencyContext, idempotent
from bvphoenix.services.etag import enforce_optional_if_match, format_etag
from bvphoenix.services.response_assessment import compute_response_assessment

router = APIRouter(tags=["response-assessments"])

_PURGE_GRACE = timedelta(days=30)

_CRITERION = Literal["recist_1_1", "volumetric", "percist"]
_CATEGORY = Literal["CR", "PR", "SD", "PD", "NE"]
_NON_TARGET = Literal["CR", "non_CR_non_PD", "PD", "NE"]

# Computed scalar columns copied from the summary onto the row.
_COMPUTED_FIELDS = (
    "criterion",
    "category",
    "target_sum_mm",
    "baseline_sum_mm",
    "nadir_sum_mm",
    "target_sum_pct_change",
    "volume_total_ml",
    "volume_pct_change",
    "new_lesions",
    "basis",
)


class ResponseAssessmentCreateIn(BaseModel):
    current_study_id: uuid.UUID
    baseline_study_id: uuid.UUID | None = None
    criterion: _CRITERION = "recist_1_1"
    notes: str | None = None


class ResponseAssessmentUpdateIn(BaseModel):
    category: _CATEGORY | None = None
    non_target_status: _NON_TARGET | None = None
    notes: str | None = None


class ResponseAssessmentOut(BaseModel):
    id: str
    patient_id: str
    assessment_date: str | None
    baseline_study_id: str | None
    current_study_id: str | None
    criterion: str
    target_sum_mm: float | None
    baseline_sum_mm: float | None
    nadir_sum_mm: float | None
    target_sum_pct_change: float | None
    volume_total_ml: float | None
    volume_pct_change: float | None
    category: str
    new_lesions: bool
    non_target_status: str | None
    basis: dict | None
    notes: str | None
    author_subject_id: str | None
    author_kind: Literal["human", "agent", "system"]
    model_id: str | None
    provider: str | None
    etag: str
    deleted_at: str | None
    created_at: str
    updated_at: str


class ResponseAssessmentRevisionOut(BaseModel):
    revision_no: int
    change_kind: Literal["create", "update", "recompute", "delete", "restore"]
    author_kind: Literal["human", "agent", "system"]
    actor_id: str | None
    diff_summary: str | None
    snapshot: dict
    created_at: str


def _out(ra: ResponseAssessment) -> ResponseAssessmentOut:
    return ResponseAssessmentOut(
        id=str(ra.id),
        patient_id=str(ra.patient_id),
        assessment_date=ra.assessment_date.isoformat() if ra.assessment_date else None,
        baseline_study_id=str(ra.baseline_study_id) if ra.baseline_study_id else None,
        current_study_id=str(ra.current_study_id) if ra.current_study_id else None,
        criterion=ra.criterion,
        target_sum_mm=ra.target_sum_mm,
        baseline_sum_mm=ra.baseline_sum_mm,
        nadir_sum_mm=ra.nadir_sum_mm,
        target_sum_pct_change=ra.target_sum_pct_change,
        volume_total_ml=ra.volume_total_ml,
        volume_pct_change=ra.volume_pct_change,
        category=ra.category,
        new_lesions=ra.new_lesions,
        non_target_status=ra.non_target_status,
        basis=ra.basis,
        notes=ra.notes,
        author_subject_id=str(ra.author_subject_id) if ra.author_subject_id else None,
        author_kind=ra.author_kind,  # type: ignore[arg-type]
        model_id=ra.model_id,
        provider=ra.provider,
        etag=str(ra.etag),
        deleted_at=ra.deleted_at.isoformat() if ra.deleted_at else None,
        created_at=ra.created_at.isoformat(),
        updated_at=ra.updated_at.isoformat(),
    )


def _snapshot(ra: ResponseAssessment) -> dict[str, Any]:
    return {
        "id": str(ra.id),
        "criterion": ra.criterion,
        "category": ra.category,
        "target_sum_mm": ra.target_sum_mm,
        "baseline_sum_mm": ra.baseline_sum_mm,
        "nadir_sum_mm": ra.nadir_sum_mm,
        "new_lesions": ra.new_lesions,
        "non_target_status": ra.non_target_status,
        "author_kind": ra.author_kind,
        "etag": str(ra.etag),
        "deleted_at": ra.deleted_at.isoformat() if ra.deleted_at else None,
    }


async def _append_revision(
    db: AsyncSession,
    *,
    ra: ResponseAssessment,
    change_kind: str,
    actor_id: uuid.UUID | None,
    author_kind: str,
    diff_summary: str | None = None,
) -> None:
    next_no = (
        await db.execute(
            select(func.coalesce(func.max(ResponseAssessmentRevision.revision_no), 0)).where(
                ResponseAssessmentRevision.response_assessment_id == ra.id
            )
        )
    ).scalar_one()
    db.add(
        ResponseAssessmentRevision(
            response_assessment_id=ra.id,
            patient_id=ra.patient_id,
            revision_no=int(next_no) + 1,
            snapshot=_snapshot(ra),
            change_kind=change_kind,
            author_kind=author_kind,
            actor_id=actor_id,
            diff_summary=diff_summary,
        )
    )
    await db.flush()


async def _ra_for_write(
    db: AsyncSession,
    request: Request,
    user: User,
    ra_id: uuid.UUID,
    *,
    allow_deleted: bool = False,
) -> ResponseAssessment:
    ra = (
        await db.execute(select(ResponseAssessment).where(ResponseAssessment.id == ra_id))
    ).scalar_one_or_none()
    if ra is None or (ra.deleted_at is not None and not allow_deleted):
        raise HTTPException(status_code=404, detail="response assessment not found")
    await _patient_for_write(db, request, user, ra.patient_id)
    if (
        getattr(request.state, "is_agent", False)
        and ra.author_kind == "human"
        and not getattr(user, "is_admin", False)
    ):
        raise HTTPException(
            status_code=403,
            detail="agent tokens cannot mutate response assessments authored by humans",
        )
    return ra


async def _validate_studies(
    db: AsyncSession,
    request: Request,
    user: User,
    patient_id: uuid.UUID,
    current_study_id: uuid.UUID,
    baseline_study_id: uuid.UUID | None,
) -> None:
    cur = await _study_or_404(db, current_study_id, request, user)
    if cur.patient_id != patient_id:
        raise HTTPException(status_code=422, detail="current study is not this patient's")
    if baseline_study_id is not None:
        base = await _study_or_404(db, baseline_study_id, request, user)
        if base.patient_id != patient_id:
            raise HTTPException(status_code=422, detail="baseline study is not this patient's")


def _apply_computed(ra: ResponseAssessment, summary: dict[str, Any]) -> None:
    for k in _COMPUTED_FIELDS:
        setattr(ra, k, summary.get(k))
    ra.baseline_study_id = summary.get("baseline_study_id")
    ra.assessment_date = summary.get("assessment_date")


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


@router.post(
    "/patients/{patient_id}/response-assessments",
    response_model=ResponseAssessmentOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_response_assessment(
    request: Request,
    patient_id: uuid.UUID,
    body: ResponseAssessmentCreateIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    audit: AuditDep,
    idem: Annotated[IdempotencyContext, Depends(idempotent)],
    dry_run: bool = Query(False, description="Compute + return without persisting."),
) -> ResponseAssessmentOut | JSONResponse:
    patient = await _patient_for_write(db, request, user, patient_id)
    if idem.replay is not None:
        return idem.replay
    await _validate_studies(
        db, request, user, patient.id, body.current_study_id, body.baseline_study_id
    )
    summary = await compute_response_assessment(
        db,
        patient_id=patient.id,
        current_study_id=body.current_study_id,
        baseline_study_id=body.baseline_study_id,
        criterion=body.criterion,
    )
    author_kind, model_id, provider, agent_token_id = _agent_provenance(request)

    ra = ResponseAssessment(
        patient_id=patient_id,
        current_study_id=body.current_study_id,
        notes=body.notes,
        author_subject_id=user.subject_id,
        author_kind=author_kind,
        model_id=model_id,
        provider=provider,
        agent_token_id=agent_token_id,
        category="NE",
        etag=uuid.uuid4(),
    )
    _apply_computed(ra, summary)

    if dry_run:
        now = datetime.now(UTC)
        ra.id = uuid.uuid4()
        ra.created_at = now
        ra.updated_at = now
        out = _out(ra)
        out.id = "dry-run"
        out.etag = "dry-run"
        return out

    db.add(ra)
    await db.flush()
    await _append_revision(
        db, ra=ra, change_kind="create", actor_id=user.subject_id, author_kind=author_kind
    )
    await db.commit()
    await db.refresh(ra)
    await audit.log(
        action="response_assessment_create",
        actor_subject_id=user.subject_id,
        resource_kind="response_assessment",
        resource_id=ra.id,
        metadata={
            "patient_id": str(patient_id),
            "category": ra.category,
            "criterion": ra.criterion,
        },
    )
    return idem.capture(
        _out(ra).model_dump(),
        status_code=status.HTTP_201_CREATED,
        extra_headers={"ETag": format_etag(str(ra.etag))},
    )


@router.get(
    "/patients/{patient_id}/response-assessments",
    response_model=list[ResponseAssessmentOut],
)
async def list_response_assessments(
    request: Request,
    patient_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    include_deleted: bool = Query(False),
    current_study_id: uuid.UUID | None = Query(
        None, description="Only assessments computed at this follow-up study."
    ),
    limit: int = Query(200, ge=1, le=1000),
) -> list[ResponseAssessmentOut]:
    await _patient_for_read(db, request, user, patient_id)
    stmt = (
        select(ResponseAssessment)
        .where(ResponseAssessment.patient_id == patient_id)
        .order_by(ResponseAssessment.created_at.desc())
        .limit(limit)
    )
    if current_study_id is not None:
        stmt = stmt.where(ResponseAssessment.current_study_id == current_study_id)
    if not include_deleted:
        stmt = stmt.where(ResponseAssessment.deleted_at.is_(None))
    rows = (await db.execute(stmt)).scalars().all()
    return [_out(ra) for ra in rows]


@router.get("/response-assessments/{ra_id}", response_model=ResponseAssessmentOut)
async def get_response_assessment(
    request: Request,
    ra_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    response: Response,
) -> ResponseAssessmentOut:
    ra = (
        await db.execute(select(ResponseAssessment).where(ResponseAssessment.id == ra_id))
    ).scalar_one_or_none()
    if ra is None:
        raise HTTPException(status_code=404, detail="response assessment not found")
    await _patient_for_read(db, request, user, ra.patient_id)
    response.headers["ETag"] = format_etag(str(ra.etag))
    return _out(ra)


@router.patch("/response-assessments/{ra_id}", response_model=ResponseAssessmentOut)
async def update_response_assessment(
    request: Request,
    ra_id: uuid.UUID,
    body: ResponseAssessmentUpdateIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    audit: AuditDep,
    response: Response,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> ResponseAssessmentOut:
    ra = await _ra_for_write(db, request, user, ra_id)
    enforce_optional_if_match(if_match, str(ra.etag), what="response assessment")
    fields = body.model_fields_set
    changed: list[str] = []
    for attr in ("category", "non_target_status", "notes"):
        if attr in fields:
            setattr(ra, attr, getattr(body, attr))
            changed.append(attr)
    if changed:
        ra.updated_at = datetime.now(UTC)
        ra.etag = uuid.uuid4()
        await db.flush()
        author_kind, _m, _p, _t = _agent_provenance(request)
        await _append_revision(
            db,
            ra=ra,
            change_kind="update",
            actor_id=user.subject_id,
            author_kind=author_kind,
            diff_summary=",".join(sorted(set(changed))),
        )
        await db.commit()
        await db.refresh(ra)
        await audit.log(
            action="response_assessment_update",
            actor_subject_id=user.subject_id,
            resource_kind="response_assessment",
            resource_id=ra.id,
            metadata={"changed": sorted(set(changed))},
        )
    response.headers["ETag"] = format_etag(str(ra.etag))
    return _out(ra)


@router.post("/response-assessments/{ra_id}/recompute", response_model=ResponseAssessmentOut)
async def recompute_response_assessment(
    request: Request,
    ra_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    audit: AuditDep,
    response: Response,
) -> ResponseAssessmentOut:
    """Re-run the computation over the current findings (they may have been
    edited or propagated since) and update the record."""
    ra = await _ra_for_write(db, request, user, ra_id)
    if ra.current_study_id is None:
        raise HTTPException(status_code=409, detail="assessment has no current study to recompute")
    summary = await compute_response_assessment(
        db,
        patient_id=ra.patient_id,
        current_study_id=ra.current_study_id,
        baseline_study_id=ra.baseline_study_id,
        criterion=ra.criterion,
    )
    _apply_computed(ra, summary)
    ra.updated_at = datetime.now(UTC)
    ra.etag = uuid.uuid4()
    await db.flush()
    author_kind, _m, _p, _t = _agent_provenance(request)
    await _append_revision(
        db, ra=ra, change_kind="recompute", actor_id=user.subject_id, author_kind=author_kind
    )
    await db.commit()
    await db.refresh(ra)
    await audit.log(
        action="response_assessment_recompute",
        actor_subject_id=user.subject_id,
        resource_kind="response_assessment",
        resource_id=ra.id,
        metadata={"category": ra.category},
    )
    response.headers["ETag"] = format_etag(str(ra.etag))
    return _out(ra)


@router.delete("/response-assessments/{ra_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_response_assessment(
    request: Request,
    ra_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    audit: AuditDep,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
    reason: str | None = Query(None, max_length=255),
) -> Response:
    ra = await _ra_for_write(db, request, user, ra_id, allow_deleted=True)
    if ra.deleted_at is not None:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    enforce_optional_if_match(if_match, str(ra.etag), what="response assessment")
    now = datetime.now(UTC)
    ra.deleted_at = now
    ra.purge_after = now + _PURGE_GRACE
    ra.delete_reason = reason
    ra.updated_at = now
    ra.etag = uuid.uuid4()
    await db.flush()
    author_kind, _m, _p, _t = _agent_provenance(request)
    await _append_revision(
        db,
        ra=ra,
        change_kind="delete",
        actor_id=user.subject_id,
        author_kind=author_kind,
        diff_summary=reason,
    )
    await db.commit()
    await audit.log(
        action="response_assessment_delete",
        actor_subject_id=user.subject_id,
        resource_kind="response_assessment",
        resource_id=ra.id,
        metadata={"patient_id": str(ra.patient_id), "soft": True},
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/response-assessments/{ra_id}/restore", response_model=ResponseAssessmentOut)
async def restore_response_assessment(
    request: Request,
    ra_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    audit: AuditDep,
    response: Response,
) -> ResponseAssessmentOut:
    ra = await _ra_for_write(db, request, user, ra_id, allow_deleted=True)
    if ra.deleted_at is None:
        raise HTTPException(status_code=409, detail="response assessment is not deleted")
    ra.deleted_at = None
    ra.purge_after = None
    ra.delete_reason = None
    ra.updated_at = datetime.now(UTC)
    ra.etag = uuid.uuid4()
    await db.flush()
    author_kind, _m, _p, _t = _agent_provenance(request)
    await _append_revision(
        db, ra=ra, change_kind="restore", actor_id=user.subject_id, author_kind=author_kind
    )
    await db.commit()
    await db.refresh(ra)
    await audit.log(
        action="response_assessment_restore",
        actor_subject_id=user.subject_id,
        resource_kind="response_assessment",
        resource_id=ra.id,
        metadata={"patient_id": str(ra.patient_id)},
    )
    response.headers["ETag"] = format_etag(str(ra.etag))
    return _out(ra)


@router.get(
    "/response-assessments/{ra_id}/revisions",
    response_model=list[ResponseAssessmentRevisionOut],
)
async def list_response_assessment_revisions(
    request: Request,
    ra_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    limit: int = Query(200, ge=1, le=1000),
) -> list[ResponseAssessmentRevisionOut]:
    ra = (
        await db.execute(select(ResponseAssessment).where(ResponseAssessment.id == ra_id))
    ).scalar_one_or_none()
    if ra is None:
        raise HTTPException(status_code=404, detail="response assessment not found")
    await _patient_for_read(db, request, user, ra.patient_id)
    rows = (
        (
            await db.execute(
                select(ResponseAssessmentRevision)
                .where(ResponseAssessmentRevision.response_assessment_id == ra_id)
                .order_by(ResponseAssessmentRevision.revision_no.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return [
        ResponseAssessmentRevisionOut(
            revision_no=r.revision_no,
            change_kind=r.change_kind,  # type: ignore[arg-type]
            author_kind=r.author_kind,  # type: ignore[arg-type]
            actor_id=str(r.actor_id) if r.actor_id else None,
            diff_summary=r.diff_summary,
            snapshot=r.snapshot,
            created_at=r.created_at.isoformat(),
        )
        for r in rows
    ]
