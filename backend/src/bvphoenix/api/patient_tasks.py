"""Patient tasks — operational checklist alongside the clinical timeline.

A ``PatientTask`` is a private operational to-do attached to a
fascicolo: "buy the medication", "ask the GP for the impegnativa",
"call the CUP to book the TAC". Distinct from ``ClinicalEvent``
(see ``db/models/patient_tasks.py`` for the rationale and FSM).

Endpoints
---------

* ``GET    /api/patients/{patient_id}/tasks`` — list (filters: status,
  category, priority, due_from/due_to, include_deleted)
* ``GET    /api/patient-tasks/{task_id}`` — read one
* ``POST   /api/patient-tasks`` — create (Idempotency-Key required)
* ``PATCH  /api/patient-tasks/{task_id}`` — update mutable metadata
  (If-Match required; ``status`` immutable, moves via transitions)
* ``DELETE /api/patient-tasks/{task_id}`` — soft delete (If-Match
  required, idempotent)
* ``POST   /api/patient-tasks/{task_id}/restore`` — clear tombstone
* ``POST   /api/patient-tasks/{task_id}/{verb}`` — FSM transition,
  one of ``start | snooze | wake | complete | drop | reopen``
  (each requires Idempotency-Key + If-Match, supports ?dry_run=true)

Every mutation:

1. checks ``can_patient`` (RBAC) + ``enforce_agent_patient_scope``
   (agent-token defence in depth) — see ``feedback_systemic_agent_patient_scope_gap``
2. enforces ``If-Match`` (412 / 428 contract) for concurrency
3. enforces ``Idempotency-Key`` on creates and transitions for replay
4. records a ``provenance_events`` row with ``target_kind='patient_task'``
   and a structured ``diff``
5. bumps ``etag``

The FSM lives in ``services/patient_tasks_fsm.py`` and is the single
source of truth for which status moves are valid.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.auth.deps import enforce_agent_patient_scope, require_user
from bvphoenix.db.models import (
    PATIENT_TASK_CATEGORIES,
    PATIENT_TASK_PRIORITIES,
    PATIENT_TASK_STATUSES,
    Patient,
    PatientContact,
    PatientTask,
    PatientTaskTransition,
    User,
)
from bvphoenix.db.session import get_db
from bvphoenix.middleware.audit_dependency import AuditDep
from bvphoenix.services import patient_tasks_fsm as fsm
from bvphoenix.services.etag import format_etag, parse_if_match
from bvphoenix.services.notifications.scheduling import (
    cancel_dispatches_for_target,
    materialise_task_dispatches,
)
from bvphoenix.services.permissions import (
    READ_METADATA,
    WRITE_REPORT,
    can_patient,
)
from bvphoenix.services.provenance_log import record_provenance

router = APIRouter(tags=["patient-tasks"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


TaskStatusLiteral = Literal["pending", "in_progress", "snoozed", "done", "dropped"]
TaskPriorityLiteral = Literal["low", "normal", "high", "urgent"]
TaskCategoryLiteral = Literal[
    "admin", "pharmacy", "appointment_prep", "transport", "communication", "personal", "other"
]


class PatientTaskOut(BaseModel):
    id: str
    patient_id: str
    title: str
    description: str | None
    category: str
    priority: str
    status: str
    due_at: str | None
    snooze_until: str | None
    completed_at: str | None
    timezone: str | None
    phase_id: str | None
    phase_assigned_by: str | None
    phase_assigned_at: str | None
    recurrence_rule: str | None
    parent_task_id: str | None
    assigned_to_contact_id: str | None
    related_event_id: str | None
    related_document_id: str | None
    labels: list | None
    links: list | None
    reminder_offsets_minutes: list[int] | None
    etag: str
    author_kind: str
    status_changed_at: str | None
    status_changed_by_kind: str | None
    status_change_reason: str | None
    deleted_at: str | None
    created_at: str
    updated_at: str


class PatientTaskCreateIn(BaseModel):
    patient_id: uuid.UUID
    title: str = Field(..., min_length=1, max_length=255)
    description: str | None = None
    category: TaskCategoryLiteral = "other"
    priority: TaskPriorityLiteral = "normal"
    due_at: datetime | None = None
    timezone: str | None = Field(default=None, max_length=64)
    phase_id: uuid.UUID | None = None
    recurrence_rule: str | None = Field(default=None, max_length=512)
    assigned_to_contact_id: uuid.UUID | None = None
    related_event_id: uuid.UUID | None = None
    related_document_id: uuid.UUID | None = None
    labels: list[str] | None = None
    links: list[dict] | None = None
    # Cap at 5 to bound dispatcher fan-out (see notification worker).
    reminder_offsets_minutes: list[int] | None = Field(default=None, max_length=5)


class PatientTaskUpdateIn(BaseModel):
    """Patch-style payload. ``patient_id`` and ``status`` are
    immutable here (status moves only via transition sub-resources so
    each move is auditable as a discrete FSM action). All other
    fields are optional."""

    title: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    category: TaskCategoryLiteral | None = None
    priority: TaskPriorityLiteral | None = None
    due_at: datetime | None = None
    timezone: str | None = Field(default=None, max_length=64)
    phase_id: uuid.UUID | None = None
    recurrence_rule: str | None = Field(default=None, max_length=512)
    assigned_to_contact_id: uuid.UUID | None = None
    related_event_id: uuid.UUID | None = None
    related_document_id: uuid.UUID | None = None
    labels: list[str] | None = None
    links: list[dict] | None = None
    reminder_offsets_minutes: list[int] | None = Field(default=None, max_length=5)


# ---- Transition sub-resource bodies -------------------------------------


class StartIn(BaseModel):
    """``POST /patient-tasks/{id}/start``. Empty body — picking up a
    pending task is a verb without parameters."""


class SnoozeIn(BaseModel):
    """``POST /patient-tasks/{id}/snooze``. Wake-up time is required.
    The task moves to ``snoozed`` and the dispatcher will wake it via
    a scheduled job when ``snooze_until`` is reached (a follow-up
    worker; for now the row simply sits until a human or agent calls
    ``/wake``)."""

    snooze_until: datetime
    reason: str | None = Field(default=None, max_length=255)


class WakeIn(BaseModel):
    """``POST /patient-tasks/{id}/wake``. Lifts a snoozed task back.
    Defaults to ``pending``; pass ``resume_in_progress=true`` if the
    snooze paused a task that was already underway."""

    resume_in_progress: bool = False


class CompleteIn(BaseModel):
    """``POST /patient-tasks/{id}/complete``. Records the finish
    timestamp (defaults to server-now) and an optional note that goes
    into ``status_change_reason``."""

    completed_at: datetime | None = None
    note: str | None = Field(default=None, max_length=255)


class DropIn(BaseModel):
    """``POST /patient-tasks/{id}/drop``. Terminates the task as
    "won't do" (the impegnativa was already there, the medication was
    swapped, ...). Reason is required so the audit chain is informative."""

    reason: str = Field(min_length=1, max_length=255)


class ReopenIn(BaseModel):
    """``POST /patient-tasks/{id}/reopen``. Lifts a done/dropped task
    back to ``pending``. ``completed_at`` is preserved as historical
    evidence; the row's status alone tells the story."""

    reason: str | None = Field(default=None, max_length=255)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _load_task(db: AsyncSession, task_id: uuid.UUID) -> PatientTask | None:
    return (
        await db.execute(select(PatientTask).where(PatientTask.id == task_id))
    ).scalar_one_or_none()


