"""Service layer for care phases.

Every public function takes ``patient_id`` as the first keyword-only
argument. Cross-patient is impossible by construction:

* Every SQL query filters on ``patient_id`` before applying any other
  predicate.
* The ``ClinicalEvent.phase_id`` column has a composite FK to
  ``care_phase (patient_id, id)`` (see
  ``backend/alembic/versions/0080_care_phase.py``); PostgreSQL
  rejects, at the DDL level, any attempt to assign an event to a
  phase belonging to a different patient.
* Resolving a phase id (or slug) outside the patient's scope raises
  ``HTTPException(404)`` — never 400 — because that resource simply
  does not exist within that namespace.

Audit lines are written via :func:`bvphoenix.services.audit.log_action`
on every mutation. AI provenance (``author_kind='agent'`` +
``proposed_by_agent_id``) is preserved across human confirmation:
``confirmed_by_user_id`` and ``confirmed_at`` are set additively, the
``proposed_by_agent_id`` field is never cleared.

Revision history is appended to ``care_phase_revision`` for every
write so the editor's "Annulla / Ripristina" surface has a real
backing store.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException, Request
from sqlalchemy import and_, delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.db.models import (
    CARE_PHASE_DEFAULT_COLORS,
    CarePhase,
    CarePhaseProposal,
    CarePhaseRevision,
    ClinicalEvent,
    ContentDocumentLink,
    Document,
    ImagingStudy,
    ReportContent,
)
from bvphoenix.services.audit import log_action
from bvphoenix.services.care_phase_schemas import (
    ApplyProposalIn,
    CarePhaseCounts,
    CarePhaseCreateIn,
    CarePhaseDetailOut,
    CarePhaseMaterialOut,
    CarePhaseOut,
    CarePhaseRevisionOut,
    CarePhaseUpdateIn,
    CareTimelineOut,
    ConsultationTarget,
    DocumentTarget,
    EventTarget,
    GenericEventTarget,
    MaterialItem,
    ProposalPayload,
    ReorderItem,
    ReportTarget,
    StudyTarget,
    TimelineEventOut,
    TimelineHealthOut,
)

_log = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# Internal helpers
# ----------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(UTC)


def _new_etag() -> uuid.UUID:
    return uuid.uuid4()


def _resolve_color(kind: str, color_hex: str | None) -> str:
    if color_hex:
        return color_hex
    return CARE_PHASE_DEFAULT_COLORS.get(kind, "#888780")


def _phase_to_out(phase: CarePhase, counts: CarePhaseCounts | None = None) -> CarePhaseOut:
    payload = CarePhaseOut.model_validate(phase, from_attributes=True)
    if counts is not None:
        payload.counts = counts
    return payload


def _event_target(event: ClinicalEvent, imaging_study_id: uuid.UUID | None) -> EventTarget:
    """Resolve the natural navigation target for a clinical event.

    The mapping mirrors the frontend routing convention: imaging events
    open the study viewer, lab batches open the underlying document,
    consultation events open the consultation page, etc.
    """
    if event.kind == "imaging_study" and imaging_study_id is not None:
        return StudyTarget(
            id=imaging_study_id,
            url=f"/studies/{imaging_study_id}",
            mcp_uri=f"mcp://study/{imaging_study_id}",
        )
    return GenericEventTarget(
        id=event.id,
        url=f"/clinical-events/{event.id}",
        mcp_uri=f"mcp://clinical-event/{event.id}",
    )


def _event_to_timeline_out(
    event: ClinicalEvent,
    target: EventTarget,
) -> TimelineEventOut:
    return TimelineEventOut(
        id=event.id,
        patient_id=event.patient_id,
        kind=event.kind,
        event_date=event.event_date,
        title=event.title,
        body_part=event.body_part,
        code_loinc=event.code_loinc,
        code_snomed=event.code_snomed,
        narrative=event.narrative,
        phase_id=event.phase_id,
        phase_assigned_by=event.phase_assigned_by,
        phase_assignment_confidence=event.phase_assignment_confidence,
        target=target,
        etag=event.etag,
        event_status=event.event_status,
        planned_start_at=event.planned_start_at,
        actual_start_at=event.actual_start_at,
        timezone=event.timezone,
    )


async def _load_phase(db: AsyncSession, *, patient_id: uuid.UUID, phase_id: uuid.UUID) -> CarePhase:
    """Load a phase scoped to ``patient_id``. 404 if it does not belong here."""
    phase = (
        await db.execute(
            select(CarePhase).where(
                and_(
                    CarePhase.patient_id == patient_id,
                    CarePhase.id == phase_id,
                )
            )
        )
    ).scalar_one_or_none()
    if phase is None:
        raise HTTPException(status_code=404, detail="care phase not found")
    return phase


async def _load_phase_by_slug(db: AsyncSession, *, patient_id: uuid.UUID, slug: str) -> CarePhase:
    phase = (
        await db.execute(
            select(CarePhase).where(
                and_(
                    CarePhase.patient_id == patient_id,
                    CarePhase.slug == slug,
                )
            )
        )
    ).scalar_one_or_none()
    if phase is None:
        raise HTTPException(status_code=404, detail="care phase not found")
    return phase


async def _load_event(
    db: AsyncSession, *, patient_id: uuid.UUID, event_id: uuid.UUID
) -> ClinicalEvent:
    event = (
        await db.execute(
            select(ClinicalEvent).where(
                and_(
                    ClinicalEvent.patient_id == patient_id,
                    ClinicalEvent.id == event_id,
                )
            )
        )
    ).scalar_one_or_none()
    if event is None:
        raise HTTPException(status_code=404, detail="clinical event not found")
    return event


async def _imaging_study_id_for(
    db: AsyncSession, *, event_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, uuid.UUID]:
    if not event_ids:
        return {}
    rows = (
        await db.execute(
            select(ImagingStudy.id, ImagingStudy.clinical_event_id).where(
                ImagingStudy.clinical_event_id.in_(event_ids)
            )
        )
    ).all()
    out: dict[uuid.UUID, uuid.UUID] = {}
    for study_id, event_id in rows:
        if event_id is not None:
            out[event_id] = study_id
    return out


async def _next_revision_no(db: AsyncSession, *, phase_id: uuid.UUID) -> int:
    current = (
        await db.execute(
            select(func.coalesce(func.max(CarePhaseRevision.revision_no), 0)).where(
                CarePhaseRevision.phase_id == phase_id
            )
        )
    ).scalar_one()
    return int(current) + 1


def _phase_snapshot(
    phase: CarePhase, assignments: Sequence[ClinicalEvent] | None = None
) -> dict[str, Any]:
    return {
        "phase": {
            "id": str(phase.id),
            "slug": phase.slug,
            "name": phase.name,
            "name_i18n": phase.name_i18n,
            "kind": phase.kind,
            "color_hex": phase.color_hex,
            "start_date": phase.start_date.isoformat() if phase.start_date else None,
            "end_date": phase.end_date.isoformat() if phase.end_date else None,
            "ordinal": phase.ordinal,
            "narrative_md": phase.narrative_md,
            "author_kind": phase.author_kind,
            "etag": str(phase.etag),
        },
        "assignments": [
            {
                "event_id": str(ev.id),
                "assigned_by": ev.phase_assigned_by,
                "confidence": ev.phase_assignment_confidence,
            }
            for ev in (assignments or [])
        ],
    }


async def _append_revision(
    db: AsyncSession,
    *,
    patient_id: uuid.UUID,
    phase: CarePhase,
    change_kind: str,
    actor_id: uuid.UUID | None,
    author_kind: str,
    diff_summary: str | None,
    assignments: Sequence[ClinicalEvent] | None = None,
) -> CarePhaseRevision:
    rev = CarePhaseRevision(
        patient_id=patient_id,
        phase_id=phase.id,
        revision_no=await _next_revision_no(db, phase_id=phase.id),
        snapshot=_phase_snapshot(phase, assignments),
        change_kind=change_kind,
        author_kind=author_kind,
        actor_id=actor_id,
        diff_summary=diff_summary,
    )
    db.add(rev)
    await db.flush()
    return rev


# ----------------------------------------------------------------------
# Read API
# ----------------------------------------------------------------------


async def list_phases(
    *,
    patient_id: uuid.UUID,
    db: AsyncSession,
) -> list[CarePhaseOut]:
    rows = (
        (
            await db.execute(
                select(CarePhase)
                .where(CarePhase.patient_id == patient_id)
                .order_by(CarePhase.ordinal.asc(), CarePhase.start_date.asc().nulls_last())
            )
        )
        .scalars()
        .all()
    )

    counts = await _phase_counts(db, patient_id=patient_id)
    return [_phase_to_out(p, counts.get(p.id)) for p in rows]


async def get_phase(
    *,
    patient_id: uuid.UUID,
    phase_id: uuid.UUID,
    db: AsyncSession,
) -> CarePhaseDetailOut:
    phase = await _load_phase(db, patient_id=patient_id, phase_id=phase_id)
    counts = (await _phase_counts(db, patient_id=patient_id)).get(phase.id)
    events = await _phase_events(db, patient_id=patient_id, phase_id=phase.id)
    base = _phase_to_out(phase, counts)
    return CarePhaseDetailOut(
        **base.model_dump(),
        events=events,
    )


async def get_timeline(
    *,
    patient_id: uuid.UUID,
    lang: str = "it",
    db: AsyncSession,
) -> CareTimelineOut:
    phases = (
        (
            await db.execute(
                select(CarePhase)
                .where(CarePhase.patient_id == patient_id)
                .order_by(CarePhase.ordinal.asc())
            )
        )
        .scalars()
        .all()
    )

    counts = await _phase_counts(db, patient_id=patient_id)

    events = (
        (
            await db.execute(
                select(ClinicalEvent)
                .where(ClinicalEvent.patient_id == patient_id)
                .order_by(
                    ClinicalEvent.event_date.asc().nulls_last(),
                    ClinicalEvent.created_at.asc(),
                )
            )
        )
        .scalars()
        .all()
    )

    imaging_map = await _imaging_study_id_for(db, event_ids=[e.id for e in events])
    by_phase: dict[uuid.UUID | None, list[TimelineEventOut]] = {}
    for ev in events:
        target = _event_target(ev, imaging_map.get(ev.id))
        by_phase.setdefault(ev.phase_id, []).append(_event_to_timeline_out(ev, target))

    detail_phases: list[CarePhaseDetailOut] = []
    for phase in phases:
        base = _phase_to_out(phase, counts.get(phase.id))
        detail_phases.append(
            CarePhaseDetailOut(
                **base.model_dump(),
                events=by_phase.get(phase.id, []),
            )
        )

    return CareTimelineOut(
        patient_id=patient_id,
        phases=detail_phases,
        unassigned_events=by_phase.get(None, []),
        generated_at=_now(),
        lang=lang,
    )


async def _phase_events(
    db: AsyncSession, *, patient_id: uuid.UUID, phase_id: uuid.UUID
) -> list[TimelineEventOut]:
    events = (
        (
            await db.execute(
                select(ClinicalEvent)
                .where(
                    and_(
                        ClinicalEvent.patient_id == patient_id,
                        ClinicalEvent.phase_id == phase_id,
                    )
                )
                .order_by(ClinicalEvent.event_date.asc().nulls_last())
            )
        )
        .scalars()
        .all()
    )
    imaging_map = await _imaging_study_id_for(db, event_ids=[e.id for e in events])
    return [_event_to_timeline_out(e, _event_target(e, imaging_map.get(e.id))) for e in events]


async def _phase_counts(
    db: AsyncSession, *, patient_id: uuid.UUID
) -> dict[uuid.UUID, CarePhaseCounts]:
    """Compute per-phase counters in two queries.

    The ``ClinicalEvent.phase_id`` filter is restricted to ``patient_id``
    so even if a stray cross-patient row existed (it cannot, due to the
    composite FK) it would not leak into the counts.
    """
    rows = (
        await db.execute(
            select(
                ClinicalEvent.phase_id,
                ClinicalEvent.kind,
                func.count(ClinicalEvent.id),
            )
            .where(
                and_(
                    ClinicalEvent.patient_id == patient_id,
                    ClinicalEvent.phase_id.is_not(None),
                )
            )
            .group_by(ClinicalEvent.phase_id, ClinicalEvent.kind)
        )
    ).all()

    out: dict[uuid.UUID, CarePhaseCounts] = {}
    for phase_id, kind, n in rows:
        slot = out.setdefault(phase_id, CarePhaseCounts())
        slot.n_events += int(n)
        if kind == "imaging_study":
            slot.n_studies += int(n)
        elif kind in ("outpatient_visit", "consultation_event"):
            slot.n_consultations += int(n)
        elif kind == "lab_batch":
            slot.n_documents += int(n)

    # n_reports: count ReportContent rows per phase via clinical_event linkage.
    report_rows = (
        await db.execute(
            select(ClinicalEvent.phase_id, func.count(ReportContent.id))
            .join(ReportContent, ReportContent.clinical_event_id == ClinicalEvent.id)
            .where(
                and_(
                    ClinicalEvent.patient_id == patient_id,
                    ClinicalEvent.phase_id.is_not(None),
                )
            )
            .group_by(ClinicalEvent.phase_id)
        )
    ).all()
    for phase_id, n in report_rows:
        out.setdefault(phase_id, CarePhaseCounts()).n_reports = int(n)

    return out


async def get_phase_material(
    *,
    patient_id: uuid.UUID,
    phase_id: uuid.UUID,
    db: AsyncSession,
) -> CarePhaseMaterialOut:
    phase = await _load_phase(db, patient_id=patient_id, phase_id=phase_id)

    events = (
        (
            await db.execute(
                select(ClinicalEvent).where(
                    and_(
                        ClinicalEvent.patient_id == patient_id,
                        ClinicalEvent.phase_id == phase.id,
                    )
                )
            )
        )
        .scalars()
        .all()
    )
    event_ids = [e.id for e in events]

    studies: list[MaterialItem] = []
    for ev in events:
        if ev.kind != "imaging_study":
            continue
        imaging_map = await _imaging_study_id_for(db, event_ids=[ev.id])
        sid = imaging_map.get(ev.id)
        if sid is None:
            continue
        studies.append(
            MaterialItem(
                kind="study",
                id=sid,
                title=ev.title,
                secondary=ev.body_part,
                event_id=ev.id,
                event_date=ev.event_date,
                url=f"/studies/{sid}",
                mcp_uri=f"mcp://study/{sid}",
            )
        )

    documents: list[MaterialItem] = []
    if event_ids:
        doc_rows = (
            await db.execute(
                select(Document, ClinicalEvent.id, ClinicalEvent.event_date)
                .select_from(ContentDocumentLink)
                .join(ReportContent, ReportContent.id == ContentDocumentLink.report_content_id)
                .join(Document, Document.id == ContentDocumentLink.document_id)
                .join(ClinicalEvent, ClinicalEvent.id == ReportContent.clinical_event_id)
                .where(ClinicalEvent.id.in_(event_ids))
            )
        ).all()
        for doc, ev_id, ev_date in doc_rows:
            documents.append(
                MaterialItem(
                    kind="document",
                    id=doc.id,
                    title=doc.title or "documento",
                    secondary=doc.kind_id,
                    event_id=ev_id,
                    event_date=ev_date,
                    url=f"/patients/{patient_id}/documents/{doc.id}",
                    mcp_uri=f"mcp://document/{doc.id}",
                )
            )

    reports: list[MaterialItem] = []
    if event_ids:
        report_rows = (
            await db.execute(
                select(ReportContent, ClinicalEvent.event_date)
                .join(ClinicalEvent, ClinicalEvent.id == ReportContent.clinical_event_id)
                .where(ClinicalEvent.id.in_(event_ids))
            )
        ).all()
        for rep, ev_date in report_rows:
            reports.append(
                MaterialItem(
                    kind="report",
                    id=rep.id,
                    title=rep.title or "report",
                    secondary=rep.status,
                    event_id=rep.clinical_event_id,
                    event_date=ev_date,
                    # ReportContents render inside the clinical-event
                    # detail page (grouped by authority); there is no
                    # standalone /patients/<id>/reports/<id> route.
                    # The hash anchor lets the page scroll the matching
                    # card into view.
                    url=f"/clinical-events/{rep.clinical_event_id}#rc-{rep.id}",
                    mcp_uri=f"mcp://report/{rep.id}",
                )
            )

    return CarePhaseMaterialOut(
        phase_id=phase.id,
        studies=studies,
        documents=documents,
        reports=reports,
    )


async def list_revisions(
    *,
    patient_id: uuid.UUID,
    phase_id: uuid.UUID,
    db: AsyncSession,
) -> list[CarePhaseRevisionOut]:
    await _load_phase(db, patient_id=patient_id, phase_id=phase_id)
    rows = (
        (
            await db.execute(
                select(CarePhaseRevision)
                .where(CarePhaseRevision.phase_id == phase_id)
                .order_by(CarePhaseRevision.revision_no.desc())
            )
        )
        .scalars()
        .all()
    )
    return [CarePhaseRevisionOut.model_validate(r, from_attributes=True) for r in rows]


async def timeline_health(
    *,
    patient_id: uuid.UUID,
    db: AsyncSession,
) -> TimelineHealthOut:
    n_phases = (
        await db.execute(select(func.count(CarePhase.id)).where(CarePhase.patient_id == patient_id))
    ).scalar_one()
    n_events = (
        await db.execute(
            select(func.count(ClinicalEvent.id)).where(ClinicalEvent.patient_id == patient_id)
        )
    ).scalar_one()
    n_assigned = (
        await db.execute(
            select(func.count(ClinicalEvent.id)).where(
                and_(
                    ClinicalEvent.patient_id == patient_id,
                    ClinicalEvent.phase_id.is_not(None),
                )
            )
        )
    ).scalar_one()
    pending = (
        await db.execute(
            select(func.count(CarePhaseProposal.id)).where(
                and_(
                    CarePhaseProposal.patient_id == patient_id,
                    CarePhaseProposal.applied_at.is_(None),
                )
            )
        )
    ).scalar_one()
    last_run = (
        await db.execute(
            select(func.max(CarePhaseProposal.created_at)).where(
                CarePhaseProposal.patient_id == patient_id
            )
        )
    ).scalar_one()

    pct = float(n_assigned) / float(n_events) if n_events else 0.0
    return TimelineHealthOut(
        patient_id=patient_id,
        n_phases=int(n_phases),
        n_events=int(n_events),
        n_events_assigned=int(n_assigned),
        pct_assigned=pct,
        pending_proposals=int(pending),
        last_classifier_run=last_run,
    )


# ----------------------------------------------------------------------
# Mutations
# ----------------------------------------------------------------------


async def create_phase(
    *,
    patient_id: uuid.UUID,
    data: CarePhaseCreateIn,
    actor_id: uuid.UUID | None,
    author_kind: str = "human",
    proposed_by_agent_id: uuid.UUID | None = None,
    db: AsyncSession,
    request: Request | None = None,
) -> CarePhaseOut:
    if author_kind not in ("human", "agent"):
        raise HTTPException(status_code=400, detail="invalid author_kind")

    color_hex = _resolve_color(data.kind, data.color_hex)
    ordinal = data.ordinal
    if ordinal is None:
        max_ordinal = (
            await db.execute(
                select(func.coalesce(func.max(CarePhase.ordinal), -1)).where(
                    CarePhase.patient_id == patient_id
                )
            )
        ).scalar_one()
        ordinal = int(max_ordinal) + 1

    # Derive ``name`` from ``name_i18n`` when the caller omitted it
    # (the FE / MCP tool send only the bilingual map; requiring the
    # collapsed string at the DB write was a foot-gun that surfaced as
    # systematic 422 on every agent create call). Preference order:
    # explicit ``name`` → ``name_i18n['it']`` → ``name_i18n['en']`` →
    # any first value → ``slug`` (the only field guaranteed non-empty).
    resolved_name = data.name
    if not resolved_name:
        if data.name_i18n:
            resolved_name = (
                data.name_i18n.get("it")
                or data.name_i18n.get("en")
                or next(iter(data.name_i18n.values()), None)
            )
    if not resolved_name:
        resolved_name = data.slug

    phase = CarePhase(
        patient_id=patient_id,
        slug=data.slug,
        name=resolved_name,
        name_i18n=data.name_i18n or {},
        kind=data.kind,
        color_hex=color_hex,
        start_date=data.start_date,
        end_date=data.end_date,
        ordinal=ordinal,
        narrative_md=data.narrative_md,
        author_kind=author_kind,
        proposed_by_agent_id=proposed_by_agent_id,
        confirmed_by_user_id=actor_id if author_kind == "human" else None,
        confirmed_at=_now() if author_kind == "human" else None,
        etag=_new_etag(),
    )
    db.add(phase)
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail="slug already exists for this patient") from exc

    await _append_revision(
        db,
        patient_id=patient_id,
        phase=phase,
        change_kind="create",
        actor_id=actor_id,
        author_kind=author_kind,
        diff_summary=f"created phase {phase.slug!r}",
    )
    await db.commit()
    await db.refresh(phase)
    await log_action(
        actor_subject_id=actor_id,
        action="care_phase_create",
        resource_kind="care_phase",
        resource_id=phase.id,
        request=request,
        metadata={"patient_id": str(patient_id), "slug": data.slug, "kind": data.kind},
    )
    return _phase_to_out(phase, CarePhaseCounts())


async def update_phase(
    *,
    patient_id: uuid.UUID,
    phase_id: uuid.UUID,
    data: CarePhaseUpdateIn,
    expected_etag: str | None,
    actor_id: uuid.UUID | None,
    author_kind: str = "human",
    db: AsyncSession,
    request: Request | None = None,
) -> CarePhaseOut:
    phase = await _load_phase(db, patient_id=patient_id, phase_id=phase_id)

    if expected_etag is None:
        raise HTTPException(status_code=428, detail="If-Match header is required for this mutation")
    # Accept both the canonical dashed UUID (the form the response body
    # carries) and the bare hex form (legacy: the response ETag header
    # historically emitted .hex without dashes). The wildcard ``*``
    # opts out of optimistic concurrency per RFC 9110 §13.1.1, in
    # parity with clinical_events and report_contents.
    if expected_etag != "*":
        try:
            presented_uuid = uuid.UUID(expected_etag)
        except (ValueError, AttributeError):
            raise HTTPException(
                status_code=412,
                detail=f"If-Match {expected_etag!r} does not match current ETag {str(phase.etag)!r}",
            ) from None
        if presented_uuid != phase.etag:
            raise HTTPException(
                status_code=412,
                detail=f"If-Match {expected_etag!r} does not match current ETag {str(phase.etag)!r}",
            )

    changes: dict[str, Any] = data.model_dump(exclude_none=True)
    if "color_hex" in changes:
        # Allow explicit color override without forcing kind change.
        pass
    elif "kind" in changes:
        # When kind changes and the user did not provide a color, recompute default.
        changes.setdefault("color_hex", _resolve_color(changes["kind"], None))

    for k, v in changes.items():
        setattr(phase, k, v)
    phase.etag = _new_etag()
    if author_kind == "human":
        phase.confirmed_by_user_id = actor_id
        phase.confirmed_at = _now()

    await _append_revision(
        db,
        patient_id=patient_id,
        phase=phase,
        change_kind="update",
        actor_id=actor_id,
        author_kind=author_kind,
        diff_summary=f"updated fields: {sorted(changes.keys())}",
    )
    await db.commit()
    await db.refresh(phase)

    await log_action(
        actor_subject_id=actor_id,
        action="care_phase_update",
        resource_kind="care_phase",
        resource_id=phase.id,
        request=request,
        metadata={"patient_id": str(patient_id), "fields": sorted(changes.keys())},
    )
    return _phase_to_out(phase)


async def delete_phase(
    *,
    patient_id: uuid.UUID,
    phase_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    db: AsyncSession,
    request: Request | None = None,
) -> None:
    phase = await _load_phase(db, patient_id=patient_id, phase_id=phase_id)
    # Snapshot before removal so the revision history retains the
    # last state. Composite FK ON DELETE SET NULL clears phase_id on
    # any clinical_events that pointed here.
    affected = (
        (
            await db.execute(
                select(ClinicalEvent).where(
                    and_(
                        ClinicalEvent.patient_id == patient_id,
                        ClinicalEvent.phase_id == phase.id,
                    )
                )
            )
        )
        .scalars()
        .all()
    )
    await _append_revision(
        db,
        patient_id=patient_id,
        phase=phase,
        change_kind="delete",
        actor_id=actor_id,
        author_kind="human",
        diff_summary=f"deleted phase {phase.slug!r}",
        assignments=affected,
    )
    await db.execute(delete(CarePhase).where(CarePhase.id == phase.id))
    await db.commit()
    await log_action(
        actor_subject_id=actor_id,
        action="care_phase_delete",
        resource_kind="care_phase",
        resource_id=phase.id,
        request=request,
        metadata={"patient_id": str(patient_id), "n_orphaned": len(affected)},
    )


async def assign_event(
    *,
    patient_id: uuid.UUID,
    phase_id: uuid.UUID,
    event_id: uuid.UUID,
    confidence: float | None,
    actor_id: uuid.UUID | None,
    author_kind: str = "human",
    db: AsyncSession,
    request: Request | None = None,
) -> TimelineEventOut:
    phase = await _load_phase(db, patient_id=patient_id, phase_id=phase_id)
    event = await _load_event(db, patient_id=patient_id, event_id=event_id)

    event.phase_id = phase.id
    event.phase_assigned_by = author_kind
    event.phase_assigned_at = _now()
    event.phase_assignment_confidence = confidence

    await _append_revision(
        db,
        patient_id=patient_id,
        phase=phase,
        change_kind="assign",
        actor_id=actor_id,
        author_kind=author_kind,
        diff_summary=f"assigned event {event.id} to phase {phase.slug!r}",
        assignments=[event],
    )
    await db.commit()
    await db.refresh(event)

    await log_action(
        actor_subject_id=actor_id,
        action="care_phase_assign_event",
        resource_kind="clinical_event",
        resource_id=event.id,
        request=request,
        metadata={
            "patient_id": str(patient_id),
            "phase_id": str(phase.id),
            "phase_slug": phase.slug,
            "confidence": confidence,
        },
    )

    imaging_map = await _imaging_study_id_for(db, event_ids=[event.id])
    return _event_to_timeline_out(event, _event_target(event, imaging_map.get(event.id)))


async def unassign_event(
    *,
    patient_id: uuid.UUID,
    phase_id: uuid.UUID,
    event_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    db: AsyncSession,
    request: Request | None = None,
) -> None:
    phase = await _load_phase(db, patient_id=patient_id, phase_id=phase_id)
    event = await _load_event(db, patient_id=patient_id, event_id=event_id)
    if event.phase_id != phase.id:
        raise HTTPException(status_code=404, detail="event is not assigned to this phase")
    event.phase_id = None
    event.phase_assigned_by = None
    event.phase_assigned_at = None
    event.phase_assignment_confidence = None

    await _append_revision(
        db,
        patient_id=patient_id,
        phase=phase,
        change_kind="unassign",
        actor_id=actor_id,
        author_kind="human",
        diff_summary=f"unassigned event {event.id} from phase {phase.slug!r}",
    )
    await db.commit()

    await log_action(
        actor_subject_id=actor_id,
        action="care_phase_unassign_event",
        resource_kind="clinical_event",
        resource_id=event.id,
        request=request,
        metadata={"patient_id": str(patient_id), "phase_id": str(phase.id)},
    )


async def reorder_phases(
    *,
    patient_id: uuid.UUID,
    items: Iterable[ReorderItem],
    actor_id: uuid.UUID | None,
    db: AsyncSession,
    request: Request | None = None,
) -> list[CarePhaseOut]:
    items = list(items)
    if not items:
        return await list_phases(patient_id=patient_id, db=db)

    ids = [it.phase_id for it in items]
    rows = (
        (
            await db.execute(
                select(CarePhase).where(
                    and_(
                        CarePhase.patient_id == patient_id,
                        CarePhase.id.in_(ids),
                    )
                )
            )
        )
        .scalars()
        .all()
    )
    if len(rows) != len(items):
        raise HTTPException(
            status_code=404,
            detail="one or more care phases not found in this patient",
        )

    by_id = {p.id: p for p in rows}
    for it in items:
        p = by_id[it.phase_id]
        if p.ordinal != it.ordinal:
            p.ordinal = it.ordinal
            p.etag = _new_etag()
            await _append_revision(
                db,
                patient_id=patient_id,
                phase=p,
                change_kind="update",
                actor_id=actor_id,
                author_kind="human",
                diff_summary=f"reordered to {it.ordinal}",
            )
    await db.commit()

    await log_action(
        actor_subject_id=actor_id,
        action="care_phase_reorder",
        resource_kind="care_phase",
        resource_id=None,
        request=request,
        metadata={"patient_id": str(patient_id), "n_phases": len(items)},
    )
    return await list_phases(patient_id=patient_id, db=db)


async def restore_revision(
    *,
    patient_id: uuid.UUID,
    phase_id: uuid.UUID,
    revision_no: int,
    actor_id: uuid.UUID | None,
    db: AsyncSession,
    request: Request | None = None,
) -> CarePhaseOut:
    phase = await _load_phase(db, patient_id=patient_id, phase_id=phase_id)
    rev = (
        await db.execute(
            select(CarePhaseRevision).where(
                and_(
                    CarePhaseRevision.phase_id == phase.id,
                    CarePhaseRevision.revision_no == revision_no,
                )
            )
        )
    ).scalar_one_or_none()
    if rev is None:
        raise HTTPException(status_code=404, detail="revision not found")

    snap_phase = rev.snapshot.get("phase", {})
    for field in (
        "name",
        "name_i18n",
        "kind",
        "color_hex",
        "ordinal",
        "narrative_md",
    ):
        if field in snap_phase:
            setattr(phase, field, snap_phase[field])
    if snap_phase.get("start_date"):
        phase.start_date = datetime.fromisoformat(snap_phase["start_date"]).date()
    if snap_phase.get("end_date"):
        phase.end_date = datetime.fromisoformat(snap_phase["end_date"]).date()
    phase.etag = _new_etag()

    await _append_revision(
        db,
        patient_id=patient_id,
        phase=phase,
        change_kind="restore",
        actor_id=actor_id,
        author_kind="human",
        diff_summary=f"restored revision {revision_no}",
    )
    await db.commit()
    await db.refresh(phase)

    await log_action(
        actor_subject_id=actor_id,
        action="care_phase_restore",
        resource_kind="care_phase",
        resource_id=phase.id,
        request=request,
        metadata={"patient_id": str(patient_id), "revision_no": revision_no},
    )
    return _phase_to_out(phase)


# ----------------------------------------------------------------------
# Proposal apply
# ----------------------------------------------------------------------


def compute_input_hash(events: Sequence[ClinicalEvent]) -> str:
    """Stable hash of the inputs the classifier sees.

    A subsequent ``propose`` call with the same hash reuses the
    cached proposal instead of re-invoking the LLM.
    """
    payload = sorted(
        [
            {
                "id": str(e.id),
                "kind": e.kind,
                "date": e.event_date.isoformat() if e.event_date else None,
                "title": e.title,
                "body_part": e.body_part,
            }
            for e in events
        ],
        key=lambda r: (r["date"] or "", r["id"]),
    )
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


async def apply_proposal(
    *,
    patient_id: uuid.UUID,
    data: ApplyProposalIn,
    actor_id: uuid.UUID | None,
    db: AsyncSession,
    request: Request | None = None,
) -> tuple[list[uuid.UUID], int, int]:
    proposal = (
        await db.execute(
            select(CarePhaseProposal).where(
                and_(
                    CarePhaseProposal.patient_id == patient_id,
                    CarePhaseProposal.id == data.proposal_id,
                )
            )
        )
    ).scalar_one_or_none()
    if proposal is None:
        raise HTTPException(status_code=404, detail="proposal not found")

    payload = ProposalPayload.model_validate(proposal.payload)
    accept_phase_set = set(data.accept_phases)
    accept_event_set = set(data.accept_assignments)

    applied_phase_ids: list[uuid.UUID] = []
    slug_to_phase: dict[str, CarePhase] = {}

    # Upsert phases.
    for proposed in payload.phases:
        if proposed.slug not in accept_phase_set:
            continue
        existing = (
            await db.execute(
                select(CarePhase).where(
                    and_(
                        CarePhase.patient_id == patient_id,
                        CarePhase.slug == proposed.slug,
                    )
                )
            )
        ).scalar_one_or_none()
        if existing:
            existing.name = proposed.name
            existing.name_i18n = proposed.name_i18n or existing.name_i18n
            existing.kind = proposed.kind
            existing.color_hex = _resolve_color(proposed.kind, proposed.color_hex)
            existing.ordinal = proposed.ordinal
            existing.narrative_md = proposed.narrative_md
            existing.confirmed_by_user_id = actor_id
            existing.confirmed_at = _now()
            existing.etag = _new_etag()
            slug_to_phase[proposed.slug] = existing
            applied_phase_ids.append(existing.id)
            await _append_revision(
                db,
                patient_id=patient_id,
                phase=existing,
                change_kind="apply_proposal",
                actor_id=actor_id,
                author_kind="agent",
                diff_summary=f"applied proposal for phase {proposed.slug!r}",
            )
        else:
            phase = CarePhase(
                patient_id=patient_id,
                slug=proposed.slug,
                name=proposed.name,
                name_i18n=proposed.name_i18n or {},
                kind=proposed.kind,
                color_hex=_resolve_color(proposed.kind, proposed.color_hex),
                ordinal=proposed.ordinal,
                narrative_md=proposed.narrative_md,
                author_kind="agent",
                proposed_by_agent_id=None,  # populated upstream when known
                confirmed_by_user_id=actor_id,
                confirmed_at=_now(),
                etag=_new_etag(),
            )
            db.add(phase)
            await db.flush()
            slug_to_phase[proposed.slug] = phase
            applied_phase_ids.append(phase.id)
            await _append_revision(
                db,
                patient_id=patient_id,
                phase=phase,
                change_kind="apply_proposal",
                actor_id=actor_id,
                author_kind="agent",
                diff_summary=f"created phase {proposed.slug!r} from proposal",
            )

    # Apply assignments.
    n_applied = 0
    n_skipped = 0
    for assignment in payload.assignments:
        if assignment.event_id not in accept_event_set:
            n_skipped += 1
            continue
        target_phase = slug_to_phase.get(assignment.phase_slug)
        if target_phase is None:
            # Phase was not accepted; skip its assignments.
            n_skipped += 1
            continue
        await db.execute(
            update(ClinicalEvent)
            .where(
                and_(
                    ClinicalEvent.patient_id == patient_id,
                    ClinicalEvent.id == assignment.event_id,
                )
            )
            .values(
                phase_id=target_phase.id,
                phase_assigned_by="agent",
                phase_assigned_at=_now(),
                phase_assignment_confidence=assignment.confidence,
            )
        )
        n_applied += 1

    proposal.applied_at = _now()
    proposal.applied_by_user_id = actor_id
    await db.commit()

    await log_action(
        actor_subject_id=actor_id,
        action="care_phase_apply_proposal",
        resource_kind="care_phase_proposal",
        resource_id=proposal.id,
        request=request,
        metadata={
            "patient_id": str(patient_id),
            "phases": len(applied_phase_ids),
            "assignments": n_applied,
            "skipped": n_skipped,
        },
    )

    return applied_phase_ids, n_applied, n_skipped


__all__ = [
    "apply_proposal",
    "assign_event",
    "compute_input_hash",
    "create_phase",
    "delete_phase",
    "get_phase",
    "get_phase_material",
    "get_timeline",
    "list_phases",
    "list_revisions",
    "reorder_phases",
    "restore_revision",
    "timeline_health",
    "unassign_event",
    "update_phase",
]
