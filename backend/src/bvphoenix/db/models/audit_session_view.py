"""Aggregated read-side audit log (ADR 0005).

A single agent triage session can fan out into 50-200 GETs across
fascicoli, slices, documents and labs. Inserting one row per GET into
``audit_log`` is prohibitively expensive at scale and provides little
forensic value: the question we actually need to answer is
"who looked at patient X between T0 and T1?", not "which exact slice".

This table aggregates reads on a per-(actor, patient, agent) basis with
a 15 minute idle window: subsequent reads inside the window bump
``last_event_at`` and ``read_count``; idle longer and a new session row
is opened. The window value is configurable via
``BVP_AUDIT_SESSION_WINDOW_MINUTES`` (default 15).

Granular write events still go to ``audit_log`` via the
``@audit_write`` decorator — the two streams are unioned by
``GET /api/patients/:pid/audit``.

Conservation policy: 10 years (matches ``audit_log``).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from bvphoenix.db.base import Base
from bvphoenix.db.models._common import uuid_pk


class AuditSessionView(Base):
    """One row per (subject, patient, agent_token, ip) idle window.

    Schema notes:

    * ``actor_subject_id`` is the *human* subject in whose name the
      reads happened (the owner of the agent token, or the user
      directly).
    * ``agent_token_id`` distinguishes runs that came through the same
      human's MCP/agent flow from those done by the human in person.
      ``None`` means a human session.
    * ``patient_id`` is ``None`` for global reads (admin endpoints,
      transparency stats) — keep them aggregated globally so the
      read-volume policy still bounds them.
    * ``conversation_id`` ties the session to an LLM thread when the
      caller emits the header (``X-Conversation-Id``) — useful for
      "show me everything this conversation looked at".
    """

    __tablename__ = "audit_session_view"

    id: Mapped[uuid.UUID] = uuid_pk()

    actor_subject_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("subjects.id", ondelete="SET NULL"),
    )
    patient_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("patients.id", ondelete="CASCADE"),
    )
    agent_token_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("agent_tokens.id", ondelete="SET NULL"),
    )
    conversation_id: Mapped[str | None] = mapped_column(String(128))

    first_event_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_event_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    read_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    resource_kinds_touched: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")

    ip_address: Mapped[str | None] = mapped_column(INET)
    user_agent: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        Index(
            "ix_audit_session_actor_patient_last",
            "actor_subject_id",
            "patient_id",
            "last_event_at",
        ),
        Index(
            "ix_audit_session_patient_last",
            "patient_id",
            "last_event_at",
        ),
        Index(
            "ix_audit_session_conversation",
            "conversation_id",
        ),
    )
