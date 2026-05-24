"""Email notifier — uses the existing SmtpEmailSender (Scaleway TEM).

Production sends through ``smtp.tem.scaleway.com``; dev falls back to
``DevEmailSender`` which appends to ``logs/dev_emails.eml`` and prints
to stdout. The choice is driven by Settings (``email_provider`` /
``smtp_host``) so this notifier stays oblivious — it just hands the
message to :func:`send_email_sync` and translates the boolean result
into a :class:`NotificationResult`.

Storage isolation: we DO NOT include the provider message id in the
NotificationResult metadata that ends up on the public API surface;
it's kept inside the dispatch row for server-side correlation only.

RFC 8058 one-click unsubscribe + ``List-Unsubscribe`` header pair are
emitted whenever ``payload.opt_out_url`` is set, so a Gmail / Outlook
recipient gets the native "Unsubscribe" affordance.
"""

from __future__ import annotations

import logging

from bvphoenix.config import get_settings
from bvphoenix.services.email import (
    EmailAttachment,
    EmailMessage,
    send_email_sync,
)
from bvphoenix.services.notifications.base import (
    NotificationChannel,
    NotificationPayload,
    NotificationResult,
    Notifier,
)

logger = logging.getLogger(__name__)


class EmailNotifier(Notifier):
    channel = NotificationChannel.EMAIL

    def supports(self, payload: NotificationPayload) -> bool:
        settings = get_settings()
        if not settings.notifications_email_enabled:
            return False
        if not payload.channel_address:
            return False
        # ICS attachments are routed through the dedicated channel
        # below, not through plain email.
        return payload.channel != NotificationChannel.ICS_ATTACHMENT

    async def send(self, payload: NotificationPayload) -> NotificationResult:
        attachments: tuple[EmailAttachment, ...] = ()
        if payload.ics_blob and payload.ics_filename:
            attachments = (
                EmailAttachment(
                    filename=payload.ics_filename,
                    # ``method=REQUEST`` tells the recipient's calendar
                    # to treat the file as an invite rather than a
                    # publish-only feed.
                    mime_type="text/calendar; method=REQUEST",
                    data=payload.ics_blob,
                ),
            )

        message = EmailMessage(
            to=payload.channel_address,
            subject=payload.subject,
            body_text=payload.body_text,
            body_html=payload.body_html,
            attachments=attachments,
            list_unsubscribe_url=payload.opt_out_url,
            # ``Precedence: bulk`` + ``Auto-Submitted: auto-generated``
            # are the two headers RFC 3834 / Microsoft EOP recommend
            # for bulk auto-mail so it's classified correctly and
            # vacation auto-responders don't bounce back at us.
            extra_headers=(
                ("Precedence", "bulk"),
                ("Auto-Submitted", "auto-generated"),
                ("X-BV-Notification-Kind", payload.kind.value),
                ("X-BV-Idempotency-Key", payload.idempotency_key),
            ),
            list_unsubscribe_post_url=payload.opt_out_url if payload.opt_out_url else None,
        )
        ok = send_email_sync(message)
        if ok:
            return NotificationResult(
                success=True,
                # send_email_sync doesn't surface the SMTP server's
                # message id back to us; that's fine for production
                # since Scaleway TEM exposes the message log in its
                # dashboard. If we ever need correlation, switch to
                # the TEM HTTP API (returns a UUID).
                provider_message_id=None,
            )
        # send_email_sync logs the exception internally; we don't have
        # a structured error code to attach. Treat as retriable —
        # SMTP failures are typically transient, and the safety-net
        # cron will re-fire the dispatch on the next tick.
        return NotificationResult(
            success=False,
            error_code="smtp_send_failed",
            retriable=True,
        )


__all__ = ["EmailNotifier"]
