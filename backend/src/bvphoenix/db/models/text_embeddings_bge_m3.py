"""BGE-M3 dense text embeddings — pgvector-backed multilingual search.

Dedicated 1024-dim table populated by BGE-M3 (BAAI/bge-m3, FlagEmbedding).
Kept separate from the 384-dim ``text_embeddings`` (MiniLM, minilm-multi-v1)
table because pgvector requires a fixed dimension per column and the two
spaces are not comparable. During the MiniLM -> BGE-M3 transition both
tables are populated; the query path resolves the active text model from
the embedding registry and reads the matching store.

This table holds the BGE-M3 **dense** vector only. The sparse (lexical)
and ColBERT (multi-vector) outputs of the same BGE-M3 forward pass land
in their own stores (Phase 2 / Phase 3): ``sparsevec`` for sparse, a
token-vector table for ColBERT late-interaction rerank.

``target_kind`` mirrors ``text_embeddings``: the text-bearing entities in
the platform (series descriptions, reports, annotations, consultations,
patient documents, fascicolo rows, and document chunks).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pgvector.sqlalchemy import SPARSEVEC, Vector
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    LargeBinary,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from bvphoenix.db.base import Base
from bvphoenix.db.models._common import uuid_pk

# BGE-M3 dense dimension. Single source of truth, imported by the workers'
# embed task and the backend query path so a mismatch can't drift.
BGE_M3_DENSE_DIM = 1024

# BGE-M3 sparse (lexical-weights) dimension = the XLM-RoBERTa vocabulary size.
# Single source of truth for the sparsevec column width AND the worker/query
# serialization, so the column and the SparseVector(..., dim) can't drift.
BGE_M3_SPARSE_DIM = 250002

# Wire-level model id written into ``model_id`` and registered in
# ``embedding_models``. Mirrors the SERIES_EMBED_MODEL_ID pattern.
BGE_M3_MODEL_ID = "bge-m3-v1"


class TextEmbeddingBgeM3(Base):
    __tablename__ = "text_embeddings_bge_m3"

    id: Mapped[uuid.UUID] = uuid_pk()
    target_kind: Mapped[str] = mapped_column(Text(), nullable=False)
    target_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    model_id: Mapped[str] = mapped_column(Text(), nullable=False)
    vector: Mapped[list[float]] = mapped_column(Vector(BGE_M3_DENSE_DIM), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "target_kind IN ('series','report','annotation','consultation',"
            "'document','patient','document_chunk')",
            name="ck_text_embeddings_bge_m3_target_kind",
        ),
        CheckConstraint(
            "model_id IN ('bge-m3-v1')",
            name="ck_text_embeddings_bge_m3_model_id",
        ),
        UniqueConstraint(
            "target_kind",
            "target_id",
            "model_id",
            name="uq_text_embeddings_bge_m3_target_model",
        ),
        Index("ix_text_embeddings_bge_m3_target", "target_kind", "target_id"),
        # The HNSW cosine index (global + the hot document_chunk partial)
        # is created in migration 0014 — mirrors how text_embeddings keeps
        # its vector index out of the ORM and in the migration/schema.
    )


class TextEmbeddingBgeM3Sparse(Base):
    """BGE-M3 SPARSE (lexical-weights) store — auxiliary signal of the SAME
    bge-m3-v1 model.

    Holds the per-text learned token weights over the XLM-RoBERTa vocab as a
    ``sparsevec``. Queried by inner product (== FlagEmbedding lexical matching;
    the weights are non-negative magnitudes, so dot product not cosine) and
    fused with the dense + FTS arms via RRF in ``chunk_search``. NOT a separate
    registry model: routing is keyed off the dense model's registry row
    (``model_metadata.sparse_store_table``, migration 0023), so a registry
    flip back to MiniLM cleanly disables this arm.
    """

    __tablename__ = "text_embeddings_bge_m3_sparse"

    id: Mapped[uuid.UUID] = uuid_pk()
    target_kind: Mapped[str] = mapped_column(Text(), nullable=False)
    target_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    model_id: Mapped[str] = mapped_column(Text(), nullable=False)
    sparse: Mapped[list[float]] = mapped_column(SPARSEVEC(BGE_M3_SPARSE_DIM), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "target_kind IN ('series','report','annotation','consultation',"
            "'document','patient','document_chunk')",
            name="ck_text_embeddings_bge_m3_sparse_target_kind",
        ),
        CheckConstraint(
            "model_id IN ('bge-m3-v1')",
            name="ck_text_embeddings_bge_m3_sparse_model_id",
        ),
        UniqueConstraint(
            "target_kind",
            "target_id",
            "model_id",
            name="uq_text_embeddings_bge_m3_sparse_target_model",
        ),
        Index("ix_text_embeddings_bge_m3_sparse_target", "target_kind", "target_id"),
        # The partial HNSW (sparsevec_ip_ops) on the document_chunk hot path
        # is created in migration 0015.
    )


class TextEmbeddingBgeM3Colbert(Base):
    """BGE-M3 ColBERT (multi-vector) store for late-interaction MaxSim rerank.

    One packed fp16 ``[n_tokens, 1024]`` matrix per text (``bytea``), read by
    ``target_id`` for the bounded top-K rerank pool in ``chunk_search``. No ANN
    index: MaxSim is not a single pgvector operator and the rerank is bounded
    to the RRF pool, so a per-token-row table would explode for zero index
    value. Auxiliary signal of bge-m3-v1, routed via the registry row's
    ``model_metadata.colbert_store_table`` (migration 0023).
    """

    __tablename__ = "text_embeddings_bge_m3_colbert"

    id: Mapped[uuid.UUID] = uuid_pk()
    target_kind: Mapped[str] = mapped_column(Text(), nullable=False)
    target_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    model_id: Mapped[str] = mapped_column(Text(), nullable=False)
    n_tokens: Mapped[int] = mapped_column(Integer(), nullable=False)
    token_dim: Mapped[int] = mapped_column(Integer(), nullable=False, server_default="1024")
    colbert: Mapped[bytes] = mapped_column(LargeBinary(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "target_kind IN ('series','report','annotation','consultation',"
            "'document','patient','document_chunk')",
            name="ck_text_embeddings_bge_m3_colbert_target_kind",
        ),
        CheckConstraint(
            "model_id IN ('bge-m3-v1')",
            name="ck_text_embeddings_bge_m3_colbert_model_id",
        ),
        CheckConstraint("n_tokens > 0", name="ck_text_embeddings_bge_m3_colbert_n_tokens"),
        CheckConstraint("token_dim = 1024", name="ck_text_embeddings_bge_m3_colbert_token_dim"),
        UniqueConstraint(
            "target_kind",
            "target_id",
            "model_id",
            name="uq_text_embeddings_bge_m3_colbert_target_model",
        ),
        Index("ix_text_embeddings_bge_m3_colbert_target", "target_kind", "target_id"),
    )
