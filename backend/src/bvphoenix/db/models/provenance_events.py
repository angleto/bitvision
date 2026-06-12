"""ProvenanceEvent — append-only lineage log.

Every interesting state transition in the v3 model emits one row
here: who did what to which target, with what source, when. The
table is append-only by service-layer convention; downstream a
hash chain (``signature_hash`` referencing the previous event for
the same target) makes the lineage forward-tamper-detectable.

Activities, target kinds and agent kinds are mirrored as Python
tuples so the API / MCP layers can enumerate valid values without
re-parsing the CHECK constraint at runtime.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from bvphoenix.db.base import Base
from bvphoenix.db.models._common import uuid_pk

PROVENANCE_TARGET_KINDS: tuple[str, ...] = (
    "patient",
    "clinical_event",
    "imaging_study",
    "series",
    "report_content",
    "document",
    "document_file",
    "marker",
    "tag",
    "external_identifier",
    "content_document_link",
    "report_content_citation",
    # Operational task on the patient timeline (v3.4). Not a clinical
    # event: pharmacy run, paperwork chase, transport booking. Has
    # its own FSM (services/patient_tasks_fsm.py) and lives in
    # patient_tasks. Audit trail target_kind kept distinct so reports
    # can filter clinical vs operational lineage.
    "patient_task",
    # Review-queue stores (migration 0024). The shared staging engine
    # (services/review_queue) records every lifecycle transition against
    # the consumer's own store: ``inbox_item`` for the patient inbound
    # inbox (fbbf5270), ``submission`` for public contributions
    # (133349a9). The engine never owns those tables; it only stamps
    # their lineage here.
    "inbox_item",
    "submission",
)

PROVENANCE_ACTIVITIES: tuple[str, ...] = (
    "create",
    "classify",
    "extract",
    "endorse",
    "sign",
    "reject",
    "supersede",
    "merge",
    "split",
    "cite",
    "link",
    "unlink",
    "redact",
    "delete",
    "restore",
    "identify",
    "update",
    # Clinical event lifecycle (migration 0103). FSM transition
    # sub-resources stamp ``transition.<verb>`` on the parent event
    # so the audit chain distinguishes "the row was patched" from
    # "the lifecycle status moved".
    "transition.confirm",
    "transition.reschedule",
    "transition.complete",
    "transition.cancel",
    "transition.mark_missed",
    "create.rescheduled",
    # Clinical event binary attachments. Distinct from ``link`` /
    # ``unlink`` because those mean "I linked an existing Document";
    # ``attachment.*`` is the lifecycle of an event-scoped upload.
    "attachment.upload",
    "attachment.delete",
    "attachment.promote",
    # Review-queue lifecycle (migration 0024): one ``transition.<to>``
    # per state-machine edge taken, mirroring the clinical-event FSM
    # convention. The target status is the activity suffix; ``from``
    # lives in the diff.
    "transition.processing",
    "transition.needs_review",
    "transition.blocked",
    "transition.accepted",
    "transition.promoting",
    "transition.promoted",
    "transition.rejected",
    "transition.expired",
    "transition.failed",
)

PROVENANCE_AGENT_KINDS: tuple[str, ...] = ("human", "agent", "system")


class ProvenanceEvent(Base):
    __tablename__ = "provenance_events"

    id: Mapped[uuid.UUID] = uuid_pk()
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    target_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    target_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    activity: Mapped[str] = mapped_column(String(32), nullable=False)
    agent_kind: Mapped[str] = mapped_column(String(8), nullable=False)
    agent_subject_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("subjects.id", ondelete="SET NULL"),
    )
    agent_token_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("agent_tokens.id", ondelete="SET NULL"),
    )
    # Modern per-assistant client_secret path leaves ``agent_token_id``
    # NULL because there is no row in ``agent_tokens`` for it; this FK
    # carries the assistant identity so the audit chain stays
    # identifiable for both auth flavours.
    agent_assistant_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("agent_assistants.id", ondelete="SET NULL"),
    )
    source_kind: Mapped[str | None] = mapped_column(String(32))
    source_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    diff: Mapped[dict | None] = mapped_column(JSONB)
    # ``metadata`` is reserved by SQLAlchemy on Declarative classes
    # (it's the registry handle on Base), so the attribute name is
    # ``event_metadata`` and the column keeps the natural ``metadata``
    # name on the DB side.
    event_metadata: Mapped[dict | None] = mapped_column("metadata", JSONB)
    signature_hash: Mapped[str | None] = mapped_column(String(64))
    prev_signature_hash: Mapped[str | None] = mapped_column(String(64))

    __table_args__ = (
        CheckConstraint(
            "target_kind IN (" + ",".join(f"'{k}'" for k in PROVENANCE_TARGET_KINDS) + ")",
            name="ck_provenance_events_target_kind",
        ),
        CheckConstraint(
            "activity IN (" + ",".join(f"'{a}'" for a in PROVENANCE_ACTIVITIES) + ")",
            name="ck_provenance_events_activity",
        ),
        CheckConstraint(
            "agent_kind IN ('human','agent','system')",
            name="ck_provenance_events_agent_kind",
        ),
        CheckConstraint(
            "(agent_kind <> 'agent') "
            "OR (agent_token_id IS NOT NULL) "
            "OR (agent_assistant_id IS NOT NULL)",
            name="ck_provenance_events_agent_identified",
        ),
        CheckConstraint(
            "(agent_kind <> 'human') OR (agent_subject_id IS NOT NULL)",
            name="ck_provenance_events_human_subject_present",
        ),
        Index(
            "ix_provenance_events_target",
            "target_kind",
            "target_id",
            text("recorded_at DESC"),
        ),
        Index(
            "ix_provenance_events_agent_token",
            "agent_token_id",
            text("recorded_at DESC"),
            postgresql_where=text("agent_token_id IS NOT NULL"),
        ),
        Index(
            "ix_provenance_events_agent_subject",
            "agent_subject_id",
            text("recorded_at DESC"),
            postgresql_where=text("agent_subject_id IS NOT NULL"),
        ),
        Index(
            "ix_provenance_events_activity_recent",
            "activity",
            text("recorded_at DESC"),
        ),
    )


__all__ = [
    "PROVENANCE_ACTIVITIES",
    "PROVENANCE_AGENT_KINDS",
    "PROVENANCE_TARGET_KINDS",
    "ProvenanceEvent",
]
