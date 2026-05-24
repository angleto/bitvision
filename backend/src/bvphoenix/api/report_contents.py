"""ReportContent — the v3 Expression layer.

A ReportContent is a structured narrative about a clinical event:
either an extracted view of a source document (``authority='original'``
or ``'derived'``, light workflow ``extracted_auto → endorsed →
stale``) or a curated BitVision synthesis (``authority=
'canonical_synthesis'``, heavy workflow ``draft → final → signed →
(stale | rejected)``).

The two workflows live in the same table; the (authority, status)
pair is gated by a CHECK constraint at the DB level and by explicit
state-transition tables here at the API level.

The ``synthesis:sign`` transition is a HUMAN-only hard gate enforced
in :func:`sign_report_content`: an agent token in
``request.state.agent_token_id`` cannot transition a row to
``status='signed'``. Every other transition can be authored by either
a human or an MCP agent (with the appropriate scope), and the
``author_kind`` of the resulting provenance row reflects the actual
caller.

Endpoints (v3 phase 3a — minimal set):
- ``GET /api/report-contents/{id}`` — read one
- ``GET /api/clinical-events/{event_id}/report-contents`` — list per event
- ``POST /api/report-contents`` — create (extracted/derived OR draft synthesis)
- ``PATCH /api/report-contents/{id}`` — content update + light status transition
- ``POST /api/report-contents/{id}/cite`` — add a citation to a source artefact
- ``POST /api/report-contents/{id}/link-document`` — Content↔Document link
- ``POST /api/report-contents/{id}/sign`` — sign canonical synthesis (HUMAN ONLY)
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.auth.deps import enforce_agent_patient_scope, require_user
from bvphoenix.db.models import (
    CITATION_TARGET_KINDS,
    CONTENT_DOCUMENT_LINK_ROLES,
    REPORT_CONTENT_AUTHORITIES,
    REPORT_CONTENT_STATUSES,
    ClinicalEvent,
    ContentDocumentLink,
    Document,
    Patient,
    ReportContent,
    ReportContentCitation,
    User,
)
from bvphoenix.db.session import get_db
from bvphoenix.middleware.audit_dependency import AuditDep
from bvphoenix.services.agent_context import AgentContext
from bvphoenix.services.evidence_links import validate_mentions_or_raise
from bvphoenix.services.permissions import (
    READ_METADATA,
    WRITE_REPORT,
    can_patient,
)
from bvphoenix.services.provenance_log import record_provenance

router = APIRouter(tags=["report-contents"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class LinkedDocumentRef(BaseModel):
    """Minimal document descriptor surfaced inline on a ReportContent
    so the UI can render an "open source document" affordance without
    a second round-trip per card. Populated only by the list endpoint
    (where the join is batched); the single-row read returns an empty
    list."""

    id: str
    title: str | None
    kind_id: str | None
    document_date: str | None
    role: str | None


class ReportContentOut(BaseModel):
    id: str
    clinical_event_id: str
    authority: str
    status: str
    language: str
    title: str | None
    narrative_md: str | None
    structured_fields: dict
    findings_md: str | None
    recommendations_md: str | None
    confidence: float | None
    deidentified_input: bool | None
    created_by_subject_id: str
    author_kind: str
    is_ai_generated: bool
    model_id: str | None
    provider: str | None
    # Legacy JWT-agent-token FK; NULL for the modern per-assistant
    # secret path (Claude.ai connector and any modern MCP client).
    # That path identifies the agent via ``agent_assistant_id`` below
    # — the audit trail has both fields so callers can resolve the
    # agent identity through whichever flow minted the credential.
    agent_token_id: str | None
    agent_assistant_id: str | None
    extracted_at: str | None
    parser_version: str | None
    endorsed_by_subject_id: str | None
    endorsed_at: str | None
    signed_by_subject_id: str | None
    signed_at: str | None
    rejected_reason: str | None
    superseded_by_id: str | None
    supersede_reason: str | None
    etag: str
    created_at: str
    updated_at: str
    linked_documents: list[LinkedDocumentRef] = []


class ReportContentCreateIn(BaseModel):
    clinical_event_id: uuid.UUID
    authority: str = Field(..., description=f"One of {REPORT_CONTENT_AUTHORITIES}")
    title: str | None = Field(default=None, max_length=255)
    language: str = Field(default="it", max_length=10)
    narrative_md: str | None = None
    structured_fields: dict = Field(default_factory=dict)
    # Synthesis-only (ignored for original / derived):
    findings_md: str | None = None
    recommendations_md: str | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    deidentified_input: bool | None = None
    # Extraction-only (ignored for canonical_synthesis):
    parser_version: str | None = Field(default=None, max_length=64)
    # Agent metadata (ignored for human callers — author_kind is
    # derived from the request, not the body).
    model_id: str | None = Field(default=None, max_length=128)
    provider: str | None = Field(default=None, max_length=64)


class ReportContentUpdateIn(BaseModel):
    title: str | None = Field(default=None, max_length=255)
    narrative_md: str | None = None
    structured_fields: dict | None = None
    findings_md: str | None = None
    recommendations_md: str | None = None
    # Light status transitions only: ``draft → final``, ``extracted_auto
    # → endorsed`` go through dedicated endpoints. The signing gate is
    # ``POST .../sign`` (human-only) so it cannot be flipped by an
    # accidental PATCH.
    status: str | None = None


class CitationIn(BaseModel):
    target_kind: str = Field(..., description=f"One of {CITATION_TARGET_KINDS}")
    target_id: uuid.UUID
    excerpt: str | None = None
    page: int | None = Field(default=None, ge=1)
    bbox: dict | None = None
    file_id: uuid.UUID | None = None
    slice_idx: int | None = Field(default=None, ge=0)
    annotation_marker_idx: int | None = Field(default=None, ge=0)
    lab_value_id: uuid.UUID | None = None


class CitationOut(BaseModel):
    id: str
    report_content_id: str
    target_kind: str
    target_id: str
    excerpt: str | None
    page: int | None
    bbox: dict | None
    file_id: str | None
    slice_idx: int | None
    annotation_marker_idx: int | None
    lab_value_id: str | None
    created_at: str


class LinkDocumentIn(BaseModel):
    document_id: uuid.UUID
    role: str = Field(..., description=f"One of {CONTENT_DOCUMENT_LINK_ROLES}")
    excerpt: str | None = None


class SignIn(BaseModel):
    """Sign requires confirmation that the signing clinician has
    reviewed the content. The free-text ``confirm`` field is a
    deliberate friction step — clients submit the literal title of
    the synthesis to confirm intent."""

    confirm_title: str = Field(..., min_length=1)


class RejectIn(BaseModel):
    reason: str = Field(..., min_length=1, max_length=2000)


class SupersedeIn(BaseModel):
    """Replace a (typically signed) ReportContent with a new draft.
    The new row inherits the ``clinical_event_id`` and the cited
    sources from the previous row; the previous row transitions to
    ``status='stale'`` and gets ``superseded_by_id`` populated."""

    title: str | None = Field(default=None, max_length=255)
    narrative_md: str | None = None
    findings_md: str | None = None
    recommendations_md: str | None = None
    structured_fields: dict | None = None
    reason: str = Field(..., min_length=1, max_length=2000)


# ---------------------------------------------------------------------------
# Status transition tables
# ---------------------------------------------------------------------------


# (authority, current_status) → set of admissible next statuses.
# Mirrors the CHECK constraint ``ck_report_contents_authority_status``.
_LIGHT_TRANSITIONS: dict[str, set[str]] = {
    "extracted_auto": {"endorsed", "stale"},
    "endorsed": {"stale"},
    "stale": set(),
}
_HEAVY_TRANSITIONS: dict[str, set[str]] = {
    "draft": {"final", "rejected"},
    "final": {"signed", "rejected", "draft"},  # back-to-draft for revisions
    "signed": {"stale"},  # only via supersede; supersede endpoint owns this
    "rejected": set(),
    "stale": set(),
}


def _admissible_next_statuses(authority: str, current: str) -> set[str]:
    if authority in ("original", "derived"):
        return _LIGHT_TRANSITIONS.get(current, set())
    if authority == "canonical_synthesis":
        return _HEAVY_TRANSITIONS.get(current, set())
    return set()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_out(rc: ReportContent) -> ReportContentOut:
    return ReportContentOut(
        id=str(rc.id),
        clinical_event_id=str(rc.clinical_event_id),
        authority=rc.authority_id,
        status=rc.status,
        language=rc.language,
        title=rc.title,
        narrative_md=rc.narrative_md,
        structured_fields=rc.structured_fields or {},
        findings_md=rc.findings_md,
        recommendations_md=rc.recommendations_md,
        confidence=rc.confidence,
        deidentified_input=rc.deidentified_input,
        created_by_subject_id=str(rc.created_by_subject_id),
        author_kind=rc.author_kind,
        is_ai_generated=(rc.author_kind == "agent"),
        model_id=rc.model_id,
        provider=rc.provider,
        agent_token_id=str(rc.agent_token_id) if rc.agent_token_id else None,
        agent_assistant_id=str(rc.agent_assistant_id) if rc.agent_assistant_id else None,
        extracted_at=rc.extracted_at.isoformat() if rc.extracted_at else None,
        parser_version=rc.parser_version,
        endorsed_by_subject_id=str(rc.endorsed_by_subject_id)
        if rc.endorsed_by_subject_id
        else None,
        endorsed_at=rc.endorsed_at.isoformat() if rc.endorsed_at else None,
        signed_by_subject_id=str(rc.signed_by_subject_id) if rc.signed_by_subject_id else None,
        signed_at=rc.signed_at.isoformat() if rc.signed_at else None,
        rejected_reason=rc.rejected_reason,
        superseded_by_id=str(rc.superseded_by_id) if rc.superseded_by_id else None,
        supersede_reason=rc.supersede_reason,
        etag=str(rc.etag),
        created_at=rc.created_at.isoformat(),
        updated_at=rc.updated_at.isoformat(),
    )


def _cit_to_out(c: ReportContentCitation) -> CitationOut:
    return CitationOut(
        id=str(c.id),
        report_content_id=str(c.report_content_id),
        target_kind=c.target_kind,
        target_id=str(c.target_id),
        excerpt=c.excerpt,
        page=c.page,
        bbox=c.bbox,
        file_id=str(c.file_id) if c.file_id else None,
        slice_idx=c.slice_idx,
        annotation_marker_idx=c.annotation_marker_idx,
        lab_value_id=str(c.lab_value_id) if c.lab_value_id else None,
        created_at=c.created_at.isoformat(),
    )


async def _load_event_and_check(
    db: AsyncSession,
    event_id: uuid.UUID,
    user: User,
    perm: str,
    *,
    request: Request,
) -> ClinicalEvent:
    """Load + authorise the parent ClinicalEvent for a report-content
    write. Agent token defense in depth: ``enforce_agent_patient_scope``
    runs after ``can_patient`` so an agent whose underlying user holds
    broad RBAC still cannot mutate report-content rows for fascicoli
    outside its consented ``agent_patient_ids`` set
    (memoria ``cross_patient_links_forbidden``)."""
    ev = (
        await db.execute(select(ClinicalEvent).where(ClinicalEvent.id == event_id))
    ).scalar_one_or_none()
    if ev is None:
        raise HTTPException(status_code=404, detail="clinical event not found")
    patient = (
        await db.execute(select(Patient).where(Patient.id == ev.patient_id))
    ).scalar_one_or_none()
    if patient is None or not await can_patient(db, user=user, action=perm, patient=patient):
        raise HTTPException(status_code=404, detail="clinical event not found")
    enforce_agent_patient_scope(request, patient.id)
    return ev


async def _load_rc_and_check(
    db: AsyncSession,
    rc_id: uuid.UUID,
    user: User,
    perm: str,
    *,
    request: Request,
) -> tuple[ReportContent, ClinicalEvent]:
    rc = (
        await db.execute(select(ReportContent).where(ReportContent.id == rc_id))
    ).scalar_one_or_none()
    if rc is None:
        raise HTTPException(status_code=404, detail="report content not found")
    ev = await _load_event_and_check(db, rc.clinical_event_id, user, perm, request=request)
    return rc, ev


async def _record_provenance(
    db: AsyncSession,
    *,
    target_kind: str,
    target_id: uuid.UUID,
    activity: str,
    user: User,
    request: Request,
    diff: dict | None = None,
) -> None:
    """Thin wrapper around :func:`record_provenance`. Kept under this
    module name so existing call sites stay readable; the audit-row
    construction lives in the shared service to keep behaviour
    consistent across every writer."""
    record_provenance(
        db,
        target_kind=target_kind,
        target_id=target_id,
        activity=activity,
        user=user,
        request=request,
        diff=diff,
    )


# ---------------------------------------------------------------------------
# Endpoints — read
# ---------------------------------------------------------------------------


@router.get(
    "/report-contents/{rc_id}",
    response_model=ReportContentOut,
)
async def read_report_content(
    rc_id: uuid.UUID,
    request: Request,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    audit: AuditDep,
    if_none_match: Annotated[str | None, Header(alias="If-None-Match")] = None,
) -> ReportContentOut:
    rc, _ = await _load_rc_and_check(db, rc_id, user, READ_METADATA, request=request)
    if if_none_match is not None and if_none_match.strip('"') == str(rc.etag):
        raise HTTPException(status_code=status.HTTP_304_NOT_MODIFIED)
    out = _to_out(rc)
    request.state.response_etag = out.etag
    return out


@router.get(
    "/clinical-events/{event_id}/report-contents",
    response_model=list[ReportContentOut],
)
async def list_report_contents_for_event(
    event_id: uuid.UUID,
    request: Request,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    audit: AuditDep,
) -> list[ReportContentOut]:
    await _load_event_and_check(db, event_id, user, READ_METADATA, request=request)
    rows = (
        (
            await db.execute(
                select(ReportContent)
                .where(ReportContent.clinical_event_id == event_id)
                .order_by(ReportContent.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    # Batch-populate ``linked_documents`` so the FE can offer an "open
    # source document" link inline on each ReportContent card without
    # paying a per-card round-trip. One JOIN regardless of how many
    # rcs the event carries; common case is 1-3.
    rc_ids = [r.id for r in rows]
    docs_by_rc: dict[uuid.UUID, list[LinkedDocumentRef]] = {}
    if rc_ids:
        link_rows = (
            await db.execute(
                select(
                    ContentDocumentLink.report_content_id,
                    ContentDocumentLink.role,
                    Document.id,
                    Document.title,
                    Document.kind_id,
                    Document.document_date,
                )
                .join(Document, Document.id == ContentDocumentLink.document_id)
                .where(ContentDocumentLink.report_content_id.in_(rc_ids))
            )
        ).all()
        for rc_id, role, doc_id, title, kind_id, document_date in link_rows:
            docs_by_rc.setdefault(rc_id, []).append(
                LinkedDocumentRef(
                    id=str(doc_id),
                    title=title,
                    kind_id=kind_id,
                    document_date=document_date.isoformat() if document_date else None,
                    role=role,
                )
            )
    out: list[ReportContentOut] = []
    for r in rows:
        item = _to_out(r)
        item.linked_documents = docs_by_rc.get(r.id, [])
        out.append(item)
    return out


# ---------------------------------------------------------------------------
# Endpoints — create / update
# ---------------------------------------------------------------------------


@router.post(
    "/report-contents",
    response_model=ReportContentOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_report_content(
    body: ReportContentCreateIn,
    request: Request,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    audit: AuditDep,
) -> ReportContentOut:
    if body.authority not in REPORT_CONTENT_AUTHORITIES or body.authority == "stale":
        raise HTTPException(
            status_code=422,
            detail="authority must be one of original / derived / canonical_synthesis",
        )
    ev = await _load_event_and_check(
        db, body.clinical_event_id, user, WRITE_REPORT, request=request
    )

    # Cross-patient guard for the Evidenze e sintesi DSL: every
    # ``@kind:UUID`` mention in any markdown field must resolve to a
    # resource of the parent event's patient. We only validate fields
    # that will actually be persisted (findings_md / recommendations_md
    # are silently dropped to None for non-canonical authorities so
    # validating them there would surface 422s on text that never
    # lands in the row).
    md_candidates: list[str] = []
    if body.narrative_md:
        md_candidates.append(body.narrative_md)
    if body.findings_md and body.authority == "canonical_synthesis":
        md_candidates.append(body.findings_md)
    if body.recommendations_md and body.authority == "canonical_synthesis":
        md_candidates.append(body.recommendations_md)
    for md_text in md_candidates:
        await validate_mentions_or_raise(db, patient_id=ev.patient_id, body=md_text)

    ctx = AgentContext.from_request(request)
    initial_status = "extracted_auto" if body.authority in ("original", "derived") else "draft"
    extracted_at = datetime.now(UTC) if body.authority in ("original", "derived") else None

    rc = ReportContent(
        clinical_event_id=body.clinical_event_id,
        authority_id=body.authority,
        status=initial_status,
        language=body.language,
        title=body.title,
        narrative_md=body.narrative_md,
        structured_fields=body.structured_fields,
        findings_md=body.findings_md if body.authority == "canonical_synthesis" else None,
        recommendations_md=body.recommendations_md
        if body.authority == "canonical_synthesis"
        else None,
        confidence=body.confidence if body.authority == "canonical_synthesis" else None,
        deidentified_input=body.deidentified_input
        if body.authority == "canonical_synthesis"
        else None,
        created_by_subject_id=user.subject_id,
        author_kind=ctx.author_kind,
        agent_token_id=ctx.agent_token_id,
        agent_assistant_id=ctx.agent_assistant_id if ctx.is_agent else None,
        model_id=body.model_id if ctx.is_agent else None,
        provider=body.provider if ctx.is_agent else None,
        extracted_at=extracted_at,
        parser_version=body.parser_version if body.authority in ("original", "derived") else None,
    )
    db.add(rc)
    await db.flush()
    activity = "extract" if body.authority in ("original", "derived") else "create"
    await _record_provenance(
        db,
        target_kind="report_content",
        target_id=rc.id,
        activity=activity,
        user=user,
        request=request,
        diff={"authority": body.authority, "status": initial_status},
    )
    await db.commit()
    await db.refresh(rc)
    out = _to_out(rc)
    request.state.response_etag = out.etag
    return out


@router.patch(
    "/report-contents/{rc_id}",
    response_model=ReportContentOut,
)
async def update_report_content(
    rc_id: uuid.UUID,
    body: ReportContentUpdateIn,
    request: Request,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    audit: AuditDep,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> ReportContentOut:
    rc, ev = await _load_rc_and_check(db, rc_id, user, WRITE_REPORT, request=request)
    if if_match is None:
        raise HTTPException(status_code=428, detail="If-Match precondition required")
    if if_match.strip('"') != str(rc.etag):
        raise HTTPException(status_code=412, detail="etag mismatch")
    if rc.status in ("signed", "stale", "rejected"):
        raise HTTPException(
            status_code=409,
            detail=f"cannot edit content in terminal status '{rc.status}'",
        )

    # Cross-patient guard for the Evidenze e sintesi DSL: every
    # ``@kind:UUID`` mention in any markdown field that we are about
    # to persist must resolve to a resource of the parent event's
    # patient. We only check the fields whose value will actually be
    # applied (findings_md / recommendations_md are silently dropped
    # for non-canonical authorities, so validating them there would
    # produce 422s on text that never lands in the DB).
    candidates: list[str] = []
    if body.narrative_md:
        candidates.append(body.narrative_md)
    if body.findings_md and rc.authority_id == "canonical_synthesis":
        candidates.append(body.findings_md)
    if body.recommendations_md and rc.authority_id == "canonical_synthesis":
        candidates.append(body.recommendations_md)
    for md_text in candidates:
        await validate_mentions_or_raise(db, patient_id=ev.patient_id, body=md_text)

    diff: dict[str, object] = {}
    if body.title is not None:
        rc.title = body.title
        diff["title"] = body.title
    if body.narrative_md is not None:
        rc.narrative_md = body.narrative_md
        diff["narrative_changed"] = True
    if body.structured_fields is not None:
        rc.structured_fields = body.structured_fields
        diff["structured_fields_changed"] = True
    if body.findings_md is not None and rc.authority_id == "canonical_synthesis":
        rc.findings_md = body.findings_md
    if body.recommendations_md is not None and rc.authority_id == "canonical_synthesis":
        rc.recommendations_md = body.recommendations_md
    if body.status is not None:
        admissible = _admissible_next_statuses(rc.authority_id, rc.status)
        if body.status == "signed":
            raise HTTPException(
                status_code=409,
                detail="use POST /report-contents/{id}/sign for the signature transition",
            )
        if body.status not in admissible:
            raise HTTPException(
                status_code=409,
                detail=f"cannot transition '{rc.status}' → '{body.status}' "
                f"on authority '{rc.authority_id}'; allowed: {sorted(admissible)}",
            )
        rc.status = body.status
        diff["status"] = body.status

    rc.etag = uuid.uuid4()
    await _record_provenance(
        db,
        target_kind="report_content",
        target_id=rc.id,
        activity="update",
        user=user,
        request=request,
        diff=diff,
    )
    await db.commit()
    await db.refresh(rc)
    out = _to_out(rc)
    request.state.response_etag = out.etag
    return out


# ---------------------------------------------------------------------------
# Endpoints — citations + document links
# ---------------------------------------------------------------------------


@router.post(
    "/report-contents/{rc_id}/cite",
    response_model=CitationOut,
    status_code=status.HTTP_201_CREATED,
)
async def cite_source(
    rc_id: uuid.UUID,
    body: CitationIn,
    request: Request,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    audit: AuditDep,
) -> CitationOut:
    _rc, ev = await _load_rc_and_check(db, rc_id, user, WRITE_REPORT, request=request)
    if body.target_kind not in CITATION_TARGET_KINDS:
        raise HTTPException(
            status_code=422,
            detail=f"target_kind must be one of {sorted(CITATION_TARGET_KINDS)}",
        )
    # Cross-patient invariant: the cited artefact must belong to the
    # same patient as the citing report_content. The check is target-
    # kind-specific.
    if body.target_kind == "document":
        target_doc = (
            await db.execute(select(Document.patient_id).where(Document.id == body.target_id))
        ).scalar_one_or_none()
        if target_doc != ev.patient_id:
            raise HTTPException(
                status_code=409,
                detail="cross-patient citation forbidden",
            )

    cit = ReportContentCitation(
        report_content_id=rc_id,
        target_kind=body.target_kind,
        target_id=body.target_id,
        excerpt=body.excerpt,
        page=body.page,
        bbox=body.bbox,
        file_id=body.file_id,
        slice_idx=body.slice_idx,
        annotation_marker_idx=body.annotation_marker_idx,
        lab_value_id=body.lab_value_id,
        agent_token_id=getattr(request.state, "agent_token_id", None),
    )
    db.add(cit)
    await db.flush()
    await _record_provenance(
        db,
        target_kind="report_content",
        target_id=rc_id,
        activity="cite",
        user=user,
        request=request,
        diff={"target_kind": body.target_kind, "target_id": str(body.target_id)},
    )
    await db.commit()
    await db.refresh(cit)
    return _cit_to_out(cit)


@router.post(
    "/report-contents/{rc_id}/link-document",
    status_code=status.HTTP_201_CREATED,
)
async def link_document(
    rc_id: uuid.UUID,
    body: LinkDocumentIn,
    request: Request,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    audit: AuditDep,
) -> dict[str, str]:
    _rc, ev = await _load_rc_and_check(db, rc_id, user, WRITE_REPORT, request=request)
    if body.role not in CONTENT_DOCUMENT_LINK_ROLES:
        raise HTTPException(
            status_code=422,
            detail=f"role must be one of {sorted(CONTENT_DOCUMENT_LINK_ROLES)}",
        )
    target_doc = (
        await db.execute(select(Document.patient_id).where(Document.id == body.document_id))
    ).scalar_one_or_none()
    if target_doc != ev.patient_id:
        raise HTTPException(
            status_code=409,
            detail="cross-patient link forbidden",
        )
    link = ContentDocumentLink(
        report_content_id=rc_id,
        document_id=body.document_id,
        role=body.role,
        excerpt=body.excerpt,
        created_by_subject_id=user.subject_id,
        agent_token_id=getattr(request.state, "agent_token_id", None),
    )
    db.add(link)
    await db.flush()
    await _record_provenance(
        db,
        target_kind="content_document_link",
        target_id=link.id,
        activity="link",
        user=user,
        request=request,
        diff={
            "report_content_id": str(rc_id),
            "document_id": str(body.document_id),
            "role": body.role,
        },
    )
    await db.commit()
    return {"id": str(link.id), "report_content_id": str(rc_id)}


# ---------------------------------------------------------------------------
# Endpoints — signature workflow (HUMAN-only hard gate)
# ---------------------------------------------------------------------------


@router.post(
    "/report-contents/{rc_id}/sign",
    response_model=ReportContentOut,
)
async def sign_report_content(
    rc_id: uuid.UUID,
    body: SignIn,
    request: Request,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    audit: AuditDep,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> ReportContentOut:
    """Sign a canonical synthesis. HUMAN-only — agent tokens are
    refused even if the assistant has a ``synthesis:sign`` scope on
    paper. The signature is a legally-binding clinical act and cannot
    be delegated to an AI."""
    if getattr(request.state, "agent_token_id", None) is not None:
        raise HTTPException(
            status_code=403,
            detail="signing a canonical synthesis is restricted to human users",
        )
    rc, _ = await _load_rc_and_check(db, rc_id, user, WRITE_REPORT, request=request)
    if rc.authority_id != "canonical_synthesis":
        raise HTTPException(
            status_code=409,
            detail="only canonical_synthesis can be signed",
        )
    if rc.status != "final":
        raise HTTPException(
            status_code=409,
            detail=f"can only sign from status='final', current is '{rc.status}'",
        )
    if if_match is None or if_match.strip('"') != str(rc.etag):
        raise HTTPException(status_code=412, detail="etag mismatch")
    if body.confirm_title.strip() != (rc.title or "").strip():
        raise HTTPException(
            status_code=409,
            detail="confirm_title must equal the synthesis title verbatim",
        )

    rc.status = "signed"
    rc.signed_by_subject_id = user.subject_id
    rc.signed_at = datetime.now(UTC)
    rc.etag = uuid.uuid4()
    await _record_provenance(
        db,
        target_kind="report_content",
        target_id=rc.id,
        activity="sign",
        user=user,
        request=request,
        diff={"signed_at": rc.signed_at.isoformat()},
    )
    await db.commit()
    await db.refresh(rc)
    out = _to_out(rc)
    request.state.response_etag = out.etag
    return out


# ---------------------------------------------------------------------------
# Endpoints — endorse / reject / supersede
# ---------------------------------------------------------------------------


@router.post(
    "/report-contents/{rc_id}/endorse",
    response_model=ReportContentOut,
)
async def endorse_report_content(
    rc_id: uuid.UUID,
    request: Request,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    audit: AuditDep,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> ReportContentOut:
    """Mark an extracted_auto report_content as endorsed (clinician
    validation, light workflow). Allowed only for original/derived
    authorities; canonical_synthesis uses the heavy workflow.

    Endorse can be authored by either a human or an MCP agent that
    carries the ``reports:endorse`` scope (the MCP-side check is in
    the scope catalog; this endpoint trusts the bearer)."""
    rc, _ = await _load_rc_and_check(db, rc_id, user, WRITE_REPORT, request=request)
    if rc.authority_id not in ("original", "derived"):
        raise HTTPException(
            status_code=409,
            detail="endorse only applies to original / derived contents; "
            "use the canonical_synthesis sign workflow for syntheses",
        )
    if rc.status != "extracted_auto":
        raise HTTPException(
            status_code=409,
            detail=f"can only endorse from status='extracted_auto', current is '{rc.status}'",
        )
    if if_match is None or if_match.strip('"') != str(rc.etag):
        raise HTTPException(status_code=412, detail="etag mismatch")

    rc.status = "endorsed"
    rc.endorsed_by_subject_id = user.subject_id
    rc.endorsed_at = datetime.now(UTC)
    rc.etag = uuid.uuid4()
    await _record_provenance(
        db,
        target_kind="report_content",
        target_id=rc.id,
        activity="endorse",
        user=user,
        request=request,
        diff={"endorsed_at": rc.endorsed_at.isoformat()},
    )
    await db.commit()
    await db.refresh(rc)
    out = _to_out(rc)
    request.state.response_etag = out.etag
    return out


@router.post(
    "/report-contents/{rc_id}/reject",
    response_model=ReportContentOut,
)
async def reject_report_content(
    rc_id: uuid.UUID,
    body: RejectIn,
    request: Request,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    audit: AuditDep,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> ReportContentOut:
    """Reject a canonical_synthesis (heavy workflow). Allowed from
    ``draft`` or ``final``; the row becomes terminal with
    ``rejected_reason`` populated."""
    rc, _ = await _load_rc_and_check(db, rc_id, user, WRITE_REPORT, request=request)
    if rc.authority_id != "canonical_synthesis":
        raise HTTPException(
            status_code=409,
            detail="reject only applies to canonical_synthesis",
        )
    if rc.status not in ("draft", "final"):
        raise HTTPException(
            status_code=409,
            detail=f"can only reject from status in (draft, final), current is '{rc.status}'",
        )
    if if_match is None or if_match.strip('"') != str(rc.etag):
        raise HTTPException(status_code=412, detail="etag mismatch")

    rc.status = "rejected"
    rc.rejected_reason = body.reason
    rc.etag = uuid.uuid4()
    await _record_provenance(
        db,
        target_kind="report_content",
        target_id=rc.id,
        activity="reject",
        user=user,
        request=request,
        diff={"reason": body.reason},
    )
    await db.commit()
    await db.refresh(rc)
    out = _to_out(rc)
    request.state.response_etag = out.etag
    return out


@router.post(
    "/report-contents/{rc_id}/supersede",
    response_model=ReportContentOut,
    status_code=status.HTTP_201_CREATED,
)
async def supersede_report_content(
    rc_id: uuid.UUID,
    body: SupersedeIn,
    request: Request,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    audit: AuditDep,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> ReportContentOut:
    """Replace an existing report_content with a new draft.

    The previous row transitions to ``status='stale'`` and gets
    ``superseded_by_id`` populated; the new row inherits the
    ``clinical_event_id``, the ``authority_id``, and the existing
    citations + content_document_links of the predecessor (the
    citations are still meaningful for the new draft as a starting
    point — the editor can add or remove them).

    Use case: a signed canonical_synthesis is replaced by a new draft
    after an addendum (signed contents are otherwise immutable);
    an extracted_auto is replaced by a fresh OCR with better quality.
    """
    old, ev = await _load_rc_and_check(db, rc_id, user, WRITE_REPORT, request=request)
    if old.status == "stale":
        raise HTTPException(
            status_code=409,
            detail="cannot supersede a row already marked stale",
        )
    if if_match is None or if_match.strip('"') != str(old.etag):
        raise HTTPException(status_code=412, detail="etag mismatch")

    # Cross-patient guard for any markdown the supersede payload
    # introduces. We validate only the fields the caller is replacing
    # (the inherited ones from ``old`` already passed validation when
    # they were first written).
    sup_candidates: list[str] = []
    if body.narrative_md:
        sup_candidates.append(body.narrative_md)
    if body.findings_md and old.authority_id == "canonical_synthesis":
        sup_candidates.append(body.findings_md)
    if body.recommendations_md and old.authority_id == "canonical_synthesis":
        sup_candidates.append(body.recommendations_md)
    for md_text in sup_candidates:
        await validate_mentions_or_raise(db, patient_id=ev.patient_id, body=md_text)

    ctx = AgentContext.from_request(request)

    new = ReportContent(
        clinical_event_id=old.clinical_event_id,
        authority_id=old.authority_id,
        status="extracted_auto" if old.authority_id != "canonical_synthesis" else "draft",
        language=old.language,
        title=body.title if body.title is not None else old.title,
        narrative_md=body.narrative_md if body.narrative_md is not None else old.narrative_md,
        structured_fields=body.structured_fields
        if body.structured_fields is not None
        else dict(old.structured_fields or {}),
        findings_md=body.findings_md if body.findings_md is not None else old.findings_md,
        recommendations_md=body.recommendations_md
        if body.recommendations_md is not None
        else old.recommendations_md,
        confidence=old.confidence,
        deidentified_input=old.deidentified_input,
        created_by_subject_id=user.subject_id,
        author_kind=ctx.author_kind,
        agent_token_id=ctx.agent_token_id,
        agent_assistant_id=ctx.agent_assistant_id if ctx.is_agent else None,
        model_id=old.model_id,
        provider=old.provider,
        extracted_at=datetime.now(UTC) if old.authority_id in ("original", "derived") else None,
        parser_version=old.parser_version,
    )
    db.add(new)
    await db.flush()  # assign new.id

    # Inherit citations + document links — the supersede action
    # carries the evidence forward; the editor can prune them after.
    old_cits = (
        (
            await db.execute(
                select(ReportContentCitation).where(
                    ReportContentCitation.report_content_id == old.id
                )
            )
        )
        .scalars()
        .all()
    )
    for c in old_cits:
        db.add(
            ReportContentCitation(
                report_content_id=new.id,
                target_kind=c.target_kind,
                target_id=c.target_id,
                excerpt=c.excerpt,
                page=c.page,
                bbox=c.bbox,
                file_id=c.file_id,
                slice_idx=c.slice_idx,
                annotation_marker_idx=c.annotation_marker_idx,
                lab_value_id=c.lab_value_id,
                agent_token_id=ctx.agent_token_id,
            )
        )
    old_links = (
        (
            await db.execute(
                select(ContentDocumentLink).where(ContentDocumentLink.report_content_id == old.id)
            )
        )
        .scalars()
        .all()
    )
    for link in old_links:
        db.add(
            ContentDocumentLink(
                report_content_id=new.id,
                document_id=link.document_id,
                role=link.role,
                excerpt=link.excerpt,
                created_by_subject_id=user.subject_id,
                agent_token_id=ctx.agent_token_id,
            )
        )

    # Mark the old row stale + point at the new one.
    old.status = "stale"
    old.superseded_by_id = new.id
    old.supersede_reason = body.reason
    old.etag = uuid.uuid4()

    await _record_provenance(
        db,
        target_kind="report_content",
        target_id=old.id,
        activity="supersede",
        user=user,
        request=request,
        diff={
            "superseded_by": str(new.id),
            "reason": body.reason,
            "carried_citations": len(old_cits),
            "carried_links": len(old_links),
        },
    )
    await _record_provenance(
        db,
        target_kind="report_content",
        target_id=new.id,
        activity="create",
        user=user,
        request=request,
        diff={"superseded_from": str(old.id)},
    )
    await db.commit()
    await db.refresh(new)
    out = _to_out(new)
    request.state.response_etag = out.etag
    return out


__all__ = [
    "REPORT_CONTENT_STATUSES",
    "router",
]
