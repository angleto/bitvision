"""Findings API — CRUD + structured query over the coded clinical reperto.

P2 of the annotation-layer overhaul (Flow note 76949928). Exposes the
``Finding`` entity (``db/models/findings.py``) so a diagnosis can be
recorded as structured, coded, measured data and queried by type / site /
morphology / size / SUV. Conventions mirror the markers API (the helpers
are reused, not duplicated): patient-scoped RBAC, agent provenance,
Idempotency-Key on create, optional If-Match on mutate, soft-delete +
restore, and a full revision history.

Endpoints (under ``/api``):

  GET    /findings/vocab
  POST   /patients/{id}/findings
  GET    /patients/{id}/findings        (structured filters)
  GET    /findings/{id}
  PATCH  /findings/{id}
  DELETE /findings/{id}
  POST   /findings/{id}/restore
  GET    /findings/{id}/revisions
  POST   /findings/{id}/geometry
  DELETE /findings/{id}/geometry/{geometry_id}

The cross-corpus ``/search`` integration and the MCP ``search_finding*``
tools are P3.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
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
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.api.markers import (
    _agent_provenance,
    _patient_for_read,
    _patient_for_write,
    _study_or_404,
)
from bvphoenix.auth import require_user
from bvphoenix.db.models import (
    AnatomySite,
    Finding,
    FindingGeometry,
    FindingRevision,
    FindingType,
    ImagingStudy,
    Marker,
    MorphologyTerm,
    Segmentation,
    User,
)
from bvphoenix.db.session import get_db
from bvphoenix.middleware.audit_dependency import AuditDep
from bvphoenix.middleware.idempotency import IdempotencyContext, idempotent
from bvphoenix.services.etag import enforce_optional_if_match, format_etag
from bvphoenix.services.permissions import apply_scope_filter, visible_studies_filter
from bvphoenix.services.text_embedding import enqueue_text_embed

router = APIRouter(tags=["findings"])

_PURGE_GRACE = timedelta(days=30)

# Typed measurement columns a caller may set (kept in one place so create
# / update / snapshot stay in sync).
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


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class VocabTermOut(BaseModel):
    id: str
    key: str
    display: str
    code_system: str | None
    code: str | None


class FindingTypeOut(VocabTermOut):
    category: str


class AnatomySiteOut(VocabTermOut):
    parent_id: str | None
    laterality_applicable: bool


class VocabOut(BaseModel):
    finding_types: list[FindingTypeOut]
    anatomy_sites: list[AnatomySiteOut]
    morphology_terms: list[VocabTermOut]


class GeometryRefIn(BaseModel):
    marker_id: uuid.UUID | None = None
    segmentation_id: uuid.UUID | None = None
    role: Literal["measurement", "bbox", "mask", "fiducial"]

    @model_validator(mode="after")
    def _one_target(self) -> GeometryRefIn:
        if self.marker_id is None and self.segmentation_id is None:
            raise ValueError("geometry ref must set marker_id or segmentation_id")
        return self


class FindingGeometryOut(BaseModel):
    id: str
    marker_id: str | None
    segmentation_id: str | None
    role: str


class _MeasurementsMixin(BaseModel):
    longest_diameter_mm: float | None = None
    short_axis_mm: float | None = None
    volume_ml: float | None = None
    suv_max: float | None = None
    suv_peak: float | None = None
    suv_mean: float | None = None
    hu_mean: float | None = None
    hu_std: float | None = None


class FindingCreateIn(_MeasurementsMixin):
    study_id: uuid.UUID
    series_id: uuid.UUID | None = None
    frame_of_reference_uid: str | None = Field(default=None, max_length=128)
    # Controlled-vocabulary slugs (validated against the vocab tables).
    type: str = Field(min_length=1, max_length=64)
    anatomy: str | None = Field(default=None, max_length=64)
    laterality: Literal["left", "right", "bilateral", "midline"] | None = None
    morphology: list[str] = Field(default_factory=list)
    bbox_lps: dict | None = None
    status: Literal["candidate", "confirmed", "retracted"] = "candidate"
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    description: str | None = None
    geometry_refs: list[GeometryRefIn] = Field(default_factory=list)


class FindingUpdateIn(_MeasurementsMixin):
    # PATCH semantics: only fields present in the payload are changed
    # (resolved via ``model_fields_set``). ``anatomy``/``laterality`` may
    # be set to null explicitly.
    type: str | None = Field(default=None, max_length=64)
    anatomy: str | None = Field(default=None, max_length=64)
    laterality: Literal["left", "right", "bilateral", "midline"] | None = None
    morphology: list[str] | None = None
    bbox_lps: dict | None = None
    status: Literal["candidate", "confirmed", "retracted"] | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    description: str | None = None


class FindingOut(BaseModel):
    id: str
    patient_id: str
    study_id: str
    series_id: str | None
    frame_of_reference_uid: str | None
    finding_type_id: str
    type: str
    anatomy_site_id: str | None
    anatomy: str | None
    laterality: str | None
    morphology: list[str]
    longest_diameter_mm: float | None
    short_axis_mm: float | None
    volume_ml: float | None
    suv_max: float | None
    suv_peak: float | None
    suv_mean: float | None
    hu_mean: float | None
    hu_std: float | None
    bbox_lps: dict | None
    status: str
    confidence: float | None
    description: str | None
    author_subject_id: str | None
    author_kind: Literal["human", "agent", "system"]
    model_id: str | None
    provider: str | None
    etag: str
    deleted_at: str | None
    created_at: str
    updated_at: str
    geometry: list[FindingGeometryOut]


class FindingRevisionOut(BaseModel):
    revision_no: int
    change_kind: Literal["create", "update", "delete", "restore"]
    author_kind: Literal["human", "agent", "system"]
    actor_id: str | None
    diff_summary: str | None
    snapshot: dict
    created_at: str


# ---------------------------------------------------------------------------
# Vocabulary helpers
# ---------------------------------------------------------------------------


async def _resolve_type(db: AsyncSession, key: str) -> FindingType:
    row = (await db.execute(select(FindingType).where(FindingType.key == key))).scalar_one_or_none()
    if row is None:
        raise HTTPException(
            status_code=422,
            detail=[
                {
                    "type": "value_error.enum",
                    "loc": ["body", "type"],
                    "msg": f"unknown finding type: {key!r}. GET /api/findings/vocab for the list.",
                    "input": key,
                }
            ],
        )
    return row


async def _resolve_anatomy(db: AsyncSession, key: str | None) -> AnatomySite | None:
    if key is None:
        return None
    row = (await db.execute(select(AnatomySite).where(AnatomySite.key == key))).scalar_one_or_none()
    if row is None:
        raise HTTPException(
            status_code=422,
            detail=[
                {
                    "type": "value_error.enum",
                    "loc": ["body", "anatomy"],
                    "msg": f"unknown anatomy site: {key!r}. GET /api/findings/vocab for the list.",
                    "input": key,
                }
            ],
        )
    return row


async def _validate_morphology(db: AsyncSession, keys: list[str]) -> list[str]:
    if not keys:
        return []
    deduped = sorted(set(keys))
    known = set(
        (await db.execute(select(MorphologyTerm.key).where(MorphologyTerm.key.in_(deduped))))
        .scalars()
        .all()
    )
    unknown = [k for k in deduped if k not in known]
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=[
                {
                    "type": "value_error.enum",
                    "loc": ["body", "morphology"],
                    "msg": f"unknown morphology terms: {unknown}. GET /api/findings/vocab.",
                    "input": unknown,
                }
            ],
        )
    return deduped


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


async def _geometry_for(db: AsyncSession, finding_ids: list[uuid.UUID]) -> dict[uuid.UUID, list]:
    if not finding_ids:
        return {}
    rows = (
        (
            await db.execute(
                select(FindingGeometry).where(FindingGeometry.finding_id.in_(finding_ids))
            )
        )
        .scalars()
        .all()
    )
    out: dict[uuid.UUID, list] = {}
    for g in rows:
        out.setdefault(g.finding_id, []).append(
            FindingGeometryOut(
                id=str(g.id),
                marker_id=str(g.marker_id) if g.marker_id else None,
                segmentation_id=str(g.segmentation_id) if g.segmentation_id else None,
                role=g.role,
            )
        )
    return out


def _out(
    f: Finding,
    *,
    type_key: str,
    anatomy_key: str | None,
    geometry: list[FindingGeometryOut],
) -> FindingOut:
    return FindingOut(
        id=str(f.id),
        patient_id=str(f.patient_id),
        study_id=str(f.study_id),
        series_id=str(f.series_id) if f.series_id else None,
        frame_of_reference_uid=f.frame_of_reference_uid,
        finding_type_id=str(f.finding_type_id),
        type=type_key,
        anatomy_site_id=str(f.anatomy_site_id) if f.anatomy_site_id else None,
        anatomy=anatomy_key,
        laterality=f.laterality,
        morphology=list(f.morphology_keys or []),
        longest_diameter_mm=f.longest_diameter_mm,
        short_axis_mm=f.short_axis_mm,
        volume_ml=f.volume_ml,
        suv_max=f.suv_max,
        suv_peak=f.suv_peak,
        suv_mean=f.suv_mean,
        hu_mean=f.hu_mean,
        hu_std=f.hu_std,
        bbox_lps=f.bbox_lps,
        status=f.status,
        confidence=f.confidence,
        description=f.description,
        author_subject_id=str(f.author_subject_id) if f.author_subject_id else None,
        author_kind=f.author_kind,  # type: ignore[arg-type]
        model_id=f.model_id,
        provider=f.provider,
        etag=str(f.etag),
        deleted_at=f.deleted_at.isoformat() if f.deleted_at else None,
        created_at=f.created_at.isoformat(),
        updated_at=f.updated_at.isoformat(),
        geometry=geometry,
    )


async def _serialize_one(db: AsyncSession, f: Finding) -> FindingOut:
    type_key = (
        await db.execute(select(FindingType.key).where(FindingType.id == f.finding_type_id))
    ).scalar_one()
    anatomy_key = None
    if f.anatomy_site_id is not None:
        anatomy_key = (
            await db.execute(select(AnatomySite.key).where(AnatomySite.id == f.anatomy_site_id))
        ).scalar_one_or_none()
    geom = (await _geometry_for(db, [f.id])).get(f.id, [])
    return _out(f, type_key=type_key, anatomy_key=anatomy_key, geometry=geom)


# ---------------------------------------------------------------------------
# Write helpers (load / gate / revision)
# ---------------------------------------------------------------------------


async def _finding_for_write(
    db: AsyncSession,
    request: Request,
    user: User,
    finding_id: uuid.UUID,
    *,
    allow_deleted: bool = False,
) -> Finding:
    f = (await db.execute(select(Finding).where(Finding.id == finding_id))).scalar_one_or_none()
    if f is None:
        raise HTTPException(status_code=404, detail="finding not found")
    if f.deleted_at is not None and not allow_deleted:
        raise HTTPException(status_code=404, detail="finding not found")
    await _patient_for_write(db, request, user, f.patient_id)
    # Agents cannot mutate a human-authored finding (mirrors markers).
    if (
        getattr(request.state, "is_agent", False)
        and f.author_kind == "human"
        and not getattr(user, "is_admin", False)
    ):
        raise HTTPException(
            status_code=403,
            detail={
                "type": "https://bitvision.example/errors/forbidden",
                "title": "Forbidden",
                "status": 403,
                "detail": "agent tokens cannot mutate findings authored by humans",
                "finding_id": str(f.id),
                "author_kind": f.author_kind,
            },
        )
    return f


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
        "confidence": f.confidence,
        "author_kind": f.author_kind,
        "etag": str(f.etag),
        "deleted_at": f.deleted_at.isoformat() if f.deleted_at else None,
    }


async def _append_finding_revision(
    db: AsyncSession,
    *,
    finding: Finding,
    change_kind: str,
    actor_id: uuid.UUID | None,
    author_kind: str,
    diff_summary: str | None = None,
) -> None:
    next_no = (
        await db.execute(
            select(func.coalesce(func.max(FindingRevision.revision_no), 0)).where(
                FindingRevision.finding_id == finding.id
            )
        )
    ).scalar_one()
    db.add(
        FindingRevision(
            finding_id=finding.id,
            patient_id=finding.patient_id,
            revision_no=int(next_no) + 1,
            snapshot=_finding_snapshot(finding),
            change_kind=change_kind,
            author_kind=author_kind,
            actor_id=actor_id,
            diff_summary=diff_summary,
        )
    )
    await db.flush()


async def _link_geometry(
    db: AsyncSession,
    *,
    finding: Finding,
    refs: list[GeometryRefIn],
) -> None:
    """Validate + persist geometry links. A linked marker / segmentation
    must belong to the finding's patient (no cross-patient linkage)."""
    for ref in refs:
        if ref.marker_id is not None:
            m = (
                await db.execute(select(Marker).where(Marker.id == ref.marker_id))
            ).scalar_one_or_none()
            if m is None or m.patient_id != finding.patient_id:
                raise HTTPException(
                    status_code=422,
                    detail=f"marker {ref.marker_id} not found for this patient",
                )
        if ref.segmentation_id is not None:
            seg = (
                await db.execute(select(Segmentation).where(Segmentation.id == ref.segmentation_id))
            ).scalar_one_or_none()
            if seg is None:
                raise HTTPException(
                    status_code=422, detail=f"segmentation {ref.segmentation_id} not found"
                )
        db.add(
            FindingGeometry(
                finding_id=finding.id,
                marker_id=ref.marker_id,
                segmentation_id=ref.segmentation_id,
                role=ref.role,
            )
        )


