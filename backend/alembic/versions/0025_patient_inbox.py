"""Patient inbound inbox: addresses, raw emails, reviewable items, allowlist.

First consumer of the shared review-queue engine (migration 0024 created
the ``review_status`` enum + provenance CHECK widening for target kind
``inbox_item``). This migration ships the patient-private store
(task fbbf5270):

* ``patient_inbox_addresses`` — rotatable capability addresses
  (``{code}+patient@<inbox domain>``);
* ``inbound_emails``        — accepted SMTP messages (raw on S3);
* ``inbox_items``           — the reviewable lots (ReviewableItemMixin
  columns + ingress reference, XOR email/upload-job);
* ``inbox_sender_allowlist`` — opt-in auto-accept senders.

Cross-patient inexpressibility: ``UNIQUE(patient_id, id)`` on every
table + composite FKs on every intra-inbox edge (the DB rejects a
cross-patient reference, services never get the chance to).

Also widens the ``provenance_events`` CHECKs again (same ORM-anchored
pattern as 0024) for the new ``inbox_address`` target kind and the
``revoke`` activity.

Revision ID: 0025_patient_inbox
Revises: 0024_review_queue_shared
Create Date: 2026-06-12
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import ENUM as PG_ENUM
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

from bvphoenix.db.models.inbox import INBOX_SOURCE_CHANNELS
from bvphoenix.db.models.provenance_events import (
    PROVENANCE_ACTIVITIES,
    PROVENANCE_TARGET_KINDS,
)

revision = "0025_patient_inbox"
down_revision = "0024_review_queue_shared"
branch_labels = None
depends_on = None


def _in_list(values: tuple[str, ...]) -> str:
    return ",".join(f"'{v}'" for v in values)


def _review_status() -> PG_ENUM:
    # Created by 0024; never create/drop it here.
    return PG_ENUM(name="review_status", create_type=False)


def upgrade() -> None:
    op.create_table(
        "patient_inbox_addresses",
        sa.Column(
            "id", PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
        ),
        sa.Column(
            "patient_id",
            PG_UUID(as_uuid=True),
            sa.ForeignKey("patients.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("code", sa.String(32), nullable=False, unique=True),
        sa.Column("tag", sa.String(32), nullable=False, server_default="patient"),
        sa.Column("label", sa.String(200)),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=text("true")),
        sa.Column(
            "created_by_subject_id",
            PG_UUID(as_uuid=True),
            sa.ForeignKey("subjects.id", ondelete="SET NULL"),
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("revoke_reason", sa.Text()),
        sa.Column(
            "etag",
            PG_UUID(as_uuid=True),
            nullable=False,
            server_default=text("gen_random_uuid()"),
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("patient_id", "id", name="uq_patient_inbox_addresses_patient_id"),
        sa.CheckConstraint(
            "(active AND revoked_at IS NULL) OR (NOT active AND revoked_at IS NOT NULL)",
            name="ck_patient_inbox_addresses_revoked",
        ),
    )
    op.create_index(
        "ix_patient_inbox_addresses_patient", "patient_inbox_addresses", ["patient_id"]
    )

    op.create_table(
        "inbound_emails",
        sa.Column(
            "id", PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
        ),
        sa.Column(
            "patient_id",
            PG_UUID(as_uuid=True),
            sa.ForeignKey("patients.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("inbox_address_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("message_id", sa.String(998)),
        sa.Column("from_address", sa.String(320)),
        sa.Column("to_address", sa.String(320)),
        sa.Column("subject", sa.Text()),
        sa.Column("received_at", sa.DateTime(timezone=True)),
        sa.Column("spf_result", sa.String(16)),
        sa.Column("dkim_result", sa.String(16)),
        sa.Column("dmarc_result", sa.String(16)),
        sa.Column("raw_s3_key", sa.String(1024), nullable=False),
        sa.Column("raw_sha256", sa.String(64), nullable=False),
        sa.Column("raw_size_bytes", sa.Integer()),
        sa.Column(
            "etag",
            PG_UUID(as_uuid=True),
            nullable=False,
            server_default=text("gen_random_uuid()"),
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("patient_id", "id", name="uq_inbound_emails_patient_id"),
        sa.ForeignKeyConstraint(
            ["patient_id", "inbox_address_id"],
            ["patient_inbox_addresses.patient_id", "patient_inbox_addresses.id"],
            name="fk_inbound_emails_address",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("inbox_address_id", "message_id", name="uq_inbound_emails_message_id"),
    )
    op.create_index("ix_inbound_emails_patient", "inbound_emails", ["patient_id"])
    op.create_index("ix_inbound_emails_address", "inbound_emails", ["inbox_address_id"])

    op.create_table(
        "inbox_items",
        sa.Column(
            "id", PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
        ),
        sa.Column(
            "patient_id",
            PG_UUID(as_uuid=True),
            sa.ForeignKey("patients.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("inbound_email_id", PG_UUID(as_uuid=True)),
        sa.Column(
            "upload_job_id",
            PG_UUID(as_uuid=True),
            sa.ForeignKey("jobs.id", ondelete="SET NULL"),
        ),
        sa.Column("source_channel", sa.String(16), nullable=False),
        sa.Column("staged_prefix", sa.String(1024)),
        sa.Column("manifest", JSONB),
        sa.Column("promoted_refs", JSONB),
        # ReviewableItemMixin columns (shared engine, migration 0024).
        sa.Column(
            "status",
            _review_status(),
            nullable=False,
            server_default=text("'received'::review_status"),
        ),
        sa.Column("auto_checks", JSONB),
        sa.Column("auto_verdict", sa.String(8)),
        sa.Column("reviewed_at", sa.DateTime(timezone=True)),
        sa.Column("review_note", sa.Text()),
        sa.Column(
            "reviewed_by_subject_id",
            PG_UUID(as_uuid=True),
            sa.ForeignKey("subjects.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "etag",
            PG_UUID(as_uuid=True),
            nullable=False,
            server_default=text("gen_random_uuid()"),
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("patient_id", "id", name="uq_inbox_items_patient_id"),
        sa.ForeignKeyConstraint(
            ["patient_id", "inbound_email_id"],
            ["inbound_emails.patient_id", "inbound_emails.id"],
            name="fk_inbox_items_inbound_email",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "(inbound_email_id IS NOT NULL)::int + (upload_job_id IS NOT NULL)::int = 1",
            name="ck_inbox_items_single_source",
        ),
        sa.CheckConstraint(
            "source_channel IN (" + _in_list(INBOX_SOURCE_CHANNELS) + ")",
            name="ck_inbox_items_source_channel",
        ),
        sa.CheckConstraint(
            "source_channel <> 'email' OR staged_prefix IS NOT NULL",
            name="ck_inbox_items_email_staged",
        ),
    )
    op.create_index("ix_inbox_items_patient", "inbox_items", ["patient_id"])
    op.create_index(
        "ix_inbox_items_patient_status", "inbox_items", ["patient_id", "status", "created_at"]
    )
    op.create_index("ix_inbox_items_status_updated", "inbox_items", ["status", "updated_at"])

    op.create_table(
        "inbox_sender_allowlist",
        sa.Column(
            "id", PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
        ),
        sa.Column(
            "patient_id",
            PG_UUID(as_uuid=True),
            sa.ForeignKey("patients.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("sender_email", sa.String(320), nullable=False),
        sa.Column("require_alignment", sa.Boolean(), nullable=False, server_default=text("true")),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=text("true")),
        sa.Column(
            "created_by_subject_id",
            PG_UUID(as_uuid=True),
            sa.ForeignKey("subjects.id", ondelete="SET NULL"),
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column(
            "etag",
            PG_UUID(as_uuid=True),
            nullable=False,
            server_default=text("gen_random_uuid()"),
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("patient_id", "id", name="uq_inbox_sender_allowlist_patient_id"),
    )
    op.create_index(
        "uq_inbox_sender_allowlist_active",
        "inbox_sender_allowlist",
        ["patient_id", "sender_email"],
        unique=True,
        postgresql_where=text("active"),
    )
    op.create_index("ix_inbox_sender_allowlist_patient", "inbox_sender_allowlist", ["patient_id"])

    # Provenance CHECK widening (ORM-anchored, the 0024 pattern): adds
    # target kind ``inbox_address`` + activity ``revoke``.
    conn = op.get_bind()
    conn.execute(
        text("ALTER TABLE provenance_events DROP CONSTRAINT ck_provenance_events_target_kind")
    )
    conn.execute(
        text(
            "ALTER TABLE provenance_events ADD CONSTRAINT ck_provenance_events_target_kind "
            f"CHECK (target_kind IN ({_in_list(PROVENANCE_TARGET_KINDS)}))"
        )
    )
    conn.execute(
        text("ALTER TABLE provenance_events DROP CONSTRAINT ck_provenance_events_activity")
    )
    conn.execute(
        text(
            "ALTER TABLE provenance_events ADD CONSTRAINT ck_provenance_events_activity "
            f"CHECK (activity IN ({_in_list(PROVENANCE_ACTIVITIES)}))"
        )
    )


def downgrade() -> None:
    conn = op.get_bind()
    prev_targets = tuple(k for k in PROVENANCE_TARGET_KINDS if k != "inbox_address")
    prev_activities = tuple(a for a in PROVENANCE_ACTIVITIES if a != "revoke")
    conn.execute(
        text("ALTER TABLE provenance_events DROP CONSTRAINT ck_provenance_events_activity")
    )
    conn.execute(
        text(
            "ALTER TABLE provenance_events ADD CONSTRAINT ck_provenance_events_activity "
            f"CHECK (activity IN ({_in_list(prev_activities)}))"
        )
    )
    conn.execute(
        text("ALTER TABLE provenance_events DROP CONSTRAINT ck_provenance_events_target_kind")
    )
    conn.execute(
        text(
            "ALTER TABLE provenance_events ADD CONSTRAINT ck_provenance_events_target_kind "
            f"CHECK (target_kind IN ({_in_list(prev_targets)}))"
        )
    )

    op.drop_table("inbox_sender_allowlist")
    op.drop_table("inbox_items")
    op.drop_table("inbound_emails")
    op.drop_table("patient_inbox_addresses")
