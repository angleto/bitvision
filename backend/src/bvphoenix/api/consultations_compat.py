"""Compat shim for the legacy ``/api/consultations*`` surface.

v3 folded ``Consultation`` into ``ReportContent`` with
``authority_id='canonical_synthesis'`` (D2). The dedicated
``Consultation`` table + endpoints were dropped, but the existing
frontend slice (``consultationsApi`` in ``frontend/src/lib/api.ts``,
plus EvidenceWorkspace / ReportComposer / consultations detail page)
still calls the old paths. Until the frontend migrates to
``reportContentsApi`` we expose a thin compat shim that translates
each legacy call into a v3 query.

Endpoints:
  GET    /api/patients/{patient_id}/consultations
         → list ReportContent rows with authority='canonical_synthesis'
           for the patient's events, in the legacy ConsultationOut shape.

  GET    /api/consultations/{id}
         → fetch one ReportContent + its citations.

  POST   /api/consultations
         → create a draft canonical_synthesis on the given event.

  PATCH  /api/consultations/{id}
         → update content (title / summary / findings / recommendations)
           on a canonical_synthesis row.

  POST   /api/consultations/{id}/sign
         → flip status to ``final`` then ``signed`` (HUMAN-only at the
           v3 backend; this shim refuses agent tokens with 403).

  POST   /api/consultations/{id}/reject
         → flip status to ``rejected`` with the given reason.

The shim is intentionally minimal: it does NOT expose the v3-only
fields (authority, parser_version, etc.) and only handles the small
subset of operations the legacy frontend performs. Phase 5 will
delete this module when the frontend switches to the v3 client.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.auth.deps import require_user
from bvphoenix.db.models import (
    ClinicalEvent,
    Patient,
    ReportContent,
    ReportContentCitation,
    User,
)
from bvphoenix.db.session import get_db
from bvphoenix.middleware.audit_dependency import AuditDep
from bvphoenix.services.agent_context import AgentContext
from bvphoenix.services.permissions import (
    READ_METADATA,
    WRITE_REPORT,
    can_patient,
)

router = APIRouter(tags=["consultations-compat"])


# ---------------------------------------------------------------------------
# Schemas (legacy shape — what the frontend still expects)
# ---------------------------------------------------------------------------


class CitationOutCompat(BaseModel):
    id: str
    target_kind: str
    target_id: str
    excerpt: str | None
    page: int | None = None
    bbox: dict | None = None
    file_id: str | None = None
    slice_idx: int | None = None
    annotation_marker_idx: int | None = None
    lab_value_id: str | None = None


class ConsultationOutCompat(BaseModel):
    id: str
    patient_id: str
    author_subject_id: str
    author_kind: str
    is_ai_generated: bool
    model_id: str | None
    provider: str | None
    # ``agent_token_id`` is the legacy JWT-agent FK (NULL for the
    # modern per-assistant secret path). ``agent_assistant_id`` is
    # the canonical identity for both flows.
    agent_token_id: str | None
    agent_assistant_id: str | None
    status: str
    title: str
    summary_md: str | None
    findings_md: str | None
    recommendations_md: str | None
    confidence: float | None
    deidentified_input: bool | None
    consent_snapshot: list | None
    signed_by_subject_id: str | None
    signed_at: str | None
    rejected_reason: str | None
    created_at: str
    updated_at: str
    citations: list[CitationOutCompat] = []


class ConsultationCreateIn(BaseModel):
    patient_id: uuid.UUID
    title: str = Field(..., min_length=1, max_length=255)
    summary_md: str | None = None
    findings_md: str | None = None
    recommendations_md: str | None = None
    confidence: float | None = None
    deidentified_input: bool | None = None
    model_id: str | None = None
    provider: str | None = None
    # Either an existing ``clinical_event_id`` (preferred), or the shim
    # creates a generic ``consultation_event`` for the patient when the
    # caller doesn't pin one.
    clinical_event_id: uuid.UUID | None = None
    # Date of the auto-minted ``consultation_event``. Defaults to today
    # because a synthesis produced now genuinely happened now, but a
    # caller importing an older consultation can say so instead of
    # having the insertion moment recorded as a clinical fact with no
    # way back (that path is now
    # ``POST /clinical-events/{id}/amend-time``). Ignored when
    # ``clinical_event_id`` pins an existing event.
    event_date: date | None = None


class ConsultationUpdateIn(BaseModel):
    title: str | None = None
    summary_md: str | None = None
    findings_md: str | None = None
    recommendations_md: str | None = None


class ConsultationSignIn(BaseModel):
    note: str | None = None


class ConsultationRejectIn(BaseModel):
    reason: str = Field(..., min_length=1, max_length=2000)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _patient_id_for_rc(db: AsyncSession, rc: ReportContent) -> uuid.UUID | None:
    return (
        await db.execute(
            select(ClinicalEvent.patient_id).where(ClinicalEvent.id == rc.clinical_event_id)
        )
    ).scalar_one_or_none()


async def _to_compat(db: AsyncSession, rc: ReportContent) -> ConsultationOutCompat:
    citations = (
        (
            await db.execute(
                select(ReportContentCitation).where(
                    ReportContentCitation.report_content_id == rc.id
                )
            )
        )
        .scalars()
        .all()
    )
    patient_id = await _patient_id_for_rc(db, rc)
    return ConsultationOutCompat(
        id=str(rc.id),
        patient_id=str(patient_id) if patient_id else "",
        author_subject_id=str(rc.created_by_subject_id),
        author_kind=rc.author_kind,
        is_ai_generated=(rc.author_kind == "agent"),
        model_id=rc.model_id,
        provider=rc.provider,
        agent_token_id=str(rc.agent_token_id) if rc.agent_token_id else None,
        agent_assistant_id=str(rc.agent_assistant_id) if rc.agent_assistant_id else None,
        status=rc.status,
        title=rc.title or "(senza titolo)",
        summary_md=rc.narrative_md,
        findings_md=rc.findings_md,
        recommendations_md=rc.recommendations_md,
        confidence=rc.confidence,
        deidentified_input=rc.deidentified_input,
        consent_snapshot=rc.consent_snapshot,
        signed_by_subject_id=(str(rc.signed_by_subject_id) if rc.signed_by_subject_id else None),
        signed_at=rc.signed_at.isoformat() if rc.signed_at else None,
        rejected_reason=rc.rejected_reason,
        created_at=rc.created_at.isoformat(),
        updated_at=rc.updated_at.isoformat(),
        citations=[
            CitationOutCompat(
                id=str(c.id),
                target_kind=c.target_kind,
                target_id=str(c.target_id),
                excerpt=c.excerpt,
                page=c.page,
                bbox=c.bbox,
                file_id=str(c.file_id) if c.file_id else None,
                slice_idx=c.slice_idx,
                annotation_marker_idx=c.annotation_marker_idx,
                lab_value_id=str(c.lab_value_id) if c.lab_value_id else None,
            )
            for c in citations
        ],
    )


async def _load_rc_or_404(db: AsyncSession, rc_id: uuid.UUID) -> ReportContent:
    rc = (
        await db.execute(select(ReportContent).where(ReportContent.id == rc_id))
    ).scalar_one_or_none()
    if rc is None or rc.authority_id != "canonical_synthesis":
        raise HTTPException(status_code=404, detail="consultation not found")
    return rc


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/patients/{patient_id}/consultations",
    response_model=list[ConsultationOutCompat],
)
async def list_patient_consultations(
    patient_id: uuid.UUID,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    audit: AuditDep,
    status: Annotated[str | None, Query()] = None,
    author_kind: Annotated[str | None, Query()] = None,
) -> list[ConsultationOutCompat]:
    patient = (
        await db.execute(select(Patient).where(Patient.id == patient_id))
    ).scalar_one_or_none()
    if patient is None or not await can_patient(
        db, user=user, action=READ_METADATA, patient=patient
    ):
        raise HTTPException(status_code=404, detail="patient not found")
    stmt = (
        select(ReportContent)
        .join(ClinicalEvent, ClinicalEvent.id == ReportContent.clinical_event_id)
        .where(
            ClinicalEvent.patient_id == patient_id,
            ReportContent.authority_id == "canonical_synthesis",
        )
        .order_by(ReportContent.created_at.desc())
    )
    if status and status != "all":
        stmt = stmt.where(ReportContent.status == status)
    if author_kind and author_kind != "all":
        stmt = stmt.where(ReportContent.author_kind == author_kind)
    rows = (await db.execute(stmt)).scalars().all()
    return [await _to_compat(db, r) for r in rows]


@router.get(
    "/consultations/{consultation_id}",
    response_model=ConsultationOutCompat,
)
async def get_consultation(
    consultation_id: uuid.UUID,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    audit: AuditDep,
) -> ConsultationOutCompat:
    rc = await _load_rc_or_404(db, consultation_id)
    patient_id = await _patient_id_for_rc(db, rc)
    if patient_id is None:
        raise HTTPException(status_code=404, detail="consultation not found")
    patient = (
        await db.execute(select(Patient).where(Patient.id == patient_id))
    ).scalar_one_or_none()
    if patient is None or not await can_patient(
        db, user=user, action=READ_METADATA, patient=patient
    ):
        raise HTTPException(status_code=404, detail="consultation not found")
    return await _to_compat(db, rc)


@router.post(
    "/consultations",
    response_model=ConsultationOutCompat,
    status_code=status.HTTP_201_CREATED,
)
async def create_consultation(
    body: ConsultationCreateIn,
    request: Request,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    audit: AuditDep,
) -> ConsultationOutCompat:
    patient = (
        await db.execute(select(Patient).where(Patient.id == body.patient_id))
    ).scalar_one_or_none()
    if patient is None or not await can_patient(
        db, user=user, action=WRITE_REPORT, patient=patient
    ):
        raise HTTPException(status_code=404, detail="patient not found")

    # Anchor the synthesis on a ClinicalEvent. If the caller pinned
    # one, reuse it; otherwise mint a placeholder ``consultation_event``
    # so the v3 invariant holds. The auto-mint is the v3-equivalent of
    # the legacy "consultation without event" semantic — phase 5 polish
    # surfaces an event-picker on the frontend.
    if body.clinical_event_id:
        event = (
            await db.execute(
                select(ClinicalEvent).where(ClinicalEvent.id == body.clinical_event_id)
            )
        ).scalar_one_or_none()
        if event is None or event.patient_id != body.patient_id:
            raise HTTPException(status_code=422, detail="clinical_event_id not on this patient")
    else:
        event = ClinicalEvent(
            patient_id=body.patient_id,
            kind="consultation_event",
            title=body.title,
            event_date=body.event_date or datetime.now(UTC).date(),
        )
        db.add(event)
        await db.flush()

    ctx = AgentContext.from_request(request)
    rc = ReportContent(
        clinical_event_id=event.id,
        authority_id="canonical_synthesis",
        status="draft",
        title=body.title,
        narrative_md=body.summary_md,
        findings_md=body.findings_md,
        recommendations_md=body.recommendations_md,
        confidence=body.confidence,
        deidentified_input=body.deidentified_input,
        created_by_subject_id=user.subject_id,
        author_kind=ctx.author_kind,
        agent_token_id=ctx.agent_token_id,
        agent_assistant_id=ctx.agent_assistant_id if ctx.is_agent else None,
        model_id=body.model_id if ctx.is_agent else None,
        provider=body.provider if ctx.is_agent else None,
    )
    db.add(rc)
    await db.commit()
    await db.refresh(rc)
    return await _to_compat(db, rc)


@router.patch(
    "/consultations/{consultation_id}",
    response_model=ConsultationOutCompat,
)
async def update_consultation(
    consultation_id: uuid.UUID,
    body: ConsultationUpdateIn,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    audit: AuditDep,
) -> ConsultationOutCompat:
    rc = await _load_rc_or_404(db, consultation_id)
    patient_id = await _patient_id_for_rc(db, rc)
    if patient_id is None:
        raise HTTPException(status_code=404, detail="consultation not found")
    patient = (
        await db.execute(select(Patient).where(Patient.id == patient_id))
    ).scalar_one_or_none()
    if patient is None or not await can_patient(
        db, user=user, action=WRITE_REPORT, patient=patient
    ):
        raise HTTPException(status_code=404, detail="consultation not found")
    if rc.status in ("signed", "stale", "rejected"):
        raise HTTPException(
            status_code=409,
            detail=f"cannot edit consultation in terminal status '{rc.status}'",
        )
    if body.title is not None:
        rc.title = body.title
    if body.summary_md is not None:
        rc.narrative_md = body.summary_md
    if body.findings_md is not None:
        rc.findings_md = body.findings_md
    if body.recommendations_md is not None:
        rc.recommendations_md = body.recommendations_md
    rc.etag = uuid.uuid4()
    await db.commit()
    await db.refresh(rc)
    return await _to_compat(db, rc)


@router.post(
    "/consultations/{consultation_id}/sign",
    response_model=ConsultationOutCompat,
)
async def sign_consultation(
    consultation_id: uuid.UUID,
    body: ConsultationSignIn | None,
    request: Request,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    audit: AuditDep,
) -> ConsultationOutCompat:
    """HUMAN-only sign. Mirrors the v3 hard gate."""
    if getattr(request.state, "agent_token_id", None) is not None:
        raise HTTPException(
            status_code=403,
            detail="signing a consultation is restricted to human users",
        )
    rc = await _load_rc_or_404(db, consultation_id)
    patient_id = await _patient_id_for_rc(db, rc)
    if patient_id is None:
        raise HTTPException(status_code=404, detail="consultation not found")
    patient = (
        await db.execute(select(Patient).where(Patient.id == patient_id))
    ).scalar_one_or_none()
    if patient is None or not await can_patient(
        db, user=user, action=WRITE_REPORT, patient=patient
    ):
        raise HTTPException(status_code=404, detail="consultation not found")
    if rc.status not in ("draft", "final"):
        raise HTTPException(
            status_code=409,
            detail=f"cannot sign from status '{rc.status}'",
        )
    rc.status = "signed"
    rc.signed_by_subject_id = user.subject_id
    rc.signed_at = datetime.now(UTC)
    rc.etag = uuid.uuid4()
    await db.commit()
    await db.refresh(rc)
    return await _to_compat(db, rc)


@router.post(
    "/consultations/{consultation_id}/reject",
    response_model=ConsultationOutCompat,
)
async def reject_consultation(
    consultation_id: uuid.UUID,
    body: ConsultationRejectIn,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    audit: AuditDep,
) -> ConsultationOutCompat:
    rc = await _load_rc_or_404(db, consultation_id)
    patient_id = await _patient_id_for_rc(db, rc)
    if patient_id is None:
        raise HTTPException(status_code=404, detail="consultation not found")
    patient = (
        await db.execute(select(Patient).where(Patient.id == patient_id))
    ).scalar_one_or_none()
    if patient is None or not await can_patient(
        db, user=user, action=WRITE_REPORT, patient=patient
    ):
        raise HTTPException(status_code=404, detail="consultation not found")
    if rc.status not in ("draft", "final"):
        raise HTTPException(status_code=409, detail=f"cannot reject from status '{rc.status}'")
    rc.status = "rejected"
    rc.rejected_reason = body.reason
    rc.etag = uuid.uuid4()
    await db.commit()
    await db.refresh(rc)
    return await _to_compat(db, rc)


__all__: list[Any] = ["router"]
