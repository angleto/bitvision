"""Pathology DZI tiling + ordinary-image class columns.

Step 2 of ``docs/pathology_wsi_spike.md`` (the deep-zoom viewer) needs
the slide row to carry the state of its pre-generated DeepZoom pyramid
and a discriminator for non-WSI pathology images (gross specimen photos,
static micrographs) that share the same viewer.

Adds, all nullable / server-default (metadata-only on PG >= 11, no table
rewrite, safe on the populated production table):

* ``slide_class``           (VARCHAR 16, default 'wsi') — wsi | gross | micrograph
* ``s3_dzi_key``            (VARCHAR 512)  — DeepZoom descriptor key (derivatives bucket)
* ``dzi_ready``             (BOOL, default false)
* ``dzi_levels`` / ``dzi_tile_size`` / ``dzi_overlap`` (INT)
* ``dzi_format``            (VARCHAR 8)    — 'jpeg' | 'png'
* ``dzi_generator_version`` (VARCHAR 32)
* ``dzi_error``             (JSONB)        — last tiling failure (code + detail)

The ``ck_pathology_slides_dzi_ready_complete`` CHECK makes a half-written
``dzi_ready=true`` row impossible so the serving endpoints can trust the
single flag. Existing rows backfill to ``slide_class='wsi'``; the tiling
backfill CLI then corrects gross/micrograph rows.

Idempotent (IF NOT EXISTS / guarded constraints), safe to re-run.

Revision ID: 0032_pathology_dzi_tiles
Revises: 0031_phase_enhancement_sets
Create Date: 2026-06-19
"""

from __future__ import annotations

from alembic import op

revision = "0032_pathology_dzi_tiles"
down_revision = "0031_phase_enhancement_sets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE pathology_slides ADD COLUMN IF NOT EXISTS "
        "slide_class varchar(16) NOT NULL DEFAULT 'wsi'"
    )
    op.execute("ALTER TABLE pathology_slides ADD COLUMN IF NOT EXISTS s3_dzi_key varchar(512)")
    op.execute(
        "ALTER TABLE pathology_slides ADD COLUMN IF NOT EXISTS "
        "dzi_ready boolean NOT NULL DEFAULT false"
    )
    op.execute("ALTER TABLE pathology_slides ADD COLUMN IF NOT EXISTS dzi_levels integer")
    op.execute("ALTER TABLE pathology_slides ADD COLUMN IF NOT EXISTS dzi_tile_size integer")
    op.execute("ALTER TABLE pathology_slides ADD COLUMN IF NOT EXISTS dzi_overlap integer")
    op.execute("ALTER TABLE pathology_slides ADD COLUMN IF NOT EXISTS dzi_format varchar(8)")
    op.execute(
        "ALTER TABLE pathology_slides ADD COLUMN IF NOT EXISTS dzi_generator_version varchar(32)"
    )
    op.execute("ALTER TABLE pathology_slides ADD COLUMN IF NOT EXISTS dzi_error jsonb")

    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'ck_pathology_slides_slide_class'
            ) THEN
                ALTER TABLE pathology_slides ADD CONSTRAINT ck_pathology_slides_slide_class
                    CHECK (slide_class IN ('wsi','gross','micrograph'));
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'ck_pathology_slides_dzi_ready_complete'
            ) THEN
                ALTER TABLE pathology_slides ADD CONSTRAINT ck_pathology_slides_dzi_ready_complete
                    CHECK (dzi_ready = FALSE OR (
                        s3_dzi_key IS NOT NULL AND dzi_levels IS NOT NULL
                        AND dzi_tile_size IS NOT NULL AND dzi_format IS NOT NULL
                        AND dzi_generator_version IS NOT NULL));
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'ck_pathology_slides_dzi_format'
            ) THEN
                ALTER TABLE pathology_slides ADD CONSTRAINT ck_pathology_slides_dzi_format
                    CHECK (dzi_format IS NULL OR dzi_format IN ('jpeg','png'));
            END IF;
        END$$;
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_pathology_slides_dzi_pending "
        "ON pathology_slides (dzi_ready) WHERE dzi_ready = FALSE"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_pathology_slides_dzi_pending")
    op.execute(
        "ALTER TABLE pathology_slides DROP CONSTRAINT IF EXISTS ck_pathology_slides_dzi_format"
    )
    op.execute(
        "ALTER TABLE pathology_slides "
        "DROP CONSTRAINT IF EXISTS ck_pathology_slides_dzi_ready_complete"
    )
    op.execute(
        "ALTER TABLE pathology_slides DROP CONSTRAINT IF EXISTS ck_pathology_slides_slide_class"
    )
    op.execute("ALTER TABLE pathology_slides DROP COLUMN IF EXISTS dzi_error")
    op.execute("ALTER TABLE pathology_slides DROP COLUMN IF EXISTS dzi_generator_version")
    op.execute("ALTER TABLE pathology_slides DROP COLUMN IF EXISTS dzi_format")
    op.execute("ALTER TABLE pathology_slides DROP COLUMN IF EXISTS dzi_overlap")
    op.execute("ALTER TABLE pathology_slides DROP COLUMN IF EXISTS dzi_tile_size")
    op.execute("ALTER TABLE pathology_slides DROP COLUMN IF EXISTS dzi_levels")
    op.execute("ALTER TABLE pathology_slides DROP COLUMN IF EXISTS dzi_ready")
    op.execute("ALTER TABLE pathology_slides DROP COLUMN IF EXISTS s3_dzi_key")
    op.execute("ALTER TABLE pathology_slides DROP COLUMN IF EXISTS slide_class")