def _finding_embed_text(
    *,
    type_display: str,
    anatomy_display: str | None,
    laterality: str | None,
    morphology: list[str],
    description: str | None,
) -> str:
    """Compose the natural-language string embedded for semantic search."""
    parts = [type_display]
    if anatomy_display:
        parts.append(anatomy_display + (f" {laterality}" if laterality else ""))
    if morphology:
        parts.append(", ".join(morphology))
    text = "; ".join(p for p in parts if p)
    if description:
        text = f"{text}. {description}" if text else description
    return text.strip()


async def _enqueue_finding_embed(db: AsyncSession, finding: Finding) -> None:
    """Best-effort: embed the finding's text under target_kind='finding' on
    every active text model (MiniLM today, and BGE-M3 once it is activated)
    so it joins /search/semantic. Mirrors the document OCR enqueue: a
    failure here must never break the write."""
    try:
        type_display = (
            await db.execute(
                select(FindingType.display).where(FindingType.id == finding.finding_type_id)
            )
        ).scalar_one_or_none() or ""
        anatomy_display: str | None = None
        if finding.anatomy_site_id is not None:
            anatomy_display = (
                await db.execute(
                    select(AnatomySite.display).where(AnatomySite.id == finding.anatomy_site_id)
                )
            ).scalar_one_or_none()
        text_value = _finding_embed_text(
            type_display=type_display,
            anatomy_display=anatomy_display,
            laterality=finding.laterality,
            morphology=list(finding.morphology_keys or []),
            description=finding.description,
        )
        if not text_value:
            return
        await enqueue_text_embed(db, target_kind="finding", target_id=finding.id, text=text_value)
    except Exception:  # pragma: no cover — best-effort, never break the write
        logging.getLogger(__name__).exception("finding embed enqueue failed for %s", finding.id)


