"""Generic text chunks across all sub-document content sources.

A row represents a contiguous slice of natural-language text from
ANY of the platform's text-bearing entities — uploaded patient
documents, clinician-authored clinical notes, AI-generated summaries,
and structured report-content narratives. Sub-document chunking lets
natural-language Q&A retrieval surface precise excerpts and
citable provenance for questions like "qual'è la classificazione
del tumore secondo l'istologico?" or "ci sono note dell'oncologo
nell'ultimo mese?".

Cross-patient defence-in-depth: ``patient_id`` is denormalised and
the chunker reads it directly from the source row (never from a
client-supplied parameter). Cross-patient leakage is also blocked by
the explicit predicate in :func:`chunk_search.search_chunks` and by
the CI security tests under ``backend/tests/security``. The table
serves four source kinds so a single composite foreign key to one
parent table is not feasible; defence in depth therefore stops at the
application layer + denorm column + test gate.

Filtering surfaces:
    ``source_kind``: 'document' | 'clinical_note' | 'summary' |
        'report_content'.
    ``author_kind``: 'human' | 'agent' | 'system' | 'unknown' —
        denormalised from the source so retrieval can exclude AI
        content with one predicate ("solo originali umani").
    ``authority_id``: copied from the source's authority taxonomy when
        applicable (documents, report_content); NULL on sources with
        no authority concept (clinical_note, summary).
    ``document_kind_id``: copied from ``documents.kind_id`` for the
        ``source_kind='document'`` rows; NULL otherwise.

``chunker_version`` is part of the unique key so multiple chunker
strategies can coexist; the worker is idempotent on
``(source_kind, source_id, chunker_version, char_start)``.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    Computed,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from bvphoenix.db.base import Base
from bvphoenix.db.models._common import uuid_pk

CHUNKER_VERSIONS: tuple[str, ...] = ("sliding-v1-w800-o100-it",)
DEFAULT_CHUNKER_VERSION = "sliding-v1-w800-o100-it"

CHUNK_SOURCE_KINDS: tuple[str, ...] = (
    "document",
    "clinical_note",
    "summary",
    "report_content",
)

CHUNK_AUTHOR_KINDS: tuple[str, ...] = (
    "human",
    "agent",
    "system",
    "unknown",
)


class TextChunk(Base):
    __tablename__ = "text_chunks"

    id: Mapped[uuid.UUID] = uuid_pk()
    source_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    source_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    patient_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    author_kind: Mapped[str] = mapped_column(String(16), nullable=False, server_default="unknown")
    authority_id: Mapped[str | None] = mapped_column(String(64))
    document_kind_id: Mapped[str | None] = mapped_column(String(64))
    chunker_version: Mapped[str] = mapped_column(String(64), nullable=False)
    page: Mapped[int | None] = mapped_column(Integer)
    char_start: Mapped[int] = mapped_column(Integer, nullable=False)
    char_end: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    text_tsv: Mapped[str] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('italian', text)", persisted=True),
        nullable=False,
    )
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "source_kind IN ('document','clinical_note','summary','report_content')",
            name="ck_text_chunks_source_kind",
        ),
        CheckConstraint(
            "author_kind IN ('human','agent','system','unknown')",
            name="ck_text_chunks_author_kind",
        ),
        UniqueConstraint(
            "source_kind",
            "source_id",
            "chunker_version",
            "char_start",
            name="uq_text_chunks_source_version_start",
        ),
        Index("ix_text_chunks_patient", "patient_id"),
        Index("ix_text_chunks_source", "source_kind", "source_id"),
        Index(
            "ix_text_chunks_patient_filter",
            "patient_id",
            "source_kind",
            "author_kind",
        ),
        Index(
            "ix_text_chunks_patient_authority",
            "patient_id",
            "authority_id",
            postgresql_where="authority_id IS NOT NULL",
        ),
        Index(
            "ix_text_chunks_text_tsv",
            "text_tsv",
            postgresql_using="gin",
        ),
    )


__all__ = [
    "CHUNKER_VERSIONS",
    "CHUNK_AUTHOR_KINDS",
    "CHUNK_SOURCE_KINDS",
    "DEFAULT_CHUNKER_VERSION",
    "TextChunk",
]
