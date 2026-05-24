"""Reindex jobs — orchestration state for rollback-safe embedding swaps.

A single ``reindex_jobs`` row tracks a multi-batch re-embedding of every
row of a given ``target_kind`` (``study`` / ``series`` / ``instance``)
from one model into another. Old and new embeddings coexist in the
``embeddings`` table keyed by ``model_id``; search continues to serve
off the current default while the new model backfills. The admin flips
the default only once the job is ``completed`` (see the model-registry
CLI ``bvphoenix-embed-models activate --default-for-kind``).

Rollback semantics are intentionally narrow: ``rollback`` deletes every
``embeddings`` row created after ``started_at`` for ``to_model_id`` —
the new vectors vanish, the old ones are untouched. No two rollbacks
should race; the CLI sets ``status = 'rolled_back'`` before issuing the
delete and refuses to rollback a ``running`` job.

Resume semantics: ``processed_items`` is advanced only when a batch
commits, so a crashed worker leaves the counter at the last durably
flushed offset. A fresh ``start`` with the same ``(target_kind,
from_model_id, to_model_id)`` is refused — callers must explicitly
resume by re-enqueueing batches against an existing job id.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from bvphoenix.db.base import Base
from bvphoenix.db.models._common import uuid_pk

REINDEX_STATUS_VALUES: tuple[str, ...] = (
    "pending",
    "running",
    "paused",
    "completed",
    "failed",
    "rolled_back",
)


class ReindexJob(Base):
    __tablename__ = "reindex_jobs"

    id: Mapped[uuid.UUID] = uuid_pk()
    # ``study`` / ``series`` / ``instance`` — matches the check constraint
    # on ``embeddings.target_kind`` so we can only ever target real rows.
    target_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    # NULL ⇒ fresh embed, no migration from a prior model. When set, the
    # worker is expected to scope its candidate set to rows that already
    # have a vector under ``from_model_id`` (the common upgrade path).
    from_model_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    to_model_id: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    total_items: Mapped[int | None] = mapped_column(Integer, nullable=True)
    processed_items: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_items: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    batch_size: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    # Free-form text for the last observed error. Kept short in practice
    # (truncated by the worker) — full tracebacks go to structlog.
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Subject (admin/service) that kicked off the job. Nullable so a
    # machine-initiated reindex is representable, but the CLI always sets
    # it — the DB-level nullability is just the last line of defense.
    created_by_subject_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("subjects.id", ondelete="SET NULL"),
        nullable=True,
    )

    __table_args__ = (
        CheckConstraint(
            "target_kind IN ('study','series','instance')",
            name="ck_reindex_jobs_target_kind",
        ),
        CheckConstraint(
            "status IN ('pending','running','paused','completed','failed','rolled_back')",
            name="ck_reindex_jobs_status",
        ),
        CheckConstraint(
            "batch_size > 0 AND batch_size <= 10000",
            name="ck_reindex_jobs_batch_size",
        ),
        Index("ix_reindex_jobs_status", "status"),
        Index("ix_reindex_jobs_to_model", "to_model_id"),
    )
