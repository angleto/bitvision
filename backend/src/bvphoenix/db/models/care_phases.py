"""Care phases — semantic grouping of clinical events on the timeline.

A care phase (``imaging-pre-op``, ``intervento-chirurgico``,
``followup-post-op``…) groups one or more ``clinical_events`` that
belong to the same chapter of a patient's history. Phases are the
colored chips on the left of the vertical timeline; events are the
dots on the right. The model is persisted (not derived) so phases are
deterministic, audit-able, cache-friendly and editable by humans.
The LLM classifier proposes phases and event-to-phase assignments;
humans confirm and may edit.

Cross-patient is impossible by construction:

* ``CarePhase`` carries ``UNIQUE (patient_id, slug)`` and
  ``UNIQUE (patient_id, id)``.
* ``ClinicalEvent.phase_id`` is part of a **composite** foreign key
  ``(patient_id, phase_id) → care_phase (patient_id, id)`` declared
  in ``ClinicalEvent.__table_args__``. PostgreSQL therefore rejects
  any cross-patient assignment at the DDL level.

See ``backend/alembic/versions/0080_care_phase.py`` for the schema.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
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

CARE_PHASE_KINDS: tuple[str, ...] = (
    "imaging",
    "surgery",
    "followup",
    "surveillance",
    "visit",
    "reassessment",
    # ``planned`` is the auto-created bucket for future events
    # (event_status in {'planned', 'confirmed'}). Singleton per patient
    # — see ``services.planned_phase.ensure_planned_phase``. Events
    # exit this bucket on terminal transitions (complete / cancel /
    # mark-missed / rescheduled) and are then unassigned, ready for
    # manual or LLM-driven classification into the proper clinical
    # phase.
    "planned",
    "other",
)

CARE_PHASE_AUTHOR_KINDS: tuple[str, ...] = ("human", "agent")

CARE_PHASE_REVISION_CHANGE_KINDS: tuple[str, ...] = (
    "create",
    "update",
    "assign",
    "unassign",
    "apply_proposal",
    "restore",
    "delete",
)

# Default palette per kind, mirroring the colors of
# `the local reference SVG (not committed)`. The service layer
# applies these when the caller does not pass an explicit color.
CARE_PHASE_DEFAULT_COLORS: dict[str, str] = {
    "imaging": "#185FA5",
    "surgery": "#993C1D",
    "followup": "#534AB7",
    "surveillance": "#185FA5",
    "visit": "#534AB7",
    "reassessment": "#854F0B",
    "other": "#888780",
}


class CarePhase(Base):
    __tablename__ = "care_phase"

    id: Mapped[uuid.UUID] = uuid_pk()
    patient_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("patients.id", ondelete="CASCADE"),
        nullable=False,
    )
    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    name_i18n: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    color_hex: Mapped[str] = mapped_column(String(7), nullable=False)
    start_date: Mapped[date | None] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    narrative_md: Mapped[str | None] = mapped_column(Text)
    author_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    proposed_by_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("agent_tokens.id", ondelete="SET NULL"),
    )
    confirmed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("subjects.id", ondelete="SET NULL"),
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    etag: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=False,
        server_default=text("gen_random_uuid()"),
    )
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
            "kind IN (" + ",".join(f"'{k}'" for k in CARE_PHASE_KINDS) + ")",
            name="ck_care_phase_kind",
        ),
        CheckConstraint(
            "author_kind IN (" + ",".join(f"'{a}'" for a in CARE_PHASE_AUTHOR_KINDS) + ")",
            name="ck_care_phase_author_kind",
        ),
        CheckConstraint("color_hex ~ '^#[0-9A-Fa-f]{6}$'", name="ck_care_phase_color_hex"),
        CheckConstraint("ordinal >= 0", name="ck_care_phase_ordinal_nonneg"),
        UniqueConstraint("patient_id", "slug", name="uq_care_phase_patient_slug"),
        UniqueConstraint("patient_id", "id", name="uq_care_phase_patient_id"),
        Index("ix_care_phase_patient_ordinal", "patient_id", "ordinal"),
    )


class CarePhaseRevision(Base):
    __tablename__ = "care_phase_revision"

    id: Mapped[uuid.UUID] = uuid_pk()
    patient_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    phase_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    revision_no: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    change_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    author_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    diff_summary: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["patient_id", "phase_id"],
            ["care_phase.patient_id", "care_phase.id"],
            name="fk_care_phase_revision_phase",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "change_kind IN (" + ",".join(f"'{k}'" for k in CARE_PHASE_REVISION_CHANGE_KINDS) + ")",
            name="ck_care_phase_revision_kind",
        ),
        CheckConstraint(
            "author_kind IN (" + ",".join(f"'{a}'" for a in CARE_PHASE_AUTHOR_KINDS) + ")",
            name="ck_care_phase_revision_author_kind",
        ),
        CheckConstraint("revision_no >= 1", name="ck_care_phase_revision_no_pos"),
        UniqueConstraint("phase_id", "revision_no", name="uq_care_phase_revision_phase_no"),
        Index(
            "ix_care_phase_revision_phase",
            "phase_id",
            "revision_no",
        ),
        Index(
            "ix_care_phase_revision_patient",
            "patient_id",
        ),
    )


class CarePhaseProposal(Base):
    __tablename__ = "care_phase_proposal"

    id: Mapped[uuid.UUID] = uuid_pk()
    patient_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("patients.id", ondelete="CASCADE"),
        nullable=False,
    )
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("jobs.id", ondelete="SET NULL"),
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    model_id: Mapped[str] = mapped_column(String(128), nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    applied_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("subjects.id", ondelete="SET NULL"),
    )

    __table_args__ = (
        Index(
            "ix_care_phase_proposal_patient_hash",
            "patient_id",
            "input_hash",
        ),
        Index(
            "ix_care_phase_proposal_patient_created",
            "patient_id",
            text("created_at DESC"),
        ),
    )


__all__ = [
    "CARE_PHASE_AUTHOR_KINDS",
    "CARE_PHASE_DEFAULT_COLORS",
    "CARE_PHASE_KINDS",
    "CARE_PHASE_REVISION_CHANGE_KINDS",
    "CarePhase",
    "CarePhaseProposal",
    "CarePhaseRevision",
]
