"""Embedding model — pgvector-backed similarity search.

Default dimension matches BiomedCLIP (512). Larger models (Vertex 768,
OpenAI ada-002 1536) will live in their own table when introduced —
pgvector requires a fixed dimension per column.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import CheckConstraint, DateTime, Index, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from bvphoenix.db.base import Base
from bvphoenix.db.models._common import uuid_pk

EMBEDDING_DIM = 512


class Embedding(Base):
    __tablename__ = "embeddings"

    id: Mapped[uuid.UUID] = uuid_pk()
    target_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    target_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    model_id: Mapped[str] = mapped_column(String(128), nullable=False)
    vector: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIM), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "target_kind IN ('study','series','instance')",
            name="ck_embeddings_target_kind",
        ),
        UniqueConstraint(
            "target_kind",
            "target_id",
            "model_id",
            name="uq_embeddings_target_model",
        ),
        Index("ix_embeddings_target", "target_kind", "target_id"),
        # HNSW ANN index on the cosine-distance operator class. Actually
        # created in migration 0013_hnsw_indexes — declared here so it is
        # visible to Base.metadata (autogenerate, test fixtures).
        Index(
            "ix_embeddings_vector_hnsw",
            "vector",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"vector": "vector_cosine_ops"},
        ),
    )
