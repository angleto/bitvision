"""Lesion tracks — longitudinal lesion follow-up.

The pivot for tumour-comparison (baseline CT vs follow-up): a
``lesion_tracks`` row is one physical lesion followed over time, and
``lesion_track_points`` links a per-study ``Finding`` (the measured node
at a timepoint) into the track. Growth is derived over the ordered points
(``services/lesion_tracks.py``). Patient-level RECIST aggregation lives in
``response_assessments`` (a later migration).

Cross-patient is impossible by construction: ``lesion_track_points`` joins
track and finding through composite foreign keys
``(patient_id, lesion_track_id) → lesion_tracks(patient_id, id)`` and
``(patient_id, finding_id) → findings(patient_id, id)``. This migration
therefore also adds the ``UNIQUE (patient_id, id)`` on ``findings`` the
second composite FK needs as its target.

Revision ID: 0027_lesion_tracks
Revises: 0026_coarse_text_embed_target_kinds
Create Date: 2026-06-17
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0027_lesion_tracks"
down_revision = "0026_coarse_text_embed_target_kinds"
branch_labels = None
depends_on = None


_STATUSES = ("active", "resolved", "retracted")
_RECIST_ROLES = ("target", "non_target", "new", "not_evaluable")
_AUTHOR_KINDS = ("human", "agent", "system")
_LINK_KINDS = ("human", "agent", "system")
_LATERALITIES = ("left", "right", "bilateral", "midline")
_REVISION_CHANGE_KINDS = ("create", "update", "add_point", "remove_point", "delete", "restore")


def _in(col: str, values: tuple[str, ...]) -> str:
    return col + " IN (" + ",".join(f"'{v}'" for v in values) + ")"


def upgrade() -> None:
    # The composite FK lesion_track_points → findings(patient_id, id)
    # needs this unique target. id is already the PK so (patient_id, id)
    # is trivially unique.
    op.create_unique_constraint("uq_findings_patient_id", "findings", ["patient_id", "id"])

    # --- lesion_tracks -------------------------------------------------
    op.create_table(
        "lesion_tracks",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("patient_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("label", sa.String(length=255), nullable=False),
        sa.Column("anatomy_site_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("laterality", sa.String(length=8), nullable=True),
        sa.Column("finding_type_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("recist_role", sa.String(length=16), nullable=True),
        sa.Column("status", sa.String(length=16), server_default="active", nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name="pk_lesion_tracks"),
        sa.ForeignKeyConstraint(
            ["patient_id"], ["patients.id"], name="fk_lesion_tracks_patient", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["anatomy_site_id"],
            ["anatomy_sites.id"],
            name="fk_lesion_tracks_anatomy",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["finding_type_id"],
            ["finding_types.id"],
            name="fk_lesion_tracks_type",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["author_subject_id"],
            ["subjects.id"],
            name="fk_lesion_tracks_author",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["agent_token_id"],
            ["agent_tokens.id"],
            name="fk_lesion_tracks_agent_token",
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint("patient_id", "id", name="uq_lesion_tracks_patient_id"),
        sa.CheckConstraint(_in("status", _STATUSES), name="ck_lesion_tracks_status"),
        sa.CheckConstraint(_in("author_kind", _AUTHOR_KINDS), name="ck_lesion_tracks_author_kind"),
        sa.CheckConstraint(
            "laterality IS NULL OR " + _in("laterality", _LATERALITIES),
            name="ck_lesion_tracks_laterality",
        ),
        sa.CheckConstraint(
            "recist_role IS NULL OR " + _in("recist_role", _RECIST_ROLES),
            name="ck_lesion_tracks_recist_role",
        ),
    )
    op.create_index("ix_lesion_tracks_patient", "lesion_tracks", ["patient_id"])
    op.create_index(
        "ix_lesion_tracks_live",
        "lesion_tracks",
        ["patient_id", "status"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )

    # --- lesion_track_points -------------------------------------------
    op.create_table(
        "lesion_track_points",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("lesion_track_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("finding_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("patient_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("is_baseline", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("timepoint_date", sa.Date(), nullable=True),
        sa.Column("registration_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("linked_by_kind", sa.String(length=16), server_default="human", nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_lesion_track_points"),
        sa.ForeignKeyConstraint(
            ["patient_id", "lesion_track_id"],
            ["lesion_tracks.patient_id", "lesion_tracks.id"],
            name="fk_lesion_track_points_track",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["patient_id", "finding_id"],
            ["findings.patient_id", "findings.id"],
            name="fk_lesion_track_points_finding",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["registration_id"],
            ["registrations.id"],
            name="fk_lesion_track_points_registration",
            ondelete="SET NULL",
        ),
        sa.CheckConstraint(
            _in("linked_by_kind", _LINK_KINDS), name="ck_lesion_track_points_link_kind"
        ),
        sa.UniqueConstraint("finding_id", name="uq_lesion_track_points_finding"),
    )
    op.create_index(
        "uq_lesion_track_points_one_baseline",
        "lesion_track_points",
        ["lesion_track_id"],
        unique=True,
        postgresql_where=sa.text("is_baseline"),
    )
    op.create_index("ix_lesion_track_points_track", "lesion_track_points", ["lesion_track_id"])
    op.create_index("ix_lesion_track_points_patient", "lesion_track_points", ["patient_id"])
    op.create_index(
        "ix_lesion_track_points_registration", "lesion_track_points", ["registration_id"]
    )

    # --- lesion_track_revision -----------------------------------------
    op.create_table(
        "lesion_track_revision",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("lesion_track_id", postgresql.UUID(as_uuid=True), nullable=False),
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
        sa.PrimaryKeyConstraint("id", name="pk_lesion_track_revision"),
        sa.ForeignKeyConstraint(
            ["lesion_track_id"],
            ["lesion_tracks.id"],
            name="fk_lesion_track_revision_track",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            _in("change_kind", _REVISION_CHANGE_KINDS), name="ck_lesion_track_revision_kind"
        ),
        sa.CheckConstraint(
            _in("author_kind", _AUTHOR_KINDS), name="ck_lesion_track_revision_author_kind"
        ),
        sa.CheckConstraint("revision_no >= 1", name="ck_lesion_track_revision_no_pos"),
        sa.UniqueConstraint(
            "lesion_track_id", "revision_no", name="uq_lesion_track_revision_track_no"
        ),
    )
    op.create_index(
        "ix_lesion_track_revision_track",
        "lesion_track_revision",
        ["lesion_track_id", "revision_no"],
    )
    op.create_index("ix_lesion_track_revision_patient", "lesion_track_revision", ["patient_id"])


def downgrade() -> None:
    op.drop_table("lesion_track_revision")
    op.drop_table("lesion_track_points")
    op.drop_table("lesion_tracks")
    op.drop_constraint("uq_findings_patient_id", "findings", type_="unique")
