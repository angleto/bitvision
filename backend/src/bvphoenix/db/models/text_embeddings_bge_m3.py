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

from pgvector.sqlalchemy import Vector
from sqlalchemy import CheckConstraint, DateTime, Index, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from bvphoenix.db.base import Base
from bvphoenix.db.models._common import uuid_pk

# BGE-M3 dense dimension. Single source of truth, imported by the workers'
# embed task and the backend query path so a mismatch can't drift.
BGE_M3_DENSE_DIM = 1024

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
