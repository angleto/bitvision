"""Persistent burned-in-pixel redaction recall runs.

The M6c GT-box UI scores recall per instance on demand; this table records the
recall of a WHOLE corpus over time so a regression is visible as the engine
evolves. One row per evaluation run (``bvphoenix-deid-recall``), keyed by
corpus + engine fingerprint so trend lines group by comparable configuration.

``missed`` holds a capped sample of the un-redacted PHI texts (for triage) — it
is PHI-bearing and must never reach a public response; it lives in the same DB
as the source PHI, so no new exposure class.

Purely additive.

Revision ID: 0045_deid_recall_runs
Revises: 0044_pixel_clean_blobs
Create Date: 2026-07-11
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

revision = "0045_deid_recall_runs"
down_revision = "0044_pixel_clean_blobs"
branch_labels = None
depends_on = None

_CORPUS_KINDS = ("synthetic", "public", "curated")


def upgrade() -> None:
    op.create_table(
        "deid_recall_runs",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True),
        sa.Column("corpus_kind", sa.String(length=16), nullable=False),
        sa.Column("corpus_version", sa.String(length=64)),
        sa.Column("corpus_hash", sa.String(length=64)),
        sa.Column("engine", JSONB(), nullable=False),
        sa.Column("coverage", sa.Float(), nullable=False),
        sa.Column("recall", sa.Float(), nullable=False),
        sa.Column("covered", sa.Integer(), nullable=False),
        sa.Column("total", sa.Integer(), nullable=False),
        sa.Column("cases", sa.Integer(), nullable=False),
        sa.Column("missed", JSONB()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "corpus_kind IN (" + ",".join(f"'{k}'" for k in _CORPUS_KINDS) + ")",
            name="ck_deid_recall_runs_corpus_kind",
        ),
    )
    op.create_index(
        "ix_deid_recall_runs_kind_created",
        "deid_recall_runs",
        ["corpus_kind", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_deid_recall_runs_kind_created", table_name="deid_recall_runs")
    op.drop_table("deid_recall_runs")
