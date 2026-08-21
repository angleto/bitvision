"""NotificationDispatch — audit + scheduling row for outbound reminders.

Each row tracks one delivery attempt by the dispatcher worker. The
``(target_kind, target_id, contact_id, channel, offset_minutes)`` tuple
encodes "remind contact X about clinical event / task Y, N minutes
before the anchor, via channel C" — a deterministic plan that the
scheduler materialises ahead of time so the calendar of pending
notifications is auditable + cancellable + replayable.

Why pre-materialise instead of computing at dispatch time
---------------------------------------------------------

Two practical wins:

1. **Concurrency safe**. The post-commit listener that watches event /
   task writes inserts one row per ``(target, contact, offset,
   channel)``. The UNIQUE on ``idempotency_key`` makes the insert
   idempotent: re-running the listener after a transient failure
   does not duplicate rows.

2. **Visible plan**. The owner of a fascicolo (or the agent that
   manages it on her behalf) can read ``notification_dispatches``
   and see exactly what is queued, when, on which channel, to whom.
   Without pre-materialisation that information would only exist as
   side-effects of cron firings.

The dispatcher worker (``workers/tasks/dispatch_notification.py``)
moves each row through ``pending → sent | failed | dead_letter |
cancelled``. The safety-net cron requeues stuck rows past their
scheduled time.

Cross-patient inexpressibility
------------------------------

Like every v3 audit row the dispatch references a ``patient_id`` and
a ``contact_id``; we don't carry a composite FK on the contact (the
``patient_contacts`` PK is ``id`` UUID, unique already), but the
service layer always inserts rows under the patient_id of the
originating event / task and the dispatcher re-checks the contact
belongs to that patient before sending.
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
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from bvphoenix.db.base import Base

# Outbound channels the dispatcher can resolve. Mirrors
# ``PATIENT_CONTACT_CHANNELS`` in db/models/patient_contacts.py so the
# scheduler picks compatible channels for a contact at materialisation
# time. We keep two enums in sync (rather than referencing one another)
# so a renaming or temporary blackout of one set does not cascade.
NOTIFICATION_CHANNELS: tuple[str, ...] = (
    "email",
    "webhook_generic",
    "webhook_telegram",
    "webhook_whatsapp",
    "ics_attachment",
)

# What's being reminded. Driven by the source table: clinical_events
# emits ``event_reminder``; patient_tasks emits ``task_reminder``;
# the experimental "follow-up survey" surface (post-event check-in)
# uses ``followup``.
NOTIFICATION_KINDS: tuple[str, ...] = (
    "event_reminder",
    "task_reminder",
    "followup",
)

# Source artefact the dispatch reminds about. Cross-references either
# clinical_events.id or patient_tasks.id; we don't enforce the FK at
# the DB level because the worker tolerates the underlying row being
# deleted between scheduling and firing (it then marks the dispatch
# ``cancelled``).
NOTIFICATION_TARGET_KINDS: tuple[str, ...] = (
    "clinical_event",
    "patient_task",
)

# Worker-state lifecycle. Driven by the dispatcher (``pending → sent
# | failed | dead_letter``) + safety-net cron (``pending → cancelled``
# on consent revocation) + admin tools (``failed → pending`` on a
# manual reset).
NOTIFICATION_STATUSES: tuple[str, ...] = (
    "pending",
    "sent",
    "failed",
    "dead_letter",
    "cancelled",
)


class NotificationDispatch(Base):
    __tablename__ = "notification_dispatches"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    patient_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("patients.id", ondelete="CASCADE"),
        nullable=False,
    )
    # FK on patient_contacts.id — ON DELETE CASCADE so removing a
    # contact wipes their queued reminders rather than leaving them
    # firing to a forwarded address that may belong to someone else.
    contact_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("patient_contacts.id", ondelete="CASCADE"),
        nullable=False,
    )
    target_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    target_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    offset_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    locale: Mapped[str] = mapped_column(String(8), nullable=False, server_default=text("'it'"))
    # Idempotency anchor: sha256 of (target_id, contact_id, offset,
    # channel, anchor instant). The post-commit listener computes the
    # same key on every re-fire so a duplicate insert is a clean
    # ON CONFLICT no-op. The anchor is part of the hash so that a MOVED
    # appointment yields a different reminder rather than colliding with
    # the one just cancelled for the old slot.
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'pending'")
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    # ID returned by the SMTP / webhook provider on success. Kept
    # server-side only; never echoed to the API response body (the
    # storage-isolation memo applies to outbound endpoints too).
    provider_message_id: Mapped[str | None] = mapped_column(String(255))
    error_code: Mapped[str | None] = mapped_column(String(64))
    # Operator-facing expansion of ``error_code`` (host, port, resolved
    # addresses, server reply). NotificationResult has carried this
    # field all along but nothing persisted it, so every failure
    # collapsed to an opaque code. Never echoed on the public API.
    error_detail: Mapped[str | None] = mapped_column(Text)
    # Who scheduled the dispatch — typically the human / agent that
    # created or last edited the source event / task.
    author_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    author_subject_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    # Arq job id, set when the safety-net cron or the post-commit
    # listener enqueues the dispatch. Useful to correlate logs.
    arq_job_id: Mapped[str | None] = mapped_column(String(64))
    # Free-form context for the template engine. Locked down to a
    # PII-safe allowlist at render time (see services/notifications/
    # templates_engine.py).
    template_context: Mapped[dict | None] = mapped_column(JSONB)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "channel IN (" + ",".join(f"'{c}'" for c in NOTIFICATION_CHANNELS) + ")",
            name="ck_notification_dispatches_channel",
        ),
        CheckConstraint(
            "kind IN (" + ",".join(f"'{k}'" for k in NOTIFICATION_KINDS) + ")",
            name="ck_notification_dispatches_kind",
        ),
        CheckConstraint(
            "target_kind IN (" + ",".join(f"'{k}'" for k in NOTIFICATION_TARGET_KINDS) + ")",
            name="ck_notification_dispatches_target_kind",
        ),
        CheckConstraint(
            "status IN (" + ",".join(f"'{s}'" for s in NOTIFICATION_STATUSES) + ")",
            name="ck_notification_dispatches_status",
        ),
        CheckConstraint(
            "author_kind IN ('human','agent','system')",
            name="ck_notification_dispatches_author_kind",
        ),
        # Idempotency: same key on the same (target, contact, offset,
        # channel) is a replay — the post-commit listener uses ON
        # CONFLICT DO NOTHING when materialising rows on event edits.
        # Partial UNIQUE (migration 0047): a CANCELLED dispatch releases
        # its key. Every re-scheduling path cancels the pending reminders
        # and re-materialises them, so a globally-unique key meant the
        # rebuilt row was silently swallowed by the ON CONFLICT and the
        # patient got no reminder for the moved appointment.
        Index(
            "uq_notification_dispatches_idem",
            "idempotency_key",
            unique=True,
            postgresql_where=text("status <> 'cancelled'"),
        ),
        # Patient-scoped lookup for the "what's queued for this
        # fascicolo" admin / opt-out view.
        Index(
            "ix_notification_dispatches_patient_scheduled",
            "patient_id",
            "scheduled_at",
        ),
        # The dispatcher cron's hot path: find pending rows whose
        # scheduled_at is past now() minus the safety-net grace
        # window. Partial index so the planner doesn't read the long
        # tail of ``sent`` rows.
        Index(
            "ix_notification_dispatches_pending_scheduled",
            "scheduled_at",
            postgresql_where=text("status = 'pending'"),
        ),
        # Per-contact wipe + opt-out token reverse lookup.
        Index("ix_notification_dispatches_contact", "contact_id"),
    )


__all__ = [
    "NOTIFICATION_CHANNELS",
    "NOTIFICATION_KINDS",
    "NOTIFICATION_STATUSES",
    "NOTIFICATION_TARGET_KINDS",
    "NotificationDispatch",
]
