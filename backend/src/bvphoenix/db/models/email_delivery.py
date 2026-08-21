"""EmailDelivery — one durable row per message that leaves the process.

Why this exists
---------------

On 2026-07-31 outbound email failed for hours because the configured
SMTP port was blackholed. Nothing recorded it. The share-link notify
endpoint answered HTTP 200, wrote a ``share_email_sent`` audit row, and
the only trace of the failure was a log line inside a pod that rotates.
The messages were simply gone, with no queue to inspect and no way to
replay them once the transport was fixed.

``notification_dispatches`` already models delivery state, but it is
patient-scoped by construction (``patient_id`` and ``contact_id`` are
both NOT NULL with FKs) so it cannot represent a share invitation to an
external clinician, a password reset, or a verification link. Those are
exactly the three paths that had no persistence at all.

This table is the transport-level ledger for *every* outbound message.
It answers one question the system previously could not: "did this
message actually reach the relay, and if not, why, and will it be
retried?"

What is deliberately NOT stored
-------------------------------

The rendered MIME body. A message is rebuilt from its ``purpose`` plus
its foreign keys at attempt time, so the ledger never becomes a second
store of PHI. For ``share_invitation`` that means ``share_link_id`` is
sufficient; for the auth purposes, ``subject_id`` plus a freshly minted
token. A row is therefore safe to keep for the retention window and
safe to expose (minus ``error_detail``) to the owning user.

Relationship to notification_dispatches
---------------------------------------

The reminder path keeps its dispatch row as the *scheduling* record and
gains a ledger row as the *transport* record, linked by
``notification_dispatch_id``. Retry of the transport belongs here; the
dispatcher stops owning it. That split fixes a real defect: the
dispatcher requeued without touching ``scheduled_at`` against a cron
that fires every 5 minutes, so its 4-attempt budget burned out in 15
minutes and left rows in ``failed`` with no code path back out.
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
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from bvphoenix.db.base import Base

# What the message is for. Drives which builder reconstructs the MIME
# payload at attempt time, so adding a purpose means adding a builder.
EMAIL_DELIVERY_PURPOSES: tuple[str, ...] = (
    "share_invitation",
    "password_reset",
    "email_verification",
    "notification",
    "probe",
)

# ``queued``      — owed, eligible once next_attempt_at passes
# ``sent``        — the relay accepted it
# ``failed``      — retry budget exhausted, or a non-retriable refusal
# ``dead_letter`` — operator-parked; never auto-retried
EMAIL_DELIVERY_STATUSES: tuple[str, ...] = (
    "queued",
    "sent",
    "failed",
    "dead_letter",
)


class EmailDelivery(Base):
    __tablename__ = "email_deliveries"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    purpose: Mapped[str] = mapped_column(String(32), nullable=False)
    recipient_email: Mapped[str] = mapped_column(String(255), nullable=False)
    # Kept for operator triage in the ledger UI. Subject lines of the
    # transactional templates are fixed strings, not PHI.
    subject_line: Mapped[str | None] = mapped_column(String(255))
    locale: Mapped[str] = mapped_column(String(8), nullable=False, server_default=text("'it'"))

    # Reconstruction anchors. All nullable: a given purpose uses one.
    # ON DELETE SET NULL rather than CASCADE — deleting a share link
    # must not erase the evidence that we tried to email someone.
    share_link_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("share_links.id", ondelete="SET NULL")
    )
    subject_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("subjects.id", ondelete="SET NULL")
    )
    notification_dispatch_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("notification_dispatches.id", ondelete="CASCADE")
    )

    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'queued'"))
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("6"))
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # Discriminated code from services/email.py (ERROR_* constants).
    # Safe to echo to the API; ``error_detail`` is operator-only since
    # it names hosts, ports and resolved addresses.
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_detail: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "purpose IN (" + ",".join(f"'{p}'" for p in EMAIL_DELIVERY_PURPOSES) + ")",
            name="ck_email_deliveries_purpose",
        ),
        CheckConstraint(
            "status IN (" + ",".join(f"'{s}'" for s in EMAIL_DELIVERY_STATUSES) + ")",
            name="ck_email_deliveries_status",
        ),
        CheckConstraint(
            "attempt_count >= 0 AND max_attempts > 0",
            name="ck_email_deliveries_attempts_sane",
        ),
        # The drain query is "queued rows whose time has come, oldest
        # first". A partial index keeps it proportional to the backlog
        # rather than to the (unbounded, mostly 'sent') table.
        Index(
            "ix_email_deliveries_due",
            "next_attempt_at",
            postgresql_where=text("status = 'queued'"),
        ),
        Index("ix_email_deliveries_recipient", "recipient_email"),
        Index("ix_email_deliveries_share_link", "share_link_id"),
    )


__all__ = [
    "EMAIL_DELIVERY_PURPOSES",
    "EMAIL_DELIVERY_STATUSES",
    "EmailDelivery",
]