# ---------------------------------------------------------------------------
# Vocabulary endpoint
# ---------------------------------------------------------------------------


@router.get("/findings/vocab", response_model=VocabOut)
async def get_vocab(
    db: Annotated[AsyncSession, Depends(get_db)],
    _user: Annotated[User, Depends(require_user)],
) -> VocabOut:
    """The controlled vocabularies a finding draws on. Pickers (UI) and
    agents read this to know the allowed type / anatomy / morphology slugs
    and their optional external codes."""
    types = (await db.execute(select(FindingType).order_by(FindingType.key))).scalars().all()
    sites = (await db.execute(select(AnatomySite).order_by(AnatomySite.key))).scalars().all()
    morphs = (await db.execute(select(MorphologyTerm).order_by(MorphologyTerm.key))).scalars().all()
    return VocabOut(
        finding_types=[
            FindingTypeOut(
                id=str(t.id),
                key=t.key,
                display=t.display,
                code_system=t.code_system,
                code=t.code,
                category=t.category,
            )
            for t in types
        ],
        anatomy_sites=[
            AnatomySiteOut(
                id=str(s.id),
                key=s.key,
                display=s.display,
                code_system=s.code_system,
                code=s.code,
                parent_id=str(s.parent_id) if s.parent_id else None,
                laterality_applicable=s.laterality_applicable,
            )
            for s in sites
        ],
        morphology_terms=[
            VocabTermOut(
                id=str(m.id),
                key=m.key,
                display=m.display,
                code_system=m.code_system,
                code=m.code,
            )
            for m in morphs
        ],
    )


