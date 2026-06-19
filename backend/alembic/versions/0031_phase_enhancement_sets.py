"""PhaseEnhancementSet — persisted cross-phase wash-out measurements.

One ROI sampled across the contrast phases of a study, with per-phase HU
and the derived APW/RPW indices, persisted as an auditable, provenance-
stamped, soft-deletable measurement (no separate revision table: a
wash-out is a recomputable snapshot, not an editable assessment).

Revision ID: 0031_phase_enhancement_sets
Revises: 0030_series_acquisition_phase
Create Date: 2026-06-18
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0031_phase_enhancement_sets"
down_revision = "0030_series_acquisition_phase"
branch_labels = None
depends_on = None

_ROI_KINDS = ("sphere", "bbox")
_AUTHOR_KINDS = ("human", "agent", "system")


def upgrade() -> None:
    op.create_table(
        "phase_enhancement_sets",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "patient_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("patients.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "study_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("imaging_studies.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("label", sa.Text()),
        sa.Column("roi_kind", sa.String(8), nullable=False),
        sa.Column("roi", postgresql.JSONB(), nullable=False),
        sa.Column("samples", postgresql.JSONB()),
        sa.Column("washout", postgresql.JSONB()),
        sa.Column("apw", sa.Float()),
        sa.Column("rpw", sa.Float()),
        sa.Column("enhanced_phase", sa.String(24)),
        sa.Column("delayed_phase", sa.String(24)),
        sa.Column(
            "author_subject_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("subjects.id", ondelete="SET NULL"),
        ),
        sa.Column("author_kind", sa.String(16), nullable=False, server_default="human"),
        sa.Column("model_id", sa.Text()),
        sa.Column("provider", sa.Text()),
        sa.Column(
            "agent_token_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent_tokens.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "etag",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.Column("purge_after", sa.DateTime(timezone=True)),
        sa.Column("delete_reason", sa.String(255)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "roi_kind IN (" + ",".join(f"'{k}'" for k in _ROI_KINDS) + ")",
            name="ck_phase_enh_roi_kind",
        ),
        sa.CheckConstraint(
            "author_kind IN (" + ",".join(f"'{a}'" for a in _AUTHOR_KINDS) + ")",
            name="ck_phase_enh_author_kind",
        ),
    )
    op.create_index("ix_phase_enh_study", "phase_enhancement_sets", ["study_id"])
    op.create_index("ix_phase_enh_patient", "phase_enhancement_sets", ["patient_id"])


def downgrade() -> None:
    op.drop_index("ix_phase_enh_patient", table_name="phase_enhancement_sets")
    op.drop_index("ix_phase_enh_study", table_name="phase_enhancement_sets")
    op.drop_table("phase_enhancement_sets")