def _to_out(t: PatientTask) -> PatientTaskOut:
    def _iso(d: datetime | None) -> str | None:
        return d.isoformat() if d else None

    return PatientTaskOut(
        id=str(t.id),
        patient_id=str(t.patient_id),
        title=t.title,
        description=t.description,
        category=t.category,
        priority=t.priority,
        status=t.status,
        due_at=_iso(t.due_at),
        snooze_until=_iso(t.snooze_until),
        completed_at=_iso(t.completed_at),
        timezone=t.timezone,
        phase_id=str(t.phase_id) if t.phase_id else None,
        phase_assigned_by=t.phase_assigned_by,
        phase_assigned_at=_iso(t.phase_assigned_at),
        recurrence_rule=t.recurrence_rule,
        parent_task_id=str(t.parent_task_id) if t.parent_task_id else None,
        assigned_to_contact_id=(
            str(t.assigned_to_contact_id) if t.assigned_to_contact_id else None
        ),
        related_event_id=str(t.related_event_id) if t.related_event_id else None,
        related_document_id=(str(t.related_document_id) if t.related_document_id else None),
        labels=t.labels,
        links=t.links,
        reminder_offsets_minutes=t.reminder_offsets_minutes,
        etag=str(t.etag),
        author_kind=t.author_kind,
        status_changed_at=_iso(t.status_changed_at),
        status_changed_by_kind=t.status_changed_by_kind,
        status_change_reason=t.status_change_reason,
        deleted_at=_iso(t.deleted_at),
        created_at=t.created_at.isoformat(),
        updated_at=t.updated_at.isoformat(),
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
    record_provenance(
        db,
        target_kind="patient_task",
        target_id=target_id,
        activity=activity,
        user=user,
        request=request,
        diff=diff,
    )


def _task_snapshot(t: PatientTask) -> dict:
    """Whole-row snapshot persisted on ``patient_task_transitions``
    for Undo + audit. ISO-formats timestamps so JSONB round-trip is safe."""

    def _iso(d: datetime | None) -> str | None:
        return d.isoformat() if d else None

    return {
        "id": str(t.id),
        "patient_id": str(t.patient_id),
        "title": t.title,
        "description": t.description,
        "category": t.category,
        "priority": t.priority,
        "status": t.status,
        "due_at": _iso(t.due_at),
        "snooze_until": _iso(t.snooze_until),
        "completed_at": _iso(t.completed_at),
        "timezone": t.timezone,
        "phase_id": str(t.phase_id) if t.phase_id else None,
        "parent_task_id": str(t.parent_task_id) if t.parent_task_id else None,
        "assigned_to_contact_id": (
            str(t.assigned_to_contact_id) if t.assigned_to_contact_id else None
        ),
        "related_event_id": str(t.related_event_id) if t.related_event_id else None,
        "related_document_id": (str(t.related_document_id) if t.related_document_id else None),
        "labels": t.labels,
        "links": t.links,
        "reminder_offsets_minutes": t.reminder_offsets_minutes,
        "status_change_reason": t.status_change_reason,
        "etag": str(t.etag),
    }


def _author_kind(request: Request) -> str:
    return "agent" if getattr(request.state, "is_agent", False) else "human"


from bvphoenix.services.etag import enforce_if_match_value


async def _check_if_match(if_match: str | None, current_etag: str) -> None:
    """Thin async wrapper around :func:`enforce_if_match_value` so the
    existing ``await _check_if_match(...)`` call sites compile
    unchanged. The wrapper is the only awaitable surface the legacy
    handlers exposed; the underlying helper is sync because it never
    touches I/O.
    """
    enforce_if_match_value(if_match, current_etag)


async def _idempotency_replay(
    db: AsyncSession,
    *,
    task_id: uuid.UUID,
    action: str,
    idempotency_key: str,
) -> PatientTaskTransition | None:
    row = (
        await db.execute(
            select(PatientTaskTransition).where(
                PatientTaskTransition.task_id == task_id,
                PatientTaskTransition.action == action,
                PatientTaskTransition.idempotency_key == idempotency_key,
            )
        )
    ).scalar_one_or_none()
    return row


def _replay_to_out(replay: PatientTaskTransition) -> PatientTaskOut:
    """Re-hydrate a ``PatientTaskOut`` from the snapshot stored on
    the transition row. Used by the idempotency replay path: a second
    call with the same key gets exactly the response the first call
    received."""
    snap = replay.snapshot_after
    return PatientTaskOut(
        id=snap["id"],
        patient_id=snap["patient_id"],
        title=snap["title"],
        description=snap.get("description"),
        category=snap.get("category", "other"),
        priority=snap.get("priority", "normal"),
        status=snap["status"],
        due_at=snap.get("due_at"),
        snooze_until=snap.get("snooze_until"),
        completed_at=snap.get("completed_at"),
        timezone=snap.get("timezone"),
        phase_id=snap.get("phase_id"),
        phase_assigned_by=None,
        phase_assigned_at=None,
        recurrence_rule=None,
        parent_task_id=snap.get("parent_task_id"),
        assigned_to_contact_id=snap.get("assigned_to_contact_id"),
        related_event_id=snap.get("related_event_id"),
        related_document_id=snap.get("related_document_id"),
        labels=snap.get("labels"),
        links=snap.get("links"),
        reminder_offsets_minutes=snap.get("reminder_offsets_minutes"),
        etag=snap["etag"],
        author_kind="agent",  # replay echo; real value lives on the live row
        status_changed_at=None,
        status_changed_by_kind=None,
        status_change_reason=snap.get("status_change_reason"),
        deleted_at=None,
        created_at=replay.created_at.isoformat(),
        updated_at=replay.created_at.isoformat(),
    )


async def _load_task_with_patient_access(
    db: AsyncSession,
    *,
    request: Request,
    task_id: uuid.UUID,
    user: User,
    action_perm: str,
    include_deleted: bool = False,
) -> PatientTask:
    """Load a task and verify the caller can act on its patient.

    Returns 404 (not 403) on no-access to keep cross-patient probing
    impossible by route-shape: an outsider sees the same 404 whether
    the task exists under a different patient or doesn't exist at all
    (cross_patient_links_forbidden memory)."""
    t = await _load_task(db, task_id)
    if t is None:
        raise HTTPException(status_code=404, detail="patient task not found")
    if not include_deleted and t.deleted_at is not None:
        raise HTTPException(status_code=404, detail="patient task not found")
    patient = (
        await db.execute(select(Patient).where(Patient.id == t.patient_id))
    ).scalar_one_or_none()
    if patient is None or not await can_patient(db, user=user, action=action_perm, patient=patient):
        raise HTTPException(status_code=404, detail="patient task not found")
    enforce_agent_patient_scope(request, patient.id)
    return t


async def _validate_same_patient_fks(
    db: AsyncSession,
    *,
    patient_id: uuid.UUID,
    phase_id: uuid.UUID | None,
    assigned_to_contact_id: uuid.UUID | None,
    related_event_id: uuid.UUID | None,
    related_document_id: uuid.UUID | None,
) -> None:
    """Belt to the DB's braces. The composite FKs already reject
    cross-patient assignment at insert time, but the resulting
    ``IntegrityError`` would surface as a 500. We pre-validate here
    so the caller gets a clean 422 with a precise field-level detail."""
    if assigned_to_contact_id is not None:
        row = (
            await db.execute(
                select(PatientContact.patient_id).where(PatientContact.id == assigned_to_contact_id)
            )
        ).scalar_one_or_none()
        if row is None or row != patient_id:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "cross_patient_reference",
                    "field": "assigned_to_contact_id",
                    "message": "contact must belong to the same patient",
                },
            )
    if related_event_id is not None:
        from bvphoenix.db.models import ClinicalEvent

        row = (
            await db.execute(
                select(ClinicalEvent.patient_id).where(ClinicalEvent.id == related_event_id)
            )
        ).scalar_one_or_none()
        if row is None or row != patient_id:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "cross_patient_reference",
                    "field": "related_event_id",
                    "message": "clinical event must belong to the same patient",
                },
            )
    if phase_id is not None:
        from bvphoenix.db.models import CarePhase

        row = (
            await db.execute(select(CarePhase.patient_id).where(CarePhase.id == phase_id))
        ).scalar_one_or_none()
        if row is None or row != patient_id:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "cross_patient_reference",
                    "field": "phase_id",
                    "message": "care phase must belong to the same patient",
                },
            )
    if related_document_id is not None:
        from bvphoenix.db.models import Document

        row = (
            await db.execute(select(Document.patient_id).where(Document.id == related_document_id))
        ).scalar_one_or_none()
        if row is None or row != patient_id:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "cross_patient_reference",
                    "field": "related_document_id",
                    "message": "document must belong to the same patient",
                },
            )


