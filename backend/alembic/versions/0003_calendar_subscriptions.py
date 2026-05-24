"""Public iCal subscription handles (revocable, HMAC-signed feed).

One row == one shareable, non-expiring calendar feed URL for a single
patient. The URL token is a stateless HMAC over (id, patient_id); this
table is the revocation + usage-audit anchor. See
``services.calendar_subscription_token`` and
``db.models.calendar_subscriptions``.

Revision ID: 0003_calendar_subscriptions
Revises: 0002_markers_kind_bbox_exclusion
Create Date: 2026-05-18
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

revision = "0003_calendar_subscriptions"
down_revision = "0002_markers_kind_bbox_exclusion"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "calendar_subscriptions",
        sa.Column(
            "id",
            PG_UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "patient_id",
            PG_UUID(as_uuid=True),
            sa.ForeignKey("patients.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("label", sa.String(255)),
        sa.Column(
            "author_kind",
            sa.String(16),
            nullable=False,
            server_default=sa.text("'human'"),
        ),
        sa.Column("created_by_subject_id", PG_UUID(as_uuid=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_by_subject_id", PG_UUID(as_uuid=True)),
        sa.Column("last_accessed_at", sa.DateTime(timezone=True)),
        sa.Column("access_count", sa.Integer, nullable=False, server_default="0"),
    )
    op.create_index(
        "ix_calendar_subscriptions_patient_id",
        "calendar_subscriptions",
        ["patient_id"],
    )
    op.create_index(
        "ix_calendar_subscriptions_patient_active",
        "calendar_subscriptions",
        ["patient_id"],
        postgresql_where=sa.text("revoked_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_calendar_subscriptions_patient_active",
        table_name="calendar_subscriptions",
    )
    op.drop_index(
        "ix_calendar_subscriptions_patient_id",
        table_name="calendar_subscriptions",
    )
    op.drop_table("calendar_subscriptions")
