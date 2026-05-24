"""Append-only audit + idempotency-replay table for clinical_events transitions.

Each call to a transition sub-resource (``/confirm``, ``/reschedule``,
``/complete``, ``/cancel``, ``/mark-missed``) appends one row here
before the underlying ``clinical_events`` row is mutated. Two use cases:

1. **Idempotency-Key replay**: same key on the same ``(event_id,
   action)`` returns the previously-persisted ``snapshot_after``
   without re-running the transition. Matches the contract the MCP
   write tools advertise — mandatory ``idempotency_key`` argument,
   second call is a no-op echo of the first response.

2. **Inline audit**: ``snapshot_before`` / ``snapshot_after`` JSONB
   captures the row state at the boundary. The full chain still lives
   in ``provenance_events``; this table is the fast path for
   ``Undo`` on the toast (read snapshot_before, PATCH it back).

The ``UNIQUE(event_id, idempotency_key)`` deliberately scopes the
key to the event: an agent that uses the same key string on a
different event is doing a different operation and will get a fresh
row (not a collision). Cross-patient is moot because the FK
``event_id -> clinical_events.id`` already constrains the patient
through the parent row.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from bvphoenix.db.base import Base
from bvphoenix.db.models._common import uuid_pk

# Verbs persisted by the API layer. Mirrors the sub-resource names
# (without the leading slash) so the audit chain is grep-friendly.
TRANSITION_ACTIONS: tuple[str, ...] = (
    "confirm",
    "reschedule",
    "complete",
    "cancel",
    "mark_missed",
)


class ClinicalEventTransition(Base):
    __tablename__ = "clinical_event_transitions"

    id: Mapped[uuid.UUID] = uuid_pk()
    event_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("clinical_events.id", ondelete="CASCADE"),
        nullable=False,
    )
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    # Whole-row snapshots, JSONB. We persist them once at write time
    # instead of doing JOINs against provenance_events later. Useful
    # for Undo and for the dry_run preview that wants to show the
    # would-be-after state.
    snapshot_before: Mapped[dict] = mapped_column(JSONB, nullable=False)
    snapshot_after: Mapped[dict] = mapped_column(JSONB, nullable=False)
    actor_subject_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    author_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        # Idempotency replay key. Same key on same (event, action) is
        # a replay; same key on a different event or action is a
        # fresh write (intentional — keys are agent-scoped, not
        # global).
        UniqueConstraint(
            "event_id",
            "action",
            "idempotency_key",
            name="uq_ce_transitions_idempotency",
        ),
        Index("ix_ce_transitions_event", "event_id"),
        Index("ix_ce_transitions_event_created", "event_id", "created_at"),
    )


__all__ = ["TRANSITION_ACTIONS", "ClinicalEventTransition"]
