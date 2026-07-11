"""Persistent burned-in-pixel redaction recall runs (M6c real-world monitor).

One row per corpus evaluation (``bvphoenix-deid-recall``); the M6c GT-box UI's
per-instance ``gt-score`` is the interactive counterpart, this is the tracked-
over-time signal. ``engine`` fingerprints the config the run scored under so
trend queries group comparable runs.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Float, Index, Integer, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from bvphoenix.db.base import Base
from bvphoenix.db.models._common import uuid_pk

CORPUS_KINDS: tuple[str, ...] = ("synthetic", "public", "curated")


class DeidRecallRun(Base):
    __tablename__ = "deid_recall_runs"

    id: Mapped[uuid.UUID] = uuid_pk()
    corpus_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    corpus_version: Mapped[str | None] = mapped_column(String(64))
    corpus_hash: Mapped[str | None] = mapped_column(String(64))
    # {app_version, git_sha, redaction_mode, vlm_enabled, tesseract, deid_method_version}
    engine: Mapped[dict] = mapped_column(JSONB, nullable=False)
    coverage: Mapped[float] = mapped_column(Float, nullable=False)
    recall: Mapped[float] = mapped_column(Float, nullable=False)
    covered: Mapped[int] = mapped_column(Integer, nullable=False)
    total: Mapped[int] = mapped_column(Integer, nullable=False)
    cases: Mapped[int] = mapped_column(Integer, nullable=False)
    # Capped sample of un-redacted PHI texts — PHI-bearing, never in a public
    # response (admin-only, and only when explicitly requested).
    missed: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "corpus_kind IN (" + ",".join(f"'{k}'" for k in CORPUS_KINDS) + ")",
            name="ck_deid_recall_runs_corpus_kind",
        ),
        Index("ix_deid_recall_runs_kind_created", "corpus_kind", "created_at"),
    )


__all__ = ["CORPUS_KINDS", "DeidRecallRun"]
