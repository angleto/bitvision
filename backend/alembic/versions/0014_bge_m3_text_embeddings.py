"""BGE-M3 dense text embeddings (1024-d) store + registry entry.

Adds ``text_embeddings_bge_m3`` (separate from the 384-d MiniLM
``text_embeddings`` — pgvector columns are fixed-dimension) with HNSW
cosine indexes, and registers ``bge-m3-v1`` in ``embedding_models`` as
ACTIVE but NOT default-for-kind.

``minilm-multi-v1`` stays the text default until the BGE-M3 vectors are
backfilled; a later ``activate_model('bge-m3-v1')`` (or follow-up
migration) flips the default so the query path switches over without a
window where it resolves to an empty store. The sparse + ColBERT outputs
of the same BGE-M3 forward pass land in their own stores in Phase 2/3.

Idempotent: IF NOT EXISTS + ON CONFLICT, safe to re-run.

Revision ID: 0014_bge_m3_text_embeddings
Revises: 0013_search_thesaurus
Create Date: 2026-05-30
"""

from __future__ import annotations

from alembic import op

revision = "0014_bge_m3_text_embeddings"
down_revision = "0013_search_thesaurus"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS public.text_embeddings_bge_m3 (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            target_kind text NOT NULL,
            target_id uuid NOT NULL,
            model_id text NOT NULL,
            vector public.vector(1024) NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT ck_text_embeddings_bge_m3_target_kind CHECK (
                target_kind IN ('series','report','annotation','consultation',
                                'document','patient','document_chunk')
            ),
            CONSTRAINT ck_text_embeddings_bge_m3_model_id CHECK (model_id IN ('bge-m3-v1'))
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_text_embeddings_bge_m3_target_model "
        "ON public.text_embeddings_bge_m3 (target_kind, target_id, model_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_text_embeddings_bge_m3_target "
        "ON public.text_embeddings_bge_m3 (target_kind, target_id)"
    )
    # Partial HNSW for the hot document_chunk path (mirrors migration 0012).
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_text_embeddings_bge_m3_chunk_hnsw "
        "ON public.text_embeddings_bge_m3 USING hnsw (vector public.vector_cosine_ops) "
        "WITH (m = 16, ef_construction = 64) "
        "WHERE target_kind = 'document_chunk' AND model_id = 'bge-m3-v1'"
    )
    # Global HNSW fallback for the other text target kinds.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_text_embeddings_bge_m3_vector_hnsw "
        "ON public.text_embeddings_bge_m3 USING hnsw (vector public.vector_cosine_ops) "
        "WITH (m = 16, ef_construction = 64)"
    )
    # Register BGE-M3 (active, NOT default — see module docstring).
    op.execute(
        """
        INSERT INTO public.embedding_models
            (name, kind, dim, provider, weights_uri, is_active, is_default_for_kind, model_metadata)
        VALUES (
            'bge-m3-v1', 'text', 1024, 'flag-embedding', 'hf:BAAI/bge-m3',
            true, false, '{"dense_dim": 1024, "sparse": true, "colbert": true}'::jsonb
        )
        ON CONFLICT (name) DO NOTHING
        """
    )


def downgrade() -> None:
    op.execute("DELETE FROM public.embedding_models WHERE name = 'bge-m3-v1'")
    op.execute("DROP TABLE IF EXISTS public.text_embeddings_bge_m3 CASCADE")
