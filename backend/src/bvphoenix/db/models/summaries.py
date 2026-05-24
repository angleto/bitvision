"""Summary model — persisted, versioned LLM summaries.

A ``Summary`` caches the output of an LLM run over a bit.vision resource
so the next viewer does not have to pay for another inference. It is
multi-level (``target_kind`` picks between ``series``, ``study``,
``patient``) and multi-language (``lang`` is currently ``it`` or ``en``).
Each row is uniquely keyed by ``(target_kind, target_id, lang,
model_id)``: one cached summary per language / model combination.

Staleness is tracked by ``source_version_hash`` — a hash over the
inputs that were fed to the LLM (descriptions, reports, annotations,
...). Callers recompute the hash from the current sources; a mismatch
means the cache is stale and the summary should be regenerated
(overwriting the row) or hard-deleted.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Index,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from bvphoenix.db.base import Base
from bvphoenix.db.models._common import TimestampMixin, uuid_pk

SUMMARY_TARGET_KINDS: tuple[str, ...] = ("series", "study", "patient")
SUMMARY_LANGS: tuple[str, ...] = ("it", "en")


class Summary(TimestampMixin, Base):
    __tablename__ = "summaries"

    id: Mapped[uuid.UUID] = uuid_pk()
    target_kind: Mapped[str] = mapped_column(Text, nullable=False)
    target_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    lang: Mapped[str] = mapped_column(Text, nullable=False, server_default="it")
    model_id: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    summary_md: Mapped[str] = mapped_column(Text, nullable=False)
    bullet_points: Mapped[list | None] = mapped_column(JSONB)
    token_usage: Mapped[dict | None] = mapped_column(JSONB)
    source_version_hash: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "target_kind IN ('series','study','patient')",
            name="ck_summaries_target_kind",
        ),
        CheckConstraint(
            "lang IN ('it','en')",
            name="ck_summaries_lang",
        ),
        UniqueConstraint(
            "target_kind",
            "target_id",
            "lang",
            "model_id",
            name="uq_summaries_target_lang_model",
        ),
        Index("ix_summaries_target", "target_kind", "target_id", "lang"),
        Index("ix_summaries_created_at_desc", "created_at"),
    )
