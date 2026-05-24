"""Provenance lineage — append-only history of every interesting state
transition in the v3 model.

Each ``provenance_events`` row records: who did what to which target,
with what source, when. The table is append-only by service-layer
convention; this module exposes a read-only view for callers that
need to answer "where did this come from / why is this in the
fascicolo / which agent touched this last".

Endpoints:
- ``GET /api/provenance/{target_kind}/{target_id}`` — history of one
  artefact, newest first.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.auth.deps import require_user
from bvphoenix.db.models import (
    PROVENANCE_TARGET_KINDS,
    ClinicalEvent,
    Document,
    ImagingStudy,
    Patient,
    ProvenanceEvent,
    ReportContent,
    Tag,
    User,
)
from bvphoenix.db.session import get_db
from bvphoenix.middleware.audit_dependency import AuditDep
from bvphoenix.services.permissions import READ_METADATA, can_patient

router = APIRouter(tags=["provenance"])


class ProvenanceEventOut(BaseModel):
    id: str
    recorded_at: str
    target_kind: str
    target_id: str
    activity: str
    agent_kind: str
    agent_subject_id: str | None
    agent_token_id: str | None
    source_kind: str | None
    source_id: str | None
    diff: dict | None
    event_metadata: dict | None
    signature_hash: str | None


async def _patient_id_for_target(
    db: AsyncSession, target_kind: str, target_id: uuid.UUID
) -> uuid.UUID | None:
    """Resolve the owning patient_id for a given (target_kind, target_id).
    Used to gate the provenance read against the caller's patient
    visibility."""
    if target_kind == "patient":
        return target_id
    if target_kind == "clinical_event":
        return (
            await db.execute(select(ClinicalEvent.patient_id).where(ClinicalEvent.id == target_id))
        ).scalar_one_or_none()
    if target_kind == "report_content":
        ev_id = (
            await db.execute(
                select(ReportContent.clinical_event_id).where(ReportContent.id == target_id)
            )
        ).scalar_one_or_none()
        if ev_id is None:
            return None
        return (
            await db.execute(select(ClinicalEvent.patient_id).where(ClinicalEvent.id == ev_id))
        ).scalar_one_or_none()
    if target_kind == "document":
        return (
            await db.execute(select(Document.patient_id).where(Document.id == target_id))
        ).scalar_one_or_none()
    # Other targets (imaging_study, series, marker, ...) — we accept the
    # visibility check at the patient level by joining through the event.
    # For MVP, return None and let the caller bypass when admin (handled
    # below).
    return None


@router.get(
    "/provenance/{target_kind}/{target_id}",
    response_model=list[ProvenanceEventOut],
    status_code=status.HTTP_200_OK,
)
async def read_provenance(
    target_kind: str,
    target_id: uuid.UUID,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    audit: AuditDep,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[ProvenanceEventOut]:
    if target_kind not in PROVENANCE_TARGET_KINDS:
        raise HTTPException(
            status_code=422,
            detail=f"target_kind must be one of {sorted(PROVENANCE_TARGET_KINDS)}",
        )
    patient_id = await _patient_id_for_target(db, target_kind, target_id)
    if patient_id is not None:
        patient = (
            await db.execute(select(Patient).where(Patient.id == patient_id))
        ).scalar_one_or_none()
        if patient is None or not await can_patient(
            db, user=user, action=READ_METADATA, patient=patient
        ):
            raise HTTPException(status_code=404, detail="provenance not visible")
    elif not user.is_admin:
        # Targets we cannot resolve to a patient (yet): admin-only for
        # now. A follow-up extends ``_patient_id_for_target`` to cover
        # imaging_studies / series / marker / tag / etc.
        raise HTTPException(status_code=404, detail="provenance not visible")

    if target_kind == "patient":
        # Patient-level provenance: aggregate every event whose target
        # belongs to this patient — clinical_events, documents,
        # report_contents (resolved through their parent
        # clinical_event), imaging_studies, tags, external_identifiers.
        # Pre-2026-05-03 the endpoint filtered ``target_kind ==
        # 'patient'`` literally, but no writer ever uses that
        # ``target_kind`` (writes target the concrete entity, not the
        # patient root), so the patient-level Provenance tab in the
        # FE was structurally empty. Aggregating across the patient's
        # entities surfaces the audit trail the user actually wants.
        ce_ids = (
            select(ClinicalEvent.id).where(ClinicalEvent.patient_id == target_id)
        ).scalar_subquery()
        doc_ids = (select(Document.id).where(Document.patient_id == target_id)).scalar_subquery()
        rc_ids = (
            select(ReportContent.id)
            .join(ClinicalEvent, ClinicalEvent.id == ReportContent.clinical_event_id)
            .where(ClinicalEvent.patient_id == target_id)
        ).scalar_subquery()
        imgs_ids = (
            select(ImagingStudy.id).where(ImagingStudy.patient_id == target_id)
        ).scalar_subquery()
        tag_ids = (select(Tag.id).where(Tag.patient_id == target_id)).scalar_subquery()
        stmt = (
            select(ProvenanceEvent)
            .where(
                or_(
                    and_(
                        ProvenanceEvent.target_kind == "patient",
                        ProvenanceEvent.target_id == target_id,
                    ),
                    and_(
                        ProvenanceEvent.target_kind == "external_identifier",
                        ProvenanceEvent.target_id == target_id,
                    ),
                    and_(
                        ProvenanceEvent.target_kind == "clinical_event",
                        ProvenanceEvent.target_id.in_(ce_ids),
                    ),
                    and_(
                        ProvenanceEvent.target_kind.in_(("document", "document_file")),
                        ProvenanceEvent.target_id.in_(doc_ids),
                    ),
                    and_(
                        ProvenanceEvent.target_kind == "report_content",
                        ProvenanceEvent.target_id.in_(rc_ids),
                    ),
                    and_(
                        ProvenanceEvent.target_kind == "imaging_study",
                        ProvenanceEvent.target_id.in_(imgs_ids),
                    ),
                    and_(
                        ProvenanceEvent.target_kind == "tag",
                        ProvenanceEvent.target_id.in_(tag_ids),
                    ),
                )
            )
            .order_by(ProvenanceEvent.recorded_at.desc())
            .limit(limit)
            .offset(offset)
        )
    else:
        stmt = (
            select(ProvenanceEvent)
            .where(
                ProvenanceEvent.target_kind == target_kind,
                ProvenanceEvent.target_id == target_id,
            )
            .order_by(ProvenanceEvent.recorded_at.desc())
            .limit(limit)
            .offset(offset)
        )

    rows = (await db.execute(stmt)).scalars().all()
    return [
        ProvenanceEventOut(
            id=str(r.id),
            recorded_at=r.recorded_at.isoformat(),
            target_kind=r.target_kind,
            target_id=str(r.target_id),
            activity=r.activity,
            agent_kind=r.agent_kind,
            agent_subject_id=str(r.agent_subject_id) if r.agent_subject_id else None,
            agent_token_id=str(r.agent_token_id) if r.agent_token_id else None,
            source_kind=r.source_kind,
            source_id=str(r.source_id) if r.source_id else None,
            diff=r.diff,
            event_metadata=r.event_metadata,
            signature_hash=r.signature_hash,
        )
        for r in rows
    ]


__all__ = ["ProvenanceEventOut", "router"]
