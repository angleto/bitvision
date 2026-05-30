"""Search index hygiene: drop the redundant HNSW, add pg_trgm GIN for ILIKE.

Three index changes that the search audit (2026-05) flagged:

  * **Drop the duplicate HNSW on ``embeddings.vector``.**
    ``ix_embeddings_vector_cosine`` (default params) and
    ``ix_embeddings_vector_hnsw`` (``m=16, ef_construction=64``) are the
    same opclass on the same column, and pgvector's HNSW defaults are
    exactly ``m=16, ef_construction=64`` — so the two graphs are
    byte-for-byte equivalent. Maintaining both doubles build time and
    RAM on every embedding insert for zero recall gain. Keep the
    explicitly-parameterised one (intent is documented in its name) and
    drop the bare one.

  * **trgm GIN on ``series.body_part_examined``.** ``/search`` filters
    body part with ``ILIKE '%x%'`` (an infix match). Without a trgm
    index that is a sequential scan over every series row, which is the
    exact case the 3s ``statement_timeout`` in the endpoint was added to
    contain. ``gin_trgm_ops`` lets the planner serve both ``LIKE`` and
    ``ILIKE`` from the index.

  * **trgm GIN on ``tags.namespace`` + ``tags.value``.** The hybrid
    search tag signal ORs ``namespace ILIKE '%kw%'`` with
    ``value ILIKE '%kw%'``. Indexing both columns lets the planner
    BitmapOr two index scans instead of seq-scanning the tag table; an
    index on only one side leaves the OR un-indexable.

``pg_trgm`` is already installed (see the initial schema), so this is
index DDL only. The indexes are created ``IF NOT EXISTS`` so a partial
re-run is safe.

Revision ID: 0008_search_indexes
Revises: 0007_opendata_pathology_constraints
Create Date: 2026-05-29
"""

from __future__ import annotations

from alembic import op

revision = "0008_search_indexes"
down_revision = "0007_opendata_pathology_constraints"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Redundant HNSW graph — keep ix_embeddings_vector_hnsw.
    op.execute("DROP INDEX IF EXISTS ix_embeddings_vector_cosine")

    # 2. Infix ILIKE on body part (study search facet filter).
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_series_body_part_trgm "
        "ON public.series USING gin (body_part_examined public.gin_trgm_ops)"
    )

    # 3. Tag-signal ILIKE on namespace + value (hybrid search).
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_tags_namespace_trgm "
        "ON public.tags USING gin (namespace public.gin_trgm_ops)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_tags_value_trgm "
        "ON public.tags USING gin (value public.gin_trgm_ops)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_tags_value_trgm")
    op.execute("DROP INDEX IF EXISTS ix_tags_namespace_trgm")
    op.execute("DROP INDEX IF EXISTS ix_series_body_part_trgm")
    # Recreate the dropped HNSW with the same (default) parameters it had.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_embeddings_vector_cosine "
        "ON public.embeddings USING hnsw (vector public.vector_cosine_ops)"
    )
