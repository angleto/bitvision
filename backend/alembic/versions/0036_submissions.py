"""Public-contribution submissions table (review-queue consumer).

The public-egress sibling of ``inbox_items`` (0025): one ``submissions`` row per
study offered to the OpenData library, driven through the ``public_contribution``
review profile (human-only accept). Reuses the shared ``review_status`` enum +
``ReviewableItemMixin`` columns created in 0024. ``submission`` is already an
allowed ``provenance_events.target_kind`` (no CHECK widening needed).

Revision ID: 0036_submissions
Revises: 0035_deid_stamp
Create Date: 2026-06-19
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import ENUM as PG_ENUM
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

revision = "0036_submissions"
down_revision = "0035_deid_stamp"
branch_labels = None
depends_on = None

_TARGET_TIERS = ("t3", "t4")


def _review_status() -> PG_ENUM:
    # Reference the existing shared enum; never create it here (0024 owns it).
    return PG_ENUM(name="review_status", create_type=False)


def upgrade() -> None:
    op.create_table(
        "submissions",
        sa.Column(
            "id", PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
        ),
        sa.Column(
            "source_study_id",
            PG_UUID(as_uuid=True),
            sa.ForeignKey("imaging_studies.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "source_patient_id",
            PG_UUID(as_uuid=True),
            sa.ForeignKey("patients.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "contributor_subject_id",
            PG_UUID(as_uuid=True),
            sa.ForeignKey("subjects.id", ondelete="SET NULL"),
        ),
        sa.Column("target_tier", sa.String(8), nullable=False),
        sa.Column(
            "public_patient_id",
            PG_UUID(as_uuid=True),
            sa.ForeignKey("patients.id", ondelete="SET NULL"),
        ),
        sa.Column("manifest", JSONB),
        sa.Column("staged_prefix", sa.String(1024)),
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
        sa.CheckConstraint(
            "target_tier IN (" + ",".join(f"'{t}'" for t in _TARGET_TIERS) + ")",
            name="ck_submissions_target_tier",
        ),
    )
    op.create_index("ix_submissions_status_created", "submissions", ["status", "created_at"])
    op.create_index("ix_submissions_status_updated", "submissions", ["status", "updated_at"])
    op.create_index("ix_submissions_contributor", "submissions", ["contributor_subject_id"])


def downgrade() -> None:
    op.drop_index("ix_submissions_contributor", table_name="submissions")
    op.drop_index("ix_submissions_status_updated", table_name="submissions")
    op.drop_index("ix_submissions_status_created", table_name="submissions")
    op.drop_table("submissions")
