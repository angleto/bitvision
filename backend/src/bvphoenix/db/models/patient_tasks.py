"""PatientTask. Operational checklist alongside the clinical timeline.

Pre-v3.4 the only first-class item on the patient timeline was a
``ClinicalEvent``: something that did happen, will happen, or was
planned/cancelled. v3.4 introduces ``PatientTask`` as a separate
entity for *operational* items that do not belong on the clinical
record: "ask the GP for the impegnativa", "buy the medication",
"call the CUP to book the TAC", "remind the caregiver to fetch
results on Friday".

Why a separate table, not a new ``kind`` of ClinicalEvent
---------------------------------------------------------

1. **Clinical record purity.** Imaging, visits, procedures, lab
   batches end up in FSE/HL7/FHIR exports as clinical events. A
   "buy medication" task is not a clinical event and must not leak
   into those exports as ``kind='other'``.

2. **Different FSM.** A ``ClinicalEvent`` lifecycle is
   ``planned → confirmed → completed | cancelled | missed | rescheduled``
   (something happens, or doesn't). A task is
   ``pending → in_progress → done | dropped | snoozed`` (someone
   does something, or doesn't). Forcing one onto the other distorts
   both audit chains.

3. **Different export semantics.** Tasks are private operational
   notes; clinical events are part of the patient's medical record.
   The UI surfaces them separately (``/patients/{id}/timeline/tasks``
   vs ``/timeline/clinical``) with an opt-in merge view.

What is shared with ``ClinicalEvent``
-------------------------------------

* Cross-patient inexpressibility: every cross-table FK is composite
  ``(patient_id, target_id)`` so PostgreSQL rejects any cross-patient
  reference at the DB level. ``UNIQUE(patient_id, id)`` is declared
  so future tables can carry composite FKs into PatientTask.
* ETag + Idempotency-Key contract on every write (managed by the
  API layer, see ``api/patient_tasks.py``).
* Author kind ('human' | 'agent') stamped on every row and on every
  status transition, so the timeline can show "AI" badges on tasks
  drafted by an MCP agent.
* Sibling ``PatientTaskTransition`` table for idempotency replay +
  inline audit of state changes, mirroring
  ``ClinicalEventTransition`` exactly.
* Soft delete (``deleted_at`` / ``deleted_by_subject_id``) with git-
  like restore, same as ``Document``.

Phase grouping
--------------

A task MAY belong to a ``CarePhase`` (same composite FK pattern as
``ClinicalEvent.phase_id``). Default is ``NULL`` because most
operational tasks are not tied to a phase (a pharmacy run is not a
"surgery phase" item). Grouping is opt-in via the UI.

Recurrence
----------

``recurrence_rule`` is stored as RFC 5545 RRULE for parity with
clinical events, but expansion is NOT done server-side in this
sprint; the column is kept ready so a follow-up PR can wire the
expander once the use case stabilises.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from bvphoenix.db.base import Base
from bvphoenix.db.models._common import uuid_pk

# ---------------------------------------------------------------------------
# Enums (string-typed, mirrored as Python tuples so the API / MCP layer can
# enumerate valid values without re-parsing the CHECK constraint at runtime).
# ---------------------------------------------------------------------------

PATIENT_TASK_STATUSES: tuple[str, ...] = (
    "pending",
    "in_progress",
    "snoozed",
    "done",
    "dropped",
)

PATIENT_TASK_PRIORITIES: tuple[str, ...] = ("low", "normal", "high", "urgent")

# Free-form categorisation. ``admin`` covers paperwork (impegnativa,
# referral, insurance forms); ``pharmacy`` is medication procurement;
# ``appointment_prep`` is anything that has to happen BEFORE a clinical
# appointment (fasting, prep meds, transport booking); ``transport``
# is logistics to/from a facility; ``communication`` is calls/emails
# to clinicians or caregivers; ``personal`` is the patient's own
# self-care reminders; ``other`` is the escape hatch.
PATIENT_TASK_CATEGORIES: tuple[str, ...] = (
    "admin",
    "pharmacy",
    "appointment_prep",
    "transport",
    "communication",
    "personal",
    "other",
)

PATIENT_TASK_AUTHOR_KINDS: tuple[str, ...] = ("human", "agent", "system")

# Verbs persisted by the API layer on PatientTaskTransition. Mirrors
# the sub-resource names so the audit chain is grep-friendly.
PATIENT_TASK_TRANSITION_ACTIONS: tuple[str, ...] = (
    "start",
    "snooze",
    "wake",
    "complete",
    "drop",
    "reopen",
    "reschedule",
)


# ---------------------------------------------------------------------------
# PatientTask
# ---------------------------------------------------------------------------


class PatientTask(Base):
    __tablename__ = "patient_tasks"

    id: Mapped[uuid.UUID] = uuid_pk()
    patient_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("patients.id", ondelete="CASCADE"),
        nullable=False,
    )

    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'other'")
    )
    priority: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'normal'")
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'pending'")
    )

    # Scheduling. ``due_at`` is the "do it by" timestamp (e.g. "before
    # tomorrow's chemo at 10:00"). ``snooze_until`` is set only while
    # the row is in ``snoozed`` state; the CHECK constraint
    # ``ck_patient_tasks_snooze_when`` makes the pair coherent.
    # ``completed_at`` is the actually-done timestamp, recorded on the
    # ``complete`` transition.
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    snooze_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    timezone: Mapped[str | None] = mapped_column(String(64))

    # ---- care phase grouping (composite FK in __table_args__) -----
    # Mirrors ``ClinicalEvent.phase_id`` exactly so the same UI
    # grouping primitive covers both surfaces in the merged timeline.
    phase_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    phase_assigned_by: Mapped[str | None] = mapped_column(String(16))
    phase_assigned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Recurrence (stored, NOT expanded server-side yet — see module
    # docstring).
    recurrence_rule: Mapped[str | None] = mapped_column(String(512))

    # Reschedule chain: a task moved to a new ``due_at`` materialises
    # a NEW task row pointing at the old one via ``parent_task_id``,
    # mirroring the ClinicalEvent reschedule pattern. The old row
    # stays in ``dropped`` state (terminal) for audit. Composite FK
    # to ``patient_tasks (patient_id, id)`` enforces same-patient
    # chain at the DB.
    parent_task_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))

    # Assignment to a contact (caregiver, family member, GP). When
    # set, the notification dispatcher uses that contact's preferred
    # channel(s) for reminders. Composite FK on
    # ``patient_contacts (patient_id, id)`` keeps the contact strictly
    # same-patient (the UNIQUE constraint on patient_contacts is added
    # by migration 0106 alongside this table).
    assigned_to_contact_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))

    # Anchors. A task may relate to a clinical event ("buy meds before
    # tomorrow's chemo") or to a document ("re-read the discharge
    # letter and call the GP"). Both anchors are optional and
    # same-patient enforced at the DB.
    related_event_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    related_document_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="SET NULL"),
    )

    # Free-form labels (["urgente", "esami", "CUP"]) and structured
    # outbound links (booking portal, referral, ...). Mirror the
    # JSONB convention used by ClinicalEvent.
    labels: Mapped[list | None] = mapped_column(JSONB)
    links: Mapped[list | None] = mapped_column(JSONB)

    # Pre-due reminder offsets in minutes (e.g. [-1440, -60] = 1 day
    # and 1 hour before due_at). Consumed by the notification
    # dispatcher (sprint C). Pydantic-side validator caps the list
    # length to 5 to bound the dispatcher fan-out.
    reminder_offsets_minutes: Mapped[list | None] = mapped_column(JSONB)

    # ETag for optimistic concurrency. Regenerated on every PATCH /
    # transition so concurrent agents detect each other's writes.
    etag: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=False,
        server_default=text("gen_random_uuid()"),
    )

    # Inline audit of the row creator + most recent status change.
    # The full chain is in ``provenance_events`` (target_kind =
    # 'patient_task'); these columns let a timeline reader see
    # "drafted by AI", "last marked done by Alice" without an extra
    # query.
    author_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    created_by_subject_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    status_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status_changed_by_kind: Mapped[str | None] = mapped_column(String(16))
    status_change_reason: Mapped[str | None] = mapped_column(String(255))

    # Soft delete, git-like. ``deleted_at`` is the tombstone marker;
    # ``restore`` clears it. Most list queries filter on
    # ``deleted_at IS NULL`` via a partial index.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_by_subject_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        CheckConstraint(
            "status IN (" + ",".join(f"'{s}'" for s in PATIENT_TASK_STATUSES) + ")",
            name="ck_patient_tasks_status",
        ),
        CheckConstraint(
            "priority IN (" + ",".join(f"'{p}'" for p in PATIENT_TASK_PRIORITIES) + ")",
            name="ck_patient_tasks_priority",
        ),
        CheckConstraint(
            "category IN (" + ",".join(f"'{c}'" for c in PATIENT_TASK_CATEGORIES) + ")",
            name="ck_patient_tasks_category",
        ),
        CheckConstraint(
            "author_kind IN ('human','agent','system')",
            name="ck_patient_tasks_author_kind",
        ),
        CheckConstraint(
            "status_changed_by_kind IS NULL "
            "OR status_changed_by_kind IN ('human','agent','system')",
            name="ck_patient_tasks_status_by_kind",
        ),
        CheckConstraint(
            "phase_assigned_by IS NULL OR phase_assigned_by IN ('human','agent','system')",
            name="ck_patient_tasks_phase_assigned_by",
        ),
        # Snoozed tasks must have a wake-up time. The FSM in
        # services/patient_tasks_fsm.py enforces this in Python too;
        # the CHECK is the belt to the FSM's braces.
        CheckConstraint(
            "status <> 'snoozed' OR snooze_until IS NOT NULL",
            name="ck_patient_tasks_snooze_when",
        ),
        # Done tasks should carry a completed_at; we keep it permissive
        # (NOT enforced) because backfills from external sources may
        # not have a precise timestamp. The service layer sets it on
        # the ``complete`` transition.
        # ---- Cross-patient inexpressibility (DB-enforced) -----------
        # UNIQUE(patient_id, id) so future tables can carry a composite
        # FK referencing this one.
        UniqueConstraint("patient_id", "id", name="uq_patient_tasks_patient_id"),
        # Phase grouping: composite FK rejects cross-patient assignment.
        ForeignKeyConstraint(
            ["patient_id", "phase_id"],
            ["care_phase.patient_id", "care_phase.id"],
            name="fk_patient_tasks_phase",
            ondelete="SET NULL",
        ),
        # Reschedule parent: composite FK keeps the chain same-patient.
        ForeignKeyConstraint(
            ["patient_id", "parent_task_id"],
            ["patient_tasks.patient_id", "patient_tasks.id"],
            name="fk_patient_tasks_parent",
            ondelete="SET NULL",
        ),
        # Assigned contact: composite FK to patient_contacts. Requires
        # ``UNIQUE(patient_id, id)`` on ``patient_contacts``, added by
        # migration 0106 in the same upgrade as this table.
        ForeignKeyConstraint(
            ["patient_id", "assigned_to_contact_id"],
            ["patient_contacts.patient_id", "patient_contacts.id"],
            name="fk_patient_tasks_assigned_contact",
            ondelete="SET NULL",
        ),
        # Related clinical event: composite FK to clinical_events.
        # ``UNIQUE(patient_id, id)`` already exists on clinical_events
        # since migration 0099.
        ForeignKeyConstraint(
            ["patient_id", "related_event_id"],
            ["clinical_events.patient_id", "clinical_events.id"],
            name="fk_patient_tasks_related_event",
            ondelete="SET NULL",
        ),
        # ---- Access pattern indices --------------------------------
        Index("ix_patient_tasks_patient", "patient_id"),
        # Active tasks per patient, ordered by due date. Powers the
        # ``/tasks?status=pending,in_progress`` list endpoint.
        Index(
            "ix_patient_tasks_active",
            "patient_id",
            "status",
            "due_at",
            postgresql_where=text("deleted_at IS NULL AND status IN ('pending','in_progress')"),
        ),
        # Snoozed tasks waiting to wake. The dispatcher wakes them up
        # when ``snooze_until <= now()``.
        Index(
            "ix_patient_tasks_snoozed",
            "snooze_until",
            postgresql_where=text("status = 'snoozed' AND deleted_at IS NULL"),
        ),
        # Phase grouping lookup.
        Index(
            "ix_patient_tasks_phase",
            "phase_id",
            postgresql_where=text("phase_id IS NOT NULL AND deleted_at IS NULL"),
        ),
        # Reschedule chain navigation.
        Index(
            "ix_patient_tasks_parent",
            "parent_task_id",
            postgresql_where=text("parent_task_id IS NOT NULL"),
        ),
        # Tombstone view: ``?include_deleted=true`` lists.
        Index(
            "ix_patient_tasks_deleted",
            "deleted_at",
            postgresql_where=text("deleted_at IS NOT NULL"),
        ),
    )


# ---------------------------------------------------------------------------
# PatientTaskTransition — append-only audit + idempotency-replay row.
# ---------------------------------------------------------------------------


class PatientTaskTransition(Base):
    __tablename__ = "patient_task_transitions"

    id: Mapped[uuid.UUID] = uuid_pk()
    task_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("patient_tasks.id", ondelete="CASCADE"),
        nullable=False,
    )
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    snapshot_before: Mapped[dict] = mapped_column(JSONB, nullable=False)
    snapshot_after: Mapped[dict] = mapped_column(JSONB, nullable=False)
    actor_subject_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    author_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "action IN (" + ",".join(f"'{a}'" for a in PATIENT_TASK_TRANSITION_ACTIONS) + ")",
            name="ck_pt_transitions_action",
        ),
        CheckConstraint(
            "author_kind IN ('human','agent','system')",
            name="ck_pt_transitions_author_kind",
        ),
        UniqueConstraint(
            "task_id",
            "action",
            "idempotency_key",
            name="uq_pt_transitions_idempotency",
        ),
        Index("ix_pt_transitions_task", "task_id"),
        Index("ix_pt_transitions_task_created", "task_id", "created_at"),
    )


__all__ = [
    "PATIENT_TASK_AUTHOR_KINDS",
    "PATIENT_TASK_CATEGORIES",
    "PATIENT_TASK_PRIORITIES",
    "PATIENT_TASK_STATUSES",
    "PATIENT_TASK_TRANSITION_ACTIONS",
    "PatientTask",
    "PatientTaskTransition",
]
