"""Per-sub-stack derivatives (derivatives.stack_index).

A single DICOM SeriesInstanceUID can hold several co-located volumes — a
Philips mDIXON series interleaves Water / Fat / In-phase / Out-of-phase
stacks at the same z-positions, and multi-echo / DWI sequences pack several
contrasts under one series. ``services.volumes.partition_substacks`` now
de-interleaves these at volume-build time and the worker / volume endpoint
write one packed derivative per stack. This migration adds the ``stack_index``
dimension to the derivatives table so a series can cache one volume per
(kind, format, stack) instead of a single (now broken, interleaved) blob.

Non-destructive: existing rows backfill to ``stack_index = 0`` (the primary
stack), which is exactly the single-stack identity, so legacy single-stack
caches keep serving from their canonical ``volume.f32`` key untouched. The
broken interleaved derivatives of genuine multi-stack series are cleaned up
out-of-band (delete row + S3 object, then re-pack) — see the v4.4 backfill.

Idempotent (IF NOT EXISTS / guarded constraint), safe to re-run.

Revision ID: 0018_derivatives_stack_index
Revises: 0017_upload_sessions
Create Date: 2026-06-04
"""

from __future__ import annotations

from alembic import op

revision = "0018_derivatives_stack_index"
down_revision = "0017_upload_sessions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. New dimension. NOT NULL DEFAULT 0 backfills every existing row to
    #    the primary stack (their current identity).
    op.execute(
        "ALTER TABLE derivatives ADD COLUMN IF NOT EXISTS stack_index smallint NOT NULL DEFAULT 0"
    )
    # 2. Widen the uniqueness key to include the stack. Drop the old
    #    3-column constraint, add the 4-column one (guarded so re-runs are
    #    no-ops — Postgres has no ADD CONSTRAINT IF NOT EXISTS for UNIQUE).
    op.execute(
        "ALTER TABLE derivatives DROP CONSTRAINT IF EXISTS uq_derivatives_series_kind_format"
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'uq_derivatives_series_kind_format_stack'
            ) THEN
                ALTER TABLE derivatives
                    ADD CONSTRAINT uq_derivatives_series_kind_format_stack
                    UNIQUE (series_id, kind, format, stack_index);
            END IF;
        END$$;
        """
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE derivatives DROP CONSTRAINT IF EXISTS uq_derivatives_series_kind_format_stack"
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'uq_derivatives_series_kind_format'
            ) THEN
                ALTER TABLE derivatives
                    ADD CONSTRAINT uq_derivatives_series_kind_format
                    UNIQUE (series_id, kind, format);
            END IF;
        END$$;
        """
    )
    op.execute("ALTER TABLE derivatives DROP COLUMN IF EXISTS stack_index")
