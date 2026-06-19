"""Series acquisition timing + contrast phase.

Multiphase contrast-enhanced CT acquires each phase (non-contrast,
arterial, portal-venous, delayed, ...) as its OWN SeriesInstanceUID at a
different time post-injection. To classify and order those phases the
series row needs an intra-study temporal key (``study_date`` is DATE-only)
and the contrast metadata, plus columns to persist the classified phase.

This migration adds, all nullable / additive (no backfill needed; the
ingest path and a one-shot backfill CLI populate them going forward):

* ``acquisition_time_of_day``  (TIME)        — AcquisitionTime / SeriesTime
* ``contrast_bolus_agent``     (TEXT)        — ContrastBolusAgent (0018,0010)
* ``contrast_bolus_start_time``(TIME)        — ContrastBolusStartTime (0018,1042)
* ``acquisition_phase``        (VARCHAR 24)  — classified phase (enum-checked)
* ``phase_confidence``         (DOUBLE)      — 0..1 classifier confidence
* ``phase_source``             (VARCHAR 8)   — 'auto' | 'human'

``acquisition_phase`` is deliberately NOT the care-timeline ``CarePhase``:
distinct concept, distinct column, distinct future MCP scope.

Idempotent (IF NOT EXISTS / guarded constraints), safe to re-run.

Revision ID: 0030_series_acquisition_phase
Revises: 0029_response_assessments
Create Date: 2026-06-18
"""

from __future__ import annotations

from alembic import op

revision = "0030_series_acquisition_phase"
down_revision = "0029_response_assessments"
branch_labels = None
depends_on = None

# Kept in sync with ``db.models.dicom.ACQUISITION_PHASES`` / ``PHASE_SOURCES``.
# Migrations are point-in-time snapshots, so the values are inlined here on
# purpose: a future taxonomy change ships as its own migration.
_PHASES = (
    "unenhanced",
    "arterial",
    "portal_venous",
    "delayed",
    "hepatobiliary",
    "corticomedullary",
    "nephrographic",
    "excretory",
    "dynamic",
    "other",
)
_SOURCES = ("auto", "human")


def upgrade() -> None:
    op.execute("ALTER TABLE series ADD COLUMN IF NOT EXISTS acquisition_time_of_day time")
    op.execute("ALTER TABLE series ADD COLUMN IF NOT EXISTS contrast_bolus_agent text")
    op.execute("ALTER TABLE series ADD COLUMN IF NOT EXISTS contrast_bolus_start_time time")
    op.execute("ALTER TABLE series ADD COLUMN IF NOT EXISTS acquisition_phase varchar(24)")
    op.execute("ALTER TABLE series ADD COLUMN IF NOT EXISTS phase_confidence double precision")
    op.execute("ALTER TABLE series ADD COLUMN IF NOT EXISTS phase_source varchar(8)")

    phases_in = ",".join(f"'{p}'" for p in _PHASES)
    sources_in = ",".join(f"'{s}'" for s in _SOURCES)
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'ck_series_acquisition_phase'
            ) THEN
                ALTER TABLE series ADD CONSTRAINT ck_series_acquisition_phase
                    CHECK (acquisition_phase IS NULL OR acquisition_phase IN ({phases_in}));
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'ck_series_phase_source'
            ) THEN
                ALTER TABLE series ADD CONSTRAINT ck_series_phase_source
                    CHECK (phase_source IS NULL OR phase_source IN ({sources_in}));
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'ck_series_phase_confidence_range'
            ) THEN
                ALTER TABLE series ADD CONSTRAINT ck_series_phase_confidence_range
                    CHECK (phase_confidence IS NULL OR (phase_confidence >= 0 AND phase_confidence <= 1));
            END IF;
        END$$;
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_series_acquisition_phase ON series (acquisition_phase)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_series_acquisition_phase")
    op.execute("ALTER TABLE series DROP CONSTRAINT IF EXISTS ck_series_phase_confidence_range")
    op.execute("ALTER TABLE series DROP CONSTRAINT IF EXISTS ck_series_phase_source")
    op.execute("ALTER TABLE series DROP CONSTRAINT IF EXISTS ck_series_acquisition_phase")
    op.execute("ALTER TABLE series DROP COLUMN IF EXISTS phase_source")
    op.execute("ALTER TABLE series DROP COLUMN IF EXISTS phase_confidence")
    op.execute("ALTER TABLE series DROP COLUMN IF EXISTS acquisition_phase")
    op.execute("ALTER TABLE series DROP COLUMN IF EXISTS contrast_bolus_start_time")
    op.execute("ALTER TABLE series DROP COLUMN IF EXISTS contrast_bolus_agent")
    op.execute("ALTER TABLE series DROP COLUMN IF EXISTS acquisition_time_of_day")
