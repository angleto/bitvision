"""Text embedding model — pgvector-backed multilingual semantic search.

Dedicated 384-dim table populated by a multilingual sentence-transformers
model (paraphrase-multilingual-MiniLM-L12-v2). Kept separate from the
BiomedCLIP ``embeddings`` table because pgvector requires a fixed
dimension per column and the text/image embedding spaces are not
comparable anyway.

``target_kind`` covers the text-bearing entities in the platform:
series descriptions, reports, annotations, consultations, patient
documents, and patient fascicolo rows.
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

TEXT_EMBEDDING_DIM = 384

TEXT_EMBEDDING_TARGET_KINDS: tuple[str, ...] = (
    "series",
    "report",
    "annotation",
    "consultation",
    "document",
    "patient",
    "document_chunk",
    "finding",
)


class TextEmbedding(Base):
    __tablename__ = "text_embeddings"

    id: Mapped[uuid.UUID] = uuid_pk()
    target_kind: Mapped[str] = mapped_column(Text(), nullable=False)
    target_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    model_id: Mapped[str] = mapped_column(Text(), nullable=False)
    vector: Mapped[list[float]] = mapped_column(Vector(TEXT_EMBEDDING_DIM), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "target_kind IN ('series','report','annotation','consultation',"
            "'document','patient','document_chunk','finding','study')",
            name="ck_text_embeddings_target_kind",
        ),
        CheckConstraint(
            "model_id IN ('minilm-multi-v1')",
            name="ck_text_embeddings_model_id",
        ),
        UniqueConstraint(
            "target_kind",
            "target_id",
            "model_id",
            name="uq_text_embeddings_target_model",
        ),
        Index("ix_text_embeddings_target", "target_kind", "target_id"),
    )
