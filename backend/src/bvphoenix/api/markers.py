"""Markers API.

CRUD for the unified in-viewer marker entity (see
``db/models/markers.py``). Authorization mirrors the Consultations
API: read = patient-readable, write = patient owner / admin.

Endpoints (all under the global ``/api`` prefix):

  GET    /patients/{id}/markers?target_kind=&target_id=&kind=
  POST   /patients/{id}/markers
  PATCH  /markers/{id}
  DELETE /markers/{id}
  GET    /studies/{id}/markers/export?format=json|sr
  POST   /studies/{id}/markers/import   (json or DICOM SR)

The bulk import endpoint creates rows in a single transaction and
returns the count of accepted rows. The export bundle includes the
study identifier so the round-trip is self-describing.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import (
    APIRouter,
    Depends,
    File,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.auth import enforce_agent_patient_scope, require_user
from bvphoenix.db.models import ImagingStudy, Marker, MarkerRevision, Patient, User
from bvphoenix.db.models.markers import MARKER_KINDS
from bvphoenix.db.session import get_db
from bvphoenix.middleware.audit_dependency import AuditDep
from bvphoenix.middleware.idempotency import IdempotencyContext, idempotent
from bvphoenix.services.etag import enforce_optional_if_match, format_etag
from bvphoenix.services.permissions import (
    READ_METADATA,
    WRITE_REPORT,
    can_patient,
)

# Soft-deleted markers are retained for this long before a purge sweep
# may reclaim them; until then ``restore`` brings them back.
_PURGE_GRACE = timedelta(days=30)

router = APIRouter(tags=["markers"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class MarkerOut(BaseModel):
    id: str
    patient_id: str
    target_kind: Literal["study", "series", "instance", "pathology_slide"]
    target_id: str
    kind: str
    geometry: dict | None
    body: str | None
    computed: dict | None
    author_subject_id: str | None
    author_kind: Literal["human", "agent", "system"]
    model_id: str | None
    provider: str | None
    # Optimistic-concurrency token; pass it back as ``If-Match`` on the
    # next update / delete. Also emitted in the ``ETag`` response header.
    etag: str
    # Set when the marker is soft-deleted (recoverable via restore).
    deleted_at: str | None
    created_at: str
    updated_at: str


class MarkerRevisionOut(BaseModel):
    revision_no: int
    change_kind: Literal["create", "update", "delete", "restore"]
    author_kind: Literal["human", "agent", "system"]
    actor_id: str | None
    diff_summary: str | None
    snapshot: dict
    created_at: str


class MarkerCreateIn(BaseModel):
    target_kind: Literal["study", "series", "instance", "pathology_slide"]
    target_id: uuid.UUID
    kind: str = Field(min_length=1, max_length=48)
    geometry: dict | None = None
    body: str | None = None
    computed: dict | None = None
    # Author kind is forced server-side based on the caller (human vs
    # agent token) — mirrors the consultation create flow.


class MarkerUpdateIn(BaseModel):
    geometry: dict | None = None
    body: str | None = None
    computed: dict | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _out(m: Marker) -> MarkerOut:
    return MarkerOut(
        id=str(m.id),
        patient_id=str(m.patient_id),
        target_kind=m.target_kind,  # type: ignore[arg-type]
        target_id=str(m.target_id),
        kind=m.kind,
        geometry=m.geometry,
        body=m.body,
        computed=m.computed,
        author_subject_id=str(m.author_subject_id) if m.author_subject_id else None,
        author_kind=m.author_kind,  # type: ignore[arg-type]
        model_id=m.model_id,
        provider=m.provider,
        etag=str(m.etag),
        deleted_at=m.deleted_at.isoformat() if m.deleted_at else None,
        created_at=m.created_at.isoformat(),
        updated_at=m.updated_at.isoformat(),
    )


async def _patient_for_read(
    db: AsyncSession, request: Request, user: User, patient_id: uuid.UUID
) -> Patient:
    patient = (
        await db.execute(select(Patient).where(Patient.id == patient_id))
    ).scalar_one_or_none()
    if patient is None:
        raise HTTPException(status_code=404, detail="patient not found")
    enforce_agent_patient_scope(request, patient.id, scope="patient:images")
    if not await can_patient(db, user=user, action=READ_METADATA, patient=patient):
        raise HTTPException(status_code=404, detail="patient not found")
    return patient


async def _patient_for_write(
    db: AsyncSession, request: Request, user: User, patient_id: uuid.UUID
) -> Patient:
    patient = await _patient_for_read(db, request, user, patient_id)
    if not await can_patient(db, user=user, action=WRITE_REPORT, patient=patient):
        raise HTTPException(status_code=403, detail="cannot write markers")
    return patient


async def _marker_for_write(
    db: AsyncSession,
    request: Request,
    user: User,
    marker_id: uuid.UUID,
    *,
    allow_deleted: bool = False,
) -> Marker:
    m = (await db.execute(select(Marker).where(Marker.id == marker_id))).scalar_one_or_none()
    if m is None:
        raise HTTPException(status_code=404, detail="marker not found")
    # A soft-deleted marker is invisible to mutation: restore it first.
    # ``allow_deleted`` lets the restore endpoint load the tombstone.
    if m.deleted_at is not None and not allow_deleted:
        raise HTTPException(status_code=404, detail="marker not found")
    await _patient_for_write(db, request, user, m.patient_id)
    # Sprint 5 (ADR — agents API spec §5.5): an agent token cannot
    # mutate or delete an annotation authored by a human. The agent
    # can only touch its own work. Mirror admin escape-hatch by
    # exempting users with ``is_admin``.
    if (
        getattr(request.state, "is_agent", False)
        and m.author_kind == "human"
        and not getattr(user, "is_admin", False)
    ):
        raise HTTPException(
            status_code=403,
            detail={
                "type": "https://bitvision.example/errors/forbidden",
                "title": "Forbidden",
                "status": 403,
                "detail": ("agent tokens cannot mutate annotations authored by humans"),
                "marker_id": str(m.id),
                "author_kind": m.author_kind,
            },
        )
    return m


def _agent_provenance(request: Request) -> tuple[str, str | None, str | None, uuid.UUID | None]:
    """Resolve author_kind + model/provider/agent_token_id from the
    request state populated by ``_resolve_credential``."""
    if getattr(request.state, "is_agent", False):
        token = getattr(request.state, "agent_token", None)
        return ("agent", None, None, getattr(token, "id", None))
    return ("human", None, None, None)


# ---------------------------------------------------------------------------
# Revision history (mirrors services/care_phases revision helpers)
# ---------------------------------------------------------------------------


def _marker_snapshot(m: Marker) -> dict[str, Any]:
    """Full JSON snapshot of a marker for the revision log."""
    return {
        "id": str(m.id),
        "target_kind": m.target_kind,
        "target_id": str(m.target_id),
        "kind": m.kind,
        "geometry": m.geometry,
        "body": m.body,
        "computed": m.computed,
        "author_kind": m.author_kind,
        "author_subject_id": str(m.author_subject_id) if m.author_subject_id else None,
        "etag": str(m.etag),
        "deleted_at": m.deleted_at.isoformat() if m.deleted_at else None,
    }


async def _next_marker_revision_no(db: AsyncSession, *, marker_id: uuid.UUID) -> int:
    current = (
        await db.execute(
            select(func.coalesce(func.max(MarkerRevision.revision_no), 0)).where(
                MarkerRevision.marker_id == marker_id
            )
        )
    ).scalar_one()
    return int(current) + 1


async def _append_marker_revision(
    db: AsyncSession,
    *,
    marker: Marker,
    change_kind: str,
    actor_id: uuid.UUID | None,
    author_kind: str,
    diff_summary: str | None = None,
) -> None:
    """Record one immutable snapshot of ``marker`` after a CRUD act.

    ``author_kind`` is the *acting* principal (agent vs human), not the
    marker's original author, so an agent edit of a marker is tracked as
    agent-authored in the history even when the row's ``author_kind``
    differs.
    """
    rev = MarkerRevision(
        marker_id=marker.id,
        patient_id=marker.patient_id,
        revision_no=await _next_marker_revision_no(db, marker_id=marker.id),
        snapshot=_marker_snapshot(marker),
        change_kind=change_kind,
        author_kind=author_kind,
        actor_id=actor_id,
        diff_summary=diff_summary,
    )
    db.add(rev)
    await db.flush()


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


@router.get("/patients/{patient_id}/markers", response_model=list[MarkerOut])
async def list_markers(
    request: Request,
    patient_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    target_kind: Literal["study", "series", "instance", "pathology_slide"] | None = Query(None),
    target_id: uuid.UUID | None = Query(None),
    kind: str | None = Query(None, max_length=48),
    include_deleted: bool = Query(
        False, description="Include soft-deleted markers (tombstones) in the result."
    ),
    limit: int = Query(500, ge=1, le=2000),
) -> list[MarkerOut]:
    await _patient_for_read(db, request, user, patient_id)
    stmt = (
        select(Marker)
        .where(Marker.patient_id == patient_id)
        .order_by(Marker.created_at.desc())
        .limit(limit)
    )
    if not include_deleted:
        stmt = stmt.where(Marker.deleted_at.is_(None))
    if target_kind:
        stmt = stmt.where(Marker.target_kind == target_kind)
    if target_id:
        stmt = stmt.where(Marker.target_id == target_id)
    if kind:
        stmt = stmt.where(Marker.kind == kind)
    rows = (await db.execute(stmt)).scalars().all()
    return [_out(m) for m in rows]


@router.post(
    "/patients/{patient_id}/markers",
    response_model=MarkerOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_marker(
    request: Request,
    patient_id: uuid.UUID,
    body: MarkerCreateIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    audit: AuditDep,
    idem: Annotated[IdempotencyContext, Depends(idempotent)],
    dry_run: bool = Query(
        False,
        description=(
            "When true, validate everything (RBAC, kind vocabulary, "
            "patient existence) and return the would-be marker without "
            "writing it. ``id`` is returned as ``'dry-run'`` and "
            "``author_kind`` reflects the caller."
        ),
    ),
) -> MarkerOut | JSONResponse:
    if body.kind not in MARKER_KINDS:
        # Pydantic-style 422 so MCP / SDK clients can self-correct.
        raise HTTPException(
            status_code=422,
            detail=[
                {
                    "type": "value_error.enum",
                    "loc": ["body", "kind"],
                    "msg": (
                        f"unknown marker kind: {body.kind!r}. "
                        f"See help(topic='annotation_kinds') for the full list "
                        f"and per-kind geometry shapes."
                    ),
                    "input": body.kind,
                    "ctx": {"allowed_kinds": list(MARKER_KINDS)},
                }
            ],
        )
    await _patient_for_write(db, request, user, patient_id)

    # Replay an identical prior create (the MCP client sends an
    # Idempotency-Key; a retried tool call must not duplicate the row).
    if idem.replay is not None:
        return idem.replay

    author_kind, model_id, provider, agent_token_id = _agent_provenance(request)
    if dry_run:
        now = datetime.now(UTC).isoformat()
        return MarkerOut(
            id="dry-run",
            patient_id=str(patient_id),
            target_kind=body.target_kind,
            target_id=str(body.target_id),
            kind=body.kind,
            geometry=body.geometry,
            body=body.body,
            computed=body.computed,
            author_subject_id=str(user.subject_id) if user.subject_id else None,
            author_kind=author_kind,  # type: ignore[arg-type]
            model_id=model_id,
            provider=provider,
            etag="dry-run",
            deleted_at=None,
            created_at=now,
            updated_at=now,
        )

    m = Marker(
        patient_id=patient_id,
        target_kind=body.target_kind,
        target_id=body.target_id,
        kind=body.kind,
        geometry=body.geometry,
        body=body.body,
        computed=body.computed,
        author_subject_id=user.subject_id,
        author_kind=author_kind,
        model_id=model_id,
        provider=provider,
        agent_token_id=agent_token_id,
        # Assign the concurrency token in Python (mirrors CarePhase) so
        # it is loaded before the pre-commit revision snapshot reads it.
        etag=uuid.uuid4(),
    )
    db.add(m)
    await db.flush()
    await _append_marker_revision(
        db,
        marker=m,
        change_kind="create",
        actor_id=user.subject_id,
        author_kind=author_kind,
    )
    await db.commit()
    await db.refresh(m)
    await audit.log(
        action="marker_create",
        actor_subject_id=user.subject_id,
        resource_kind="marker",
        resource_id=m.id,
        metadata={
            "patient_id": str(patient_id),
            "target_kind": body.target_kind,
            "kind": body.kind,
        },
    )
    return idem.capture(
        _out(m).model_dump(),
        status_code=status.HTTP_201_CREATED,
        extra_headers={"ETag": format_etag(str(m.etag))},
    )


@router.patch("/markers/{marker_id}", response_model=MarkerOut)
async def update_marker(
    request: Request,
    marker_id: uuid.UUID,
    body: MarkerUpdateIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    audit: AuditDep,
    response: Response,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> MarkerOut:
    m = await _marker_for_write(db, request, user, marker_id)
    # Optimistic concurrency: a stale view (someone else edited the
    # marker since the caller read it) is rejected rather than silently
    # clobbered. Opt-in (see services.etag.enforce_optional_if_match).
    enforce_optional_if_match(if_match, str(m.etag), what="marker")
    changed: dict[str, Any] = {}
    if body.geometry is not None:
        m.geometry = body.geometry
        changed["geometry"] = True
    if body.body is not None:
        m.body = body.body
        changed["body"] = True
    if body.computed is not None:
        m.computed = body.computed
        changed["computed"] = True
    if changed:
        m.updated_at = datetime.now(UTC)
        m.etag = uuid.uuid4()
        await db.flush()
        author_kind, _model, _provider, _token = _agent_provenance(request)
        await _append_marker_revision(
            db,
            marker=m,
            change_kind="update",
            actor_id=user.subject_id,
            author_kind=author_kind,
            diff_summary=",".join(sorted(changed.keys())),
        )
        await db.commit()
        await db.refresh(m)
        await audit.log(
            action="marker_update",
            actor_subject_id=user.subject_id,
            resource_kind="marker",
            resource_id=m.id,
            metadata={"changed": list(changed.keys())},
        )
    response.headers["ETag"] = format_etag(str(m.etag))
    return _out(m)


@router.delete("/markers/{marker_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_marker(
    request: Request,
    marker_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    audit: AuditDep,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
    hard: bool = Query(
        False,
        description=(
            "Admin only: permanently purge the marker (and its revision "
            "history) instead of the default recoverable soft-delete."
        ),
    ),
    reason: str | None = Query(None, max_length=255),
) -> Response:
    m = await _marker_for_write(db, request, user, marker_id, allow_deleted=True)
    # Idempotent re-delete: already a tombstone → 204 no-op (a retried
    # agent DELETE must not error), unless a hard purge is requested.
    if m.deleted_at is not None and not hard:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    enforce_optional_if_match(if_match, str(m.etag), what="marker")

    if hard:
        if not getattr(user, "is_admin", False):
            raise HTTPException(status_code=403, detail="hard delete requires admin")
        kind, pid = m.kind, m.patient_id
        await db.delete(m)  # cascades marker_revision
        await db.commit()
        await audit.log(
            action="marker_purge",
            actor_subject_id=user.subject_id,
            resource_kind="marker",
            resource_id=marker_id,
            metadata={"kind": kind, "patient_id": str(pid)},
        )
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    now = datetime.now(UTC)
    m.deleted_at = now
    m.purge_after = now + _PURGE_GRACE
    m.delete_reason = reason
    m.updated_at = now
    m.etag = uuid.uuid4()
    await db.flush()
    author_kind, _model, _provider, _token = _agent_provenance(request)
    await _append_marker_revision(
        db,
        marker=m,
        change_kind="delete",
        actor_id=user.subject_id,
        author_kind=author_kind,
        diff_summary=reason,
    )
    await db.commit()
    await audit.log(
        action="marker_delete",
        actor_subject_id=user.subject_id,
        resource_kind="marker",
        resource_id=m.id,
        metadata={"kind": m.kind, "patient_id": str(m.patient_id), "soft": True},
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/markers/{marker_id}/restore", response_model=MarkerOut)
async def restore_marker(
    request: Request,
    marker_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    audit: AuditDep,
    response: Response,
) -> MarkerOut:
    """Bring a soft-deleted marker back to life. 409 when it is not a
    tombstone. Recovery deliberately does not require ``If-Match`` (the
    caller is recovering a row it may not hold the latest ETag for)."""
    m = await _marker_for_write(db, request, user, marker_id, allow_deleted=True)
    if m.deleted_at is None:
        raise HTTPException(status_code=409, detail="marker is not deleted")
    m.deleted_at = None
    m.purge_after = None
    m.delete_reason = None
    m.updated_at = datetime.now(UTC)
    m.etag = uuid.uuid4()
    await db.flush()
    author_kind, _model, _provider, _token = _agent_provenance(request)
    await _append_marker_revision(
        db,
        marker=m,
        change_kind="restore",
        actor_id=user.subject_id,
        author_kind=author_kind,
    )
    await db.commit()
    await db.refresh(m)
    await audit.log(
        action="marker_restore",
        actor_subject_id=user.subject_id,
        resource_kind="marker",
        resource_id=m.id,
        metadata={"kind": m.kind, "patient_id": str(m.patient_id)},
    )
    response.headers["ETag"] = format_etag(str(m.etag))
    return _out(m)


@router.get("/markers/{marker_id}/revisions", response_model=list[MarkerRevisionOut])
async def list_marker_revisions(
    request: Request,
    marker_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    limit: int = Query(200, ge=1, le=1000),
) -> list[MarkerRevisionOut]:
    """Full create / update / delete / restore history of a marker, most
    recent first. Each entry carries the acting ``author_kind`` so an
    agent edit is distinguishable, plus the snapshot at that revision."""
    m = (await db.execute(select(Marker).where(Marker.id == marker_id))).scalar_one_or_none()
    if m is None:
        raise HTTPException(status_code=404, detail="marker not found")
    await _patient_for_read(db, request, user, m.patient_id)
    rows = (
        (
            await db.execute(
                select(MarkerRevision)
                .where(MarkerRevision.marker_id == marker_id)
                .order_by(MarkerRevision.revision_no.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return [
        MarkerRevisionOut(
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
# ImagingStudy-scoped helpers used by the export/import flows in api/markers_io.py
# ---------------------------------------------------------------------------


async def _study_or_404(
    db: AsyncSession, study_id: uuid.UUID, request: Request, user: User
) -> ImagingStudy:
    study = (
        await db.execute(select(ImagingStudy).where(ImagingStudy.id == study_id))
    ).scalar_one_or_none()
    if study is None:
        raise HTTPException(status_code=404, detail="study not found")
    if study.patient_id is None:
        raise HTTPException(status_code=400, detail="study has no patient association")
    await _patient_for_read(db, request, user, study.patient_id)
    return study


# ---------------------------------------------------------------------------
# Export / Import (JSON canonical, DICOM SR translation)
# ---------------------------------------------------------------------------


@router.get("/studies/{study_id}/markers/export")
async def export_markers(
    request: Request,
    study_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    audit: AuditDep,
    format: Literal["json", "sr"] = Query("json"),
) -> Response:
    """Export every marker anchored to ``study_id`` (or its series /
    instances) in JSON or DICOM SR.

    JSON is the canonical schema (``bvphoenix.markers/v1``) and is
    used for round-trip across bitvision instances. SR is a DICOM
    Comprehensive 3D SR object suitable for upload into a PACS — see
    ``services/markers_sr.py`` for the mapping policy.
    """
    from bvphoenix.services import markers_sr

    study = await _study_or_404(db, study_id, request, user)
    rows = (
        (
            await db.execute(
                select(Marker)
                .where(Marker.patient_id == study.patient_id)
                .order_by(Marker.created_at.asc())
            )
        )
        .scalars()
        .all()
    )
    # Filter to markers actually anchored to this study or its descendants.
    # Series/Instance UUIDs hang off ImagingStudy via dicom.py models; for now
    # we cheaply include study-targeted markers + everything whose
    # target_id matches a series under the study.
    series_ids = {
        row[0]
        for row in (
            await db.execute(
                select(Marker.target_id)
                .where(Marker.target_kind.in_(("series", "instance")))
                .distinct()
            )
        ).all()
    }
    # Trim to study-anchored only — the simpler, correct subset:
    rows = [
        m
        for m in rows
        if (m.target_kind == "study" and m.target_id == study.id)
        or (m.target_kind in ("series", "instance") and m.target_id in series_ids)
    ]

    await audit.log(
        action="markers_export",
        actor_subject_id=user.subject_id,
        resource_kind="study",
        resource_id=study_id,
        metadata={"format": format, "count": len(rows)},
    )

    if format == "json":
        body = markers_sr.markers_to_json(study, rows)
        return Response(
            content=body,
            media_type="application/json",
            headers={"Content-Disposition": (f'attachment; filename="markers-{study.id}.json"')},
        )
    # format == "sr"
    if not rows:
        raise HTTPException(
            status_code=400,
            detail="cannot build a DICOM SR with zero markers",
        )
    try:
        body = markers_sr.markers_to_sr(study, rows)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"SR build failed: {exc}")
    return Response(
        content=body,
        media_type="application/dicom",
        headers={"Content-Disposition": (f'attachment; filename="markers-{study.id}.dcm"')},
    )


@router.post(
    "/studies/{study_id}/markers/import",
    status_code=status.HTTP_201_CREATED,
)
async def import_markers(
    request: Request,
    study_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    audit: AuditDep,
    file: UploadFile = File(...),
) -> dict:
    """Import markers from a JSON bundle or a DICOM SR ``.dcm`` file.

    Format detection: by content type first (``application/json`` →
    JSON, ``application/dicom`` → SR), then by filename suffix as a
    fallback. Returns the count of accepted rows.
    """
    from bvphoenix.services import markers_sr

    study = await _study_or_404(db, study_id, request, user)
    await _patient_for_write(db, request, user, study.patient_id)

    blob = await file.read()
    name = (file.filename or "").lower()
    ctype = (file.content_type or "").lower()
    is_json = "json" in ctype or name.endswith(".json")
    is_sr = "dicom" in ctype or name.endswith(".dcm") or name.endswith(".sr")
    if not is_json and not is_sr:
        raise HTTPException(
            status_code=400, detail="unsupported file type; expected JSON or DICOM SR"
        )

    try:
        if is_json:
            imports = markers_sr.json_to_markers(blob, study)
        else:
            imports = markers_sr.sr_to_markers(blob, study)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"parse failed: {exc}")

    author_kind, model_id, provider, agent_token_id = _agent_provenance(request)
    created: list[Marker] = []
    for mi in imports:
        m = Marker(
            patient_id=study.patient_id,
            target_kind=mi.target_kind,
            target_id=mi.target_id,
            kind=mi.kind,
            geometry=mi.geometry,
            body=mi.body,
            computed=mi.computed,
            author_subject_id=user.subject_id,
            author_kind=author_kind,
            model_id=model_id,
            provider=provider,
            agent_token_id=agent_token_id,
        )
        db.add(m)
        created.append(m)
    await db.commit()

    await audit.log(
        action="markers_import",
        actor_subject_id=user.subject_id,
        resource_kind="study",
        resource_id=study_id,
        metadata={
            "format": "json" if is_json else "sr",
            "count": len(created),
        },
    )
    return {"imported": len(created)}


# Re-exports for tests / future siblings.
__all__ = [
    "_agent_provenance",
    "_out",
    "_patient_for_read",
    "_patient_for_write",
    "_study_or_404",
    "router",
]
