"""1:N ``patient_contacts`` table — replaces the JSONB embedded list.

Why
---

Up to alembic 0070 every additional contact attached to a patient
(family member, caregiver, GP, etc.) lived inside the ``patients.contacts``
JSONB column as a free-form object. That worked for purely informational
contacts but had three structural limitations:

1. **GDPR audit trail per contact.** Each contact may carry independent
   personal data (email, phone) and consent state. Tracking who edited
   what, and when, on a JSONB array requires re-deriving the diff from
   the version chain — slow, brittle, and impossible to query
   relationally (e.g. "every contact who consented to be contacted in
   the last 90 days").

2. **Delegation referential integrity.** A contact can be promoted to
   a *delegate* with a ``Grant`` + ``ShareLink``. Storing the resulting
   ids inside JSONB means there's no FK back to ``grants`` /
   ``share_links``: dropping the link does not touch the JSONB row,
   and reading the row's delegation pointers requires every consumer
   to know how to decode the schema.

3. **Multi-caregiver scenarios.** A patient may legitimately have many
   contacts, and we want to be able to mark one as ``is_primary`` for
   "default contact" semantics in lists / hand-overs. JSONB ordering
   conveys that today only by convention; a real column makes it a
   first-class invariant.

Layout
------

* ``id`` — UUID PK, **preserved across migration** so existing
  delegation pointers (``share_links.recipient_*``, audit log entries)
  keep matching.
* ``patient_id`` — FK to ``patients.id`` with ``ON DELETE CASCADE`` so
  deleting the fascicolo also drops the contacts.
* ``label`` / ``relationship`` / ``email`` / ``phone`` — same fields
  the JSONB row carried.
* ``notes`` — new, free-text per-contact note (e.g. "best to call
  after 18:00").
* ``is_primary`` — boolean. A partial unique index (``WHERE is_primary
  IS TRUE``) enforces at most one primary contact per patient.
* ``consent_to_contact`` — boolean GDPR consent. Default ``False``;
  must be explicitly toggled. Future tasks can add a separate
  ``consent_recorded_at`` and audit row when this changes.
* ``delegation_subject_id`` / ``delegation_share_link_id`` /
  ``delegation_grant_id`` / ``delegation_level`` — FK columns that
  replace the JSONB stash. ``ON DELETE SET NULL`` so revoking a grant
  cleans the pointer without forcing a contact deletion.

The legacy ``patients.contacts`` JSONB column is left in place for
backward-compat reads during the soak window. New code must read /
write through this table only; ``_patient_out`` reads from here and
ignores the JSONB stash.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    LargeBinary,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from bvphoenix.db.base import Base

# Outbound notification channels supported on a PatientContact. Each
# value matches a key the backend NotificationDispatch.channel column
# accepts. We keep the catalog flat (no hierarchy) so the consent flags
# can be checked with a single column lookup.
PATIENT_CONTACT_CHANNELS: tuple[str, ...] = (
    "email",
    "webhook_generic",
    "webhook_telegram",
    "webhook_whatsapp",
    "ics_attachment",
)

# Email delivery state — driven by the Resend bounce webhook. Once
# 'bounced' or 'suppressed' the dispatcher refuses to send to that
# contact's email until an operator resets the field manually.
EMAIL_DELIVERY_STATES: tuple[str, ...] = (
    "active",
    "bounced",
    "suppressed",
    "unsubscribed",
)


class PatientContact(Base):
    """One additional contact attached to a patient. See module docstring."""

    __tablename__ = "patient_contacts"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    patient_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("patients.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    label: Mapped[str] = mapped_column(String(120), nullable=False)
    relationship_: Mapped[str | None] = mapped_column("relationship", String(80))
    email: Mapped[str | None] = mapped_column(String(255))
    phone: Mapped[str | None] = mapped_column(String(64))
    notes: Mapped[str | None] = mapped_column(Text)

    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    consent_to_contact: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )

    # ---- Outbound notification channels (migration 0107) -----------
    # ``preferred_channels``: ordered list of channels the dispatcher
    # tries (one delivery per row in ``notification_dispatches``). The
    # dispatcher honours the umbrella ``consent_to_contact`` AND the
    # per-channel consent flag below before sending — a contact with
    # consent_to_contact=False is opted out entirely.
    preferred_channels: Mapped[list | None] = mapped_column(
        JSONB, nullable=False, server_default=text("'[\"email\"]'::jsonb")
    )
    preferred_locale: Mapped[str] = mapped_column(
        String(8), nullable=False, server_default=text("'it'")
    )
    telegram_chat_id: Mapped[str | None] = mapped_column(String(64))
    whatsapp_phone: Mapped[str | None] = mapped_column(String(32))  # E.164
    webhook_url: Mapped[str | None] = mapped_column(String(512))
    # HMAC secret for webhook signature. Encrypted at rest via pgcrypto
    # ``pgp_sym_encrypt`` with ``BVP_WEBHOOK_ENCRYPTION_KEY``; reads go
    # through the dispatcher service which knows the key. Stored as raw
    # bytes so the encryption boundary stays clean.
    webhook_secret_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary)
    # Per-channel consent. Default False — the recipient must opt in.
    # Granular so a contact who wants email reminders but not
    # WhatsApp business messages doesn't have to refuse everything.
    consent_email: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    consent_telegram: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    consent_whatsapp: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    consent_webhook: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    # Email delivery health. Driven by the Resend bounce webhook +
    # one-click List-Unsubscribe header. Once 'bounced' the dispatcher
    # short-circuits before opening a connection to the provider.
    email_delivery_state: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'active'")
    )
    # Single-click opt-out token (RFC 8058). UUID generated server-side;
    # the dispatcher embeds it in every email footer / webhook payload
    # so the recipient can revoke consent without logging in.
    opt_out_token: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=False,
        server_default=text("gen_random_uuid()"),
    )

    # ---- Delegation FKs (replaces the JSONB stash) ----
    delegation_subject_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("subjects.id", ondelete="SET NULL"),
    )
    delegation_share_link_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("share_links.id", ondelete="SET NULL"),
    )
    delegation_grant_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("grants.id", ondelete="SET NULL"),
    )
    delegation_level: Mapped[str | None] = mapped_column(String(16))

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
        # At most one primary contact per patient. Partial index so
        # the constraint is silent for patients with zero or many
        # non-primary contacts.
        Index(
            "ix_patient_contacts_primary_unique",
            "patient_id",
            unique=True,
            postgresql_where="is_primary IS TRUE",
        ),
        # Opt-out token must be unique across the whole table — it's
        # the only secret the recipient holds when clicking the link.
        Index("ix_patient_contacts_opt_out_token", "opt_out_token", unique=True),
        CheckConstraint(
            "email_delivery_state IN (" + ",".join(f"'{s}'" for s in EMAIL_DELIVERY_STATES) + ")",
            name="ck_patient_contacts_email_delivery_state",
        ),
    )


__all__ = [
    "EMAIL_DELIVERY_STATES",
    "PATIENT_CONTACT_CHANNELS",
    "PatientContact",
]