# ---------------------------------------------------------------------------
# Structured query (shared filter helper + corpus-wide search)
# ---------------------------------------------------------------------------


def _apply_structured_filters(
    stmt: Select,
    *,
    type: str | None,
    anatomy: str | None,
    laterality: str | None,
    morphology: list[str] | None,
    status_filter: str | None,
    min_diameter_mm: float | None,
    max_diameter_mm: float | None,
    min_volume_ml: float | None,
    min_suv_max: float | None,
) -> Select:
    """Apply the shared structured-query predicates. ``stmt`` must already
    join FindingType (+ outerjoin AnatomySite) so the key filters resolve.
    Used by both the patient-scoped list and the corpus search."""
    if type:
        stmt = stmt.where(FindingType.key == type)
    if anatomy:
        stmt = stmt.where(AnatomySite.key == anatomy)
    if laterality:
        stmt = stmt.where(Finding.laterality == laterality)
    if morphology:
        # ARRAY contains: the finding must carry every requested slug.
        stmt = stmt.where(Finding.morphology_keys.contains(morphology))
    if status_filter:
        stmt = stmt.where(Finding.status == status_filter)
    if min_diameter_mm is not None:
        stmt = stmt.where(Finding.longest_diameter_mm >= min_diameter_mm)
    if max_diameter_mm is not None:
        stmt = stmt.where(Finding.longest_diameter_mm <= max_diameter_mm)
    if min_volume_ml is not None:
        stmt = stmt.where(Finding.volume_ml >= min_volume_ml)
    if min_suv_max is not None:
        stmt = stmt.where(Finding.suv_max >= min_suv_max)
    return stmt


