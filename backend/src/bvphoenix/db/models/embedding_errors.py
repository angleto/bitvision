"""EmbeddingError — per-target failure log for embedding workers.

Written by background workers (``embed_series`` etc.) every time the
task body raises. Read by the admin coverage endpoint to drive the
"why did this fail" column in the dashboard.

Schema mirrors migration ``0018_embedding_errors`` exactly; keep the
two in sync.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Index, Integer, Text, func, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from bvphoenix.db.base import Base
from bvphoenix.db.models._common import uuid_pk


class EmbeddingError(Base):
    __tablename__ = "embedding_errors"

    id: Mapped[uuid.UUID] = uuid_pk()
    target_kind: Mapped[str] = mapped_column(Text, nullable=False)
    target_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    model_id: Mapped[str] = mapped_column(Text, nullable=False)
    error_message: Mapped[str] = mapped_column(Text, nullable=False)
    # Optional short tag — e.g. "NoPixelDataError", "CudaOOM", "NetworkError".
    # Best-effort classifier; ``error_message`` is the authoritative detail.
    error_class: Mapped[str | None] = mapped_column(Text, nullable=True)
    failed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    __table_args__ = (
        CheckConstraint(
            "target_kind IN ('study','series','instance')",
            name="ck_embedding_errors_target_kind",
        ),
        Index(
            "ix_embedding_errors_target_model",
            "target_kind",
            "target_id",
            "model_id",
        ),
        Index(
            "ix_embedding_errors_failed_at",
            text("failed_at DESC"),
        ),
    )
