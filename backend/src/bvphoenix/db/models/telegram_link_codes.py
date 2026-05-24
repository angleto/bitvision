"""Single-use linking codes for the Telegram contact-channel bind flow.

Telegram does not let BitVision look up a user's ``chat_id`` from a
phone number or username — the only way to obtain it is to receive
a message from that user. So binding a Telegram identity to a
PatientContact runs through a deep-link dance:

1. Operator clicks "Collega Telegram" on the PatientContactsPanel
   for a contact.
2. Backend mints a ``TelegramLinkCode`` row (random 10-char URL-safe
   alphanumeric code, 15 min TTL, single-use).
3. Frontend shows a ``https://t.me/<bot>?start=<code>`` link / QR.
4. The recipient opens the link; Telegram delivers ``/start <code>``
   to our bot.
5. Bot webhook (``POST /api/notifications/telegram/webhook``) looks
   up the code, copies the ``chat_id`` onto the PatientContact,
   stamps ``used_at`` on this row, and posts a confirmation back to
   the user.

The table is intentionally narrow: the only secret it carries is
the code itself, which is single-use + short-lived. Once redeemed
the row is kept for audit (we know who linked which chat_id when)
but its code is dead — re-redeeming fails on ``used_at IS NOT NULL``.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from bvphoenix.db.base import Base


class TelegramLinkCode(Base):
    __tablename__ = "telegram_link_codes"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    code: Mapped[str] = mapped_column(String(32), nullable=False)
    patient_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("patients.id", ondelete="CASCADE"),
        nullable=False,
    )
    contact_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("patient_contacts.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Mints via :func:`services.notifications.telegram_link.create_code`
    # so we know who pressed the "Collega" button.
    created_by_subject_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Stamped by the bot webhook on successful redemption. Once set
    # the code is dead.
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The chat_id the bot resolved from the ``/start`` message.
    # Server-side only; the public API surface returns the channel
    # status ("linked" / "pending" / "expired") not the raw id.
    telegram_chat_id: Mapped[str | None] = mapped_column(String(64))

    __table_args__ = (
        # Codes must be globally unique while live so the deep-link
        # is unambiguous.
        Index("ix_telegram_link_codes_code_unique", "code", unique=True),
        # Per-contact lookup for the "do I have a pending link?" UI poll.
        Index(
            "ix_telegram_link_codes_contact_pending",
            "contact_id",
            postgresql_where=text("used_at IS NULL"),
        ),
        CheckConstraint(
            # Codes are URL-safe base32 strings (no I / O / 0 / 1 ambiguity)
            # produced by ``secrets.choice`` over a 32-char alphabet.
            "length(code) BETWEEN 8 AND 32",
            name="ck_telegram_link_codes_length",
        ),
    )


__all__ = ["TelegramLinkCode"]
