"""Telegram contact-binding flow.

The user does not type their Telegram chat_id by hand — they can't,
Telegram doesn't expose it through any UI surface. Instead we mint
a single-use code, embed it in a ``t.me/<bot>?start=<code>`` deep
link, and let the bot's webhook capture the chat_id when the user
opens the link.

Pieces:

* :func:`create_link_code` — call from the "Collega Telegram" UI
  button. Returns the code + deep-link URL.
* :func:`resolve_link_code` — call from the bot webhook when
  ``/start <code>`` arrives. Stamps the contact + code row.
* :func:`get_pending_for_contact` — UI poll helper for the modal
  ("ho già collegato? il code è ancora valido?").

The bot token + bot username come from Settings (``BVP_TELEGRAM_BOT_TOKEN``
+ ``BVP_TELEGRAM_BOT_USERNAME``). The username is the @handle the
deep-link URL needs; without it we cannot build a working URL, so
the service refuses to mint codes when the username is empty.
"""

from __future__ import annotations

import logging
import secrets
import uuid
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.config import get_settings
from bvphoenix.db.models import (
    NotificationDispatch,
    PatientContact,
    TelegramLinkCode,
)

logger = logging.getLogger(__name__)

# Time the deep-link stays valid. Short on purpose: a code is the
# only secret in the flow, and 15 min is plenty for "user opens
# panel → clicks button → opens Telegram". A user who waits longer
# can press the button again to mint a fresh code.
_LINK_TTL = timedelta(minutes=15)

# URL-safe alphabet — drops the I / l / 1 / 0 / O ambiguity so a code
# read aloud over the phone has a fighting chance of round-tripping.
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789"
_CODE_LENGTH = 10


def _mint_code() -> str:
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LENGTH))


def _build_deep_link(bot_username: str, code: str) -> str:
    handle = bot_username.lstrip("@")
    return f"https://t.me/{handle}?start={code}"


async def create_link_code(
    db: AsyncSession,
    *,
    patient_id: uuid.UUID,
    contact_id: uuid.UUID,
    created_by_subject_id: uuid.UUID | None,
) -> tuple[TelegramLinkCode, str]:
    """Mint a fresh code for the given contact + return the row plus
    the deep-link URL the caller should surface in the UI.

    Side effect: invalidates any other pending codes for the same
    contact (sets ``used_at = now()`` on them) so a user who clicked
    the button twice doesn't end up with multiple live codes.
    """
    settings = get_settings()
    bot_username = getattr(settings, "telegram_bot_username", "") or ""
    if not settings.telegram_bot_token or not bot_username:
        raise RuntimeError(
            "Telegram bot is not configured (BVP_TELEGRAM_BOT_TOKEN + "
            "BVP_TELEGRAM_BOT_USERNAME required)"
        )

    # Soft-invalidate older pending codes so the user only ever has
    # one live link in flight per contact.
    now = datetime.now(UTC)
    await db.execute(
        update(TelegramLinkCode)
        .where(
            TelegramLinkCode.contact_id == contact_id,
            TelegramLinkCode.used_at.is_(None),
        )
        .values(used_at=now)
    )

    # Loop on rare collisions. The unique index would 23505 otherwise;
    # rather than catching IntegrityError mid-transaction we draw a
    # fresh code in the unlikely case the first one clashed.
    for _ in range(5):
        code = _mint_code()
        existing = (
            await db.execute(select(TelegramLinkCode.id).where(TelegramLinkCode.code == code))
        ).scalar_one_or_none()
        if existing is None:
            break
    else:
        raise RuntimeError("could not mint a unique telegram link code after 5 tries")

    row = TelegramLinkCode(
        code=code,
        patient_id=patient_id,
        contact_id=contact_id,
        created_by_subject_id=created_by_subject_id,
        expires_at=now + _LINK_TTL,
    )
    db.add(row)
    await db.flush()
    return row, _build_deep_link(bot_username, code)