@router.get("/findings/search", response_model=list[FindingOut])
async def search_findings(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    type: str | None = Query(None, max_length=64, description="finding type key"),
    anatomy: str | None = Query(None, max_length=64, description="anatomy site key"),
    laterality: str | None = Query(None, max_length=8),
    morphology: list[str] | None = Query(None, description="require ALL of these morphology keys"),
    status_filter: str | None = Query(None, alias="status"),
    min_diameter_mm: float | None = Query(None, ge=0),
    max_diameter_mm: float | None = Query(None, ge=0),
    min_volume_ml: float | None = Query(None, ge=0),
    min_suv_max: float | None = Query(None, ge=0),
    scope: Literal["all", "mine", "public"] = Query("all"),
    limit: int = Query(200, ge=1, le=1000),
) -> list[FindingOut]:
    """Corpus-wide structured query over every finding on a study the
    caller may read (visibility-scoped via ``visible_studies_filter``; no
    cross-patient leak). The payoff of the typed/coded columns: "all my
    exams with a spiculated nodule > 1 cm in the RUL". Live findings only.
    """
    visible = apply_scope_filter(await visible_studies_filter(db, user), scope, user)
    visible_ids = visible.with_only_columns(ImagingStudy.id).subquery()
    stmt = (
        select(Finding, FindingType.key, AnatomySite.key)
        .join(FindingType, FindingType.id == Finding.finding_type_id)
        .outerjoin(AnatomySite, AnatomySite.id == Finding.anatomy_site_id)
        .where(
            Finding.study_id.in_(select(visible_ids.c.id)),
            Finding.deleted_at.is_(None),
        )
        .order_by(Finding.created_at.desc())
        .limit(limit)
    )
    stmt = _apply_structured_filters(
        stmt,
        type=type,
        anatomy=anatomy,
        laterality=laterality,
        morphology=morphology,
        status_filter=status_filter,
        min_diameter_mm=min_diameter_mm,
        max_diameter_mm=max_diameter_mm,
        min_volume_ml=min_volume_ml,
        min_suv_max=min_suv_max,
    )
    rows = (await db.execute(stmt)).all()
    geom = await _geometry_for(db, [r[0].id for r in rows])
    return [
        _out(f, type_key=tk, anatomy_key=ak, geometry=geom.get(f.id, [])) for (f, tk, ak) in rows
    ]


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


