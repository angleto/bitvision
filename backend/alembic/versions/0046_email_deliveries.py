"""Durable transport ledger for every outbound email.

Outbound mail had no persistence outside the patient-scoped
``notification_dispatches`` table, which cannot represent a share
invitation, a password reset or a verification link (``patient_id`` and
``contact_id`` are both NOT NULL). When the SMTP port was blackholed on
2026-07-31 the failed messages left no queryable trace and could not be
replayed once the transport was fixed.

``email_deliveries`` records one row per message handed to the relay,
with a discriminated ``error_code`` and an exponential-backoff
``next_attempt_at`` so a transport outage is survivable rather than
lossy. The MIME body is never stored: messages are rebuilt from
``purpose`` plus the FK anchors, so the ledger does not become a second
PHI store.

Also widens ``notification_dispatches`` with ``error_detail``. That
column already exists on ``NotificationResult`` (services/notifications/
base.py) and is populated in five places by the webhook notifier, but
nothing persisted it, so every failure collapsed to an opaque code.

Purely additive.

Revision ID: 0046_email_deliveries
Revises: 0045_deid_recall_runs
Create Date: 2026-07-31
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

revision = "0046_email_deliveries"
down_revision = "0045_deid_recall_runs"
branch_labels = None
depends_on = None

_PURPOSES = ("share_invitation", "password_reset", "email_verification", "notification", "probe")
_STATUSES = ("queued", "sent", "failed", "dead_letter")


def upgrade() -> None:
    op.create_table(
        "email_deliveries",
        sa.Column(
            "id",
            PG_UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("purpose", sa.String(length=32), nullable=False),
        sa.Column("recipient_email", sa.String(length=255), nullable=False),
        sa.Column("subject_line", sa.String(length=255)),
        sa.Column("locale", sa.String(length=8), nullable=False, server_default=sa.text("'it'")),
        # SET NULL, not CASCADE: deleting a share link must not erase
        # the evidence that we tried to email someone about it.
        sa.Column(
            "share_link_id",
            PG_UUID(as_uuid=True),
            sa.ForeignKey("share_links.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "subject_id",
            PG_UUID(as_uuid=True),
            sa.ForeignKey("subjects.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "notification_dispatch_id",
            PG_UUID(as_uuid=True),
            sa.ForeignKey("notification_dispatches.id", ondelete="CASCADE"),
        ),
        sa.Column("status", sa.String(length=16), nullable=False, server_default=sa.text("'queued'")),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default=sa.text("6")),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("error_code", sa.String(length=64)),
        sa.Column("error_detail", sa.Text()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "purpose IN (" + ",".join(f"'{p}'" for p in _PURPOSES) + ")",
            name="ck_email_deliveries_purpose",
        ),
        sa.CheckConstraint(
            "status IN (" + ",".join(f"'{s}'" for s in _STATUSES) + ")",
            name="ck_email_deliveries_status",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0 AND max_attempts > 0",
            name="ck_email_deliveries_attempts_sane",
        ),
    )
    # Partial: the drain query only ever looks at queued rows, and the
    # table is dominated by 'sent' ones that must not bloat the index.
    op.create_index(
        "ix_email_deliveries_due",
        "email_deliveries",
        ["next_attempt_at"],
        postgresql_where=sa.text("status = 'queued'"),
    )
    op.create_index("ix_email_deliveries_recipient", "email_deliveries", ["recipient_email"])
    op.create_index("ix_email_deliveries_share_link", "email_deliveries", ["share_link_id"])

    op.add_column(
        "notification_dispatches",
        sa.Column("error_detail", sa.Text()),
    )


def downgrade() -> None:
    op.drop_column("notification_dispatches", "error_detail")
    op.drop_index("ix_email_deliveries_share_link", table_name="email_deliveries")
    op.drop_index("ix_email_deliveries_recipient", table_name="email_deliveries")
    op.drop_index("ix_email_deliveries_due", table_name="email_deliveries")
    op.drop_table("email_deliveries")
