"""Partial HNSW indexes for the hot, populated (target_kind, model_id) combos.

A single HNSW index over a whole embeddings table cannot use the
``WHERE target_kind=... AND model_id=...`` equality predicates to prune —
it walks the full graph and post-filters, which both costs more and (when
the matching subset is small) silently under-returns. A *partial* HNSW
index whose predicate matches the query's predicate fixes both: every row
in the graph already satisfies the filter, so there is no post-filtering
and recall is full.

This migration is **additive** — it does NOT drop the global HNSW
indexes — so no ``(target_kind, model_id)`` combination can lose its only
index. It targets the two combos that are both populated and queried as a
unit today:

  * ``embeddings`` WHERE series + ``biomedclip-v1`` — the image
    similarity / hybrid-image path.
  * ``text_embeddings`` WHERE document_chunk + ``minilm-multi-v1`` — the
    per-patient chunk RAG path (the single highest-traffic vector query).

Dropping the now-redundant global indexes is a follow-up that needs a
``SELECT target_kind, model_id, count(*)`` census first, so a combo that
exists only in production is not left unindexed.

Revision ID: 0012_partial_hnsw_indexes
Revises: 0011_reconcile_embedding_registry
Create Date: 2026-05-30
"""

from __future__ import annotations

from alembic import op

revision = "0012_partial_hnsw_indexes"
down_revision = "0011_reconcile_embedding_registry"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_embeddings_series_biomedclip_hnsw "
        "ON public.embeddings USING hnsw (vector public.vector_cosine_ops) "
        "WITH (m='16', ef_construction='64') "
        "WHERE target_kind = 'series' AND model_id = 'biomedclip-v1'"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_text_embeddings_chunk_minilm_hnsw "
        "ON public.text_embeddings USING hnsw (vector public.vector_cosine_ops) "
        "WITH (m='16', ef_construction='64') "
        "WHERE target_kind = 'document_chunk' AND model_id = 'minilm-multi-v1'"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_text_embeddings_chunk_minilm_hnsw")
    op.execute("DROP INDEX IF EXISTS ix_embeddings_series_biomedclip_hnsw")
