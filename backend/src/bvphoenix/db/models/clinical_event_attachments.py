"""Binary attachments on a ClinicalEvent.

Materially different from ``links``: those are URL references to
external services (Drive, Dropbox, booking portal). Attachments here
carry the actual bytes in object storage, scoped to the event +
patient.

The composite FK ``(patient_id, event_id)`` matches the parent
clinical_events tuple and makes cross-patient attachment rejection
a DB-level guarantee. Storage key layout::

    clinical_event_attachments/{patient_id}/{event_id}/{att_id}/{filename}

under ``settings.s3_bucket_raw``.

When a user wants to keep an attachment beyond the event lifecycle,
the ``promote-to-document`` action reconciles it against the patient
drive: it matches the bytes against an existing Document by
``content_sha256`` (and creates one via the canonical ingest path when
there is no match), then records a :class:`ClinicalEventDocument` link.
The raw attachment row stays around as the downloadable source;
``deleted_at`` is set when the user removes it from the event surface.

``content_sha256`` is the reconciliation anchor: identical bytes
already curated in the drive are detected without a second copy. It is
``NULL`` on pre-0038 rows until the backfill streams them from S3.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    String,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from bvphoenix.db.base import Base


class ClinicalEventAttachment(Base):
    __tablename__ = "clinical_event_attachments"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    event_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    patient_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    mime: Mapped[str] = mapped_column(String(128), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False)
    uploaded_by_subject_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    uploaded_by_kind: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'human'")
    )
    # SHA-256 of the uploaded bytes — the reconciliation anchor against
    # the drive ``documents.content_sha256`` index. Computed at upload;
    # NULL on pre-0038 rows until the backfill streams them from S3.
    content_sha256: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "uploaded_by_kind IN ('human','agent','system')",
            name="ck_ce_attachments_uploader_kind",
        ),
        CheckConstraint("size_bytes >= 0", name="ck_ce_attachments_size_nonneg"),
        ForeignKeyConstraint(
            ["patient_id", "event_id"],
            ["clinical_events.patient_id", "clinical_events.id"],
            name="fk_ce_attachments_event",
            ondelete="CASCADE",
        ),
        Index(
            "ix_ce_attachments_event",
            "event_id",
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "ix_ce_attachments_patient",
            "patient_id",
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "ix_ce_attachments_sha",
            "patient_id",
            "content_sha256",
            postgresql_where=text("content_sha256 IS NOT NULL AND deleted_at IS NULL"),
        ),
    )


__all__ = ["ClinicalEventAttachment"]
