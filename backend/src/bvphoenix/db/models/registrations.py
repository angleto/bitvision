"""Cross-modal series registration registry (Sprint 6, ADR P3).

A row per registration job: maps a ``moving_series`` onto a
``fixed_series`` via SimpleITK rigid (default) or Demons non-rigid.
The output deformation field / transform parameters land in S3 as a
``.nii.gz`` (warp field) or ``.tfm`` (rigid). The table is the
authoritative status: queued → running → succeeded / failed.

Lifetime ties to the Job row (one-to-one) so the existing
cleanup_expired_jobs cron applies — when the job expires, the
registration row is dropped and the warp field is GC'd.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from bvphoenix.db.base import Base
from bvphoenix.db.models._common import uuid_pk

REGISTRATION_KINDS: tuple[str, ...] = ("rigid", "demons")
REGISTRATION_STATUSES: tuple[str, ...] = (
    "queued",
    "running",
    "succeeded",
    "failed",
    "cancelled",
)


class Registration(Base):
    __tablename__ = "registrations"

    id: Mapped[uuid.UUID] = uuid_pk()
    fixed_series_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("series.id", ondelete="CASCADE"),
        nullable=False,
    )
    moving_series_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("series.id", ondelete="CASCADE"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="queued")

    # Pointer to the saved transform / warp field. ``None`` until the
    # worker writes it.
    s3_bucket: Mapped[str | None] = mapped_column(String(255))
    s3_key: Mapped[str | None] = mapped_column(String(512))

    # Free-form result metadata: similarity metric, iterations, time
    # budget, etc. Populated by the worker on success.
    result_meta: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    error: Mapped[str | None] = mapped_column(Text)

    # Optional pointer to the bvphoenix Job row for queue + progress
    # surfaces. NULL when the registration ran inline (rare).
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("jobs.id", ondelete="SET NULL"),
    )

    requested_by_subject_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("subjects.id", ondelete="SET NULL"),
    )
    agent_token_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("agent_tokens.id", ondelete="SET NULL"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_registrations_fixed", "fixed_series_id"),
        Index("ix_registrations_moving", "moving_series_id"),
        Index("ix_registrations_status_created", "status", "created_at"),
    )
