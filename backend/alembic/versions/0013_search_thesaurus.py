"""Versioned radiology synonym thesaurus for query expansion.

Free-text search misses obvious matches because the corpus mixes Italian
prose, English radiology, and acronyms: a search for "TC" never finds a
study described "computed tomography", "RM" misses "MRI", "mdc" misses
"mezzo di contrasto". A curated, editable, *versioned* thesaurus lets the
query side OR-expand those equivalences (``services/thesaurus.py``)
without a redeploy and without polluting the dense-vector query.

The table is intentionally tiny and hand-curated (bilingual IT/EN
radiology). ``version`` lets the eval harness pin a thesaurus snapshot so
a relevance delta maps to a known thesaurus state. RadLex/UMLS mapping is
the upgrade path (licensing footprint), so the seed starts hand-built.

Revision ID: 0013_search_thesaurus
Revises: 0012_partial_hnsw_indexes
Create Date: 2026-05-30
"""

from __future__ import annotations

from alembic import op

revision = "0013_search_thesaurus"
down_revision = "0012_partial_hnsw_indexes"
branch_labels = None
depends_on = None

# term -> equivalent surface forms OR'd into the FTS query. Symmetric
# pairs are listed both ways so expansion works whichever side is typed.
_SEED: dict[str, list[str]] = {
    "tc": ["ct", "computed tomography", "tomografia computerizzata"],
    "ct": ["tc", "computed tomography", "tomografia computerizzata"],
    "rm": ["mri", "risonanza magnetica", "magnetic resonance"],
    "mri": ["rm", "risonanza magnetica", "magnetic resonance"],
    "rx": ["radiografia", "x-ray", "radiograph"],
    "eco": ["ecografia", "ultrasound", "us"],
    "ecografia": ["ultrasound", "us", "eco"],
    "mdc": ["contrasto", "mezzo di contrasto", "contrast"],
    "contrasto": ["mdc", "mezzo di contrasto", "contrast"],
    "ggo": ["ground glass", "vetro smerigliato"],
    "pet": ["tomografia ad emissione di positroni", "positron emission"],
    "torace": ["chest", "toracico"],
    "addome": ["abdomen", "addominale"],
    "encefalo": ["brain", "cerebrale", "cervello"],
    "mammografia": ["mammography", "mammogram"],
}


def upgrade() -> None:
    op.execute(
        "CREATE TABLE IF NOT EXISTS public.search_synonyms ("
        "  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),"
        "  term varchar(64) NOT NULL,"
        "  variants text[] NOT NULL,"
        "  lang varchar(8) NOT NULL DEFAULT 'mul',"
        "  version integer NOT NULL DEFAULT 1,"
        "  is_active boolean NOT NULL DEFAULT true,"
        "  created_at timestamptz NOT NULL DEFAULT now()"
        ")"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_search_synonyms_active "
        "ON public.search_synonyms (is_active, lower(term))"
    )
    for term, variants in _SEED.items():
        # Postgres array literal: {"a","b"} with each element quoted.
        arr = "{" + ",".join('"' + v.replace('"', '\\"') + '"' for v in variants) + "}"
        op.execute(
            "INSERT INTO public.search_synonyms (term, variants) "
            f"SELECT '{term}', '{arr}'::text[] "
            "WHERE NOT EXISTS (SELECT 1 FROM public.search_synonyms WHERE term = "
            f"'{term}')"
        )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS public.search_synonyms")
