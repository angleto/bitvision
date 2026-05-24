"""ClinicalEvent. Temporal axis, atomic level: a single event in a patient's timeline.

Pre-v3 the only first-class event was a DICOM ``Study``. v3 promotes
the concept of "an event in the patient's clinical timeline" to a
table of its own. ``ClinicalEvent`` is the parent; specialised
projections (``ImagingStudy`` from ``dicom.py``, future
``SurgicalProcedure``, ``OutpatientVisit``…) link back via 1:1 FKs.

Every report_content, document link, citation, and tag in the v3
model targets a ``clinical_event_id`` rather than a study/procedure
specific id, so a query "give me everything about this event"
crosses zero subtype tables.

The ``kind`` column discriminates the projection: when ``kind ==
'imaging_study'`` the ``imaging_studies`` row exists and carries the
DICOM-specific fields; for other kinds the imaging child is absent
and the event is described purely by its narrative + linked
documents.

Phase grouping: an event may belong to a ``care_phase`` (see
``care_phases.py``). The link is materialised by
``ClinicalEvent.phase_id``, which is part of a **composite** foreign
key ``(patient_id, phase_id) → care_phase (patient_id, id)``. The
composite FK makes cross-patient phase assignment impossible at the
DB level: PostgreSQL rejects any insert/update where the phase
belongs to a different patient.

Conceptual placement: see ``docs/data-model.md §0`` (three-axis model)
and ``docs/care-timeline-phases.md §1.5``. The grouping level
(``CarePhase``) lives in ``care_phases.py``; the organisational axis
(``Folder``) and the cross-cutting axis (``Tag``) are orthogonal.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    Float,
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

CLINICAL_EVENT_KINDS: tuple[str, ...] = (
    "imaging_study",
    "surgical_procedure",
    "outpatient_visit",
    "inpatient_admission",
    "lab_batch",
    "consultation_event",
    # Specialised review kinds added 2026-05-03 after the internal
    # session showed the existing enum collapsed too many distinct
    # clinical episodes onto ``other``: a pathology readout, a tumour
    # board, a cardio ECG and an endoscopy session each carry their
    # own dynamics in a real fascicolo.
    "pathology_review",
    "mdt_meeting",
    "cardio_diagnostic",
    "endoscopy",
    # ``radiology_appointment`` (migration 0104) = scheduled radiology
    # exam (TC, RX, MRI, eco) BEFORE the DICOM lands. Distinct from
    # ``imaging_study`` which represents the materialised DICOM study;
    # this kind covers the planned/confirmed appointment phase that
    # the user owns from the calendar UX.
    "radiology_appointment",
    "other",
)

# Tracks where a row originated; ``NULL`` for manually-created events.
# Used by the backfill migrations to mark their rows so the marker
# does not contaminate ``narrative`` (which is reserved for clinical
# free-text). Free-form on purpose — adding a new backfill source
# does not require a schema change.
CLINICAL_EVENT_SOURCES: tuple[str, ...] = (
    "imaging_ingest",
    "backfill_imaging_v1",
    "backfill_documents_v1",
)

# Lifecycle for an event on the timeline. ``completed`` is the
# historical default (the only state the v3 model supported before
# migration 0098): something that happened. ``planned``/``confirmed``
# represent a future appointment; ``cancelled``/``missed`` close the
# lifecycle without a "did happen"; ``rescheduled`` is a terminal
# marker pointing at the new occurrence row via ``parent_event_id``.
# The FSM that enforces valid transitions lives in
# ``bvphoenix.services.clinical_events_fsm`` (introduced in step 2).
CLINICAL_EVENT_STATUSES: tuple[str, ...] = (
    "planned",
    "confirmed",
    "completed",
    "cancelled",
    "missed",
    "rescheduled",
)

# Who triggered the most recent status change. Mirrors the
# ``author_kind`` semantics used throughout the audit chain so a
# timeline reader can tell at a glance whether a planned event was
# created by a human, proposed by an agent, or materialised by a
# system pipeline (ICS import, GCal pull).
CLINICAL_EVENT_STATUS_KINDS: tuple[str, ...] = ("human", "agent", "system")


class ClinicalEvent(Base):
    __tablename__ = "clinical_events"

    id: Mapped[uuid.UUID] = uuid_pk()
    patient_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("patients.id", ondelete="CASCADE"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    event_date: Mapped[date | None] = mapped_column(Date)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    body_part: Mapped[str | None] = mapped_column(String(64))
    code_loinc: Mapped[str | None] = mapped_column(String(32))
    code_snomed: Mapped[str | None] = mapped_column(String(32))
    narrative: Mapped[str | None] = mapped_column(Text)
    # Provenance marker for system-created events (backfills, ingest
    # pipelines). NULL means human/agent-created. Read by the timeline
    # UI to show a "system-generated, please review" hint and by the
    # tests in ``test_dicom_ingest_clinical_event.py``.
    source: Mapped[str | None] = mapped_column(String(64))
    etag: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=False,
        server_default=text("gen_random_uuid()"),
    )
    # ---- care phase grouping (composite FK in __table_args__) ----------
    phase_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    phase_assigned_by: Mapped[str | None] = mapped_column(String(16))
    phase_assigned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    phase_assignment_confidence: Mapped[float | None] = mapped_column(Float)
    # ---- planning & calendar (migration 0098) --------------------------
    # Lifecycle status. Defaults to ``completed`` so the historical
    # rows (DATE-only events that already happened) keep working
    # without an explicit value. The CHECK constraint
    # ``ck_clinical_events_time_required_by_status`` enforces that
    # planned/confirmed have ``planned_start_at`` and completed/missed
    # have ``actual_start_at``.
    event_status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'completed'")
    )
    # Timezone-aware timestamps. ``planned_*`` for future scheduling
    # (set when the event is created as planned/confirmed); ``actual_*``
    # for the realised time (set when transitioning to completed). The
    # legacy ``event_date`` DATE above is kept in sync by a BEFORE
    # INSERT/UPDATE trigger so existing queries and indices keep
    # working without modification.
    planned_start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    planned_end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    actual_start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    actual_end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # IANA timezone name (e.g. ``Europe/Rome``). Used by the
    # ``event_date`` derivation trigger so that an event at 23:30
    # Rome-time lands on the local DATE the user expects, not on the
    # UTC-next-day.
    timezone: Mapped[str | None] = mapped_column(String(64))
    # Free-form location struct: ``{facility, room, city, address,
    # phone, provider_id?}``. Validated by pydantic schemas; no FK to
    # a provider table yet (introduced in step 4 of the roadmap).
    location_struct: Mapped[dict | None] = mapped_column(JSONB)
    # RFC 5545 RRULE + EXDATE for recurring events (weekly follow-ups,
    # monthly imaging, ...). Expansion happens server-side in the
    # ``/calendar`` feed, not at write time.
    recurrence_rule: Mapped[str | None] = mapped_column(String(512))
    recurrence_exdates: Mapped[list | None] = mapped_column(JSONB)
    # Reschedule chain: a row in ``rescheduled`` state points at its
    # successor via ``parent_event_id``. Composite FK to clinical_events
    # on ``(patient_id, parent_event_id)`` makes cross-patient parent
    # impossible at the DB level (seventh layer of cross-patient
    # defense).
    parent_event_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    # Pre-event reminder offsets in minutes (negative). Cross-channel
    # dispatching lives in workers/ (step 4); the column is stored
    # here so a reminder configuration survives event editing without
    # an extra table round-trip.
    reminder_offsets_minutes: Mapped[list | None] = mapped_column(JSONB)
    # External calendar binding. ``external_calendar_link_id`` will
    # FK to the ``external_calendar_links`` table when step 4 lands;
    # for step 1 it's a nullable column kept ready for the eventual
    # bind without needing a follow-up migration on this hot table.
    external_calendar_link_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    external_event_id: Mapped[str | None] = mapped_column(String(255))
    external_event_etag: Mapped[str | None] = mapped_column(String(255))
    # Inline audit of the most recent status change. The full chain
    # is still in ``provenance_events``; these columns let a timeline
    # reader see "last edited by agent at …" without an extra query.
    status_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status_changed_by_kind: Mapped[str | None] = mapped_column(String(16))
    status_change_reason: Mapped[str | None] = mapped_column(String(255))
    # ---- Meet link, generic links (migration 0101) ---------------------------
    # ``meeting_url``: click-to-join entry point (Meet / Zoom / Jitsi).
    # ``links``: free-form list of ``{label, url}`` references (booking
    # portal, structure website, referral, ...). Binary attachments
    # live on the dedicated ``clinical_event_attachments`` table since
    # migration 0102 — the URL-JSONB ``attachments`` column was dropped
    # because it overlapped with ``links`` and didn't carry the actual
    # file the users wanted to upload.
    meeting_url: Mapped[str | None] = mapped_column(String(512))
    links: Mapped[list | None] = mapped_column(JSONB)
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
            "kind IN (" + ",".join(f"'{k}'" for k in CLINICAL_EVENT_KINDS) + ")",
            name="ck_clinical_events_kind",
        ),
        CheckConstraint(
            "phase_assigned_by IS NULL OR phase_assigned_by IN ('human','agent')",
            name="ck_clinical_events_phase_assigned_by",
        ),
        CheckConstraint(
            "phase_assignment_confidence IS NULL "
            "OR (phase_assignment_confidence >= 0 AND phase_assignment_confidence <= 1)",
            name="ck_clinical_events_phase_confidence_range",
        ),
        # Lifecycle CHECKs from migration 0098.
        CheckConstraint(
            "event_status IN (" + ",".join(f"'{s}'" for s in CLINICAL_EVENT_STATUSES) + ")",
            name="ck_clinical_events_status",
        ),
        CheckConstraint(
            # planned/confirmed REQUIRE planned_start_at; other statuses
            # are permissive (a historical row with ``event_status =
            # 'completed'`` and no ``actual_start_at`` is legitimate
            # — happens for fascicoli backfilled from documents that
            # lost their original document_date).
            "event_status NOT IN ('planned','confirmed') OR planned_start_at IS NOT NULL",
            name="ck_clinical_events_time_required_by_status",
        ),
        CheckConstraint(
            "status_changed_by_kind IS NULL "
            "OR status_changed_by_kind IN ('human','agent','system')",
            name="ck_clinical_events_status_by_kind",
        ),
        # Composite FK: cross-patient phase assignment is impossible.
        ForeignKeyConstraint(
            ["patient_id", "phase_id"],
            ["care_phase.patient_id", "care_phase.id"],
            name="fk_clinical_events_phase",
            ondelete="SET NULL",
        ),
        # UNIQUE on (patient_id, id): required so the composite FK
        # ``fk_clinical_events_parent`` below has a unique tuple to
        # reference (Postgres composite FKs need a matching unique
        # constraint or unique index on the referenced columns).
        UniqueConstraint("patient_id", "id", name="uq_clinical_events_patient_id"),
        # Composite FK: cross-patient parent event impossible (seventh
        # layer of cross-patient defense — DB-enforced).
        ForeignKeyConstraint(
            ["patient_id", "parent_event_id"],
            ["clinical_events.patient_id", "clinical_events.id"],
            name="fk_clinical_events_parent",
            ondelete="SET NULL",
        ),
        Index("ix_clinical_events_patient", "patient_id"),
        Index(
            "ix_clinical_events_patient_date",
            "patient_id",
            text("event_date DESC"),
        ),
        Index("ix_clinical_events_kind", "kind"),
        Index(
            "ix_clinical_events_phase",
            "phase_id",
            postgresql_where=text("phase_id IS NOT NULL"),
        ),
        # Calendar feed indices (partial). One per access pattern so
        # the planner never falls back to a full scan on hot routes.
        Index(
            "ix_clinical_events_patient_status_planned",
            "patient_id",
            "event_status",
            "planned_start_at",
            postgresql_where=text("event_status IN ('planned','confirmed')"),
        ),
        Index(
            "ix_clinical_events_patient_actual",
            "patient_id",
            text("actual_start_at DESC NULLS LAST"),
            postgresql_where=text("event_status IN ('completed','missed')"),
        ),
        Index(
            "ix_clinical_events_parent",
            "parent_event_id",
            postgresql_where=text("parent_event_id IS NOT NULL"),
        ),
        Index(
            "ix_clinical_events_external_calendar",
            "external_calendar_link_id",
            "external_event_id",
            postgresql_where=text("external_calendar_link_id IS NOT NULL"),
        ),
    )


__all__ = [
    "CLINICAL_EVENT_KINDS",
    "CLINICAL_EVENT_SOURCES",
    "CLINICAL_EVENT_STATUSES",
    "CLINICAL_EVENT_STATUS_KINDS",
    "ClinicalEvent",
]
