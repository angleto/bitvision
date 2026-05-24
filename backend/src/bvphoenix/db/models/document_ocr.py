"""OCR cache for patient document files (Sprint 3, ADR 0007).

A row is keyed on ``(file_id, content_sha256, ocr_engine_version)``.
The agent / endpoint asks "give me text for file X"; the service:

1. Looks up by ``(file_id, sha256, engine)`` — cache hit returns the
   row immediately.
2. On miss, runs the OCR pipeline (pdfminer text-layer extraction
   first; Tesseract italian as the rasterised fallback) and inserts a
   new cache row.
3. Forced re-run via ``?force=true`` simply ignores the cache and
   inserts another row with the latest engine version.

``bbox_words`` is a JSONB array of ``{page, x, y, w, h, text}``
records used by the frontend to render highlighters. Text-layer
extractions skip it (no per-word bbox).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from bvphoenix.db.base import Base
from bvphoenix.db.models._common import uuid_pk


class DocumentOCR(Base):
    __tablename__ = "document_ocr"

    id: Mapped[uuid.UUID] = uuid_pk()
    document_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
    )
    file_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("document_files.id", ondelete="CASCADE"),
    )
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    ocr_engine: Mapped[str] = mapped_column(String(32), nullable=False)
    ocr_engine_version: Mapped[str] = mapped_column(String(32), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    page_count: Mapped[int | None] = mapped_column(Integer)
    bbox_words: Mapped[list | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "file_id",
            "content_sha256",
            "ocr_engine_version",
            name="uq_document_ocr_cache",
        ),
        Index("ix_document_ocr_document", "document_id"),
        Index("ix_document_ocr_engine", "ocr_engine", "ocr_engine_version"),
    )
