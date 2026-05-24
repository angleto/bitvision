"""Idempotency-Key cache table.

ADR 0002 — every mutating endpoint that opts in to idempotency captures
the response on first execution and replays it byte-identically on a
subsequent request that carries the same ``Idempotency-Key`` *and* the
same body hash. ``dry_run`` is part of the body hash, so a preview and
its real-apply are tracked as two distinct cache entries.

Records expire after 24h by default and are reaped by a cron job; the
``expires_at`` index lets the reaper sweep without a full scan. The
table is keyed on ``(idempotency_key, request_hash)`` to support the
"key reused with different body" case: same key + different hash → 422
``idempotency_conflict``.

Schema fields:

* ``idempotency_key`` — opaque UUID/string supplied by the client.
* ``request_hash`` — SHA-256 hex of the canonical request envelope
  (method, path, body, dry-run flag).
* ``actor_subject_id`` — who minted the entry (audit only; the cache
  is keyed on the request shape, not the actor).
* ``response_status`` — captured HTTP status (``201``, ``200``, ``409``,
  ``422``, …). Replay returns this verbatim.
* ``response_body`` — captured response body, JSONB. ``NULL`` when the
  response was empty (``204``).
* ``response_headers`` — captured headers worth replaying (``ETag``,
  ``Location``). Stored as JSONB to keep the column serialisable.
* ``created_at`` / ``expires_at`` — record lifecycle timestamps.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, PrimaryKeyConstraint, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from bvphoenix.db.base import Base


class IdempotencyRecord(Base):
    """Replay cache for ``Idempotency-Key`` mutations.

    The PK is composite — same key with a different request hash is
    NOT a duplicate and is allowed to coexist (the API surface treats
    that mismatch as a 422 ``idempotency_conflict`` at runtime, before
    the row would even be inserted).
    """

    __tablename__ = "idempotency_records"

    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_subject_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("subjects.id", ondelete="SET NULL"),
    )
    method: Mapped[str] = mapped_column(String(8), nullable=False)
    path: Mapped[str] = mapped_column(String(512), nullable=False)
    response_status: Mapped[int] = mapped_column(Integer, nullable=False)
    response_body: Mapped[dict | list | None] = mapped_column(JSONB, nullable=True)
    response_headers: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        PrimaryKeyConstraint("idempotency_key", "request_hash", name="pk_idempotency_records"),
        Index("ix_idempotency_expires", "expires_at"),
        Index("ix_idempotency_actor", "actor_subject_id", "created_at"),
    )
