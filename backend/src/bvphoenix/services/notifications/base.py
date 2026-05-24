"""Notifier abstraction — common surface across email / webhook / ics.

Each delivery channel implements the :class:`Notifier` ABC. The
dispatcher resolves a ``NotificationDispatch`` row to the right
notifier via the ``NOTIFIER_REGISTRY`` factory map, hands it the
:class:`NotificationPayload`, and stores the :class:`NotificationResult`
back on the dispatch row.

The payload is intentionally narrow and immutable: the dispatcher
fills it once at scheduling time from the source event/task + the
contact + the rendered template, and the notifier is supposed to do
nothing but ship the bytes. No DB lookup, no template re-render, no
side-effect on the dispatch row.

Storage isolation (memoria ``feedback_storage_isolation``): the
``provider_message_id`` and ``error_detail`` that come back must never
be echoed verbatim in an API response body — they may contain
provider-internal identifiers or stack traces. The dispatch row stores
them server-side; the public surface exposes only ``status`` and a
truncated ``error_code``.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class NotificationChannel(StrEnum):
    """Mirror of ``NOTIFICATION_CHANNELS`` in db/models/notifications.py.

    Kept as a StrEnum so the dispatcher can pattern-match on the value
    while still serialising to the same string the DB CHECK constraint
    expects."""

    EMAIL = "email"
    WEBHOOK_GENERIC = "webhook_generic"
    WEBHOOK_TELEGRAM = "webhook_telegram"
    WEBHOOK_WHATSAPP = "webhook_whatsapp"
    ICS_ATTACHMENT = "ics_attachment"


class NotificationKind(StrEnum):
    EVENT_REMINDER = "event_reminder"
    TASK_REMINDER = "task_reminder"
    FOLLOWUP = "followup"


class NotificationTargetKind(StrEnum):
    CLINICAL_EVENT = "clinical_event"
    PATIENT_TASK = "patient_task"


@dataclass(frozen=True, slots=True)
class NotificationPayload:
    """Frozen, audit-trail-friendly delivery descriptor.

    Holds everything a Notifier needs to ship one message AND nothing
    more — no DB session, no logger, no helper functions hanging off
    the payload. This keeps the per-notifier code testable in
    isolation and makes the payload safe to log (after redaction)
    when debugging a delivery failure.
    """

    # Routing
    patient_id: uuid.UUID
    contact_id: uuid.UUID
    target_kind: NotificationTargetKind
    target_id: uuid.UUID
    channel: NotificationChannel
    kind: NotificationKind

    # Scheduling
    locale: str
    scheduled_at: datetime
    offset_minutes: int

    # Rendered content. ``subject`` / ``body_text`` / ``body_html`` are
    # already locale-resolved + PII-allowlist-passed at this point. The
    # notifier doesn't get the chance to re-render.
    subject: str
    body_text: str
    body_html: str | None = None
    # ICS bytes when the channel is ICS_ATTACHMENT or when an email
    # notifier wants to attach a .ics — None otherwise.
    ics_blob: bytes | None = None
    ics_filename: str | None = None
    # Channel-specific routing target (the email address, the Telegram
    # chat_id, the webhook URL). The notifier reads this rather than
    # the contact row so the dispatcher can override at send time
    # (test smoke, opt-out preview).
    channel_address: str = ""
    # RFC 8058 one-click unsubscribe URL. Email notifier sets this on
    # the List-Unsubscribe header; webhook notifiers include it in the
    # JSON payload so external integrations can render it as a footer.
    opt_out_url: str | None = None

    # Provenance — for the dispatch row's author_kind column.
    author_kind: str = "system"  # "human" | "agent" | "system"
    author_subject_id: uuid.UUID | None = None

    # Stable replay key. Mirrors notification_dispatches.idempotency_key
    # so the notifier can log it for cross-correlation with the audit
    # row. The notifier does NOT use it for deduplication — the DB
    # unique constraint does that one layer up.
    idempotency_key: str = ""

    # Server-side webhook HMAC secret (decrypted from
    # ``patient_contacts.webhook_secret_encrypted``) — only the
    # GenericWebhookNotifier reads this; never returned through the
    # public API. ``None`` when the contact has no secret configured,
    # in which case the notifier sends an unsigned POST (the
    # receiver-side integration decides whether to accept it).
    webhook_secret: bytes | None = None


@dataclass(frozen=True, slots=True)
class NotificationResult:
    """Outcome of one delivery attempt.

    ``provider_message_id`` and ``error_detail`` may carry sensitive
    provider chatter — the dispatcher persists them on the dispatch row
    but the API layer never returns them on a public response body.
    """

    success: bool
    # Provider-side id for the sent message (Scaleway TEM message id,
    # Telegram update id, webhook receipt id). None on dev / failure.
    provider_message_id: str | None = None
    # Short machine-readable error code (e.g. ``smtp_timeout``,
    # ``http_502``, ``invalid_address``). Lives in
    # ``notification_dispatches.error_code`` (capped at 64 chars).
    error_code: str | None = None
    # Free-form detail. Server-side only — useful for the operator
    # debug view, NEVER echoed in HTTP responses.
    error_detail: str | None = None
    # ``True`` when the dispatcher should leave the row at
    # ``pending`` and retry on the next cron tick (typically transient
    # network failures). ``False`` for permanent errors that need
    # ``dead_letter`` (bounced address, bad webhook URL).
    retriable: bool = False
    # Metadata the dispatcher folds into the dispatch row for audit.
    metadata: dict = field(default_factory=dict)


class Notifier(ABC):
    """Channel-agnostic delivery interface.

    Each subclass binds to exactly one :class:`NotificationChannel`
    and is registered in ``NOTIFIER_REGISTRY``. The dispatcher
    instantiates one notifier per channel at startup; the instances
    are stateless beyond their configuration (settings, HTTP client),
    so a single instance handles every send for that channel.
    """

    channel: NotificationChannel

    @abstractmethod
    async def send(self, payload: NotificationPayload) -> NotificationResult:
        """Ship the payload. Must not raise — return a result with
        ``success=False`` instead so the dispatcher can persist the
        outcome cleanly."""

    @abstractmethod
    def supports(self, payload: NotificationPayload) -> bool:
        """Lightweight pre-flight check. The dispatcher calls this
        before calling :func:`send` so it can short-circuit
        impossible deliveries (missing channel address, channel-
        disabled feature flag) without paying the per-channel I/O
        cost.

        Implementations should be cheap (no network); when in doubt,
        return ``True`` and let :func:`send` produce a clean failure
        result."""


# Registry resolved at app startup. Populated by the dispatcher service
# (services/notifications/__init__.py) so the ABC stays import-cycle
# free.
NOTIFIER_REGISTRY: dict[NotificationChannel, Notifier] = {}


__all__ = [
    "NOTIFIER_REGISTRY",
    "NotificationChannel",
    "NotificationKind",
    "NotificationPayload",
    "NotificationResult",
    "NotificationTargetKind",
    "Notifier",
]
