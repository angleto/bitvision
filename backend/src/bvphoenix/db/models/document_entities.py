"""Cache table for the rule-based entity extractor (Sprint 4, ADR 0008).

One row per ``(document_id, extractor_version, content_sha256)``.
Bumping ``extractor_version`` invalidates the cache; the agent /
operator can also force a re-run via the API (``?force=true``).

Stores the full extraction payload as JSONB so reads are
``SELECT entities_jsonb`` away from the API surface — no per-entity
join needed for the hot path.
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


class DocumentEntities(Base):
    __tablename__ = "document_entities"

    id: Mapped[uuid.UUID] = uuid_pk()
    document_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
    )
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    extractor_version: Mapped[str] = mapped_column(String(48), nullable=False)
    entities_jsonb: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "document_id",
            "extractor_version",
            "content_sha256",
            name="uq_document_entities_cache",
        ),
        Index("ix_document_entities_document", "document_id"),
        Index(
            "ix_document_entities_extractor",
            "extractor_version",
            "created_at",
        ),
    )
