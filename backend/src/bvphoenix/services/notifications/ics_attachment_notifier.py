"""ICS-attachment notifier — calendar invite delivered via email.

This is the "light channel" the user picked in the planning sessions:
the recipient imports a single .ics file once and the VALARM blocks
inside fire reminders locally on their calendar app. No server-side
push from BitVision past the initial delivery, no Resend/Telegram
infrastructure, no per-event polling.

Semantically distinct from the plain EmailNotifier:

* Refuses to send when ``payload.ics_blob`` is empty (the channel
  exists precisely to deliver the calendar payload).
* Uses an email subject that hints at the calendar nature even when
  the recipient's mail client doesn't unpack the attachment inline.
* Routes through the same SMTP relay (Scaleway TEM) as the plain
  EmailNotifier — the difference is the payload shape, not the
  transport.

Storage isolation: same posture as EmailNotifier.
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


class ICSAttachmentNotifier(Notifier):
    channel = NotificationChannel.ICS_ATTACHMENT

    def supports(self, payload: NotificationPayload) -> bool:
        settings = get_settings()
        if not settings.notifications_email_enabled:
            return False
        if not payload.channel_address:
            return False
        return bool(payload.ics_blob and payload.ics_filename)

    async def send(self, payload: NotificationPayload) -> NotificationResult:
        assert payload.ics_blob is not None
        assert payload.ics_filename is not None

        message = EmailMessage(
            to=payload.channel_address,
            subject=payload.subject,
            body_text=payload.body_text,
            body_html=payload.body_html,
            attachments=(
                EmailAttachment(
                    filename=payload.ics_filename,
                    mime_type="text/calendar; method=REQUEST",
                    data=payload.ics_blob,
                ),
            ),
            list_unsubscribe_url=payload.opt_out_url,
            list_unsubscribe_post_url=payload.opt_out_url if payload.opt_out_url else None,
            extra_headers=(
                ("Precedence", "bulk"),
                ("Auto-Submitted", "auto-generated"),
                ("X-BV-Notification-Kind", payload.kind.value),
                ("X-BV-Notification-Channel", "ics_attachment"),
                ("X-BV-Idempotency-Key", payload.idempotency_key),
            ),
        )
        ok = send_email_sync(message)
        if ok:
            return NotificationResult(success=True)
        return NotificationResult(success=False, error_code="ics_email_send_failed", retriable=True)


__all__ = ["ICSAttachmentNotifier"]
