"""Lesion tracks API — longitudinal lesion follow-up.

A ``LesionTrack`` follows one physical lesion across studies; each
``LesionTrackPoint`` links a per-study ``Finding`` (the measured node at a
timepoint). The trajectory endpoint answers "has the tumour grown?" as a
derived computation over the ordered points (``services/lesion_tracks.py``).

Conventions mirror the findings API (helpers reused, not duplicated):
patient-scoped RBAC, agent provenance + the agent-cannot-mutate-human
guard, Idempotency-Key on create, optional If-Match on mutate, soft-delete
+ restore, full revision history. Cross-patient is impossible by
construction (composite FK on ``lesion_track_points``); the API also
returns a clean 422 before the DB rejects a cross-patient finding link.

Endpoints (under ``/api``):

  POST   /patients/{id}/lesion-tracks
  GET    /patients/{id}/lesion-tracks
  GET    /lesion-tracks/{id}
  PATCH  /lesion-tracks/{id}
  DELETE /lesion-tracks/{id}
  POST   /lesion-tracks/{id}/restore
  GET    /lesion-tracks/{id}/revisions
  POST   /lesion-tracks/{id}/points
  DELETE /lesion-tracks/{id}/points/{point_id}
  GET    /lesion-tracks/{id}/trajectory
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.api.findings import _resolve_anatomy, _resolve_type
from bvphoenix.api.markers import (
    _agent_provenance,
    _patient_for_read,
    _patient_for_write,
)
from bvphoenix.auth import require_user
from bvphoenix.db.models import (
    AnatomySite,
    Finding,
    FindingType,
    ImagingStudy,
    LesionTrack,
    LesionTrackPoint,
    LesionTrackRevision,
    Registration,
    User,
)
from bvphoenix.db.session import get_db
from bvphoenix.middleware.audit_dependency import AuditDep
from bvphoenix.middleware.idempotency import IdempotencyContext, idempotent
from bvphoenix.services.etag import enforce_optional_if_match, format_etag
from bvphoenix.services.lesion_tracks import TrackTimepoint, compute_trajectory

router = APIRouter(tags=["lesion-tracks"])

_PURGE_GRACE = timedelta(days=30)

_RECIST_ROLE = Literal["target", "non_target", "new", "not_evaluable"]
_LATERALITY = Literal["left", "right", "bilateral", "midline"]
_STATUS = Literal["active", "resolved", "retracted"]


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class LesionTrackPointIn(BaseModel):
    finding_id: uuid.UUID
    is_baseline: bool = False
    registration_id: uuid.UUID | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class LesionTrackCreateIn(BaseModel):
    label: str = Field(min_length=1, max_length=255)
    anatomy: str | None = Field(default=None, max_length=64)
    laterality: _LATERALITY | None = None
    type: str | None = Field(default=None, max_length=64, description="finding type key")
    recist_role: _RECIST_ROLE | None = None
    status: _STATUS = "active"
    description: str | None = None
    # Optionally seed the baseline timepoint in the same call.
    baseline_finding_id: uuid.UUID | None = None


class LesionTrackUpdateIn(BaseModel):
    label: str | None = Field(default=None, min_length=1, max_length=255)
    anatomy: str | None = Field(default=None, max_length=64)
    laterality: _LATERALITY | None = None
    type: str | None = Field(default=None, max_length=64)
    recist_role: _RECIST_ROLE | None = None
    status: _STATUS | None = None
    description: str | None = None


class LesionTrackPointOut(BaseModel):
    id: str
    finding_id: str
    is_baseline: bool
    timepoint_date: str | None
    registration_id: str | None
    linked_by_kind: Literal["human", "agent", "system"]
    confidence: float | None
    created_at: str


class LesionTrackOut(BaseModel):
    id: str
    patient_id: str
    label: str
    anatomy_site_id: str | None
    anatomy: str | None
    laterality: str | None
    finding_type_id: str | None
    type: str | None
    recist_role: str | None
    status: str
    description: str | None
    author_subject_id: str | None
    author_kind: Literal["human", "agent", "system"]
    model_id: str | None
    provider: str | None
    etag: str
    deleted_at: str | None
    created_at: str
    updated_at: str
    points: list[LesionTrackPointOut]


class LesionTrackRevisionOut(BaseModel):
    revision_no: int
    change_kind: Literal["create", "update", "add_point", "remove_point", "delete", "restore"]
    author_kind: Literal["human", "agent", "system"]
    actor_id: str | None
    diff_summary: str | None
    snapshot: dict
    created_at: str


class TrajectoryOut(BaseModel):
    baseline: dict | None
    latest: dict | None
    timepoints: list[dict]
    summary: dict | None


# ---------------------------------------------------------------------------
# Load / serialize helpers
# ---------------------------------------------------------------------------


async def _track_for_write(
    db: AsyncSession,
    request: Request,
    user: User,
    track_id: uuid.UUID,
    *,
    allow_deleted: bool = False,
) -> LesionTrack:
    t = (
        await db.execute(select(LesionTrack).where(LesionTrack.id == track_id))
    ).scalar_one_or_none()
    if t is None:
        raise HTTPException(status_code=404, detail="lesion track not found")
    if t.deleted_at is not None and not allow_deleted:
        raise HTTPException(status_code=404, detail="lesion track not found")
    await _patient_for_write(db, request, user, t.patient_id)
    if (
        getattr(request.state, "is_agent", False)
        and t.author_kind == "human"
        and not getattr(user, "is_admin", False)
    ):
        raise HTTPException(
            status_code=403,
            detail={
                "type": "https://bitvision.example/errors/forbidden",
                "title": "Forbidden",
                "status": 403,
                "detail": "agent tokens cannot mutate lesion tracks authored by humans",
                "lesion_track_id": str(t.id),
                "author_kind": t.author_kind,
            },
        )
    return t


async def _finding_of_patient(
    db: AsyncSession, finding_id: uuid.UUID, patient_id: uuid.UUID
) -> Finding:
    """Load a live finding that belongs to ``patient_id`` (clean 422 before
    the composite FK would reject a cross-patient link)."""
    f = (await db.execute(select(Finding).where(Finding.id == finding_id))).scalar_one_or_none()
    if f is None or f.deleted_at is not None or f.patient_id != patient_id:
        raise HTTPException(
            status_code=422,
            detail=f"finding {finding_id} not found for this patient",
        )
    return f


async def _study_date_for(db: AsyncSession, study_id: uuid.UUID) -> date | None:
    return (
        await db.execute(select(ImagingStudy.study_date).where(ImagingStudy.id == study_id))
    ).scalar_one_or_none()


async def _points_for(
    db: AsyncSession, track_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[LesionTrackPoint]]:
    if not track_ids:
        return {}
    rows = (
        (
            await db.execute(
                select(LesionTrackPoint).where(LesionTrackPoint.lesion_track_id.in_(track_ids))
            )
        )
        .scalars()
        .all()
    )
    out: dict[uuid.UUID, list[LesionTrackPoint]] = {}
    for p in rows:
        out.setdefault(p.lesion_track_id, []).append(p)
    # Deterministic order: by timepoint_date (undated last), then created_at.
    for pts in out.values():
        pts.sort(
            key=lambda p: (p.timepoint_date is None, p.timepoint_date or date.min, p.created_at)
        )
    return out


def _point_out(p: LesionTrackPoint) -> LesionTrackPointOut:
    return LesionTrackPointOut(
        id=str(p.id),
        finding_id=str(p.finding_id),
        is_baseline=p.is_baseline,
        timepoint_date=p.timepoint_date.isoformat() if p.timepoint_date else None,
        registration_id=str(p.registration_id) if p.registration_id else None,
        linked_by_kind=p.linked_by_kind,  # type: ignore[arg-type]
        confidence=p.confidence,
        created_at=p.created_at.isoformat(),
    )


def _track_out(
    t: LesionTrack,
    *,
    type_key: str | None,
    anatomy_key: str | None,
    points: list[LesionTrackPoint],
) -> LesionTrackOut:
    return LesionTrackOut(
        id=str(t.id),
        patient_id=str(t.patient_id),
        label=t.label,
        anatomy_site_id=str(t.anatomy_site_id) if t.anatomy_site_id else None,
        anatomy=anatomy_key,
        laterality=t.laterality,
        finding_type_id=str(t.finding_type_id) if t.finding_type_id else None,
        type=type_key,
        recist_role=t.recist_role,
        status=t.status,
        description=t.description,
        author_subject_id=str(t.author_subject_id) if t.author_subject_id else None,
        author_kind=t.author_kind,  # type: ignore[arg-type]
        model_id=t.model_id,
        provider=t.provider,
        etag=str(t.etag),
        deleted_at=t.deleted_at.isoformat() if t.deleted_at else None,
        created_at=t.created_at.isoformat(),
        updated_at=t.updated_at.isoformat(),
        points=[_point_out(p) for p in points],
    )


async def _serialize_one(db: AsyncSession, t: LesionTrack) -> LesionTrackOut:
    type_key = None
    if t.finding_type_id is not None:
        type_key = (
            await db.execute(select(FindingType.key).where(FindingType.id == t.finding_type_id))
        ).scalar_one_or_none()
    anatomy_key = None
    if t.anatomy_site_id is not None:
        anatomy_key = (
            await db.execute(select(AnatomySite.key).where(AnatomySite.id == t.anatomy_site_id))
        ).scalar_one_or_none()
    points = (await _points_for(db, [t.id])).get(t.id, [])
    return _track_out(t, type_key=type_key, anatomy_key=anatomy_key, points=points)


def _track_snapshot(t: LesionTrack) -> dict[str, Any]:
    return {
        "id": str(t.id),
        "label": t.label,
        "anatomy_site_id": str(t.anatomy_site_id) if t.anatomy_site_id else None,
        "laterality": t.laterality,
        "finding_type_id": str(t.finding_type_id) if t.finding_type_id else None,
        "recist_role": t.recist_role,
        "status": t.status,
        "author_kind": t.author_kind,
        "etag": str(t.etag),
        "deleted_at": t.deleted_at.isoformat() if t.deleted_at else None,
    }


async def _append_revision(
    db: AsyncSession,
    *,
    track: LesionTrack,
    change_kind: str,
    actor_id: uuid.UUID | None,
    author_kind: str,
    diff_summary: str | None = None,
) -> None:
    next_no = (
        await db.execute(
            select(func.coalesce(func.max(LesionTrackRevision.revision_no), 0)).where(
                LesionTrackRevision.lesion_track_id == track.id
            )
        )
    ).scalar_one()
    db.add(
        LesionTrackRevision(
            lesion_track_id=track.id,
            patient_id=track.patient_id,
            revision_no=int(next_no) + 1,
            snapshot=_track_snapshot(track),
            change_kind=change_kind,
            author_kind=author_kind,
            actor_id=actor_id,
            diff_summary=diff_summary,
        )
    )
    await db.flush()


async def _add_point(
    db: AsyncSession,
    *,
    track: LesionTrack,
    finding: Finding,
    is_baseline: bool,
    registration_id: uuid.UUID | None,
    confidence: float | None,
    linked_by_kind: str,
) -> LesionTrackPoint:
    """Create a point linking ``finding`` into ``track``. Enforces the
    single-baseline rule with a clean 409 (the partial-unique index is the
    DB backstop) and rejects a finding already tracked elsewhere."""
    existing = (
        await db.execute(select(LesionTrackPoint).where(LesionTrackPoint.finding_id == finding.id))
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail=f"finding {finding.id} is already linked to a lesion track",
        )
    if is_baseline:
        has_baseline = (
            await db.execute(
                select(LesionTrackPoint.id).where(
                    LesionTrackPoint.lesion_track_id == track.id,
                    LesionTrackPoint.is_baseline.is_(True),
                )
            )
        ).scalar_one_or_none()
        if has_baseline is not None:
            raise HTTPException(
                status_code=409,
                detail="track already has a baseline; unset it before setting another",
            )
    if registration_id is not None:
        reg = (
            await db.execute(select(Registration).where(Registration.id == registration_id))
        ).scalar_one_or_none()
        if reg is None:
            raise HTTPException(status_code=422, detail=f"registration {registration_id} not found")
    point = LesionTrackPoint(
        lesion_track_id=track.id,
        finding_id=finding.id,
        patient_id=track.patient_id,
        is_baseline=is_baseline,
        timepoint_date=await _study_date_for(db, finding.study_id),
        registration_id=registration_id,
        linked_by_kind=linked_by_kind,
        confidence=confidence,
    )
    db.add(point)
    await db.flush()
    return point


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


@router.post(
    "/patients/{patient_id}/lesion-tracks",
    response_model=LesionTrackOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_lesion_track(
    request: Request,
    patient_id: uuid.UUID,
    body: LesionTrackCreateIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    audit: AuditDep,
    idem: Annotated[IdempotencyContext, Depends(idempotent)],
    dry_run: bool = Query(False, description="Validate + return the would-be track, no write."),
) -> LesionTrackOut | JSONResponse:
    patient = await _patient_for_write(db, request, user, patient_id)
    if idem.replay is not None:
        return idem.replay

    anatomy = await _resolve_anatomy(db, body.anatomy)
    ftype = await _resolve_type(db, body.type) if body.type else None
    baseline_finding = None
    if body.baseline_finding_id is not None:
        baseline_finding = await _finding_of_patient(db, body.baseline_finding_id, patient.id)
    author_kind, model_id, provider, agent_token_id = _agent_provenance(request)

    if dry_run:
        now = datetime.now(UTC).isoformat()
        return LesionTrackOut(
            id="dry-run",
            patient_id=str(patient_id),
            label=body.label,
            anatomy_site_id=str(anatomy.id) if anatomy else None,
            anatomy=anatomy.key if anatomy else None,
            laterality=body.laterality,
            finding_type_id=str(ftype.id) if ftype else None,
            type=ftype.key if ftype else None,
            recist_role=body.recist_role,
            status=body.status,
            description=body.description,
            author_subject_id=str(user.subject_id) if user.subject_id else None,
            author_kind=author_kind,  # type: ignore[arg-type]
            model_id=model_id,
            provider=provider,
            etag="dry-run",
            deleted_at=None,
            created_at=now,
            updated_at=now,
            points=[],
        )

    t = LesionTrack(
        patient_id=patient_id,
        label=body.label,
        anatomy_site_id=anatomy.id if anatomy else None,
        laterality=body.laterality,
        finding_type_id=ftype.id if ftype else None,
        recist_role=body.recist_role,
        status=body.status,
        description=body.description,
        author_subject_id=user.subject_id,
        author_kind=author_kind,
        model_id=model_id,
        provider=provider,
        agent_token_id=agent_token_id,
        etag=uuid.uuid4(),
    )
    db.add(t)
    await db.flush()
    if baseline_finding is not None:
        await _add_point(
            db,
            track=t,
            finding=baseline_finding,
            is_baseline=True,
            registration_id=None,
            confidence=None,
            linked_by_kind=author_kind,
        )
    await _append_revision(
        db, track=t, change_kind="create", actor_id=user.subject_id, author_kind=author_kind
    )
    await db.commit()
    await db.refresh(t)
    await audit.log(
        action="lesion_track_create",
        actor_subject_id=user.subject_id,
        resource_kind="lesion_track",
        resource_id=t.id,
        metadata={"patient_id": str(patient_id), "label": t.label},
    )
    out = await _serialize_one(db, t)
    return idem.capture(
        out.model_dump(),
        status_code=status.HTTP_201_CREATED,
        extra_headers={"ETag": format_etag(str(t.etag))},
    )


@router.get("/patients/{patient_id}/lesion-tracks", response_model=list[LesionTrackOut])
async def list_lesion_tracks(
    request: Request,
    patient_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    status_filter: str | None = Query(None, alias="status"),
    recist_role: str | None = Query(None),
    include_deleted: bool = Query(False),
    limit: int = Query(200, ge=1, le=1000),
) -> list[LesionTrackOut]:
    await _patient_for_read(db, request, user, patient_id)
    stmt = (
        select(LesionTrack)
        .where(LesionTrack.patient_id == patient_id)
        .order_by(LesionTrack.created_at.desc())
        .limit(limit)
    )
    if not include_deleted:
        stmt = stmt.where(LesionTrack.deleted_at.is_(None))
    if status_filter:
        stmt = stmt.where(LesionTrack.status == status_filter)
    if recist_role:
        stmt = stmt.where(LesionTrack.recist_role == recist_role)
    tracks = (await db.execute(stmt)).scalars().all()
    points_map = await _points_for(db, [t.id for t in tracks])
    # Resolve vocab keys in bulk-friendly per-row queries (small N).
    out: list[LesionTrackOut] = []
    for t in tracks:
        type_key = None
        if t.finding_type_id is not None:
            type_key = (
                await db.execute(select(FindingType.key).where(FindingType.id == t.finding_type_id))
            ).scalar_one_or_none()
        anatomy_key = None
        if t.anatomy_site_id is not None:
            anatomy_key = (
                await db.execute(select(AnatomySite.key).where(AnatomySite.id == t.anatomy_site_id))
            ).scalar_one_or_none()
        out.append(
            _track_out(
                t, type_key=type_key, anatomy_key=anatomy_key, points=points_map.get(t.id, [])
            )
        )
    return out


@router.get("/lesion-tracks/{track_id}", response_model=LesionTrackOut)
async def get_lesion_track(
    request: Request,
    track_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    response: Response,
) -> LesionTrackOut:
    t = (
        await db.execute(select(LesionTrack).where(LesionTrack.id == track_id))
    ).scalar_one_or_none()
    if t is None:
        raise HTTPException(status_code=404, detail="lesion track not found")
    await _patient_for_read(db, request, user, t.patient_id)
    response.headers["ETag"] = format_etag(str(t.etag))
    return await _serialize_one(db, t)


@router.patch("/lesion-tracks/{track_id}", response_model=LesionTrackOut)
async def update_lesion_track(
    request: Request,
    track_id: uuid.UUID,
    body: LesionTrackUpdateIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    audit: AuditDep,
    response: Response,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> LesionTrackOut:
    t = await _track_for_write(db, request, user, track_id)
    enforce_optional_if_match(if_match, str(t.etag), what="lesion track")
    fields = body.model_fields_set
    changed: list[str] = []

    if "label" in fields and body.label is not None:
        t.label = body.label
        changed.append("label")
    if "anatomy" in fields:
        anatomy = await _resolve_anatomy(db, body.anatomy)
        t.anatomy_site_id = anatomy.id if anatomy else None
        changed.append("anatomy")
    if "type" in fields:
        ftype = await _resolve_type(db, body.type) if body.type else None
        t.finding_type_id = ftype.id if ftype else None
        changed.append("type")
    for attr in ("laterality", "recist_role", "status", "description"):
        if attr in fields:
            setattr(t, attr, getattr(body, attr))
            changed.append(attr)

    if changed:
        t.updated_at = datetime.now(UTC)
        t.etag = uuid.uuid4()
        await db.flush()
        author_kind, _m, _p, _tk = _agent_provenance(request)
        await _append_revision(
            db,
            track=t,
            change_kind="update",
            actor_id=user.subject_id,
            author_kind=author_kind,
            diff_summary=",".join(sorted(set(changed))),
        )
        await db.commit()
        await db.refresh(t)
        await audit.log(
            action="lesion_track_update",
            actor_subject_id=user.subject_id,
            resource_kind="lesion_track",
            resource_id=t.id,
            metadata={"changed": sorted(set(changed))},
        )
    response.headers["ETag"] = format_etag(str(t.etag))
    return await _serialize_one(db, t)


@router.delete("/lesion-tracks/{track_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_lesion_track(
    request: Request,
    track_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    audit: AuditDep,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
    hard: bool = Query(False, description="Admin only: permanently purge instead of soft-delete."),
    reason: str | None = Query(None, max_length=255),
) -> Response:
    t = await _track_for_write(db, request, user, track_id, allow_deleted=True)
    if t.deleted_at is not None and not hard:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    enforce_optional_if_match(if_match, str(t.etag), what="lesion track")

    if hard:
        if not getattr(user, "is_admin", False):
            raise HTTPException(status_code=403, detail="hard delete requires admin")
        tid, pid = t.id, t.patient_id
        await db.delete(t)
        await db.commit()
        await audit.log(
            action="lesion_track_purge",
            actor_subject_id=user.subject_id,
            resource_kind="lesion_track",
            resource_id=tid,
            metadata={"patient_id": str(pid)},
        )
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    now = datetime.now(UTC)
    t.deleted_at = now
    t.purge_after = now + _PURGE_GRACE
    t.delete_reason = reason
    t.updated_at = now
    t.etag = uuid.uuid4()
    await db.flush()
    author_kind, _m, _p, _tk = _agent_provenance(request)
    await _append_revision(
        db,
        track=t,
        change_kind="delete",
        actor_id=user.subject_id,
        author_kind=author_kind,
        diff_summary=reason,
    )
    await db.commit()
    await audit.log(
        action="lesion_track_delete",
        actor_subject_id=user.subject_id,
        resource_kind="lesion_track",
        resource_id=t.id,
        metadata={"patient_id": str(t.patient_id), "soft": True},
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/lesion-tracks/{track_id}/restore", response_model=LesionTrackOut)
async def restore_lesion_track(
    request: Request,
    track_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    audit: AuditDep,
    response: Response,
) -> LesionTrackOut:
    t = await _track_for_write(db, request, user, track_id, allow_deleted=True)
    if t.deleted_at is None:
        raise HTTPException(status_code=409, detail="lesion track is not deleted")
    t.deleted_at = None
    t.purge_after = None
    t.delete_reason = None
    t.updated_at = datetime.now(UTC)
    t.etag = uuid.uuid4()
    await db.flush()
    author_kind, _m, _p, _tk = _agent_provenance(request)
    await _append_revision(
        db, track=t, change_kind="restore", actor_id=user.subject_id, author_kind=author_kind
    )
    await db.commit()
    await db.refresh(t)
    await audit.log(
        action="lesion_track_restore",
        actor_subject_id=user.subject_id,
        resource_kind="lesion_track",
        resource_id=t.id,
        metadata={"patient_id": str(t.patient_id)},
    )
    response.headers["ETag"] = format_etag(str(t.etag))
    return await _serialize_one(db, t)


@router.get("/lesion-tracks/{track_id}/revisions", response_model=list[LesionTrackRevisionOut])
async def list_lesion_track_revisions(
    request: Request,
    track_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    limit: int = Query(200, ge=1, le=1000),
) -> list[LesionTrackRevisionOut]:
    t = (
        await db.execute(select(LesionTrack).where(LesionTrack.id == track_id))
    ).scalar_one_or_none()
    if t is None:
        raise HTTPException(status_code=404, detail="lesion track not found")
    await _patient_for_read(db, request, user, t.patient_id)
    rows = (
        (
            await db.execute(
                select(LesionTrackRevision)
                .where(LesionTrackRevision.lesion_track_id == track_id)
                .order_by(LesionTrackRevision.revision_no.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return [
        LesionTrackRevisionOut(
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


# ---------------------------------------------------------------------------
# Points (link / unlink a finding as a timepoint)
# ---------------------------------------------------------------------------


@router.post("/lesion-tracks/{track_id}/points", response_model=LesionTrackOut)
async def add_lesion_track_point(
    request: Request,
    track_id: uuid.UUID,
    body: LesionTrackPointIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    audit: AuditDep,
    response: Response,
) -> LesionTrackOut:
    t = await _track_for_write(db, request, user, track_id)
    finding = await _finding_of_patient(db, body.finding_id, t.patient_id)
    author_kind, _m, _p, _tk = _agent_provenance(request)
    await _add_point(
        db,
        track=t,
        finding=finding,
        is_baseline=body.is_baseline,
        registration_id=body.registration_id,
        confidence=body.confidence,
        linked_by_kind=author_kind,
    )
    t.updated_at = datetime.now(UTC)
    t.etag = uuid.uuid4()
    await db.flush()
    await _append_revision(
        db,
        track=t,
        change_kind="add_point",
        actor_id=user.subject_id,
        author_kind=author_kind,
        diff_summary=f"finding={finding.id}",
    )
    await db.commit()
    await db.refresh(t)
    await audit.log(
        action="lesion_track_add_point",
        actor_subject_id=user.subject_id,
        resource_kind="lesion_track",
        resource_id=t.id,
        metadata={"finding_id": str(finding.id), "is_baseline": body.is_baseline},
    )
    response.headers["ETag"] = format_etag(str(t.etag))
    return await _serialize_one(db, t)


@router.delete(
    "/lesion-tracks/{track_id}/points/{point_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def remove_lesion_track_point(
    request: Request,
    track_id: uuid.UUID,
    point_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    audit: AuditDep,
) -> Response:
    t = await _track_for_write(db, request, user, track_id)
    p = (
        await db.execute(
            select(LesionTrackPoint).where(
                LesionTrackPoint.id == point_id,
                LesionTrackPoint.lesion_track_id == t.id,
            )
        )
    ).scalar_one_or_none()
    if p is None:
        raise HTTPException(status_code=404, detail="timepoint not found")
    finding_id = p.finding_id
    await db.delete(p)
    t.updated_at = datetime.now(UTC)
    t.etag = uuid.uuid4()
    await db.flush()
    author_kind, _m, _p, _tk = _agent_provenance(request)
    await _append_revision(
        db,
        track=t,
        change_kind="remove_point",
        actor_id=user.subject_id,
        author_kind=author_kind,
        diff_summary=f"finding={finding_id}",
    )
    await db.commit()
    await audit.log(
        action="lesion_track_remove_point",
        actor_subject_id=user.subject_id,
        resource_kind="lesion_track",
        resource_id=t.id,
        metadata={"point_id": str(point_id), "finding_id": str(finding_id)},
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Trajectory (derived growth over time)
# ---------------------------------------------------------------------------


@router.get("/lesion-tracks/{track_id}/trajectory", response_model=TrajectoryOut)
async def get_lesion_track_trajectory(
    request: Request,
    track_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
) -> TrajectoryOut:
    """Derived longitudinal trajectory: per-timepoint volume/diameter
    deltas vs baseline and vs previous, doubling time, overall direction.
    Pure read — the findings are the source of truth, nothing persisted."""
    t = (
        await db.execute(select(LesionTrack).where(LesionTrack.id == track_id))
    ).scalar_one_or_none()
    if t is None:
        raise HTTPException(status_code=404, detail="lesion track not found")
    await _patient_for_read(db, request, user, t.patient_id)
    points = (await _points_for(db, [t.id])).get(t.id, [])
    if not points:
        return TrajectoryOut(baseline=None, latest=None, timepoints=[], summary=None)

    finding_ids = [p.finding_id for p in points]
    findings = {
        f.id: f
        for f in (await db.execute(select(Finding).where(Finding.id.in_(finding_ids)))).scalars()
    }
    tps: list[TrackTimepoint] = []
    for p in points:
        f = findings.get(p.finding_id)
        if f is None:
            continue
        tps.append(
            TrackTimepoint(
                point_id=p.id,
                finding_id=p.finding_id,
                measured_on=p.timepoint_date,
                is_baseline=p.is_baseline,
                volume_ml=f.volume_ml,
                longest_diameter_mm=f.longest_diameter_mm,
                short_axis_mm=f.short_axis_mm,
                suv_max=f.suv_max,
            )
        )
    traj = compute_trajectory(tps)
    return TrajectoryOut(**traj)


# ---------------------------------------------------------------------------
# Propagation (semi-automatic re-measure on a follow-up study)
# ---------------------------------------------------------------------------


class PropagateIn(BaseModel):
    followup_series_id: uuid.UUID
    refine: bool = True


@router.post("/lesion-tracks/{track_id}/propagate")
async def propagate_lesion_endpoint(
    request: Request,
    track_id: uuid.UUID,
    body: PropagateIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    audit: AuditDep,
    dry_run: bool = Query(False, description="Validate the request without enqueueing."),
) -> dict:
    """Propagate the track's baseline lesion onto a follow-up series: a
    worker registers the two studies, warps the baseline mask onto the
    follow-up, re-segments on the follow-up's real voxels, and records a
    new (system-authored, candidate) Finding + timepoint. Returns the job.

    The follow-up series must belong to the same patient (else 422, before
    the worker), and the baseline finding must carry a segmentation mask.
    """
    from arq import create_pool as _create_pool

    from bvphoenix.config import get_settings
    from bvphoenix.db.models import FindingGeometry, ImagingStudy, Segmentation, Series
    from bvphoenix.services import jobs as jobs_service
    from bvphoenix.services.arq_redis import redis_settings as _redis_settings

    t = await _track_for_write(db, request, user, track_id)

    fu = (
        await db.execute(
            select(Series, ImagingStudy)
            .join(ImagingStudy, ImagingStudy.id == Series.study_id)
            .where(Series.id == body.followup_series_id)
        )
    ).first()
    if fu is None:
        raise HTTPException(status_code=404, detail="follow-up series not found")
    _fu_series, fu_study = fu
    if fu_study.patient_id != t.patient_id:
        raise HTTPException(
            status_code=422, detail="follow-up series does not belong to this patient"
        )

    base_point = (
        await db.execute(
            select(LesionTrackPoint).where(
                LesionTrackPoint.lesion_track_id == t.id,
                LesionTrackPoint.is_baseline.is_(True),
            )
        )
    ).scalar_one_or_none()
    if base_point is None:
        raise HTTPException(
            status_code=422, detail="track has no baseline timepoint to propagate from"
        )
    has_mask = (
        await db.execute(
            select(FindingGeometry.id)
            .join(Segmentation, Segmentation.id == FindingGeometry.segmentation_id)
            .where(
                FindingGeometry.finding_id == base_point.finding_id,
                FindingGeometry.role == "mask",
            )
        )
    ).scalar_one_or_none()
    if has_mask is None:
        raise HTTPException(
            status_code=422,
            detail="baseline finding has no segmentation mask; segment the baseline lesion first",
        )

    if dry_run:
        return {
            "status": "dry-run",
            "track_id": str(t.id),
            "followup_series_id": str(body.followup_series_id),
            "refine": body.refine,
        }

    job_result = await jobs_service.enqueue_or_get(
        db,
        kind="propagate_lesion",
        owner_subject_id=user.subject_id,
        canonical_input={
            "track_id": str(t.id),
            "followup_series_id": str(body.followup_series_id),
            "refine": body.refine,
        },
        scope_ids=[str(t.id)],
    )
    if not job_result.deduped:
        settings = get_settings()
        redis = await _create_pool(_redis_settings(settings.redis_url))
        handle = await redis.enqueue_job(
            "propagate_lesion",
            str(t.id),
            str(body.followup_series_id),
            body.refine,
            str(job_result.job.id),
        )
        await redis.close()
        if handle is not None:
            await jobs_service.set_arq_job_id(db, job_result.job.id, handle.job_id)
    await db.commit()
    await audit.log(
        action="lesion_track_propagate",
        actor_subject_id=user.subject_id,
        resource_kind="lesion_track",
        resource_id=t.id,
        metadata={"followup_series_id": str(body.followup_series_id), "refine": body.refine},
    )
    return {"status": "queued", "job_id": str(job_result.job.id), "track_id": str(t.id)}
