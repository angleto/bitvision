"""Resumable upload sessions (DESIGN.md §11.6).

A bulk upload is split into a durable, resumable session whose handle is
created BEFORE any bytes (status ``awaiting_bytes``), so it survives a network
drop / tab close / PC crash from byte zero — unlike the legacy single POST
(``/api/upload/bulk``) where the whole byte-transfer + S3 staging was one
non-resumable request.

Lifecycle:

    awaiting_bytes ─▶ uploading ─▶ ready ─▶ committed
                           │                   (job_id linked, hands off to
                           └─▶ aborted          the unchanged ingest worker)
                           └─▶ expired  (swept by the cleanup cron)

Per file, the client uploads fixed 8 MiB chunks (``PATCH .../files/{idx}``)
carrying ``Upload-Offset``; each chunk is one S3 multipart upload-part against
a per-file multipart upload. ``received_offset`` + ``parts`` are persisted per
chunk so a reconnect resumes from the last server-acked offset. When a file's
``received_offset == declared_size`` the part list is completed into
``s3_key`` and the file flips to ``staged``. Commit verifies every file is
staged, builds the same ``canonical_input`` manifest the legacy endpoint built
(pointing at the staged keys) and calls the UNCHANGED
``jobs_service.enqueue_or_get`` + ``ingest_bulk_files`` worker.

All bytes flow THROUGH the backend (no presigned PUT — storage isolation).
Abandoned sessions are swept by the cleanup cron, which aborts each file's
multipart upload immediately rather than waiting for the bucket lifecycle rule.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from bvphoenix.db.base import Base
from bvphoenix.db.models._common import uuid_pk

# Session states. "active" = occupies a slot / is sweepable-if-stale; the
# terminal states are committed (handed to the ingest job), aborted (explicit
# DELETE), expired (swept). Kept here so the service layer and the DDL never
# drift.
UPLOAD_SESSION_STATUS_VALUES: tuple[str, ...] = (
    "awaiting_bytes",
    "uploading",
    "ready",
    "committed",
    "aborted",
    "expired",
)
UPLOAD_SESSION_ACTIVE_STATUSES: frozenset[str] = frozenset({"awaiting_bytes", "uploading", "ready"})

UPLOAD_SESSION_FILE_STATUS_VALUES: tuple[str, ...] = ("pending", "uploading", "staged")


class UploadSession(Base):
    __tablename__ = "upload_sessions"

    id: Mapped[uuid.UUID] = uuid_pk()
    # The principal that opened the session. FK to ``subjects`` (like jobs)
    # so an agent token / admin service can own one uniformly; the parallel-
    # session cap is keyed on this column.
    owner_subject_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("subjects.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Upload target. Nullable + no hard FK: the session is transient and the
    # commit step re-resolves + re-checks WRITE permission on these via the
    # shared resolvers (which 404 if the patient/folder vanished meanwhile),
    # so a strict FK would only add lock contention on a short-lived row.
    patient_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    folder_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    tier: Mapped[str] = mapped_column(String(2), nullable=False, default="t1")
    # ISO/ZIP handling flags, mirrored verbatim into the commit's
    # canonical_input so the worker behaves exactly as the legacy endpoint.
    keep_iso_archive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    wrap_iso_in_folder: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    extract_iso_contents: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    status: Mapped[str] = mapped_column(String(16), nullable=False, default="awaiting_bytes")
    declared_total_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    received_total_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    # Set at commit: the ingest Job this session handed off to. ON DELETE
    # SET NULL so pruning a terminal job does not cascade-delete the session
    # audit row before its own sweep.
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("jobs.id", ondelete="SET NULL"),
        nullable=True,
    )
    # Optional create-dedup: a re-issued create with the same manifest maps
    # back to the active session via the partial unique index below, so a
    # retried "start upload" does not spawn a second session.
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Mirror of the scope ids (patient) for cross-device discovery, same role
    # as jobs.scope_ids.
    scope_ids: Mapped[list[uuid.UUID] | None] = mapped_column(
        ARRAY(PG_UUID(as_uuid=True)), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    # Sweep deadline. NOT NULL so a stuck session cannot leak rows + S3
    # multipart parts indefinitely.
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "status IN ('awaiting_bytes','uploading','ready','committed','aborted','expired')",
            name="ck_upload_sessions_status",
        ),
        CheckConstraint("tier IN ('t1','t2','t3','t4')", name="ck_upload_sessions_tier"),
        CheckConstraint(
            "declared_total_bytes >= 0 AND received_total_bytes >= 0",
            name="ck_upload_sessions_bytes_nonneg",
        ),
        # Create-dedup: same key + active state ⇒ collision; the service
        # interprets the unique-violation as "return the existing session".
        Index(
            "ix_upload_sessions_idem_active_uniq",
            "idempotency_key",
            unique=True,
            postgresql_where="idempotency_key IS NOT NULL "
            "AND status IN ('awaiting_bytes','uploading','ready')",
        ),
        # Parallel-session cap + sweeper scan of a single owner's live work.
        Index(
            "ix_upload_sessions_owner_active",
            "owner_subject_id",
            "status",
            postgresql_where="status IN ('awaiting_bytes','uploading','ready')",
        ),
        # Cleanup cron: find stale / expired sessions.
        Index("ix_upload_sessions_expires", "expires_at"),
        # Cross-device discovery (same shape as jobs.scope_ids).
        Index(
            "ix_upload_sessions_scope_ids_gin",
            "scope_ids",
            postgresql_using="gin",
            postgresql_where="scope_ids IS NOT NULL",
        ),
    )


class UploadSessionFile(Base):
    __tablename__ = "upload_session_files"

    id: Mapped[uuid.UUID] = uuid_pk()
    session_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("upload_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    # 0-based position in the client's selection; also the suffix of the
    # staged key (``_ingest_jobs/{session_id}/{file_index}.bin``).
    file_index: Mapped[int] = mapped_column(Integer, nullable=False)
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    relative_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    declared_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    declared_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Staged object key (server-side only — never echoed to the client).
    s3_key: Mapped[str] = mapped_column(Text, nullable=False)
    # The per-file S3 multipart upload, created lazily on the first chunk;
    # NULL until then. Server-side only.
    s3_upload_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Receipt-ordered ``[{"PartNumber": int, "ETag": str, "size": int}, ...]``.
    parts: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    # Authoritative server-acked byte offset; the client resumes from here.
    received_offset: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        UniqueConstraint("session_id", "file_index", name="uq_upload_session_files_idx"),
        CheckConstraint(
            "status IN ('pending','uploading','staged')",
            name="ck_upload_session_files_status",
        ),
        CheckConstraint(
            "declared_size >= 0 AND received_offset >= 0",
            name="ck_upload_session_files_bytes_nonneg",
        ),
        Index("ix_upload_session_files_session", "session_id"),
    )
