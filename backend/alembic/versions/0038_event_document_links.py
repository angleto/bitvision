"""Event ↔ Document reconciliation links.

Turns the placeholder attachment-promotion into a real, bidirectional
link between a ClinicalEvent and a curated drive Document:

* ``clinical_event_attachments`` gains ``content_sha256`` (the
  reconciliation anchor against ``documents.content_sha256``) and a
  partial index over it; the old ``promoted_to_document_id`` 1:1 column
  is dropped — it only ever held synthetic UUIDs pointing at documents
  that were never materialised.
* new ``clinical_event_documents`` n:m link table: an event can
  reference several curated documents and one referto can be relevant
  to several events. ``source_attachment_id`` distinguishes a pure
  "attach from Drive" reference (NULL) from the curated face of a raw
  event upload (set). The composite FK ``(patient_id, event_id)`` keeps
  cross-patient links unrepresentable at the DB level.

No ``provenance_events`` CHECK widening: event↔document links reuse the
existing ``link`` / ``unlink`` activities (reserved by design for
"linked an existing Document"); promotion keeps ``attachment.promote``.

Revision ID: 0038_event_document_links
Revises: 0037_folders_patient_inheritance
Create Date: 2026-06-28
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

revision = "0038_event_document_links"
down_revision = "0037_folders_patient_inheritance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- clinical_event_attachments: add the hash anchor, drop the dud.
    op.add_column(
        "clinical_event_attachments",
        sa.Column("content_sha256", sa.String(64), nullable=True),
    )
    op.create_index(
        "ix_ce_attachments_sha",
        "clinical_event_attachments",
        ["patient_id", "content_sha256"],
        postgresql_where=text("content_sha256 IS NOT NULL AND deleted_at IS NULL"),
    )
    op.drop_column("clinical_event_attachments", "promoted_to_document_id")

    # --- clinical_event_documents: the real n:m link table.
    op.create_table(
        "clinical_event_documents",
        sa.Column(
            "id", PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
        ),
        sa.Column("event_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("patient_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column(
            "document_id",
            PG_UUID(as_uuid=True),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "source_attachment_id",
            PG_UUID(as_uuid=True),
            sa.ForeignKey("clinical_event_attachments.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("link_role", sa.String(16), nullable=False, server_default=text("'reference'")),
        sa.Column("created_by_subject_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("created_by_kind", sa.String(16), nullable=False, server_default=text("'human'")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "created_by_kind IN ('human','agent','system')",
            name="ck_ce_documents_creator_kind",
        ),
        sa.CheckConstraint(
            "link_role IN ('reference','attachment')",
            name="ck_ce_documents_role",
        ),
        sa.ForeignKeyConstraint(
            ["patient_id", "event_id"],
            ["clinical_events.patient_id", "clinical_events.id"],
            name="fk_ce_documents_event",
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "uq_ce_documents_event_doc_live",
        "clinical_event_documents",
        ["event_id", "document_id"],
        unique=True,
        postgresql_where=text("deleted_at IS NULL"),
    )
    op.create_index(
        "ix_ce_documents_event",
        "clinical_event_documents",
        ["event_id"],
        postgresql_where=text("deleted_at IS NULL"),
    )
    op.create_index(
        "ix_ce_documents_document",
        "clinical_event_documents",
        ["document_id"],
        postgresql_where=text("deleted_at IS NULL"),
    )
    op.create_index(
        "ix_ce_documents_patient",
        "clinical_event_documents",
        ["patient_id"],
        postgresql_where=text("deleted_at IS NULL"),
    )
    op.create_index(
        "ix_ce_documents_source_attachment",
        "clinical_event_documents",
        ["source_attachment_id"],
        postgresql_where=text("source_attachment_id IS NOT NULL AND deleted_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_ce_documents_source_attachment", table_name="clinical_event_documents")
    op.drop_index("ix_ce_documents_patient", table_name="clinical_event_documents")
    op.drop_index("ix_ce_documents_document", table_name="clinical_event_documents")
    op.drop_index("ix_ce_documents_event", table_name="clinical_event_documents")
    op.drop_index("uq_ce_documents_event_doc_live", table_name="clinical_event_documents")
    op.drop_table("clinical_event_documents")

    op.add_column(
        "clinical_event_attachments",
        sa.Column("promoted_to_document_id", PG_UUID(as_uuid=True), nullable=True),
    )
    op.drop_index("ix_ce_attachments_sha", table_name="clinical_event_attachments")
    op.drop_column("clinical_event_attachments", "content_sha256")
