"""BGE-M3 sparse (sparsevec) + ColBERT (packed token-vectors) stores.

Phase 2 + Phase 3 of the BGE-M3 hybrid. The same single BGE-M3 forward pass
(``BGEM3FlagModel.encode(return_dense/sparse/colbert)``) already produces the
1024-d dense vector (``text_embeddings_bge_m3``, migration 0014); this adds the
two auxiliary stores of the SAME ``bge-m3-v1`` model:

* ``text_embeddings_bge_m3_sparse`` — the learned lexical weights over the
  XLM-RoBERTa vocab (250002) as a ``sparsevec``. Queried by INNER PRODUCT
  (``sparsevec_ip_ops`` / ``<#>``): the weights are non-negative magnitudes
  and the intended score is the dot product over shared tokens, matching
  FlagEmbedding's ``compute_lexical_matching_score``. Cosine would normalize
  the magnitudes away. Fused with the dense + FTS arms via RRF.
* ``text_embeddings_bge_m3_colbert`` — one packed fp16 ``[n_tokens, 1024]``
  matrix per text (``bytea``) for late-interaction MaxSim rerank over the
  bounded RRF top-K pool. No ANN index (MaxSim is not a single pgvector
  operator; per-token rows would explode the table for zero index value).

Both are auxiliary to bge-m3-v1, NOT new registry models: the query path
routes to them off the dense model's spec (text_models.TEXT_MODELS), so a
registry flip back to MiniLM cleanly disables both arms. Partial HNSW on the
sparse store covers only the hot ``document_chunk`` path (the other text
target kinds are unpopulated, per the B-4 finding), mirroring 0014/0012.

Idempotent: IF NOT EXISTS, safe to re-run.

Revision ID: 0015_bge_m3_sparse_colbert
Revises: 0014_bge_m3_text_embeddings
Create Date: 2026-06-02
"""

from __future__ import annotations

from alembic import op

revision = "0015_bge_m3_sparse_colbert"
down_revision = "0014_bge_m3_text_embeddings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- SPARSE (lexical weights) store ---
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS public.text_embeddings_bge_m3_sparse (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            target_kind text NOT NULL,
            target_id uuid NOT NULL,
            model_id text NOT NULL,
            sparse public.sparsevec(250002) NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT ck_text_embeddings_bge_m3_sparse_target_kind CHECK (
                target_kind IN ('series','report','annotation','consultation',
                                'document','patient','document_chunk')
            ),
            CONSTRAINT ck_text_embeddings_bge_m3_sparse_model_id CHECK (model_id IN ('bge-m3-v1'))
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_text_embeddings_bge_m3_sparse_target_model "
        "ON public.text_embeddings_bge_m3_sparse (target_kind, target_id, model_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_text_embeddings_bge_m3_sparse_target "
        "ON public.text_embeddings_bge_m3_sparse (target_kind, target_id)"
    )
    # Partial HNSW for the hot document_chunk path, inner-product opclass
    # (BGE-M3 lexical weights are non-negative magnitudes; <#> dot product
    # == FlagEmbedding lexical matching). No global HNSW: the other text
    # target kinds are unpopulated for text per B-4.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_text_embeddings_bge_m3_sparse_chunk_hnsw "
        "ON public.text_embeddings_bge_m3_sparse USING hnsw (sparse public.sparsevec_ip_ops) "
        "WITH (m = 16, ef_construction = 64) "
        "WHERE target_kind = 'document_chunk' AND model_id = 'bge-m3-v1'"
    )

    # --- COLBERT (packed multi-vector) store ---
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS public.text_embeddings_bge_m3_colbert (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            target_kind text NOT NULL,
            target_id uuid NOT NULL,
            model_id text NOT NULL,
            n_tokens integer NOT NULL,
            token_dim integer NOT NULL DEFAULT 1024,
            colbert bytea NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT ck_text_embeddings_bge_m3_colbert_target_kind CHECK (
                target_kind IN ('series','report','annotation','consultation',
                                'document','patient','document_chunk')
            ),
            CONSTRAINT ck_text_embeddings_bge_m3_colbert_model_id CHECK (model_id IN ('bge-m3-v1')),
            CONSTRAINT ck_text_embeddings_bge_m3_colbert_n_tokens CHECK (n_tokens > 0),
            CONSTRAINT ck_text_embeddings_bge_m3_colbert_token_dim CHECK (token_dim = 1024)
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_text_embeddings_bge_m3_colbert_target_model "
        "ON public.text_embeddings_bge_m3_colbert (target_kind, target_id, model_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_text_embeddings_bge_m3_colbert_target "
        "ON public.text_embeddings_bge_m3_colbert (target_kind, target_id)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS public.text_embeddings_bge_m3_colbert CASCADE")
    op.execute("DROP TABLE IF EXISTS public.text_embeddings_bge_m3_sparse CASCADE")
