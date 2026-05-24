"""Generic long-running jobs (DESIGN.md §11).

A single ``jobs`` row tracks any backend operation that can exceed
~1s and benefits from progress reporting, retry deduplication, and
cross-session recoverability: ZIP exports, volume packing, embedding
reindexes, deidentify+repack, summary generation, etc.

The contract (§11.4): the ``idempotency_key`` is a hash of
``(kind, scope_ids, canonical_input, owner_subject_id)`` and the
partial unique index on it for active states is what makes a retry
return the existing job instead of starting a duplicate.

Per-user queue cap (§11.5) is enforced in the service layer at
enqueue time, after the idempotency lookup, so retrying the same
operation does not consume a slot.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

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
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from bvphoenix.db.base import Base
from bvphoenix.db.models._common import uuid_pk

JOB_STATUS_VALUES: tuple[str, ...] = (
    "queued",
    "running",
    "succeeded",
    "failed",
    "cancelled",
)

# Active = a slot against the per-user cap. Terminal states do not
# count. Kept here so the service layer and the partial-index DDL
# never drift apart.
JOB_ACTIVE_STATUSES: frozenset[str] = frozenset({"queued", "running"})
JOB_TERMINAL_STATUSES: frozenset[str] = frozenset({"succeeded", "failed", "cancelled"})


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = uuid_pk()
    # Operation type, e.g. ``fascicolo_export``, ``gdpr_export``,
    # ``pack_volume``. Free-form by design: each consumer registers
    # its own kind in the service layer.
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    # Owner is the principal that requested the job. We FK to the
    # generic ``subjects`` table (rather than ``users``) so an admin
    # service or agent token can also own a job uniformly. Per-user
    # cap enforcement is keyed on this column.
    owner_subject_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("subjects.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Deterministic hash of (kind, scope_ids, canonical input,
    # owner). Same key + active job ⇒ dedup hit. The partial unique
    # index ``ix_jobs_idem_active_uniq`` is the actual constraint;
    # the column is just non-null so the index can fire.
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    # Total work units. NULL until the worker computes it (e.g. number
    # of files in a ZIP). Frontend renders an indeterminate spinner
    # while this is NULL.
    progress_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    progress_done: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Short stage label for the UI: 'packing', 'uploading', etc.
    stage: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Canonical input. Re-passed to the worker; also rehydratable for
    # debug. JSONB so we can index specific kinds later if needed.
    input: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    # On success: presigned S3 URL or internal pointer (e.g.
    # ``s3://bucket/key`` if the frontend re-signs at fetch time).
    result_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    # On failure: structured payload ``{code, message, traceback_id}``.
    # Kept JSONB so we can grep error codes without parsing strings.
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Cleanup deadline. The cleanup worker prunes rows past this and
    # deletes their S3 artifacts. NOT NULL so a misconfigured kind
    # cannot leak rows indefinitely.
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Arq job id if the job was enqueued. Lets us look up worker
    # state, cancel, or reclaim stalled rows on worker startup.
    arq_job_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Scoped resource ids (study, patient, document, ...). Mirrors
    # what was hashed into ``idempotency_key`` so we can also *query*
    # by scope without recomputing the hash. Enables cross-device
    # recovery: a study card on a fresh laptop fetches
    # ``GET /api/jobs?kind=study_export&scope_id=<uuid>`` and rebinds
    # to the in-flight job that was started elsewhere. Nullable for
    # legacy rows; the GIN index lets ``scope_ids @> ARRAY[id]``
    # queries stay fast as the table grows.
    scope_ids: Mapped[list[uuid.UUID] | None] = mapped_column(
        ARRAY(PG_UUID(as_uuid=True)),
        nullable=True,
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('queued','running','succeeded','failed','cancelled')",
            name="ck_jobs_status",
        ),
        CheckConstraint(
            "progress_done >= 0",
            name="ck_jobs_progress_done_nonneg",
        ),
        CheckConstraint(
            "progress_total IS NULL OR progress_total >= 0",
            name="ck_jobs_progress_total_nonneg",
        ),
        # Dedup primitive: same key + active state ⇒ collision, the
        # service layer interprets the unique-violation as a dedup hit
        # and returns the existing row.
        Index(
            "ix_jobs_idem_active_uniq",
            "idempotency_key",
            unique=True,
            postgresql_where="status IN ('queued','running')",
        ),
        # Per-user cap query: COUNT(*) WHERE owner_subject_id = ?
        # AND status IN active. The partial index makes the count
        # cheap and bounded by the cap itself (default 20).
        Index(
            "ix_jobs_owner_active",
            "owner_subject_id",
            "status",
            postgresql_where="status IN ('queued','running')",
        ),
        # Operational: list active jobs by kind for admin / debug.
        Index("ix_jobs_kind_status", "kind", "status"),
        # Cleanup worker scan.
        Index("ix_jobs_expires", "expires_at"),
        # Cross-device recovery: ``GET /api/jobs?kind=X&scope_id=Y``
        # uses ``scope_ids @> ARRAY[Y]``. GIN keeps the lookup fast
        # against an unbounded job table; the partial WHERE limits
        # storage to rows that actually carry scope.
        Index(
            "ix_jobs_scope_ids_gin",
            "scope_ids",
            postgresql_using="gin",
            postgresql_where="scope_ids IS NOT NULL",
        ),
    )
