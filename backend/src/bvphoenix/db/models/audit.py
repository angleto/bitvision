"""Append-only audit trail. Every non-trivial action lands here so
owners can always answer "who looked at my data, when, under which grant"
(authorization.md §7).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from bvphoenix.db.base import Base
from bvphoenix.db.models._common import uuid_pk


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[uuid.UUID] = uuid_pk()
    actor_subject_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("subjects.id", ondelete="SET NULL"),
    )
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_kind: Mapped[str | None] = mapped_column(String(32))
    resource_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, nullable=False, server_default="{}")
    ip_address: Mapped[str | None] = mapped_column(INET)
    user_agent: Mapped[str | None] = mapped_column(Text)

    # AI / agent provenance (Sprint 1, ADR 0005). NULL for human writes.
    agent_token_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("agent_tokens.id", ondelete="SET NULL"),
    )
    model_version: Mapped[str | None] = mapped_column(String(128))
    conversation_id: Mapped[str | None] = mapped_column(String(128))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("ix_audit_actor_time", "actor_subject_id", "created_at"),
        Index("ix_audit_resource_time", "resource_kind", "resource_id", "created_at"),
        Index("ix_audit_log_agent_token", "agent_token_id", "created_at"),
        Index("ix_audit_log_conversation", "conversation_id"),
    )
