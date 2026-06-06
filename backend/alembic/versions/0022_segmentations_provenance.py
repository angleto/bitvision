"""Segmentation provenance + patient scope + voxel metric.

P1 of the annotation-layer overhaul: promote masks onto the
``Segmentation`` ORM row with full lineage. Adds patient scope, the
acting-principal ``author_kind`` (so an AI mask is distinguishable from a
human one), ``model_id``, and the ``nonzero_voxels`` size metric that the
import path already computed and discarded. The ORM row itself is now
written by the three mask write paths (upload / interactive / auto
worker); this migration only adds the columns.

Revision ID: 0022_segmentations_provenance
Revises: 0021_text_embeddings_finding_target
Create Date: 2026-06-06
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0022_segmentations_provenance"
down_revision = "0021_text_embeddings_finding_target"
branch_labels = None
depends_on = None

_AUTHOR_KINDS = ("human", "agent", "system")


def upgrade() -> None:
    op.add_column(
        "segmentations", sa.Column("patient_id", postgresql.UUID(as_uuid=True), nullable=True)
    )
    op.add_column(
        "segmentations",
        sa.Column("author_kind", sa.String(length=16), server_default="human", nullable=False),
    )
    op.add_column("segmentations", sa.Column("model_id", sa.Text(), nullable=True))
    op.add_column("segmentations", sa.Column("nonzero_voxels", sa.BigInteger(), nullable=True))

    op.create_foreign_key(
        "fk_segmentations_patient",
        "segmentations",
        "patients",
        ["patient_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_check_constraint(
        "ck_segmentations_author_kind",
        "segmentations",
        "author_kind = ANY (ARRAY["
        + ", ".join(f"'{k}'::character varying" for k in _AUTHOR_KINDS)
        + "]::text[])",
    )
    op.create_index("ix_segmentations_patient", "segmentations", ["patient_id"])

    # Backfill (no-op on an empty table today, correct for any legacy rows).
    op.execute(
        "UPDATE segmentations s SET patient_id = st.patient_id "
        "FROM series se JOIN imaging_studies st ON st.id = se.study_id "
        "WHERE se.id = s.series_id AND s.patient_id IS NULL"
    )
    op.execute("UPDATE segmentations SET author_kind = 'agent' WHERE agent_token_id IS NOT NULL")


def downgrade() -> None:
    op.drop_index("ix_segmentations_patient", table_name="segmentations")
    op.drop_constraint("ck_segmentations_author_kind", "segmentations", type_="check")
    op.drop_constraint("fk_segmentations_patient", "segmentations", type_="foreignkey")
    op.drop_column("segmentations", "nonzero_voxels")
    op.drop_column("segmentations", "model_id")
    op.drop_column("segmentations", "author_kind")
    op.drop_column("segmentations", "patient_id")