# ---------------------------------------------------------------------------
# Read endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/patient-tasks/{task_id}",
    response_model=PatientTaskOut,
    status_code=status.HTTP_200_OK,
)
async def read_patient_task(
    task_id: uuid.UUID,
    request: Request,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    audit: AuditDep,
    if_none_match: Annotated[str | None, Header(alias="If-None-Match")] = None,
    include_deleted: Annotated[bool, Query()] = False,
) -> PatientTaskOut:
    del audit
    t = await _load_task_with_patient_access(
        db,
        request=request,
        task_id=task_id,
        user=user,
        action_perm=READ_METADATA,
        include_deleted=include_deleted,
    )
    if if_none_match is not None and if_none_match.strip('"') == str(t.etag):
        raise HTTPException(status_code=status.HTTP_304_NOT_MODIFIED)
    out = _to_out(t)
    request.state.response_etag = out.etag
    return out


@router.get(
    "/patients/{patient_id}/tasks",
    response_model=list[PatientTaskOut],
)
async def list_patient_tasks(
    patient_id: uuid.UUID,
    request: Request,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    audit: AuditDep,
    statuses: Annotated[
        list[str] | None,
        Query(
            description=(
                "Filter by status (multi). Allowed: pending, in_progress, snoozed, "
                "done, dropped. Omit to include all non-deleted."
            ),
        ),
    ] = None,
    category: Annotated[str | None, Query()] = None,
    priority: Annotated[str | None, Query()] = None,
    due_from: Annotated[datetime | None, Query()] = None,
    due_to: Annotated[datetime | None, Query()] = None,
    include_deleted: Annotated[bool, Query()] = False,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[PatientTaskOut]:
    del audit
    patient = (
        await db.execute(select(Patient).where(Patient.id == patient_id))
    ).scalar_one_or_none()
    if patient is None or not await can_patient(
        db, user=user, action=READ_METADATA, patient=patient
    ):
        raise HTTPException(status_code=404, detail="patient not found")
    enforce_agent_patient_scope(request, patient.id)

    if statuses is not None:
        invalid = [s for s in statuses if s not in PATIENT_TASK_STATUSES]
        if invalid:
            raise HTTPException(
                status_code=422,
                detail=(f"invalid status(es) {invalid}, allowed: {list(PATIENT_TASK_STATUSES)}"),
            )
    if category is not None and category not in PATIENT_TASK_CATEGORIES:
        raise HTTPException(
            status_code=422,
            detail=f"invalid category, allowed: {list(PATIENT_TASK_CATEGORIES)}",
        )
    if priority is not None and priority not in PATIENT_TASK_PRIORITIES:
        raise HTTPException(
            status_code=422,
            detail=f"invalid priority, allowed: {list(PATIENT_TASK_PRIORITIES)}",
        )

    stmt = (
        select(PatientTask)
        .where(PatientTask.patient_id == patient_id)
        .order_by(
            PatientTask.due_at.asc().nulls_last(),
            PatientTask.created_at.desc(),
        )
        .limit(limit)
        .offset(offset)
    )
    if not include_deleted:
        stmt = stmt.where(PatientTask.deleted_at.is_(None))
    if statuses:
        stmt = stmt.where(PatientTask.status.in_(statuses))
    if category is not None:
        stmt = stmt.where(PatientTask.category == category)
    if priority is not None:
        stmt = stmt.where(PatientTask.priority == priority)
    if due_from is not None:
        stmt = stmt.where(PatientTask.due_at >= due_from)
    if due_to is not None:
        stmt = stmt.where(PatientTask.due_at <= due_to)

    tasks = (await db.execute(stmt)).scalars().all()
    return [_to_out(t) for t in tasks]


# ---------------------------------------------------------------------------
# Write endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/patient-tasks",
    response_model=PatientTaskOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_patient_task(
    body: PatientTaskCreateIn,
    request: Request,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    audit: AuditDep,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> PatientTaskOut:
    del audit
    if not idempotency_key:
        raise HTTPException(status_code=428, detail="Idempotency-Key header required")
    patient = (
        await db.execute(select(Patient).where(Patient.id == body.patient_id))
    ).scalar_one_or_none()
    if patient is None or not await can_patient(
        db, user=user, action=WRITE_REPORT, patient=patient
    ):
        raise HTTPException(status_code=404, detail="patient not found")
    enforce_agent_patient_scope(request, patient.id)

    await _validate_same_patient_fks(
        db,
        patient_id=body.patient_id,
        phase_id=body.phase_id,
        assigned_to_contact_id=body.assigned_to_contact_id,
        related_event_id=body.related_event_id,
        related_document_id=body.related_document_id,
    )

    author_kind = _author_kind(request)
    t = PatientTask(
        patient_id=body.patient_id,
        title=body.title,
        description=body.description,
        category=body.category,
        priority=body.priority,
        status="pending",
        due_at=body.due_at,
        timezone=body.timezone,
        phase_id=body.phase_id,
        phase_assigned_by=author_kind if body.phase_id else None,
        phase_assigned_at=datetime.now(UTC) if body.phase_id else None,
        recurrence_rule=body.recurrence_rule,
        assigned_to_contact_id=body.assigned_to_contact_id,
        related_event_id=body.related_event_id,
        related_document_id=body.related_document_id,
        labels=body.labels,
        links=body.links,
        reminder_offsets_minutes=body.reminder_offsets_minutes,
        author_kind=author_kind,
        created_by_subject_id=user.subject_id if author_kind == "human" else None,
    )
    db.add(t)
    await db.flush()
    await _record_provenance(
        db,
        target_id=t.id,
        activity="create",
        user=user,
        request=request,
        diff={
            "title": body.title,
            "category": body.category,
            "priority": body.priority,
        },
    )
    # Materialise notification dispatches for a task with a due_at +
    # reminder offsets. Tasks without an anchor produce no dispatches.
    if t.due_at and body.reminder_offsets_minutes:
        await materialise_task_dispatches(db, t)
    await db.commit()
    await db.refresh(t)
    out = _to_out(t)
    request.state.response_etag = out.etag
    return out


_UPDATABLE_FIELDS = (
    "title",
    "description",
    "category",
    "priority",
    "due_at",
    "timezone",
    "phase_id",
    "recurrence_rule",
    "assigned_to_contact_id",
    "related_event_id",
    "related_document_id",
    "labels",
    "links",
    "reminder_offsets_minutes",
)


@router.patch(
    "/patient-tasks/{task_id}",
    response_model=PatientTaskOut,
)
async def patch_patient_task(
    task_id: uuid.UUID,
    body: PatientTaskUpdateIn,
    request: Request,
    response: Response,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    audit: AuditDep,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> PatientTaskOut:
    """Update mutable metadata. ``status`` and ``patient_id`` are
    immutable here (use transition endpoints for status moves).
    ``If-Match`` is required."""
    del audit
    t = await _load_task_with_patient_access(
        db,
        request=request,
        task_id=task_id,
        user=user,
        action_perm=WRITE_REPORT,
    )
    await _check_if_match(if_match, str(t.etag))

    payload = body.model_dump(exclude_unset=True)
    if payload:
        await _validate_same_patient_fks(
            db,
            patient_id=t.patient_id,
            phase_id=payload.get("phase_id"),
            assigned_to_contact_id=payload.get("assigned_to_contact_id"),
            related_event_id=payload.get("related_event_id"),
            related_document_id=payload.get("related_document_id"),
        )

    diff: dict[str, object] = {}
    for field in _UPDATABLE_FIELDS:
        if field not in payload:
            continue
        new_value = payload[field]
        old_value = getattr(t, field)
        if old_value == new_value:
            continue
        setattr(t, field, new_value)
        diff[field] = {"from": str(old_value), "to": str(new_value)}

    if "phase_id" in payload and payload.get("phase_id") is not None:
        t.phase_assigned_by = _author_kind(request)
        t.phase_assigned_at = datetime.now(UTC)

    if not diff:
        out = _to_out(t)
        response.headers["ETag"] = format_etag(str(t.etag))
        request.state.response_etag = out.etag
        return out

    t.etag = uuid.uuid4()
    await _record_provenance(
        db,
        target_id=t.id,
        activity="update",
        user=user,
        request=request,
        diff=diff,
    )
    # Reschedule notification dispatches when the timing-relevant
    # fields move. cancel_dispatches_for_target keeps the audit
    # trail; materialise_task_dispatches inserts fresh rows.
    timing_changed = any(k in diff for k in ("due_at", "reminder_offsets_minutes", "timezone"))
    if timing_changed:
        await cancel_dispatches_for_target(db, "patient_task", t.id, reason="rescheduled")
        if t.due_at and t.reminder_offsets_minutes:
            await materialise_task_dispatches(db, t)
    await db.commit()
    await db.refresh(t)
    out = _to_out(t)
    response.headers["ETag"] = format_etag(str(t.etag))
    request.state.response_etag = out.etag
    return out


@router.delete(
    "/patient-tasks/{task_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_patient_task(
    task_id: uuid.UUID,
    request: Request,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    audit: AuditDep,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Response:
    """Soft delete (tombstone). Idempotent: deleting an already-deleted
    task is a no-op 204. ``/restore`` brings it back."""
    del audit
    t = await _load_task_with_patient_access(
        db,
        request=request,
        task_id=task_id,
        user=user,
        action_perm=WRITE_REPORT,
        include_deleted=True,
    )
    if t.deleted_at is not None:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    await _check_if_match(if_match, str(t.etag))
    t.deleted_at = datetime.now(UTC)
    t.deleted_by_subject_id = user.subject_id
    t.etag = uuid.uuid4()
    await _record_provenance(
        db,
        target_id=t.id,
        activity="delete",
        user=user,
        request=request,
        diff={"title": t.title},
    )
    await cancel_dispatches_for_target(db, "patient_task", t.id, reason="task_deleted")
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/patient-tasks/{task_id}/restore",
    response_model=PatientTaskOut,
    status_code=status.HTTP_200_OK,
)
async def restore_patient_task(
    task_id: uuid.UUID,
    request: Request,
    response: Response,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    audit: AuditDep,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> PatientTaskOut:
    del audit
    t = await _load_task_with_patient_access(
        db,
        request=request,
        task_id=task_id,
        user=user,
        action_perm=WRITE_REPORT,
        include_deleted=True,
    )
    if t.deleted_at is None:
        raise HTTPException(status_code=409, detail="task is not deleted")
    await _check_if_match(if_match, str(t.etag))
    t.deleted_at = None
    t.deleted_by_subject_id = None
    t.etag = uuid.uuid4()
    await _record_provenance(
        db,
        target_id=t.id,
        activity="restore",
        user=user,
        request=request,
        diff={"title": t.title},
    )
    await db.commit()
    await db.refresh(t)
    out = _to_out(t)
    response.headers["ETag"] = format_etag(str(t.etag))
    request.state.response_etag = out.etag
    return out


# ---------------------------------------------------------------------------
# Transition sub-resources (FSM-checked)
# ---------------------------------------------------------------------------


async def _persist_transition(
    db: AsyncSession,
    *,
    t: PatientTask,
    new_status: str,
    action: str,
    snapshot_before: dict,
    user: User,
    request: Request,
    reason: str | None,
    idempotency_key: str,
    extra_diff: dict | None = None,
) -> PatientTask:
    t.status = new_status
    t.status_changed_at = datetime.now(UTC)
    t.status_changed_by_kind = _author_kind(request)
    t.status_change_reason = reason
    t.etag = uuid.uuid4()
    await db.flush()
    snapshot_after = _task_snapshot(t)
    db.add(
        PatientTaskTransition(
            task_id=t.id,
            action=action,
            idempotency_key=idempotency_key,
            snapshot_before=snapshot_before,
            snapshot_after=snapshot_after,
            actor_subject_id=user.subject_id,
            author_kind=_author_kind(request),
            reason=reason,
        )
    )
    diff: dict = {"status": {"from": snapshot_before["status"], "to": new_status}}
    if extra_diff:
        diff.update(extra_diff)
    await _record_provenance(
        db,
        target_id=t.id,
        activity=f"transition.{action}",
        user=user,
        request=request,
        diff=diff,
    )
    # Notification scheduling side effects of the task FSM:
    # - complete / drop → the task is off the calendar; cancel
    #   pending reminders
    # - reopen (back to pending) → if due_at + offsets are still
    #   set, re-materialise so the recipient gets reminded again
    # - snooze / wake → the dispatcher row's scheduled_at was
    #   computed from due_at, which we have NOT moved here; the
    #   existing dispatches stay valid
    if new_status in (fsm.DONE, fsm.DROPPED):
        await cancel_dispatches_for_target(
            db, "patient_task", t.id, reason=f"transition_{new_status}"
        )
    elif action == "reopen" and t.due_at and t.reminder_offsets_minutes:
        await materialise_task_dispatches(db, t)
    await db.commit()
    await db.refresh(t)
    return t


def _transition_response(t: PatientTask, request: Request, response: Response) -> PatientTaskOut:
    out = _to_out(t)
    response.headers["ETag"] = format_etag(str(t.etag))
    request.state.response_etag = out.etag
    return out


def _require_idem(idempotency_key: str | None) -> str:
    if not idempotency_key:
        raise HTTPException(status_code=428, detail="Idempotency-Key header required")
    return idempotency_key


@router.post(
    "/patient-tasks/{task_id}/start",
    response_model=PatientTaskOut,
    status_code=status.HTTP_200_OK,
)
async def start_task(
    task_id: uuid.UUID,
    body: StartIn,
    request: Request,
    response: Response,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    audit: AuditDep,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    dry_run: Annotated[bool, Query()] = False,
) -> PatientTaskOut:
    """``pending`` → ``in_progress``. Empty body."""
    del audit, body
    idem = _require_idem(idempotency_key)
    t = await _load_task_with_patient_access(
        db, request=request, task_id=task_id, user=user, action_perm=WRITE_REPORT
    )
    await _check_if_match(if_match, str(t.etag))
    replay = await _idempotency_replay(db, task_id=task_id, action="start", idempotency_key=idem)
    if replay is not None:
        return _replay_to_out(replay)
    fsm.assert_transition_allowed(from_status=t.status, to_status=fsm.IN_PROGRESS)
    snapshot_before = _task_snapshot(t)
    if dry_run:
        return PatientTaskOut(**{**_to_out(t).model_dump(), "status": fsm.IN_PROGRESS})
    t = await _persist_transition(
        db,
        t=t,
        new_status=fsm.IN_PROGRESS,
        action="start",
        snapshot_before=snapshot_before,
        user=user,
        request=request,
        reason=None,
        idempotency_key=idem,
    )
    return _transition_response(t, request, response)


@router.post(
    "/patient-tasks/{task_id}/snooze",
    response_model=PatientTaskOut,
    status_code=status.HTTP_200_OK,
)
async def snooze_task(
    task_id: uuid.UUID,
    body: SnoozeIn,
    request: Request,
    response: Response,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    audit: AuditDep,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    dry_run: Annotated[bool, Query()] = False,
) -> PatientTaskOut:
    """``pending``/``in_progress`` → ``snoozed`` with ``snooze_until``."""
    del audit
    idem = _require_idem(idempotency_key)
    t = await _load_task_with_patient_access(
        db, request=request, task_id=task_id, user=user, action_perm=WRITE_REPORT
    )
    await _check_if_match(if_match, str(t.etag))
    replay = await _idempotency_replay(db, task_id=task_id, action="snooze", idempotency_key=idem)
    if replay is not None:
        return _replay_to_out(replay)
    fsm.assert_transition_allowed(from_status=t.status, to_status=fsm.SNOOZED)
    snapshot_before = _task_snapshot(t)
    if dry_run:
        out = _to_out(t).model_dump()
        out["status"] = fsm.SNOOZED
        out["snooze_until"] = body.snooze_until.isoformat()
        return PatientTaskOut(**out)
    t.snooze_until = body.snooze_until
    t = await _persist_transition(
        db,
        t=t,
        new_status=fsm.SNOOZED,
        action="snooze",
        snapshot_before=snapshot_before,
        user=user,
        request=request,
        reason=body.reason,
        idempotency_key=idem,
        extra_diff={"snooze_until": body.snooze_until.isoformat()},
    )
    return _transition_response(t, request, response)


@router.post(
    "/patient-tasks/{task_id}/wake",
    response_model=PatientTaskOut,
    status_code=status.HTTP_200_OK,
)
async def wake_task(
    task_id: uuid.UUID,
    body: WakeIn,
    request: Request,
    response: Response,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    audit: AuditDep,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    dry_run: Annotated[bool, Query()] = False,
) -> PatientTaskOut:
    """``snoozed`` → ``pending`` (default) or ``in_progress`` (resume)."""
    del audit
    idem = _require_idem(idempotency_key)
    t = await _load_task_with_patient_access(
        db, request=request, task_id=task_id, user=user, action_perm=WRITE_REPORT
    )
    await _check_if_match(if_match, str(t.etag))
    replay = await _idempotency_replay(db, task_id=task_id, action="wake", idempotency_key=idem)
    if replay is not None:
        return _replay_to_out(replay)
    target = fsm.IN_PROGRESS if body.resume_in_progress else fsm.PENDING
    fsm.assert_transition_allowed(from_status=t.status, to_status=target)
    snapshot_before = _task_snapshot(t)
    if dry_run:
        out = _to_out(t).model_dump()
        out["status"] = target
        out["snooze_until"] = None
        return PatientTaskOut(**out)
    t.snooze_until = None
    t = await _persist_transition(
        db,
        t=t,
        new_status=target,
        action="wake",
        snapshot_before=snapshot_before,
        user=user,
        request=request,
        reason=None,
        idempotency_key=idem,
    )
    return _transition_response(t, request, response)


@router.post(
    "/patient-tasks/{task_id}/complete",
    response_model=PatientTaskOut,
    status_code=status.HTTP_200_OK,
)
async def complete_task(
    task_id: uuid.UUID,
    body: CompleteIn,
    request: Request,
    response: Response,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    audit: AuditDep,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    dry_run: Annotated[bool, Query()] = False,
) -> PatientTaskOut:
    """``pending``/``in_progress`` → ``done`` with optional ``completed_at``."""
    del audit
    idem = _require_idem(idempotency_key)
    t = await _load_task_with_patient_access(
        db, request=request, task_id=task_id, user=user, action_perm=WRITE_REPORT
    )
    await _check_if_match(if_match, str(t.etag))
    replay = await _idempotency_replay(db, task_id=task_id, action="complete", idempotency_key=idem)
    if replay is not None:
        return _replay_to_out(replay)
    fsm.assert_transition_allowed(from_status=t.status, to_status=fsm.DONE)
    snapshot_before = _task_snapshot(t)
    when = body.completed_at or datetime.now(UTC)
    if dry_run:
        out = _to_out(t).model_dump()
        out["status"] = fsm.DONE
        out["completed_at"] = when.isoformat()
        return PatientTaskOut(**out)
    t.completed_at = when
    t = await _persist_transition(
        db,
        t=t,
        new_status=fsm.DONE,
        action="complete",
        snapshot_before=snapshot_before,
        user=user,
        request=request,
        reason=body.note,
        idempotency_key=idem,
        extra_diff={"completed_at": when.isoformat()},
    )
    return _transition_response(t, request, response)


@router.post(
    "/patient-tasks/{task_id}/drop",
    response_model=PatientTaskOut,
    status_code=status.HTTP_200_OK,
)
async def drop_task(
    task_id: uuid.UUID,
    body: DropIn,
    request: Request,
    response: Response,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    audit: AuditDep,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    dry_run: Annotated[bool, Query()] = False,
) -> PatientTaskOut:
    """``pending``/``in_progress`` → ``dropped`` (won't do). Reason required."""
    del audit
    idem = _require_idem(idempotency_key)
    t = await _load_task_with_patient_access(
        db, request=request, task_id=task_id, user=user, action_perm=WRITE_REPORT
    )
    await _check_if_match(if_match, str(t.etag))
    replay = await _idempotency_replay(db, task_id=task_id, action="drop", idempotency_key=idem)
    if replay is not None:
        return _replay_to_out(replay)
    fsm.assert_transition_allowed(from_status=t.status, to_status=fsm.DROPPED)
    snapshot_before = _task_snapshot(t)
    if dry_run:
        out = _to_out(t).model_dump()
        out["status"] = fsm.DROPPED
        out["status_change_reason"] = body.reason
        return PatientTaskOut(**out)
    t = await _persist_transition(
        db,
        t=t,
        new_status=fsm.DROPPED,
        action="drop",
        snapshot_before=snapshot_before,
        user=user,
        request=request,
        reason=body.reason,
        idempotency_key=idem,
    )
    return _transition_response(t, request, response)


@router.post(
    "/patient-tasks/{task_id}/reopen",
    response_model=PatientTaskOut,
    status_code=status.HTTP_200_OK,
)
async def reopen_task(
    task_id: uuid.UUID,
    body: ReopenIn,
    request: Request,
    response: Response,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    audit: AuditDep,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    dry_run: Annotated[bool, Query()] = False,
) -> PatientTaskOut:
    """``done``/``dropped`` → ``pending``. The original ``completed_at``
    stays as historical evidence; the status alone tells the story."""
    del audit
    idem = _require_idem(idempotency_key)
    t = await _load_task_with_patient_access(
        db, request=request, task_id=task_id, user=user, action_perm=WRITE_REPORT
    )
    await _check_if_match(if_match, str(t.etag))
    replay = await _idempotency_replay(db, task_id=task_id, action="reopen", idempotency_key=idem)
    if replay is not None:
        return _replay_to_out(replay)
    fsm.assert_transition_allowed(from_status=t.status, to_status=fsm.PENDING)
    snapshot_before = _task_snapshot(t)
    if dry_run:
        out = _to_out(t).model_dump()
        out["status"] = fsm.PENDING
        return PatientTaskOut(**out)
    t = await _persist_transition(
        db,
        t=t,
        new_status=fsm.PENDING,
        action="reopen",
        snapshot_before=snapshot_before,
        user=user,
        request=request,
        reason=body.reason,
        idempotency_key=idem,
    )
    return _transition_response(t, request, response)
