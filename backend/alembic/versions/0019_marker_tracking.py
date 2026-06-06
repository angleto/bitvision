"""Marker tracking: etag, soft-delete, revision history, sphere kind.

P0 of the annotation-layer overhaul. Makes the in-viewer ``Marker`` a
tracked, recoverable entity so a diagnosis authored via MCP (create /
edit / delete) is always attributable and reversible:

* per-row ``etag`` (uuid) for ``If-Match`` optimistic concurrency;
* soft-delete columns (``deleted_at`` / ``purge_after`` /
  ``delete_reason``) + partial live index;
* a ``marker_revision`` snapshot table (mirrors ``care_phase_revision``)
  recording every create / update / delete / restore;
* widens ``ck_markers_kind`` with ``measurement.sphere`` (CircleROI /
  PERCIST sphere), previously mis-persisted as ``measurement.distance``.

Revision ID: 0019_marker_tracking
Revises: 0018_derivatives_stack_index
Create Date: 2026-06-06
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0019_marker_tracking"
down_revision = "0018_derivatives_stack_index"
branch_labels = None
depends_on = None


_KINDS_WITH_SPHERE = (
    "measurement.distance",
    "measurement.angle",
    "measurement.area",
    "measurement.ellipse",
    "measurement.freehand",
    "measurement.arrow",
    "measurement.text",
    "measurement.probe",
    "measurement.bbox",
    "measurement.sphere",
    "bbox.lesion",
    "bbox.exclusion",
    "fiducial",
    "reading-note",
    "text-overlay",
)
_KINDS_WITHOUT_SPHERE = tuple(k for k in _KINDS_WITH_SPHERE if k != "measurement.sphere")

_REVISION_CHANGE_KINDS = ("create", "update", "delete", "restore")


def _kinds_array_sql(kinds: tuple[str, ...]) -> str:
    return ", ".join(f"'{k}'::character varying" for k in kinds)


def upgrade() -> None:
    # --- markers: tracking columns -------------------------------------
    op.add_column(
        "markers",
        sa.Column(
            "etag",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
    )
    op.add_column("markers", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("markers", sa.Column("purge_after", sa.DateTime(timezone=True), nullable=True))
    op.add_column("markers", sa.Column("delete_reason", sa.String(length=255), nullable=True))

    # --- markers: widen kind CHECK with measurement.sphere -------------
    op.execute("ALTER TABLE markers DROP CONSTRAINT IF EXISTS ck_markers_kind")
    op.execute(
        "ALTER TABLE markers ADD CONSTRAINT ck_markers_kind CHECK "
        f"(((kind)::text = ANY ((ARRAY[{_kinds_array_sql(_KINDS_WITH_SPHERE)}])::text[])))"
    )

    # --- markers: partial live index -----------------------------------
    op.create_index(
        "ix_markers_live",
        "markers",
        ["patient_id", "target_kind", "target_id"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )

    # --- marker_revision -----------------------------------------------
    op.create_table(
        "marker_revision",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("marker_id", postgresql.UUID(as_uuid=True), nullable=False),
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
        sa.PrimaryKeyConstraint("id", name="pk_marker_revision"),
        sa.ForeignKeyConstraint(
            ["marker_id"],
            ["markers.id"],
            name="fk_marker_revision_marker",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "change_kind = ANY (ARRAY["
            + ", ".join(f"'{k}'::character varying" for k in _REVISION_CHANGE_KINDS)
            + "]::text[])",
            name="ck_marker_revision_kind",
        ),
        sa.CheckConstraint(
            "author_kind = ANY (ARRAY['human'::character varying, "
            "'agent'::character varying, 'system'::character varying]::text[])",
            name="ck_marker_revision_author_kind",
        ),
        sa.CheckConstraint("revision_no >= 1", name="ck_marker_revision_no_pos"),
        sa.UniqueConstraint("marker_id", "revision_no", name="uq_marker_revision_marker_no"),
    )
    op.create_index("ix_marker_revision_marker", "marker_revision", ["marker_id", "revision_no"])
    op.create_index("ix_marker_revision_patient", "marker_revision", ["patient_id"])


def downgrade() -> None:
    op.drop_index("ix_marker_revision_patient", table_name="marker_revision")
    op.drop_index("ix_marker_revision_marker", table_name="marker_revision")
    op.drop_table("marker_revision")

    op.drop_index("ix_markers_live", table_name="markers")

    # Drop any sphere rows before narrowing the CHECK again.
    op.execute("DELETE FROM markers WHERE kind = 'measurement.sphere'")
    op.execute("ALTER TABLE markers DROP CONSTRAINT IF EXISTS ck_markers_kind")
    op.execute(
        "ALTER TABLE markers ADD CONSTRAINT ck_markers_kind CHECK "
        f"(((kind)::text = ANY ((ARRAY[{_kinds_array_sql(_KINDS_WITHOUT_SPHERE)}])::text[])))"
    )

    op.drop_column("markers", "delete_reason")
    op.drop_column("markers", "purge_after")
    op.drop_column("markers", "deleted_at")
    op.drop_column("markers", "etag")
