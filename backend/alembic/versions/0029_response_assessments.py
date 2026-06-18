"""Response assessments — patient-level RECIST / volumetric response.

Roll-up above lesion tracks: aggregate the patient's target lesions into a
sum of diameters / total volume and classify the response (CR/PR/SD/PD)
against baseline and nadir. A derived but persisted, versioned, auditable
clinical record.

Revision ID: 0029_response_assessments
Revises: 0028_decouple_dataset_from_license
Create Date: 2026-06-18
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0029_response_assessments"
down_revision = "0028_decouple_dataset_from_license"
branch_labels = None
depends_on = None

_CRITERIA = ("recist_1_1", "volumetric", "percist")
_CATEGORIES = ("CR", "PR", "SD", "PD", "NE")
_NON_TARGET = ("CR", "non_CR_non_PD", "PD", "NE")
_AUTHOR_KINDS = ("human", "agent", "system")
_REVISION_CHANGE_KINDS = ("create", "update", "recompute", "delete", "restore")


def _in(col: str, values: tuple[str, ...]) -> str:
    return col + " IN (" + ",".join(f"'{v}'" for v in values) + ")"


def upgrade() -> None:
    op.create_table(
        "response_assessments",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("patient_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("assessment_date", sa.Date(), nullable=True),
        sa.Column("baseline_study_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("current_study_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("criterion", sa.String(length=16), server_default="recist_1_1", nullable=False),
        sa.Column("target_sum_mm", sa.Float(), nullable=True),
        sa.Column("baseline_sum_mm", sa.Float(), nullable=True),
        sa.Column("nadir_sum_mm", sa.Float(), nullable=True),
        sa.Column("target_sum_pct_change", sa.Float(), nullable=True),
        sa.Column("volume_total_ml", sa.Float(), nullable=True),
        sa.Column("volume_pct_change", sa.Float(), nullable=True),
        sa.Column("category", sa.String(length=4), nullable=False),
        sa.Column("new_lesions", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("non_target_status", sa.String(length=16), nullable=True),
        sa.Column("basis", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("author_subject_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("author_kind", sa.String(length=16), server_default="human", nullable=False),
        sa.Column("model_id", sa.Text(), nullable=True),
        sa.Column("provider", sa.Text(), nullable=True),
        sa.Column("agent_token_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "etag",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("purge_after", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delete_reason", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_response_assessments"),
        sa.ForeignKeyConstraint(
            ["patient_id"],
            ["patients.id"],
            name="fk_response_assessments_patient",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["baseline_study_id"],
            ["imaging_studies.id"],
            name="fk_response_assessments_baseline",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["current_study_id"],
            ["imaging_studies.id"],
            name="fk_response_assessments_current",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["author_subject_id"],
            ["subjects.id"],
            name="fk_response_assessments_author",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["agent_token_id"],
            ["agent_tokens.id"],
            name="fk_response_assessments_agent_token",
            ondelete="SET NULL",
        ),
        sa.CheckConstraint(_in("criterion", _CRITERIA), name="ck_response_assessments_criterion"),
        sa.CheckConstraint(_in("category", _CATEGORIES), name="ck_response_assessments_category"),
        sa.CheckConstraint(
            "non_target_status IS NULL OR " + _in("non_target_status", _NON_TARGET),
            name="ck_response_assessments_non_target",
        ),
        sa.CheckConstraint(
            _in("author_kind", _AUTHOR_KINDS), name="ck_response_assessments_author_kind"
        ),
    )
    op.create_index("ix_response_assessments_patient", "response_assessments", ["patient_id"])
    op.create_index(
        "ix_response_assessments_live",
        "response_assessments",
        ["patient_id", "current_study_id"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )

    op.create_table(
        "response_assessment_revision",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("response_assessment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("patient_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("revision_no", sa.Integer(), nullable=False),
        sa.Column("snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("change_kind", sa.String(length=16), nullable=False),
        sa.Column("author_kind", sa.String(length=16), nullable=False),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("diff_summary", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_response_assessment_revision"),
        sa.ForeignKeyConstraint(
            ["response_assessment_id"],
            ["response_assessments.id"],
            name="fk_response_assessment_revision_ra",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            _in("change_kind", _REVISION_CHANGE_KINDS),
            name="ck_response_assessment_revision_kind",
        ),
        sa.CheckConstraint(
            _in("author_kind", _AUTHOR_KINDS), name="ck_response_assessment_revision_author_kind"
        ),
        sa.CheckConstraint("revision_no >= 1", name="ck_response_assessment_revision_no_pos"),
        sa.UniqueConstraint(
            "response_assessment_id",
            "revision_no",
            name="uq_response_assessment_revision_ra_no",
        ),
    )
    op.create_index(
        "ix_response_assessment_revision_ra",
        "response_assessment_revision",
        ["response_assessment_id", "revision_no"],
    )
    op.create_index(
        "ix_response_assessment_revision_patient",
        "response_assessment_revision",
        ["patient_id"],
    )


def downgrade() -> None:
    op.drop_table("response_assessment_revision")
    op.drop_table("response_assessments")
