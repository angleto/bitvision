"""Webhook notifiers — generic POST + Telegram bot.

Three variants in one file because they share the HTTP scaffolding:

* :class:`GenericWebhookNotifier` — POST signed JSON to
  ``contact.webhook_url``. HMAC-SHA256 over the body with the
  contact's per-contact secret (decrypted from
  ``patient_contacts.webhook_secret_encrypted``). Header
  ``X-BV-Signature: sha256=<hex>``.
* :class:`TelegramNotifier` — POST to
  ``https://api.telegram.org/bot<TOKEN>/sendMessage``. Bot token is
  server-global (``settings.telegram_bot_token``); per-contact stores
  only the ``chat_id`` (Telegram auths the bot, not the recipient).
* :class:`WhatsAppNotifier` — stub behind a feature flag. Returns
  ``error_code='provider_disabled'`` until the operator wires a
  business API provider (Twilio / 360dialog). Lives here because it
  matches the same shape; activating it later is a one-flag flip.

Storage isolation: secrets stay server-side, response bodies from
external providers are NOT echoed in our API responses.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging

import httpx

from bvphoenix.config import get_settings
from bvphoenix.services.notifications.base import (
    NotificationChannel,
    NotificationPayload,
    NotificationResult,
    Notifier,
)

logger = logging.getLogger(__name__)


def _build_webhook_body(payload: NotificationPayload) -> dict:
    """Public JSON shape POSTed to a generic webhook endpoint.

    PII-allowlist: only fields the recipient genuinely needs to render
    a reminder. We intentionally omit the patient's full name, codice
    fiscale, diagnosis text — the recipient already knows who the
    fascicolo belongs to from the subscription context."""
    return {
        "kind": payload.kind.value,
        "locale": payload.locale,
        "subject": payload.subject,
        "body": payload.body_text,
        "target_kind": payload.target_kind.value,
        "scheduled_at": payload.scheduled_at.isoformat(),
        "offset_minutes": payload.offset_minutes,
        "opt_out_url": payload.opt_out_url,
        "idempotency_key": payload.idempotency_key,
    }


def _sign(body: bytes, secret: bytes) -> str:
    """RFC-style HMAC-SHA256 header value. Reused by webhook
    verification on the receiver side (the contact's integration)."""
    return "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()


class GenericWebhookNotifier(Notifier):
    channel = NotificationChannel.WEBHOOK_GENERIC

    def supports(self, payload: NotificationPayload) -> bool:
        settings = get_settings()
        if not settings.notifications_webhook_enabled:
            return False
        return bool(payload.channel_address)

    async def send(self, payload: NotificationPayload) -> NotificationResult:
        settings = get_settings()
        body_obj = _build_webhook_body(payload)
        body_bytes = json.dumps(body_obj, separators=(",", ":")).encode("utf-8")
        # The dispatcher decrypts the contact's
        # webhook_secret_encrypted via
        # ``services.notifications.webhook_secret.decrypt_secret`` and
        # stamps the plaintext on ``payload.webhook_secret``. When
        # absent the notifier sends an unsigned POST and the
        # receiver-side integration decides whether to accept it
        # (in production we recommend rejecting unsigned).
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "bitvision-phoenix-notifier/1.0",
            "X-BV-Notification-Kind": payload.kind.value,
            "X-BV-Idempotency-Key": payload.idempotency_key,
        }
        if payload.webhook_secret:
            headers["X-BV-Signature"] = _sign(body_bytes, payload.webhook_secret)

        try:
            async with httpx.AsyncClient(timeout=settings.webhook_timeout_seconds) as client:
                resp = await client.post(
                    payload.channel_address,
                    content=body_bytes,
                    headers=headers,
                )
        except httpx.TimeoutException:
            return NotificationResult(success=False, error_code="webhook_timeout", retriable=True)
        except httpx.HTTPError as exc:
            return NotificationResult(
                success=False,
                error_code="webhook_http_error",
                error_detail=str(exc),
                retriable=True,
            )
        if 200 <= resp.status_code < 300:
            return NotificationResult(success=True)
        # 4xx is permanent (bad URL, signature rejected by receiver);
        # 5xx is transient.
        retriable = resp.status_code >= 500
        return NotificationResult(
            success=False,
            error_code=f"webhook_http_{resp.status_code}",
            error_detail=resp.text[:512] or None,
            retriable=retriable,
        )


class TelegramNotifier(Notifier):
    channel = NotificationChannel.WEBHOOK_TELEGRAM

    def supports(self, payload: NotificationPayload) -> bool:
        settings = get_settings()
        if not settings.notifications_telegram_enabled:
            return False
        if not settings.telegram_bot_token:
            return False
        return bool(payload.channel_address)

    async def send(self, payload: NotificationPayload) -> NotificationResult:
        settings = get_settings()
        # Telegram MarkdownV2 needs escaping of reserved chars; keep
        # the body plain-text to avoid the gotcha entirely. Subject is
        # rendered bold via ``*`` markdown.
        text = f"{payload.subject}\n\n{payload.body_text}"
        if payload.opt_out_url:
            text += f"\n\nOpt-out: {payload.opt_out_url}"
        url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
        body = {"chat_id": payload.channel_address, "text": text}
        try:
            async with httpx.AsyncClient(timeout=settings.webhook_timeout_seconds) as client:
                resp = await client.post(url, json=body)
        except httpx.TimeoutException:
            return NotificationResult(success=False, error_code="telegram_timeout", retriable=True)
        except httpx.HTTPError as exc:
            return NotificationResult(
                success=False,
                error_code="telegram_http_error",
                error_detail=str(exc),
                retriable=True,
            )
        if 200 <= resp.status_code < 300:
            try:
                payload_resp = resp.json()
                message_id = str(payload_resp.get("result", {}).get("message_id", ""))
            except ValueError:
                message_id = None
            return NotificationResult(success=True, provider_message_id=message_id or None)
        retriable = resp.status_code >= 500
        return NotificationResult(
            success=False,
            error_code=f"telegram_http_{resp.status_code}",
            error_detail=resp.text[:512] or None,
            retriable=retriable,
        )


class WhatsAppNotifier(Notifier):
    channel = NotificationChannel.WEBHOOK_WHATSAPP

    def supports(self, payload: NotificationPayload) -> bool:
        # Feature flag stays false in production until the WhatsApp
        # Business API account is wired by ops.
        settings = get_settings()
        return bool(settings.notifications_whatsapp_enabled)

    async def send(self, payload: NotificationPayload) -> NotificationResult:
        # Deliberate stub. Returning a permanent error code keeps the
        # dispatcher row at ``dead_letter`` rather than retrying.
        return NotificationResult(
            success=False,
            error_code="provider_disabled",
            error_detail="WhatsApp channel is not yet provisioned; flip BVP_NOTIFICATIONS_WHATSAPP_ENABLED after the business API is approved.",
            retriable=False,
        )


__all__ = ["GenericWebhookNotifier", "TelegramNotifier", "WhatsAppNotifier"]
