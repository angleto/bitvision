"""Dual-config FTS on study / series descriptions.

The study + series description indexes were ``to_tsvector('simple', …)``
— no stemming, no stopwords. On an Italian-first corpus that silently
loses recall: a search for "polmoni" never matches the singular
"polmone" (both stem to "polmon"). Switching wholesale to
``italian`` would instead break the English radiology acronyms and
DICOM code-strings ("T2 FLAIR", "STIR", "MIP") that the ``simple`` path
matched verbatim.

The fix is a **dual** vector: the Italian-stemmed lexemes OR'd
(``tsvector ||``) with the raw ``simple`` tokens, materialised as a
generated + stored column (mirroring the proven ``text_chunks.text_tsv``
pattern) and GIN-indexed. Queries OR the two ``plainto_tsquery`` configs
the same way (see ``services/fts.py``), so a row matches on either the
stemmed or the exact reading.

The old ``simple``-only expression indexes are dropped — no query
references that expression after this migration (every FTS site is
moved to the generated column / the dual inline expression in the same
change).

Note: ``ADD COLUMN … GENERATED ALWAYS AS … STORED`` rewrites the table
and takes a brief ACCESS EXCLUSIVE lock while it backfills. The
study/series tables are small relative to ``instances`` so this is a
short operation; schedule it like any other DDL.

Revision ID: 0009_dual_config_fts
Revises: 0009_derivative_geometry
Create Date: 2026-05-29
"""

from __future__ import annotations

from alembic import op

revision = "0010_dual_config_fts"
down_revision = "0009_derivative_geometry"
branch_labels = None
depends_on = None

_STUDY_TSV = (
    "to_tsvector('italian'::regconfig, coalesce(study_description, '')) "
    "|| to_tsvector('simple'::regconfig, coalesce(study_description, ''))"
)
_SERIES_TSV = (
    "to_tsvector('italian'::regconfig, coalesce(series_description, '')) "
    "|| to_tsvector('simple'::regconfig, coalesce(series_description, ''))"
)


def upgrade() -> None:
    op.execute(
        f"ALTER TABLE public.imaging_studies "
        f"ADD COLUMN IF NOT EXISTS study_description_tsv tsvector "
        f"GENERATED ALWAYS AS ({_STUDY_TSV}) STORED"
    )
    op.execute(
        f"ALTER TABLE public.series "
        f"ADD COLUMN IF NOT EXISTS series_description_tsv tsvector "
        f"GENERATED ALWAYS AS ({_SERIES_TSV}) STORED"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_studies_description_tsv "
        "ON public.imaging_studies USING gin (study_description_tsv)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_series_description_tsv "
        "ON public.series USING gin (series_description_tsv)"
    )
    # Superseded by the dual-config indexes above.
    op.execute("DROP INDEX IF EXISTS ix_studies_description_fts")
    op.execute("DROP INDEX IF EXISTS ix_series_description_fts")


def downgrade() -> None:
    # Restore the original simple-only expression indexes first.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_studies_description_fts "
        "ON public.imaging_studies USING gin "
        "(to_tsvector('simple'::regconfig, coalesce(study_description, '')))"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_series_description_fts "
        "ON public.series USING gin "
        "(to_tsvector('simple'::regconfig, coalesce(series_description, '')))"
    )
    op.execute("DROP INDEX IF EXISTS ix_series_description_tsv")
    op.execute("DROP INDEX IF EXISTS ix_studies_description_tsv")
    op.execute("ALTER TABLE public.series DROP COLUMN IF EXISTS series_description_tsv")
    op.execute("ALTER TABLE public.imaging_studies DROP COLUMN IF EXISTS study_description_tsv")
