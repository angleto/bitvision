"""Embedding model registry — config-driven backend selection.

The ``embeddings`` table stores vectors tagged with a free-form
``model_id`` string; this table turns that string into a first-class
object so adding a new embedding backend is "insert one row + deploy
weights" with zero code change.

Columns of note:

* ``name`` is the stable identifier that ``embeddings.model_id`` points
  at. Version suffixes (``-v1``, ``-v2``) are the convention because the
  row itself is effectively immutable once referenced by vector data —
  retraining warrants a new row rather than rewriting an old one.
* ``kind`` partitions the registry by modality so callers can ask for
  "the default image model" or "every active text model" without
  provider-specific knowledge.
* ``weights_uri`` is a scheme-prefixed string (``hf:org/name``,
  ``s3://bucket/path``, ``https://...``). ``NULL`` is allowed for
  API-backed providers where the weights are remote (OpenAI, Vertex).
* ``is_default_for_kind`` + the partial unique index from migration
  0016 guarantee at most one active, non-deprecated default per kind.
* ``deprecated_at`` is the retirement flag: historical rows in
  ``embeddings`` may still reference the name, so we never delete —
  we only mark.

Attribute name ``model_metadata`` avoids SQLAlchemy's reserved
``metadata`` attribute on ``Base``; the column itself is still
``model_metadata`` in Postgres for the same reason (keeps ORM and SQL
names in sync).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from bvphoenix.db.base import Base
from bvphoenix.db.models._common import uuid_pk


class EmbeddingModel(Base):
    __tablename__ = "embedding_models"

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    dim: Mapped[int] = mapped_column(Integer, nullable=False)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    # NULL for API-backed providers (OpenAI, Vertex) where the "weights"
    # are remote and identified by provider + name alone.
    weights_uri: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    is_default_for_kind: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    # NULL until the model is retired. Kept on deprecated rows so
    # historical embeddings in ``embeddings`` still have a registry
    # entry to resolve.
    deprecated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # ``model_metadata`` rather than ``metadata`` — the latter collides
    # with SQLAlchemy's declarative metadata attribute on ``Base``.
    model_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (
        UniqueConstraint("name", name="uq_embedding_models_name"),
        CheckConstraint(
            "kind IN ('image','text','multimodal')",
            name="ck_embedding_models_kind",
        ),
        CheckConstraint("dim > 0", name="ck_embedding_models_dim_positive"),
        Index("ix_embedding_models_kind_active", "kind", "is_active"),
    )