async def get_pending_for_contact(
    db: AsyncSession, contact_id: uuid.UUID
) -> TelegramLinkCode | None:
    """Return the most-recent unused code for ``contact_id`` that
    hasn't expired yet. The UI polls this to decide when to dismiss
    the "open Telegram" modal — the moment ``used_at`` flips, the
    bind succeeded."""
    now = datetime.now(UTC)
    return (
        await db.execute(
            select(TelegramLinkCode)
            .where(
                TelegramLinkCode.contact_id == contact_id,
                TelegramLinkCode.used_at.is_(None),
                TelegramLinkCode.expires_at > now,
            )
            .order_by(TelegramLinkCode.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def get_last_for_contact(db: AsyncSession, contact_id: uuid.UUID) -> TelegramLinkCode | None:
    """Most-recent code regardless of status. UI uses this to render
    the "Telegram collegato" badge after a successful redemption."""
    return (
        await db.execute(
            select(TelegramLinkCode)
            .where(TelegramLinkCode.contact_id == contact_id)
            .order_by(TelegramLinkCode.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def resolve_link_code(
    db: AsyncSession,
    *,
    code: str,
    telegram_chat_id: str,
) -> TelegramLinkCode | None:
    """Bot webhook entry. Looks up the code, copies the chat_id onto
    the matching PatientContact, stamps used_at on the link row.

    Returns the redeemed link row on success, ``None`` when:
    - the code is unknown
    - the code has been redeemed already
    - the code has expired

    The caller (bot webhook) uses the return value to decide which
    confirmation message to send back to the user.
    """
    now = datetime.now(UTC)
    row = (
        await db.execute(select(TelegramLinkCode).where(TelegramLinkCode.code == code))
    ).scalar_one_or_none()
    if row is None or row.used_at is not None:
        return None
    if row.expires_at <= now:
        return None
    contact = (
        await db.execute(select(PatientContact).where(PatientContact.id == row.contact_id))
    ).scalar_one_or_none()
    if contact is None:
        return None
    contact.telegram_chat_id = telegram_chat_id
    # Default to opt-in on linking — the operator can revoke later.
    # The flip is intentional here: a user who actively typed /start
    # to a code we minted has clearly consented to the channel.
    contact.consent_telegram = True
    row.used_at = now
    row.telegram_chat_id = telegram_chat_id
    await db.flush()
    return row


# ---------------------------------------------------------------------------
# Bot-side helpers — wraps the Telegram HTTP API for the bot webhook
# confirmation message + the optional setWebhook bootstrap call.
# ---------------------------------------------------------------------------


async def send_telegram_message(chat_id: str, text: str, *, parse_mode: str | None = None) -> bool:
    """POST to ``sendMessage``. Returns True on 2xx. Used by the bot
    webhook to acknowledge a successful ``/start <code>`` redemption
    and by ``send_test_notification`` for smoke tests."""
    settings = get_settings()
    if not settings.telegram_bot_token:
        logger.warning("send_telegram_message called without BVP_TELEGRAM_BOT_TOKEN")
        return False
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    body: dict = {"chat_id": chat_id, "text": text}
    if parse_mode:
        body["parse_mode"] = parse_mode
    try:
        async with httpx.AsyncClient(timeout=settings.webhook_timeout_seconds) as client:
            resp = await client.post(url, json=body)
        return 200 <= resp.status_code < 300
    except httpx.HTTPError:
        logger.exception("telegram sendMessage failed for chat_id=%s", chat_id)
        return False


async def set_telegram_webhook(public_url: str) -> tuple[bool, str]:
    """Call ``setWebhook`` on the Telegram bot. Idempotent. Returns
    ``(ok, description)`` so the caller can surface the upstream
    response. Operator setup: deploy once, point Telegram at
    ``https://api.bitvision.example/api/notifications/telegram/webhook``.
    """
    settings = get_settings()
    if not settings.telegram_bot_token:
        return False, "BVP_TELEGRAM_BOT_TOKEN not set"
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/setWebhook"
    body = {"url": public_url}
    secret = getattr(settings, "telegram_webhook_secret", "") or ""
    if secret:
        # Telegram verifies our endpoint by echoing this token in the
        # ``X-Telegram-Bot-Api-Secret-Token`` header on every update.
        body["secret_token"] = secret
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json=body)
            payload = resp.json()
        return bool(payload.get("ok")), str(payload.get("description", ""))
    except httpx.HTTPError as exc:
        return False, str(exc)


# ---------------------------------------------------------------------------
# Channel-cancellation helper used when the user "unlinks" Telegram.
# ---------------------------------------------------------------------------


async def unlink_telegram(db: AsyncSession, contact: PatientContact) -> None:
    """Clear the Telegram binding on a contact. Also cancels any
    pending telegram notification_dispatches so the dispatcher
    doesn't try to use a stale chat_id."""
    contact.telegram_chat_id = None
    contact.consent_telegram = False
    await db.execute(
        update(NotificationDispatch)
        .where(
            NotificationDispatch.contact_id == contact.id,
            NotificationDispatch.channel == "webhook_telegram",
            NotificationDispatch.status == "pending",
        )
        .values(status="cancelled", error_code="telegram_unlinked")
    )


__all__ = [
    "create_link_code",
    "get_last_for_contact",
    "get_pending_for_contact",
    "resolve_link_code",
    "send_telegram_message",
    "set_telegram_webhook",
    "unlink_telegram",
]
