"""Link rows binding a ClinicalEvent to a curated drive Document.

An event can reference a patient :class:`Document` two ways:

* **attach from Drive** — the user (or an agent) picks an already
  curated document; ``source_attachment_id`` is ``NULL`` and the row is
  a pure reference, no new bytes change hands.
* **reconcile / promote a raw attachment** — a file uploaded straight
  to the event (:class:`ClinicalEventAttachment`) is matched against an
  existing drive document by content hash, or materialised into a new
  one; the link carries ``source_attachment_id`` so the raw upload
  stays traceable to its curated face.

Replaces the placeholder ``clinical_event_attachments.promoted_to_document_id``
1:1 column (dropped in 0038). One curated referto can be relevant to
several events and an event can reference several documents, so the
relationship is genuinely n:m. The composite FK ``(patient_id,
event_id)`` keeps cross-patient linking inexpressible at the DB level,
mirroring the attachment table and the project-wide invariant that a
cross-patient association must be *unrepresentable*, not merely
rejected.
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
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from bvphoenix.db.base import Base


class ClinicalEventDocument(Base):
    __tablename__ = "clinical_event_documents"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    event_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    patient_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    document_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
    )
    # ``NULL`` ⇒ attached directly from the Drive (pure reference).
    # Set     ⇒ reconciled / promoted from this raw event attachment.
    # ON DELETE SET NULL: hard-purging the raw attachment downgrades the
    # link to a plain reference rather than destroying the curated link.
    source_attachment_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("clinical_event_attachments.id", ondelete="SET NULL"),
    )
    # ``reference`` = picked from the Drive; ``attachment`` = the curated
    # face of a raw event upload. Kept distinct so the UI can decide
    # whether the row also has a downloadable raw blob behind it.
    link_role: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'reference'")
    )
    created_by_subject_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    created_by_kind: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'human'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "created_by_kind IN ('human','agent','system')",
            name="ck_ce_documents_creator_kind",
        ),
        CheckConstraint(
            "link_role IN ('reference','attachment')",
            name="ck_ce_documents_role",
        ),
        ForeignKeyConstraint(
            ["patient_id", "event_id"],
            ["clinical_events.patient_id", "clinical_events.id"],
            name="fk_ce_documents_event",
            ondelete="CASCADE",
        ),
        # One live link per (event, document) pair; partial over live
        # rows so a previously unlinked pair can be re-linked.
        Index(
            "uq_ce_documents_event_doc_live",
            "event_id",
            "document_id",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "ix_ce_documents_event",
            "event_id",
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "ix_ce_documents_document",
            "document_id",
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "ix_ce_documents_patient",
            "patient_id",
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "ix_ce_documents_source_attachment",
            "source_attachment_id",
            postgresql_where=text("source_attachment_id IS NOT NULL AND deleted_at IS NULL"),
        ),
    )


__all__ = ["ClinicalEventDocument"]
