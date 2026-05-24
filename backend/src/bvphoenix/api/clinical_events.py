"""Clinical events — patient-timeline records.

A ClinicalEvent (v3 architecture) is the umbrella concept for any
event in a patient's clinical history. Imaging studies are the most
common projection (``kind='imaging_study'`` + a row in
``imaging_studies`` carrying the DICOM-specific fields), but visits,
procedures, admissions, and lab batches all live on the same axis.

Every report content, document link, citation and tag in v3 attaches
to a ``clinical_event_id``, so a query "give me everything about this
event" crosses zero subtype tables.

Endpoints (v3 phase 3a):
- ``GET /api/clinical-events/{id}`` — read one
- ``GET /api/patients/{patient_id}/clinical-events`` — list for patient
- ``POST /api/clinical-events`` — create (manual, non-imaging)
- ``PATCH /api/clinical-events/{id}`` — update mutable metadata
- ``DELETE /api/clinical-events/{id}`` — delete (refused for imaging_study
  WITH a live imaging_studies row, whose lifecycle is owned by the DICOM
  deletion path because the imaging_studies FK cascades on delete; ALLOWED
  for orphan imaging events whose imaging row is already gone)

Imaging events are created server-side by the bulk-upload pipeline
when a DICOM study lands; manual creation through this endpoint is
for the other event kinds (visits, procedures, admissions, lab
batches). Mutations require ``If-Match`` and bump ``etag`` so
optimistic concurrency works across human / agent edits.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.auth.deps import enforce_agent_patient_scope, require_user
from bvphoenix.db.models import (
    CLINICAL_EVENT_KINDS,
    CLINICAL_EVENT_STATUSES,
    ClinicalEvent,
    ClinicalEventTransition,
    ImagingStudy,
    Patient,
    User,
)
from bvphoenix.db.session import get_db
from bvphoenix.middleware.audit_dependency import AuditDep
from bvphoenix.services import clinical_events_fsm as fsm
from bvphoenix.services.etag import format_etag, parse_if_match
from bvphoenix.services.evidence_links import validate_mentions_or_raise
from bvphoenix.services.notifications.scheduling import (
    cancel_dispatches_for_target,
    materialise_event_dispatches,
)
from bvphoenix.services.permissions import (
    READ_METADATA,
    WRITE_REPORT,
    can_patient,
)
from bvphoenix.services.planned_phase import ensure_planned_phase
from bvphoenix.services.provenance_log import record_provenance

router = APIRouter(tags=["clinical-events"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


EventStatusLiteral = Literal[
    "planned", "confirmed", "completed", "cancelled", "missed", "rescheduled"
]


class ClinicalEventOut(BaseModel):
    id: str
    patient_id: str
    kind: str
    event_date: date | None
    title: str
    body_part: str | None
    code_loinc: str | None
    code_snomed: str | None
    narrative: str | None
    # When ``kind == 'imaging_study'`` the matching imaging_studies
    # row id is surfaced here so callers can fetch DICOM detail with
    # one extra hop. Null for non-imaging events.
    imaging_study_id: str | None
    etag: str
    created_at: str
    updated_at: str
    # ---- planning & calendar (migration 0098) ----------------------
    event_status: str
    planned_start_at: str | None
    planned_end_at: str | None
    actual_start_at: str | None
    actual_end_at: str | None
    timezone: str | None
    location_struct: dict | None
    recurrence_rule: str | None
    recurrence_exdates: list | None
    reminder_offsets_minutes: list[int] | None
    parent_event_id: str | None
    status_changed_at: str | None
    status_changed_by_kind: str | None
    status_change_reason: str | None
    # Meet link + links (migration 0101); attachments are now a
    # separate sub-resource (binary upload table, migration 0102).
    meeting_url: str | None
    links: list | None


class ClinicalEventCreateIn(BaseModel):
    patient_id: uuid.UUID
    kind: str = Field(
        ...,
        description=f"One of {CLINICAL_EVENT_KINDS}. Use 'imaging_study' only "
        "for events that already have an imaging_studies row; the bulk-upload "
        "pipeline normally owns imaging-event creation.",
    )
    event_date: date | None = None
    title: str = Field(..., min_length=1, max_length=255)
    body_part: str | None = Field(default=None, max_length=64)
    code_loinc: str | None = Field(default=None, max_length=32)
    code_snomed: str | None = Field(default=None, max_length=32)
    narrative: str | None = None
    # ---- planning & calendar (optional; default keeps back-compat) ---
    # Default ``completed`` so legacy callers that only set
    # ``event_date`` keep behaving exactly as before — they create a
    # historical event without touching the planning surface.
    event_status: EventStatusLiteral = "completed"
    planned_start_at: datetime | None = None
    planned_end_at: datetime | None = None
    actual_start_at: datetime | None = None
    actual_end_at: datetime | None = None
    timezone: str | None = Field(default=None, max_length=64)
    location_struct: dict | None = None
    recurrence_rule: str | None = Field(default=None, max_length=512)
    recurrence_exdates: list[date] | None = None
    reminder_offsets_minutes: list[int] | None = None
    meeting_url: str | None = Field(default=None, max_length=512)
    links: list[dict] | None = None


class ClinicalEventUpdateIn(BaseModel):
    """Patch-style payload. Every field is optional; only the supplied
    keys are applied. ``patient_id`` and ``event_status`` are
    intentionally immutable here: ``patient_id`` defines who the row
    belongs to; ``event_status`` lives on the dedicated transition
    sub-resources (``/confirm``, ``/reschedule``, ``/complete``,
    ``/cancel``, ``/mark-missed``) so every change is auditable as a
    discrete action with its own FSM check and Idempotency-Key.

    ``kind`` IS patchable: a user may legitimately re-classify a
    misclassified appointment (e.g. ``outpatient_visit`` →
    ``cardio_diagnostic``). The DICOM-ownership guard for
    ``imaging_study`` is enforced server-side: if the row has a live
    ``imaging_studies`` projection the kind cannot change away from
    ``imaging_study``, and a non-imaging row cannot promote to
    ``imaging_study`` (the ingestion pipeline owns that path)."""

    kind: str | None = None
    event_date: date | None = None
    title: str | None = Field(default=None, min_length=1, max_length=255)
    body_part: str | None = Field(default=None, max_length=64)
    code_loinc: str | None = Field(default=None, max_length=32)
    code_snomed: str | None = Field(default=None, max_length=32)
    narrative: str | None = None
    # Planning metadata is patchable (when not in a terminal state,
    # see service-layer FSM in step 2); status itself is not.
    planned_start_at: datetime | None = None
    planned_end_at: datetime | None = None
    timezone: str | None = Field(default=None, max_length=64)
    location_struct: dict | None = None
    recurrence_rule: str | None = Field(default=None, max_length=512)
    recurrence_exdates: list[date] | None = None
    reminder_offsets_minutes: list[int] | None = None
    meeting_url: str | None = Field(default=None, max_length=512)
    links: list[dict] | None = None


# ---- Transition sub-resource bodies ---------------------------------


class ConfirmIn(BaseModel):
    """``POST /clinical-events/{id}/confirm``. confirmed_at defaults
    to server-now if omitted; useful for retroactive confirmation
    (the patient called and said the appointment is set)."""

    confirmed_at: datetime | None = None


class RescheduleIn(BaseModel):
    """``POST /clinical-events/{id}/reschedule``.

    Side effect: creates a new ClinicalEvent row with status='planned'
    pointing at the moved slot, and flips the current event to
    status='rescheduled' linking the two via ``parent_event_id``.
    The composite FK ``(patient_id, parent_event_id)`` guarantees
    same-patient chain.
    """

    new_planned_start_at: datetime
    new_planned_end_at: datetime | None = None
    timezone: str | None = Field(default=None, max_length=64)
    reason: str = Field(min_length=1, max_length=255)


class CompleteIn(BaseModel):
    """``POST /clinical-events/{id}/complete``. Records the realised
    timestamp and an optional narrative (validated against the
    cross-patient mention DSL via ``validate_mentions_or_raise``)."""

    actual_start_at: datetime
    actual_end_at: datetime | None = None
    narrative: str | None = None


class CancelIn(BaseModel):
    """``POST /clinical-events/{id}/cancel``. Reason is required so
    the audit chain has something concrete to show."""

    reason: str = Field(min_length=1, max_length=255)


class MarkMissedIn(BaseModel):
    """``POST /clinical-events/{id}/mark-missed``. Note is optional
    (a no-show often has nothing to add)."""

    note: str | None = Field(default=None, max_length=255)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _load_event_with_imaging(
    db: AsyncSession, event_id: uuid.UUID
) -> tuple[ClinicalEvent, ImagingStudy | None] | None:
    row = (
        await db.execute(select(ClinicalEvent).where(ClinicalEvent.id == event_id))
    ).scalar_one_or_none()
    if row is None:
        return None
    imaging = (
        await db.execute(select(ImagingStudy).where(ImagingStudy.clinical_event_id == event_id))
    ).scalar_one_or_none()
    return row, imaging


def _to_out(ev: ClinicalEvent, imaging: ImagingStudy | None) -> ClinicalEventOut:
    return ClinicalEventOut(
        id=str(ev.id),
        patient_id=str(ev.patient_id),
        kind=ev.kind,
        event_date=ev.event_date,
        title=ev.title,
        body_part=ev.body_part,
        code_loinc=ev.code_loinc,
        code_snomed=ev.code_snomed,
        narrative=ev.narrative,
        imaging_study_id=str(imaging.id) if imaging is not None else None,
        etag=str(ev.etag),
        created_at=ev.created_at.isoformat(),
        updated_at=ev.updated_at.isoformat(),
        event_status=ev.event_status,
        planned_start_at=ev.planned_start_at.isoformat() if ev.planned_start_at else None,
        planned_end_at=ev.planned_end_at.isoformat() if ev.planned_end_at else None,
        actual_start_at=ev.actual_start_at.isoformat() if ev.actual_start_at else None,
        actual_end_at=ev.actual_end_at.isoformat() if ev.actual_end_at else None,
        timezone=ev.timezone,
        location_struct=ev.location_struct,
        recurrence_rule=ev.recurrence_rule,
        recurrence_exdates=ev.recurrence_exdates,
        reminder_offsets_minutes=ev.reminder_offsets_minutes,
        parent_event_id=str(ev.parent_event_id) if ev.parent_event_id else None,
        status_changed_at=ev.status_changed_at.isoformat() if ev.status_changed_at else None,
        status_changed_by_kind=ev.status_changed_by_kind,
        status_change_reason=ev.status_change_reason,
        meeting_url=ev.meeting_url,
        links=ev.links,
    )


async def _record_provenance(
    db: AsyncSession,
    *,
    target_id: uuid.UUID,
    activity: str,
    user: User,
    request: Request,
    diff: dict | None = None,
) -> None:
    """Thin wrapper around :func:`record_provenance` that fixes
    ``target_kind='clinical_event'``. Kept as a per-module name so
    existing call sites do not need rewriting; the heavy lifting is
    in the shared service so the audit chain stays consistent across
    every writer."""
    record_provenance(
        db,
        target_kind="clinical_event",
        target_id=target_id,
        activity=activity,
        user=user,
        request=request,
        diff=diff,
    )


def _event_snapshot(ev: ClinicalEvent) -> dict:
    """Whole-row snapshot persisted on ``clinical_event_transitions``
    for Undo and audit. Stores only DB-native values, ISO-formatting
    timestamps so the JSONB column is round-trip safe."""
    return {
        "id": str(ev.id),
        "patient_id": str(ev.patient_id),
        "kind": ev.kind,
        "title": ev.title,
        "event_date": ev.event_date.isoformat() if ev.event_date else None,
        "narrative": ev.narrative,
        "event_status": ev.event_status,
        "planned_start_at": (ev.planned_start_at.isoformat() if ev.planned_start_at else None),
        "planned_end_at": (ev.planned_end_at.isoformat() if ev.planned_end_at else None),
        "actual_start_at": (ev.actual_start_at.isoformat() if ev.actual_start_at else None),
        "actual_end_at": (ev.actual_end_at.isoformat() if ev.actual_end_at else None),
        "timezone": ev.timezone,
        "location_struct": ev.location_struct,
        "parent_event_id": (str(ev.parent_event_id) if ev.parent_event_id else None),
        "status_change_reason": ev.status_change_reason,
        "meeting_url": ev.meeting_url,
        "links": ev.links,
        "etag": str(ev.etag),
    }


def _author_kind(request: Request) -> str:
    """Translate the auth middleware's ``request.state.is_agent`` flag
    into the audit-friendly enum used by both
    ``clinical_events.status_changed_by_kind`` and
    ``clinical_event_transitions.author_kind``."""
    return "agent" if getattr(request.state, "is_agent", False) else "human"


from bvphoenix.services.etag import enforce_if_match_value


async def _check_if_match(if_match: str | None, current_etag: str) -> None:
    """Thin async wrapper around :func:`enforce_if_match_value` —
    kept awaitable so the existing ``await _check_if_match(...)``
    sprinkled across the FSM transitions stays one-line."""
    enforce_if_match_value(if_match, current_etag)


async def _idempotency_replay(
    db: AsyncSession,
    *,
    event_id: uuid.UUID,
    action: str,
    idempotency_key: str,
) -> ClinicalEventTransition | None:
    """If a previous transition with the same (event_id, action, key)
    exists, return its row so the caller can echo ``snapshot_after``
    without re-running. Honours the agent-side idempotency contract."""
    row = (
        await db.execute(
            select(ClinicalEventTransition).where(
                ClinicalEventTransition.event_id == event_id,
                ClinicalEventTransition.action == action,
                ClinicalEventTransition.idempotency_key == idempotency_key,
            )
        )
    ).scalar_one_or_none()
    return row


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/clinical-events/{event_id}",
    response_model=ClinicalEventOut,
    status_code=status.HTTP_200_OK,
)
async def read_clinical_event(
    event_id: uuid.UUID,
    request: Request,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    audit: AuditDep,
    if_none_match: Annotated[str | None, Header(alias="If-None-Match")] = None,
) -> ClinicalEventOut:
    loaded = await _load_event_with_imaging(db, event_id)
    if loaded is None:
        raise HTTPException(status_code=404, detail="clinical event not found")
    ev, imaging = loaded

    patient = (
        await db.execute(select(Patient).where(Patient.id == ev.patient_id))
    ).scalar_one_or_none()
    if patient is None or not await can_patient(
        db, user=user, action=READ_METADATA, patient=patient
    ):
        raise HTTPException(status_code=404, detail="clinical event not found")
    enforce_agent_patient_scope(request, patient.id)

    if if_none_match is not None and if_none_match.strip('"') == str(ev.etag):
        # Tell FastAPI to return 304 without body. Raise via HTTPException
        # is the cleanest path that bypasses response_model validation.
        raise HTTPException(status_code=status.HTTP_304_NOT_MODIFIED)

    out = _to_out(ev, imaging)
    request.state.response_etag = out.etag
    return out


@router.get(
    "/patients/{patient_id}/clinical-events",
    response_model=list[ClinicalEventOut],
)
async def list_patient_clinical_events(
    patient_id: uuid.UUID,
    request: Request,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    audit: AuditDep,
    kind: Annotated[
        str | None, Query(description="Filter by event kind (e.g. 'imaging_study')")
    ] = None,
    statuses: Annotated[
        list[str] | None,
        Query(
            description=(
                "Filter by event_status (multi). Allowed: planned, confirmed, "
                "completed, cancelled, missed, rescheduled. Omit to include all."
            ),
        ),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[ClinicalEventOut]:
    patient = (
        await db.execute(select(Patient).where(Patient.id == patient_id))
    ).scalar_one_or_none()
    if patient is None or not await can_patient(
        db, user=user, action=READ_METADATA, patient=patient
    ):
        raise HTTPException(status_code=404, detail="patient not found")
    enforce_agent_patient_scope(request, patient.id)

    if statuses is not None:
        invalid = [s for s in statuses if s not in CLINICAL_EVENT_STATUSES]
        if invalid:
            raise HTTPException(
                status_code=422,
                detail=f"invalid status(es) {invalid}, allowed: {list(CLINICAL_EVENT_STATUSES)}",
            )

    stmt = (
        select(ClinicalEvent)
        .where(ClinicalEvent.patient_id == patient_id)
        .order_by(ClinicalEvent.event_date.desc().nulls_last(), ClinicalEvent.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    if kind is not None:
        stmt = stmt.where(ClinicalEvent.kind == kind)
    if statuses:
        stmt = stmt.where(ClinicalEvent.event_status.in_(statuses))

    events = (await db.execute(stmt)).scalars().all()

    # Single batched query for the imaging projections.
    imaging_by_event: dict[uuid.UUID, ImagingStudy] = {}
    if events:
        event_ids = [e.id for e in events]
        imaging_rows = (
            (
                await db.execute(
                    select(ImagingStudy).where(ImagingStudy.clinical_event_id.in_(event_ids))
                )
            )
            .scalars()
            .all()
        )
        imaging_by_event = {row.clinical_event_id: row for row in imaging_rows}  # type: ignore[misc]

    return [_to_out(e, imaging_by_event.get(e.id)) for e in events]


class EventCandidate(BaseModel):
    """One ranked candidate ClinicalEvent for a Document → Event link.

    The agent picks one and follows up with a ``confirm_event_link``
    that creates the ReportContent + ContentDocumentLink. Score is
    a non-binding hint based on temporal proximity (and, in a future
    iteration, body-part / kind / OCR-extracted-date matching)."""

    event_id: str
    score: float
    title: str
    kind: str
    event_date: date | None
    rationale: str


@router.get(
    "/documents/{document_id}/propose-events",
    response_model=list[EventCandidate],
)
async def propose_event_links_for_document(
    document_id: uuid.UUID,
    request: Request,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    audit: AuditDep,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
) -> list[EventCandidate]:
    """Suggest ClinicalEvents that the document might belong to.

    MVP heuristic: the patient's events ordered by recency, with a
    score that penalises temporal distance from ``document.document_date``
    (or the document's ``created_at`` when document_date is null).
    Returns the top ``limit`` candidates; the agent picks one and
    confirms via ``confirm_event_link``. Cross-patient candidates
    are never proposed (the join goes through patient_id)."""
    from bvphoenix.db.models import Document

    doc = (
        await db.execute(select(Document).where(Document.id == document_id))
    ).scalar_one_or_none()
    if doc is None:
        raise HTTPException(status_code=404, detail="document not found")
    patient = (
        await db.execute(select(Patient).where(Patient.id == doc.patient_id))
    ).scalar_one_or_none()
    if patient is None or not await can_patient(
        db, user=user, action=READ_METADATA, patient=patient
    ):
        raise HTTPException(status_code=404, detail="document not found")
    enforce_agent_patient_scope(request, patient.id)

    anchor_date = doc.document_date or doc.created_at.date()
    rows = (
        (
            await db.execute(
                select(ClinicalEvent)
                .where(ClinicalEvent.patient_id == doc.patient_id)
                .order_by(
                    ClinicalEvent.event_date.desc().nulls_last(), ClinicalEvent.created_at.desc()
                )
                .limit(limit * 4)  # over-fetch then re-rank
            )
        )
        .scalars()
        .all()
    )

    scored: list[EventCandidate] = []
    for ev in rows:
        if ev.event_date is None:
            distance_days = 365  # null date is a weak match
            rationale = "no event_date on candidate"
        else:
            distance_days = abs((ev.event_date - anchor_date).days)
            rationale = (
                f"{distance_days}d from document anchor"
                if distance_days > 0
                else "same day as document anchor"
            )
        # Triangular score: 1.0 at distance 0, linear decay to 0 at 90 days.
        score = max(0.0, 1.0 - (distance_days / 90.0))
        scored.append(
            EventCandidate(
                event_id=str(ev.id),
                score=round(score, 3),
                title=ev.title,
                kind=ev.kind,
                event_date=ev.event_date,
                rationale=rationale,
            )
        )

    scored.sort(key=lambda c: c.score, reverse=True)
    return scored[:limit]


@router.post(
    "/clinical-events",
    response_model=ClinicalEventOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_clinical_event(
    body: ClinicalEventCreateIn,
    request: Request,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    audit: AuditDep,
) -> ClinicalEventOut:
    if body.kind not in CLINICAL_EVENT_KINDS:
        raise HTTPException(
            status_code=422,
            detail=f"invalid kind, allowed: {sorted(CLINICAL_EVENT_KINDS)}",
        )
    if body.kind == "imaging_study":
        # Imaging events are created by the ingestion pipeline alongside
        # the imaging_studies row. Manual creation through this endpoint
        # would leave a dangling clinical_event with no DICOM payload.
        raise HTTPException(
            status_code=422,
            detail="imaging_study events are created by the ingestion pipeline, "
            "not via the manual endpoint",
        )
    patient = (
        await db.execute(select(Patient).where(Patient.id == body.patient_id))
    ).scalar_one_or_none()
    if patient is None or not await can_patient(
        db, user=user, action=WRITE_REPORT, patient=patient
    ):
        raise HTTPException(status_code=404, detail="patient not found")
    enforce_agent_patient_scope(request, patient.id)

    if body.narrative:
        await validate_mentions_or_raise(db, patient_id=patient.id, body=body.narrative)

    # FSM invariant for create: planned/confirmed require a
    # ``planned_start_at`` (a scheduled event must know its when).
    # The DB CHECK also enforces this, but we want a 422 with a
    # human-readable detail rather than a 500 IntegrityError.
    if body.event_status in ("planned", "confirmed") and body.planned_start_at is None:
        raise HTTPException(
            status_code=422,
            detail=(
                f"event_status='{body.event_status}' requires planned_start_at "
                "(a scheduled event must know its when)"
            ),
        )

    # Status-change provenance: who/when, kind derived from the
    # request actor. ``status_changed_at`` is set on create for any
    # non-default status so the audit chain has the creation moment.
    status_kind: str | None = None
    status_when: datetime | None = None
    if body.event_status != "completed":
        status_kind = "agent" if getattr(request.state, "is_agent", False) else "human"
        status_when = datetime.now(UTC)

    ev = ClinicalEvent(
        patient_id=body.patient_id,
        kind=body.kind,
        event_date=body.event_date,
        title=body.title,
        body_part=body.body_part,
        code_loinc=body.code_loinc,
        code_snomed=body.code_snomed,
        narrative=body.narrative,
        event_status=body.event_status,
        planned_start_at=body.planned_start_at,
        planned_end_at=body.planned_end_at,
        actual_start_at=body.actual_start_at,
        actual_end_at=body.actual_end_at,
        timezone=body.timezone,
        location_struct=body.location_struct,
        recurrence_rule=body.recurrence_rule,
        recurrence_exdates=body.recurrence_exdates,
        reminder_offsets_minutes=body.reminder_offsets_minutes,
        meeting_url=body.meeting_url,
        links=body.links,
        status_changed_at=status_when,
        status_changed_by_kind=status_kind,
    )
    db.add(ev)
    await db.flush()  # assign id
    # Auto-assign planned/confirmed events to the per-patient
    # "Pianificati" care phase so they group on the timeline. The
    # phase is created on first use (see ensure_planned_phase).
    if body.event_status in ("planned", "confirmed"):
        planned_phase = await ensure_planned_phase(
            db,
            patient_id=body.patient_id,
            author_kind=status_kind or "system",
        )
        ev.phase_id = planned_phase.id
        ev.phase_assigned_by = status_kind or "system"
        ev.phase_assigned_at = status_when or datetime.now(UTC)
        ev.phase_assignment_confidence = 1.0
        await db.flush()
    await _record_provenance(
        db,
        target_id=ev.id,
        activity="create",
        user=user,
        request=request,
        diff={
            "kind": body.kind,
            "title": body.title,
            "event_status": body.event_status,
        },
    )
    # Materialise notification_dispatches rows (one per contact ×
    # offset × channel) for upcoming events with a planned anchor and
    # reminder offsets. Idempotent — re-running on a patch is safe.
    if body.event_status in ("planned", "confirmed") and body.planned_start_at:
        await materialise_event_dispatches(db, ev)
    await db.commit()
    await db.refresh(ev)

    out = _to_out(ev, None)
    request.state.response_etag = out.etag
    return out


_UPDATABLE_FIELDS = (
    "kind",
    "event_date",
    "title",
    "body_part",
    "code_loinc",
    "code_snomed",
    "narrative",
    # Planning metadata (transition of ``event_status`` itself happens
    # via the dedicated sub-resources, not via PATCH).
    "planned_start_at",
    "planned_end_at",
    "timezone",
    "location_struct",
    "recurrence_rule",
    "recurrence_exdates",
    "reminder_offsets_minutes",
    "meeting_url",
    "links",
)


@router.patch(
    "/clinical-events/{event_id}",
    response_model=ClinicalEventOut,
)
async def patch_clinical_event(
    event_id: uuid.UUID,
    body: ClinicalEventUpdateIn,
    request: Request,
    response: Response,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    audit: AuditDep,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> ClinicalEventOut:
    """Update mutable metadata on a ClinicalEvent. ``kind`` and
    ``patient_id`` are immutable. ``If-Match`` is required and must
    match the current ``etag``; on success a new ``etag`` is minted
    and surfaced in the response ``ETag`` header."""
    del audit  # used by middleware via the dependency, not in handler logic
    loaded = await _load_event_with_imaging(db, event_id)
    if loaded is None:
        raise HTTPException(status_code=404, detail="clinical event not found")
    ev, imaging = loaded

    patient = (
        await db.execute(select(Patient).where(Patient.id == ev.patient_id))
    ).scalar_one_or_none()
    if patient is None or not await can_patient(
        db, user=user, action=WRITE_REPORT, patient=patient
    ):
        raise HTTPException(status_code=404, detail="clinical event not found")
    enforce_agent_patient_scope(request, patient.id)

    presented = parse_if_match(if_match)
    if presented is None:
        raise HTTPException(
            status_code=status.HTTP_428_PRECONDITION_REQUIRED,
            detail="If-Match header is required for this mutation",
        )
    if presented != "*" and presented != str(ev.etag):
        raise HTTPException(
            status_code=status.HTTP_412_PRECONDITION_FAILED,
            detail="If-Match does not match current ETag",
        )

    payload = body.model_dump(exclude_unset=True)
    if payload.get("narrative"):
        await validate_mentions_or_raise(db, patient_id=ev.patient_id, body=payload["narrative"])
    # ``kind`` is patchable but two invariants must hold:
    # 1. the new kind is in the supported enum;
    # 2. the DICOM ingestion pipeline keeps owning ``imaging_study``:
    #    rows currently paired with an imaging_studies projection
    #    cannot leave that kind, and no row may promote into
    #    ``imaging_study`` from outside.
    if "kind" in payload:
        new_kind = payload["kind"]
        if new_kind not in CLINICAL_EVENT_KINDS:
            raise HTTPException(
                status_code=422,
                detail=f"invalid kind, allowed: {sorted(CLINICAL_EVENT_KINDS)}",
            )
        if ev.kind == "imaging_study" and imaging is not None and new_kind != "imaging_study":
            raise HTTPException(
                status_code=409,
                detail=(
                    "imaging_study with a live imaging_studies projection cannot "
                    "change kind here; delete the imaging study via the DICOM "
                    "deletion path first"
                ),
            )
        if new_kind == "imaging_study" and ev.kind != "imaging_study":
            raise HTTPException(
                status_code=422,
                detail=(
                    "imaging_study events are materialised by the DICOM ingestion "
                    "pipeline; cannot promote a non-imaging event into imaging_study"
                ),
            )
    diff: dict[str, object] = {}
    for field in _UPDATABLE_FIELDS:
        if field not in payload:
            continue
        new_value = payload[field]
        old_value = getattr(ev, field)
        if old_value == new_value:
            continue
        setattr(ev, field, new_value)
        diff[field] = {"from": old_value, "to": new_value}

    if not diff:
        # Nothing to do, but the request is well-formed. Return current
        # state without bumping the ETag — the agent gets a confirmation
        # that its view is current.
        out = _to_out(ev, imaging)
        response.headers["ETag"] = format_etag(str(ev.etag))
        request.state.response_etag = out.etag
        return out

    # Bump the etag so concurrent agents detect the write.
    ev.etag = uuid.uuid4()
    await _record_provenance(
        db,
        target_id=ev.id,
        activity="update",
        user=user,
        request=request,
        diff=diff,
    )
    # Reschedule notification dispatches if anchor or offsets changed.
    # cancel_dispatches_for_target keeps the same idempotency keys
    # cancelled rather than deleting them, so the audit trail stays
    # intact. materialise_event_dispatches then inserts fresh rows
    # with the new schedule.
    timing_changed = any(
        k in diff for k in ("planned_start_at", "reminder_offsets_minutes", "timezone")
    )
    if timing_changed and ev.event_status in ("planned", "confirmed"):
        await cancel_dispatches_for_target(db, "clinical_event", ev.id, reason="rescheduled")
        await materialise_event_dispatches(db, ev)
    await db.commit()
    await db.refresh(ev)
    # Re-load imaging child (unchanged but model_refresh may detach).
    imaging = (
        await db.execute(select(ImagingStudy).where(ImagingStudy.clinical_event_id == ev.id))
    ).scalar_one_or_none()

    out = _to_out(ev, imaging)
    response.headers["ETag"] = format_etag(str(ev.etag))
    request.state.response_etag = out.etag
    return out


@router.delete(
    "/clinical-events/{event_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_clinical_event(
    event_id: uuid.UUID,
    request: Request,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    audit: AuditDep,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Response:
    """Delete a ClinicalEvent. Imaging events with a live imaging_studies
    row are refused here because their lifecycle is owned by the DICOM
    deletion path (the ``imaging_studies`` FK cascades on delete, which
    would silently take the imaging row with it). Orphan imaging events
    (``kind='imaging_study'`` with no matching imaging_studies row,
    typically left behind when the DICOM study was deleted but the
    umbrella event survived) ARE deletable here: the cascade concern
    doesn't apply, and forbidding the delete would strand the orphan
    chip on the patient timeline forever."""
    del audit
    loaded = await _load_event_with_imaging(db, event_id)
    if loaded is None:
        raise HTTPException(status_code=404, detail="clinical event not found")
    ev, imaging = loaded

    patient = (
        await db.execute(select(Patient).where(Patient.id == ev.patient_id))
    ).scalar_one_or_none()
    if patient is None or not await can_patient(
        db, user=user, action=WRITE_REPORT, patient=patient
    ):
        raise HTTPException(status_code=404, detail="clinical event not found")
    enforce_agent_patient_scope(request, patient.id)

    if ev.kind == "imaging_study" and imaging is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                "imaging_study clinical events with a live imaging row are "
                "owned by the imaging deletion path; delete the imaging "
                "study instead"
            ),
        )

    presented = parse_if_match(if_match)
    if presented is None:
        raise HTTPException(
            status_code=status.HTTP_428_PRECONDITION_REQUIRED,
            detail="If-Match header is required for this mutation",
        )
    if presented != "*" and presented != str(ev.etag):
        raise HTTPException(
            status_code=status.HTTP_412_PRECONDITION_FAILED,
            detail="If-Match does not match current ETag",
        )

    # Capture the diff *before* the delete so the provenance row has
    # something to show in the audit trail.
    snapshot = {
        "kind": ev.kind,
        "title": ev.title,
        "event_date": ev.event_date.isoformat() if ev.event_date else None,
    }
    await _record_provenance(
        db,
        target_id=ev.id,
        activity="delete",
        user=user,
        request=request,
        diff=snapshot,
    )
    await db.delete(ev)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Transition sub-resources (FSM-checked)
# ---------------------------------------------------------------------------
# Each verb is its own POST endpoint so the audit chain has a precise
# ``activity = "transition.<verb>"`` (vs a generic PATCH that would
# need diff-parsing to learn which transition happened). All five
# share the same gatekeeping pattern:
#
#   1. Load event + verify patient scope (404 if not visible).
#   2. ``If-Match`` mandatory (412 on mismatch, 428 missing).
#   3. ``Idempotency-Key`` mandatory; replay returns ``snapshot_after``
#      from ``clinical_event_transitions``.
#   4. FSM check via ``fsm.assert_transition_allowed`` (422
#      ``invalid_transition`` if not).
#   5. ``?dry_run=true`` returns the would-be ``snapshot_after``
#      without persisting (no transition row written either).
#   6. Apply mutation + write a ``ClinicalEventTransition`` audit row
#      + ``record_provenance(activity="transition.<verb>")``.
#   7. Bump ``etag``; surface in ``ETag`` response header.


async def _load_event_for_transition(
    db: AsyncSession,
    *,
    request: Request,
    event_id: uuid.UUID,
    user: User,
) -> ClinicalEvent:
    """Load + access-check. Raises 404 (not 403) on no-access to keep
    cross-patient probing impossible by route-shape: an outsider sees
    the same 404 whether the event exists under a different patient
    or doesn't exist at all.

    Agent token defense in depth: ``enforce_agent_patient_scope`` runs
    after ``can_patient`` so an agent whose underlying user holds
    broad RBAC still cannot transition events for fascicoli outside
    its consented ``agent_patient_ids`` set (memoria
    ``cross_patient_links_forbidden``)."""
    loaded = await _load_event_with_imaging(db, event_id)
    if loaded is None:
        raise HTTPException(status_code=404, detail="clinical event not found")
    ev, _imaging = loaded
    patient = (
        await db.execute(select(Patient).where(Patient.id == ev.patient_id))
    ).scalar_one_or_none()
    if patient is None or not await can_patient(
        db, user=user, action=WRITE_REPORT, patient=patient
    ):
        raise HTTPException(status_code=404, detail="clinical event not found")
    enforce_agent_patient_scope(request, patient.id)
    return ev


async def _persist_transition(
    db: AsyncSession,
    *,
    ev: ClinicalEvent,
    new_status: str,
    action: str,
    snapshot_before: dict,
    user: User,
    request: Request,
    reason: str | None,
    idempotency_key: str,
    extra_diff: dict | None = None,
) -> ClinicalEvent:
    """Common shutdown sequence: bump etag, append transition row,
    record provenance, commit. Returns the refreshed event so the
    caller can serialise the response.

    Side effect on phase membership: on terminal-from-planned
    transitions (completed / cancelled / missed / rescheduled) we
    clear ``phase_id`` so the row exits the per-patient "Pianificati"
    bucket. The event is then unassigned, ready for the LLM
    classifier or a human to place it in the proper clinical phase.
    ``confirmed`` keeps the phase: a confirmed appointment is still
    in the future bucket."""
    ev.event_status = new_status
    ev.status_changed_at = datetime.now(UTC)
    ev.status_changed_by_kind = _author_kind(request)
    ev.status_change_reason = reason
    if new_status in (fsm.COMPLETED, fsm.CANCELLED, fsm.MISSED, fsm.RESCHEDULED):
        ev.phase_id = None
        ev.phase_assigned_by = None
        ev.phase_assigned_at = None
        ev.phase_assignment_confidence = None
    ev.etag = uuid.uuid4()
    await db.flush()
    snapshot_after = _event_snapshot(ev)
    db.add(
        ClinicalEventTransition(
            event_id=ev.id,
            action=action,
            idempotency_key=idempotency_key,
            snapshot_before=snapshot_before,
            snapshot_after=snapshot_after,
            actor_subject_id=user.subject_id,
            author_kind=_author_kind(request),
            reason=reason,
        )
    )
    diff: dict = {"event_status": {"from": snapshot_before["event_status"], "to": new_status}}
    if extra_diff:
        diff.update(extra_diff)
    await _record_provenance(
        db,
        target_id=ev.id,
        activity=f"transition.{action}",
        user=user,
        request=request,
        diff=diff,
    )
    # Notification-scheduling side effects of the FSM move:
    # - completed / cancelled / missed / rescheduled → the event is
    #   off the future calendar, any pending reminder is moot
    # - confirmed → the event is still on the calendar; existing
    #   dispatches stay valid (same anchor + offsets)
    if new_status in (fsm.COMPLETED, fsm.CANCELLED, fsm.MISSED, fsm.RESCHEDULED):
        await cancel_dispatches_for_target(
            db, "clinical_event", ev.id, reason=f"transition_{new_status}"
        )
    await db.commit()
    await db.refresh(ev)
    return ev


def _transition_response(
    ev: ClinicalEvent,
    imaging: ImagingStudy | None,
    request: Request,
    response: Response,
) -> ClinicalEventOut:
    out = _to_out(ev, imaging)
    response.headers["ETag"] = format_etag(str(ev.etag))
    request.state.response_etag = out.etag
    return out


def _replay_to_out(replay: ClinicalEventTransition) -> ClinicalEventOut:
    """Return the snapshot stored at the original transition. Idempotent
    replay path: the caller's second request with the same key gets
    exactly the response the first one received."""
    snap = replay.snapshot_after
    return ClinicalEventOut(
        id=snap["id"],
        patient_id=snap["patient_id"],
        kind=snap["kind"],
        event_date=date.fromisoformat(snap["event_date"]) if snap.get("event_date") else None,
        title=snap["title"],
        body_part=None,
        code_loinc=None,
        code_snomed=None,
        narrative=snap.get("narrative"),
        imaging_study_id=None,
        etag=snap["etag"],
        created_at=replay.created_at.isoformat(),
        updated_at=replay.created_at.isoformat(),
        event_status=snap["event_status"],
        planned_start_at=snap.get("planned_start_at"),
        planned_end_at=snap.get("planned_end_at"),
        actual_start_at=snap.get("actual_start_at"),
        actual_end_at=snap.get("actual_end_at"),
        timezone=snap.get("timezone"),
        location_struct=snap.get("location_struct"),
        recurrence_rule=None,
        recurrence_exdates=None,
        reminder_offsets_minutes=None,
        parent_event_id=snap.get("parent_event_id"),
        status_changed_at=None,
        status_changed_by_kind=None,
        status_change_reason=snap.get("status_change_reason"),
        meeting_url=snap.get("meeting_url"),
        links=snap.get("links"),
    )


@router.post(
    "/clinical-events/{event_id}/confirm",
    response_model=ClinicalEventOut,
    status_code=status.HTTP_200_OK,
)
async def confirm_event(
    event_id: uuid.UUID,
    body: ConfirmIn,
    request: Request,
    response: Response,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    audit: AuditDep,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    dry_run: Annotated[bool, Query()] = False,
) -> ClinicalEventOut:
    """Move ``planned`` -> ``confirmed``. The optional ``confirmed_at``
    is recorded as ``status_changed_at``; falls back to server-now."""
    del audit
    if not idempotency_key:
        raise HTTPException(status_code=428, detail="Idempotency-Key header required")
    ev = await _load_event_for_transition(db, request=request, event_id=event_id, user=user)
    await _check_if_match(if_match, str(ev.etag))
    replay = await _idempotency_replay(
        db, event_id=event_id, action="confirm", idempotency_key=idempotency_key
    )
    if replay is not None:
        return _replay_to_out(replay)
    fsm.assert_transition_allowed(from_status=ev.event_status, to_status=fsm.CONFIRMED)
    snapshot_before = _event_snapshot(ev)
    if dry_run:
        # Snapshot is unused here, but kept on snapshot_before for
        # parity with the persist path (mental model: same shape).
        del snapshot_before
        return ClinicalEventOut(**{**_to_out(ev, None).model_dump(), "event_status": fsm.CONFIRMED})
    if body.confirmed_at is not None:
        ev.status_changed_at = body.confirmed_at
    ev = await _persist_transition(
        db,
        ev=ev,
        new_status=fsm.CONFIRMED,
        action="confirm",
        snapshot_before=snapshot_before,
        user=user,
        request=request,
        reason=None,
        idempotency_key=idempotency_key,
    )
    return _transition_response(ev, None, request, response)


@router.post(
    "/clinical-events/{event_id}/reschedule",
    response_model=ClinicalEventOut,
    status_code=status.HTTP_201_CREATED,
)
async def reschedule_event(
    event_id: uuid.UUID,
    body: RescheduleIn,
    request: Request,
    response: Response,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    audit: AuditDep,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    dry_run: Annotated[bool, Query()] = False,
) -> ClinicalEventOut:
    """Move the event to a new slot. Returns the NEW (planned) event;
    the response ``X-Replaced-Event-Id`` header carries the id of the
    original event, which is left in ``rescheduled`` state pointing
    at the new row via ``parent_event_id`` (same-patient enforced by
    composite FK at the DB)."""
    del audit
    if not idempotency_key:
        raise HTTPException(status_code=428, detail="Idempotency-Key header required")
    ev = await _load_event_for_transition(db, request=request, event_id=event_id, user=user)
    await _check_if_match(if_match, str(ev.etag))
    replay = await _idempotency_replay(
        db, event_id=event_id, action="reschedule", idempotency_key=idempotency_key
    )
    if replay is not None:
        return _replay_to_out(replay)
    fsm.assert_transition_allowed(from_status=ev.event_status, to_status=fsm.RESCHEDULED)
    snapshot_before = _event_snapshot(ev)
    if dry_run:
        # Preview: pretend the new row would be created and the old
        # one marked rescheduled. We do not write to the DB.
        out = _to_out(ev, None).model_dump()
        out["event_status"] = fsm.RESCHEDULED
        return ClinicalEventOut(**out)
    # 1. Mark the old event as rescheduled (terminal). status_change
    #    audit is captured here.
    old = await _persist_transition(
        db,
        ev=ev,
        new_status=fsm.RESCHEDULED,
        action="reschedule",
        snapshot_before=snapshot_before,
        user=user,
        request=request,
        reason=body.reason,
        idempotency_key=idempotency_key,
    )
    # 2. Materialise the NEW event row pointing at ``old`` via the
    #    composite FK ``(patient_id, parent_event_id)``. Same patient
    #    by construction (we read patient_id from ``old`` itself).
    new_ev = ClinicalEvent(
        patient_id=old.patient_id,
        kind=old.kind,
        title=old.title,
        body_part=old.body_part,
        code_loinc=old.code_loinc,
        code_snomed=old.code_snomed,
        event_status=fsm.PLANNED,
        planned_start_at=body.new_planned_start_at,
        planned_end_at=body.new_planned_end_at,
        timezone=body.timezone or old.timezone,
        location_struct=old.location_struct,
        reminder_offsets_minutes=old.reminder_offsets_minutes,
        parent_event_id=old.id,
        status_changed_at=datetime.now(UTC),
        status_changed_by_kind=_author_kind(request),
        status_change_reason=f"rescheduled from {old.id}: {body.reason}",
    )
    db.add(new_ev)
    await db.flush()
    # Auto-assign the NEW (planned) event to the "Pianificati"
    # bucket, mirroring the create-event path.
    planned_phase = await ensure_planned_phase(
        db,
        patient_id=new_ev.patient_id,
        author_kind=_author_kind(request),
    )
    new_ev.phase_id = planned_phase.id
    new_ev.phase_assigned_by = _author_kind(request)
    new_ev.phase_assigned_at = datetime.now(UTC)
    new_ev.phase_assignment_confidence = 1.0
    await db.flush()
    await _record_provenance(
        db,
        target_id=new_ev.id,
        activity="create.rescheduled",
        user=user,
        request=request,
        diff={
            "parent_event_id": str(old.id),
            "planned_start_at": body.new_planned_start_at.isoformat(),
            "reason": body.reason,
        },
    )
    await db.commit()
    await db.refresh(new_ev)
    response.headers["X-Replaced-Event-Id"] = str(old.id)
    return _transition_response(new_ev, None, request, response)


@router.post(
    "/clinical-events/{event_id}/complete",
    response_model=ClinicalEventOut,
    status_code=status.HTTP_200_OK,
)
async def complete_event(
    event_id: uuid.UUID,
    body: CompleteIn,
    request: Request,
    response: Response,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    audit: AuditDep,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    dry_run: Annotated[bool, Query()] = False,
) -> ClinicalEventOut:
    """Move ``planned`` / ``confirmed`` / ``missed`` -> ``completed``.
    Records the realised timestamp and an optional narrative (cross-
    patient guarded via ``validate_mentions_or_raise``)."""
    del audit
    if not idempotency_key:
        raise HTTPException(status_code=428, detail="Idempotency-Key header required")
    ev = await _load_event_for_transition(db, request=request, event_id=event_id, user=user)
    await _check_if_match(if_match, str(ev.etag))
    replay = await _idempotency_replay(
        db, event_id=event_id, action="complete", idempotency_key=idempotency_key
    )
    if replay is not None:
        return _replay_to_out(replay)
    fsm.assert_transition_allowed(from_status=ev.event_status, to_status=fsm.COMPLETED)
    if body.narrative:
        await validate_mentions_or_raise(db, patient_id=ev.patient_id, body=body.narrative)
    snapshot_before = _event_snapshot(ev)
    if dry_run:
        out = _to_out(ev, None).model_dump()
        out["event_status"] = fsm.COMPLETED
        out["actual_start_at"] = body.actual_start_at.isoformat()
        return ClinicalEventOut(**out)
    ev.actual_start_at = body.actual_start_at
    if body.actual_end_at is not None:
        ev.actual_end_at = body.actual_end_at
    if body.narrative is not None:
        ev.narrative = body.narrative
    ev = await _persist_transition(
        db,
        ev=ev,
        new_status=fsm.COMPLETED,
        action="complete",
        snapshot_before=snapshot_before,
        user=user,
        request=request,
        reason=None,
        idempotency_key=idempotency_key,
        extra_diff={"actual_start_at": body.actual_start_at.isoformat()},
    )
    return _transition_response(ev, None, request, response)


@router.post(
    "/clinical-events/{event_id}/cancel",
    response_model=ClinicalEventOut,
    status_code=status.HTTP_200_OK,
)
async def cancel_event(
    event_id: uuid.UUID,
    body: CancelIn,
    request: Request,
    response: Response,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    audit: AuditDep,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    dry_run: Annotated[bool, Query()] = False,
) -> ClinicalEventOut:
    """Move planned/confirmed -> ``cancelled``. Terminal. ``reason``
    is mandatory so the audit chain is informative; we don't take
    silent cancellations on the record."""
    del audit
    if not idempotency_key:
        raise HTTPException(status_code=428, detail="Idempotency-Key header required")
    ev = await _load_event_for_transition(db, request=request, event_id=event_id, user=user)
    await _check_if_match(if_match, str(ev.etag))
    replay = await _idempotency_replay(
        db, event_id=event_id, action="cancel", idempotency_key=idempotency_key
    )
    if replay is not None:
        return _replay_to_out(replay)
    fsm.assert_transition_allowed(from_status=ev.event_status, to_status=fsm.CANCELLED)
    snapshot_before = _event_snapshot(ev)
    if dry_run:
        out = _to_out(ev, None).model_dump()
        out["event_status"] = fsm.CANCELLED
        out["status_change_reason"] = body.reason
        return ClinicalEventOut(**out)
    ev = await _persist_transition(
        db,
        ev=ev,
        new_status=fsm.CANCELLED,
        action="cancel",
        snapshot_before=snapshot_before,
        user=user,
        request=request,
        reason=body.reason,
        idempotency_key=idempotency_key,
    )
    return _transition_response(ev, None, request, response)


@router.post(
    "/clinical-events/{event_id}/mark-missed",
    response_model=ClinicalEventOut,
    status_code=status.HTTP_200_OK,
)
async def mark_event_missed(
    event_id: uuid.UUID,
    body: MarkMissedIn,
    request: Request,
    response: Response,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    audit: AuditDep,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    dry_run: Annotated[bool, Query()] = False,
) -> ClinicalEventOut:
    """Move planned/confirmed -> ``missed``. Use when the appointment
    happened-time has passed without the patient showing up. Not
    terminal: ``missed -> rescheduled`` and ``missed -> completed``
    (late arrival) are still allowed by the FSM."""
    del audit
    if not idempotency_key:
        raise HTTPException(status_code=428, detail="Idempotency-Key header required")
    ev = await _load_event_for_transition(db, request=request, event_id=event_id, user=user)
    await _check_if_match(if_match, str(ev.etag))
    replay = await _idempotency_replay(
        db, event_id=event_id, action="mark_missed", idempotency_key=idempotency_key
    )
    if replay is not None:
        return _replay_to_out(replay)
    fsm.assert_transition_allowed(from_status=ev.event_status, to_status=fsm.MISSED)
    snapshot_before = _event_snapshot(ev)
    if dry_run:
        out = _to_out(ev, None).model_dump()
        out["event_status"] = fsm.MISSED
        return ClinicalEventOut(**out)
    ev = await _persist_transition(
        db,
        ev=ev,
        new_status=fsm.MISSED,
        action="mark_missed",
        snapshot_before=snapshot_before,
        user=user,
        request=request,
        reason=body.note,
        idempotency_key=idempotency_key,
    )
    return _transition_response(ev, None, request, response)