@router.post(
    "/patients/{patient_id}/findings",
    response_model=FindingOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_finding(
    request: Request,
    patient_id: uuid.UUID,
    body: FindingCreateIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    audit: AuditDep,
    idem: Annotated[IdempotencyContext, Depends(idempotent)],
    dry_run: bool = Query(False, description="Validate + return the would-be finding, no write."),
) -> FindingOut | JSONResponse:
    patient = await _patient_for_write(db, request, user, patient_id)
    study = await _study_or_404(db, body.study_id, request, user)
    if study.patient_id != patient.id:
        raise HTTPException(status_code=404, detail="study not found for this patient")
    if idem.replay is not None:
        return idem.replay

    ftype = await _resolve_type(db, body.type)
    anatomy = await _resolve_anatomy(db, body.anatomy)
    morphology = await _validate_morphology(db, body.morphology)
    author_kind, model_id, provider, agent_token_id = _agent_provenance(request)

    if dry_run:
        now = datetime.now(UTC).isoformat()
        return FindingOut(
            id="dry-run",
            patient_id=str(patient_id),
            study_id=str(body.study_id),
            series_id=str(body.series_id) if body.series_id else None,
            frame_of_reference_uid=body.frame_of_reference_uid,
            finding_type_id=str(ftype.id),
            type=ftype.key,
            anatomy_site_id=str(anatomy.id) if anatomy else None,
            anatomy=anatomy.key if anatomy else None,
            laterality=body.laterality,
            morphology=morphology,
            **{k: getattr(body, k) for k in _MEASUREMENT_FIELDS},
            bbox_lps=body.bbox_lps,
            status=body.status,
            confidence=body.confidence,
            description=body.description,
            author_subject_id=str(user.subject_id) if user.subject_id else None,
            author_kind=author_kind,  # type: ignore[arg-type]
            model_id=model_id,
            provider=provider,
            etag="dry-run",
            deleted_at=None,
            created_at=now,
            updated_at=now,
            geometry=[],
        )

    f = Finding(
        patient_id=patient_id,
        study_id=body.study_id,
        series_id=body.series_id,
        frame_of_reference_uid=body.frame_of_reference_uid,
        finding_type_id=ftype.id,
        anatomy_site_id=anatomy.id if anatomy else None,
        laterality=body.laterality,
        morphology_keys=morphology,
        bbox_lps=body.bbox_lps,
        status=body.status,
        confidence=body.confidence,
        description=body.description,
        author_subject_id=user.subject_id,
        author_kind=author_kind,
        model_id=model_id,
        provider=provider,
        agent_token_id=agent_token_id,
        etag=uuid.uuid4(),
        **{k: getattr(body, k) for k in _MEASUREMENT_FIELDS},
    )
    db.add(f)
    await db.flush()
    await _link_geometry(db, finding=f, refs=body.geometry_refs)
    await _append_finding_revision(
        db, finding=f, change_kind="create", actor_id=user.subject_id, author_kind=author_kind
    )
    await db.commit()
    await db.refresh(f)
    await audit.log(
        action="finding_create",
        actor_subject_id=user.subject_id,
        resource_kind="finding",
        resource_id=f.id,
        metadata={"patient_id": str(patient_id), "type": ftype.key, "status": f.status},
    )
    await _enqueue_finding_embed(db, f)
    out = await _serialize_one(db, f)
    return idem.capture(
        out.model_dump(),
        status_code=status.HTTP_201_CREATED,
        extra_headers={"ETag": format_etag(str(f.etag))},
    )


@router.get("/patients/{patient_id}/findings", response_model=list[FindingOut])
async def list_findings(
    request: Request,
    patient_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    study_id: uuid.UUID | None = Query(None),
    type: str | None = Query(None, max_length=64, description="finding type key"),
    anatomy: str | None = Query(None, max_length=64, description="anatomy site key"),
    laterality: str | None = Query(None, max_length=8),
    morphology: list[str] | None = Query(None, description="require ALL of these morphology keys"),
    status_filter: str | None = Query(None, alias="status"),
    min_diameter_mm: float | None = Query(None, ge=0),
    max_diameter_mm: float | None = Query(None, ge=0),
    min_volume_ml: float | None = Query(None, ge=0),
    min_suv_max: float | None = Query(None, ge=0),
    include_deleted: bool = Query(False),
    limit: int = Query(200, ge=1, le=1000),
) -> list[FindingOut]:
    """Structured query over a patient's findings: the payoff of the
    typed/indexed columns ("liver lesion > 2 cm and spiculated")."""
    await _patient_for_read(db, request, user, patient_id)
    stmt = (
        select(Finding, FindingType.key, AnatomySite.key)
        .join(FindingType, FindingType.id == Finding.finding_type_id)
        .outerjoin(AnatomySite, AnatomySite.id == Finding.anatomy_site_id)
        .where(Finding.patient_id == patient_id)
        .order_by(Finding.created_at.desc())
        .limit(limit)
    )
    if not include_deleted:
        stmt = stmt.where(Finding.deleted_at.is_(None))
    if study_id:
        stmt = stmt.where(Finding.study_id == study_id)
    stmt = _apply_structured_filters(
        stmt,
        type=type,
        anatomy=anatomy,
        laterality=laterality,
        morphology=morphology,
        status_filter=status_filter,
        min_diameter_mm=min_diameter_mm,
        max_diameter_mm=max_diameter_mm,
        min_volume_ml=min_volume_ml,
        min_suv_max=min_suv_max,
    )

    rows = (await db.execute(stmt)).all()
    findings = [r[0] for r in rows]
    geom = await _geometry_for(db, [f.id for f in findings])
    return [
        _out(f, type_key=tk, anatomy_key=ak, geometry=geom.get(f.id, [])) for (f, tk, ak) in rows
    ]


@router.get("/findings/{finding_id}", response_model=FindingOut)
async def get_finding(
    request: Request,
    finding_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    response: Response,
) -> FindingOut:
    f = (await db.execute(select(Finding).where(Finding.id == finding_id))).scalar_one_or_none()
    if f is None:
        raise HTTPException(status_code=404, detail="finding not found")
    await _patient_for_read(db, request, user, f.patient_id)
    response.headers["ETag"] = format_etag(str(f.etag))
    return await _serialize_one(db, f)


@router.patch("/findings/{finding_id}", response_model=FindingOut)
async def update_finding(
    request: Request,
    finding_id: uuid.UUID,
    body: FindingUpdateIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    audit: AuditDep,
    response: Response,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> FindingOut:
    f = await _finding_for_write(db, request, user, finding_id)
    enforce_optional_if_match(if_match, str(f.etag), what="finding")
    fields = body.model_fields_set
    changed: list[str] = []

    if "type" in fields and body.type is not None:
        f.finding_type_id = (await _resolve_type(db, body.type)).id
        changed.append("type")
    if "anatomy" in fields:
        anatomy = await _resolve_anatomy(db, body.anatomy)
        f.anatomy_site_id = anatomy.id if anatomy else None
        changed.append("anatomy")
    if "laterality" in fields:
        f.laterality = body.laterality
        changed.append("laterality")
    if "morphology" in fields and body.morphology is not None:
        f.morphology_keys = await _validate_morphology(db, body.morphology)
        changed.append("morphology")
    for m in _MEASUREMENT_FIELDS:
        if m in fields:
            setattr(f, m, getattr(body, m))
            changed.append(m)
    for attr in ("bbox_lps", "status", "confidence", "description"):
        if attr in fields:
            setattr(f, attr, getattr(body, attr))
            changed.append(attr)

    if changed:
        f.updated_at = datetime.now(UTC)
        f.etag = uuid.uuid4()
        await db.flush()
        author_kind, _m, _p, _t = _agent_provenance(request)
        await _append_finding_revision(
            db,
            finding=f,
            change_kind="update",
            actor_id=user.subject_id,
            author_kind=author_kind,
            diff_summary=",".join(sorted(set(changed))),
        )
        await db.commit()
        await db.refresh(f)
        await audit.log(
            action="finding_update",
            actor_subject_id=user.subject_id,
            resource_kind="finding",
            resource_id=f.id,
            metadata={"changed": sorted(set(changed))},
        )
        await _enqueue_finding_embed(db, f)
    response.headers["ETag"] = format_etag(str(f.etag))
    return await _serialize_one(db, f)


@router.delete("/findings/{finding_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_finding(
    request: Request,
    finding_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    audit: AuditDep,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
    hard: bool = Query(False, description="Admin only: permanently purge instead of soft-delete."),
    reason: str | None = Query(None, max_length=255),
) -> Response:
    f = await _finding_for_write(db, request, user, finding_id, allow_deleted=True)
    if f.deleted_at is not None and not hard:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    enforce_optional_if_match(if_match, str(f.etag), what="finding")

    if hard:
        if not getattr(user, "is_admin", False):
            raise HTTPException(status_code=403, detail="hard delete requires admin")
        fid, pid = f.id, f.patient_id
        await db.delete(f)
        await db.commit()
        await audit.log(
            action="finding_purge",
            actor_subject_id=user.subject_id,
            resource_kind="finding",
            resource_id=fid,
            metadata={"patient_id": str(pid)},
        )
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    now = datetime.now(UTC)
    f.deleted_at = now
    f.purge_after = now + _PURGE_GRACE
    f.delete_reason = reason
    f.updated_at = now
    f.etag = uuid.uuid4()
    await db.flush()
    author_kind, _m, _p, _t = _agent_provenance(request)
    await _append_finding_revision(
        db,
        finding=f,
        change_kind="delete",
        actor_id=user.subject_id,
        author_kind=author_kind,
        diff_summary=reason,
    )
    await db.commit()
    await audit.log(
        action="finding_delete",
        actor_subject_id=user.subject_id,
        resource_kind="finding",
        resource_id=f.id,
        metadata={"patient_id": str(f.patient_id), "soft": True},
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/findings/{finding_id}/restore", response_model=FindingOut)
async def restore_finding(
    request: Request,
    finding_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    audit: AuditDep,
    response: Response,
) -> FindingOut:
    f = await _finding_for_write(db, request, user, finding_id, allow_deleted=True)
    if f.deleted_at is None:
        raise HTTPException(status_code=409, detail="finding is not deleted")
    f.deleted_at = None
    f.purge_after = None
    f.delete_reason = None
    f.updated_at = datetime.now(UTC)
    f.etag = uuid.uuid4()
    await db.flush()
    author_kind, _m, _p, _t = _agent_provenance(request)
    await _append_finding_revision(
        db, finding=f, change_kind="restore", actor_id=user.subject_id, author_kind=author_kind
    )
    await db.commit()
    await db.refresh(f)
    await audit.log(
        action="finding_restore",
        actor_subject_id=user.subject_id,
        resource_kind="finding",
        resource_id=f.id,
        metadata={"patient_id": str(f.patient_id)},
    )
    response.headers["ETag"] = format_etag(str(f.etag))
    return await _serialize_one(db, f)


@router.get("/findings/{finding_id}/revisions", response_model=list[FindingRevisionOut])
async def list_finding_revisions(
    request: Request,
    finding_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    limit: int = Query(200, ge=1, le=1000),
) -> list[FindingRevisionOut]:
    f = (await db.execute(select(Finding).where(Finding.id == finding_id))).scalar_one_or_none()
    if f is None:
        raise HTTPException(status_code=404, detail="finding not found")
    await _patient_for_read(db, request, user, f.patient_id)
    rows = (
        (
            await db.execute(
                select(FindingRevision)
                .where(FindingRevision.finding_id == finding_id)
                .order_by(FindingRevision.revision_no.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return [
        FindingRevisionOut(
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


@router.post("/findings/{finding_id}/geometry", response_model=FindingOut)
async def add_finding_geometry(
    request: Request,
    finding_id: uuid.UUID,
    body: GeometryRefIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    audit: AuditDep,
    response: Response,
) -> FindingOut:
    f = await _finding_for_write(db, request, user, finding_id)
    await _link_geometry(db, finding=f, refs=[body])
    await db.commit()
    await db.refresh(f)
    await audit.log(
        action="finding_geometry_add",
        actor_subject_id=user.subject_id,
        resource_kind="finding",
        resource_id=f.id,
        metadata={"role": body.role},
    )
    response.headers["ETag"] = format_etag(str(f.etag))
    return await _serialize_one(db, f)


@router.delete(
    "/findings/{finding_id}/geometry/{geometry_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def remove_finding_geometry(
    request: Request,
    finding_id: uuid.UUID,
    geometry_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    audit: AuditDep,
) -> Response:
    f = await _finding_for_write(db, request, user, finding_id)
    g = (
        await db.execute(
            select(FindingGeometry).where(
                FindingGeometry.id == geometry_id,
                FindingGeometry.finding_id == f.id,
            )
        )
    ).scalar_one_or_none()
    if g is None:
        raise HTTPException(status_code=404, detail="geometry link not found")
    await db.delete(g)
    await db.commit()
    await audit.log(
        action="finding_geometry_remove",
        actor_subject_id=user.subject_id,
        resource_kind="finding",
        resource_id=f.id,
        metadata={"geometry_id": str(geometry_id)},
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
