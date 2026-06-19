"""Burned-in-pixel PHI risk + de-id lifecycle columns on series/instances.

Adds the columns the M0 public-egress gate and the contribution review queue
use to flag instances that may carry PHI burned into pixel data
(``services.pixel_deid.classify_pixel_risk``), populated at ingest:

* ``series.pixel_phi_risk``       none|low|high (max over the series' instances)
* ``instances.pixel_phi_risk``    none|low|high
* ``instances.pixel_deid_status`` lifecycle (unprocessed|cleaned|...|blocked)
* ``instances.pixel_deid_method`` JSONB redaction audit (NULL until M4)
* ``instances.pixel_deid_at``     timestamptz (NULL until processed)

All nullable + additive; no backfill — the egress gate classifies the bytes on
the fly for pre-existing rows, so a NULL never means "safe".

Revision ID: 0034_pixel_phi_risk
Revises: 0033_marker_pathology_target
Create Date: 2026-06-19
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0034_pixel_phi_risk"
down_revision = "0033_marker_pathology_target"
branch_labels = None
depends_on = None

_RISK = ("none", "low", "high")
_STATUS = ("unprocessed", "cleaned", "quarantined", "approved", "rejected", "blocked")


def _in_sql(col: str, values: tuple[str, ...]) -> str:
    return f"{col} IS NULL OR {col} IN (" + ",".join(f"'{v}'" for v in values) + ")"


def upgrade() -> None:
    op.add_column("series", sa.Column("pixel_phi_risk", sa.String(length=8), nullable=True))
    op.add_column("instances", sa.Column("pixel_phi_risk", sa.String(length=8), nullable=True))
    op.add_column("instances", sa.Column("pixel_deid_status", sa.String(length=16), nullable=True))
    op.add_column("instances", sa.Column("pixel_deid_method", JSONB(), nullable=True))
    op.add_column(
        "instances", sa.Column("pixel_deid_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_check_constraint(
        "ck_series_pixel_phi_risk", "series", _in_sql("pixel_phi_risk", _RISK)
    )
    op.create_check_constraint(
        "ck_instances_pixel_phi_risk", "instances", _in_sql("pixel_phi_risk", _RISK)
    )
    op.create_check_constraint(
        "ck_instances_pixel_deid_status", "instances", _in_sql("pixel_deid_status", _STATUS)
    )
    op.create_index(
        "ix_instances_pixel_phi_risk",
        "instances",
        ["pixel_phi_risk"],
        postgresql_where=sa.text("pixel_phi_risk = 'high'"),
    )


def downgrade() -> None:
    op.drop_index("ix_instances_pixel_phi_risk", table_name="instances")
    op.drop_constraint("ck_instances_pixel_deid_status", "instances", type_="check")
    op.drop_constraint("ck_instances_pixel_phi_risk", "instances", type_="check")
    op.drop_constraint("ck_series_pixel_phi_risk", "series", type_="check")
    op.drop_column("instances", "pixel_deid_at")
    op.drop_column("instances", "pixel_deid_method")
    op.drop_column("instances", "pixel_deid_status")
    op.drop_column("instances", "pixel_phi_risk")
    op.drop_column("series", "pixel_phi_risk")
