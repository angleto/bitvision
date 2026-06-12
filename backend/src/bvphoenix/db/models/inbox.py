"""Patient inbound inbox — addresses, raw emails, reviewable items.

The patient inbox (task fbbf5270) is the *patient-private* consumer of
the shared review/staging engine (``services/review_queue``): external
senders (a lab, a clinic's front desk) mail attachments to a
capability-style address ``{code}+patient@<inbox domain>``; the system
stages the parts, runs the auto-checks and queues one
:class:`InboxItem` per message (granularity = the *lot*, with a
per-component manifest) for the owner — or an authorised agent — to
accept into the fascicolo or reject.

Four tables:

* :class:`PatientInboxAddress` — rotatable/revocable bearer capability.
  The ``code`` is an opaque ~80-bit Crockford base32 string, never the
  patient id; a patient may hold several (one per sender, like
  app-specific passwords) so revoking one does not burn the others.
* :class:`InboundEmail` — one row per accepted SMTP message: envelope +
  selected headers + authentication results + a pointer to the raw
  ``.eml`` on S3. PHI-preserving store (this is the patient's own
  fascicolo perimeter), retention-bounded by
  ``BVP_INBOUND_EMAIL_RAW_RETENTION_DAYS``.
* :class:`InboxItem` — the reviewable lot (``ReviewableItemMixin``).
  Exactly one ingress reference: ``inbound_email_id`` XOR
  ``upload_job_id`` (a held bulk-upload job awaiting review). The
  component manifest (filenames, sizes, hashes, av results, routing)
  lives in ``manifest`` JSONB; the staged blobs under ``staged_prefix``
  on S3, *outside* the canonical fascicolo keyspace.
* :class:`InboxSenderAllowlist` — opt-in trusted senders for
  auto-accept (only honoured with SPF/DKIM alignment, never the bare
  ``From``).

Cross-patient inexpressibility: every table carries
``UNIQUE(patient_id, id)`` and every intra-inbox reference is a
composite FK ``(patient_id, …)``, so a cross-patient link is a
constraint violation, not a bug to catch in review.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from bvphoenix.db.base import Base
from bvphoenix.db.models._common import TimestampMixin, UpdatedAtMixin, uuid_pk
from bvphoenix.services.review_queue.store import ReviewableItemMixin

# Ingress channels an InboxItem can arrive through. ``email`` items
# reference an InboundEmail; ``upload_ui`` / ``upload_mcp`` items
# reference a held bulk-upload Job (created but not enqueued until the
# reviewer accepts).
INBOX_SOURCE_CHANNELS: tuple[str, ...] = ("email", "upload_ui", "upload_mcp")


class PatientInboxAddress(Base, TimestampMixin):
    __tablename__ = "patient_inbox_addresses"

    id: Mapped[uuid.UUID] = uuid_pk()
    patient_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("patients.id", ondelete="CASCADE"),
        nullable=False,
    )
    # The bearer capability. Globally unique so RCPT routing is a
    # single lookup; opaque (Crockford base32, ~80 bits via
    # ``services/inbox/codes.py``) so it reveals nothing about the
    # patient and withstands online enumeration.
    code: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    # Sub-address tag (the ``+patient`` part). Fixed per deployment via
    # ``BVP_INBOUND_EMAIL_TAG`` but stored per-row so rotating the
    # deployment tag does not orphan parked addresses.
    tag: Mapped[str] = mapped_column(String(32), nullable=False, server_default="patient")
    # Human label ("Laboratorio Rossi") so the owner can tell their
    # addresses apart and revoke the right one.
    label: Mapped[str | None] = mapped_column(String(200))
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    created_by_subject_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("subjects.id", ondelete="SET NULL"),
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoke_reason: Mapped[str | None] = mapped_column(Text)
    etag: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=False,
        server_default=text("gen_random_uuid()"),
    )

    __table_args__ = (
        UniqueConstraint("patient_id", "id", name="uq_patient_inbox_addresses_patient_id"),
        # A revoked address must record when; an active one must not.
        CheckConstraint(
            "(active AND revoked_at IS NULL) OR (NOT active AND revoked_at IS NOT NULL)",
            name="ck_patient_inbox_addresses_revoked",
        ),
        Index("ix_patient_inbox_addresses_patient", "patient_id"),
    )


class InboundEmail(Base, TimestampMixin):
    __tablename__ = "inbound_emails"

    id: Mapped[uuid.UUID] = uuid_pk()
    patient_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("patients.id", ondelete="CASCADE"),
        nullable=False,
    )
    inbox_address_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    # RFC 5322 Message-ID, deduped *per address* (the same message
    # legitimately lands on two patients' addresses when CC'd).
    message_id: Mapped[str | None] = mapped_column(String(998))
    from_address: Mapped[str | None] = mapped_column(String(320))
    to_address: Mapped[str | None] = mapped_column(String(320))
    subject: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Authentication-Results signals (pass/fail/none/...). Signals for
    # the reviewer and the auto-accept gate, never a hard SMTP gate.
    spf_result: Mapped[str | None] = mapped_column(String(16))
    dkim_result: Mapped[str | None] = mapped_column(String(16))
    dmarc_result: Mapped[str | None] = mapped_column(String(16))
    # Raw ``.eml`` on S3 (retention-bounded) + content hash for dedup
    # of identical re-sends that lack a Message-ID.
    raw_s3_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    raw_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    raw_size_bytes: Mapped[int | None] = mapped_column()
    etag: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=False,
        server_default=text("gen_random_uuid()"),
    )

    __table_args__ = (
        UniqueConstraint("patient_id", "id", name="uq_inbound_emails_patient_id"),
        # Same-patient by construction: the address this email landed on.
        ForeignKeyConstraint(
            ["patient_id", "inbox_address_id"],
            ["patient_inbox_addresses.patient_id", "patient_inbox_addresses.id"],
            name="fk_inbound_emails_address",
            ondelete="CASCADE",
        ),
        UniqueConstraint("inbox_address_id", "message_id", name="uq_inbound_emails_message_id"),
        Index("ix_inbound_emails_patient", "patient_id"),
        Index("ix_inbound_emails_address", "inbox_address_id"),
    )


class InboxItem(Base, ReviewableItemMixin, TimestampMixin, UpdatedAtMixin):
    __tablename__ = "inbox_items"

    id: Mapped[uuid.UUID] = uuid_pk()
    patient_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("patients.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Exactly one ingress reference (CHECK below): an inbound email or
    # a held bulk-upload job. Jobs carry no patient column (scope lives
    # in their canonical input), so that edge is a plain FK and the
    # service layer asserts the job's patient matches at item creation.
    inbound_email_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    upload_job_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("jobs.id", ondelete="SET NULL"),
    )
    source_channel: Mapped[str] = mapped_column(String(16), nullable=False)
    # Staged component blobs live under this S3 prefix (raw bucket,
    # ``_inbox/...``), outside the canonical fascicolo keyspace, until
    # the item is promoted or purged.
    staged_prefix: Mapped[str | None] = mapped_column(String(1024))
    # Per-component manifest: [{name, s3_key, size_bytes, content_type,
    # sha256, routed_as, ...}]. The auto-check results live in the
    # mixin's ``auto_checks``; promotion outcomes in ``promoted_refs``.
    manifest: Mapped[dict | None] = mapped_column(JSONB)
    promoted_refs: Mapped[dict | None] = mapped_column(JSONB)

    __table_args__ = (
        UniqueConstraint("patient_id", "id", name="uq_inbox_items_patient_id"),
        ForeignKeyConstraint(
            ["patient_id", "inbound_email_id"],
            ["inbound_emails.patient_id", "inbound_emails.id"],
            name="fk_inbox_items_inbound_email",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "(inbound_email_id IS NOT NULL)::int + (upload_job_id IS NOT NULL)::int = 1",
            name="ck_inbox_items_single_source",
        ),
        CheckConstraint(
            "source_channel IN (" + ",".join(f"'{c}'" for c in INBOX_SOURCE_CHANNELS) + ")",
            name="ck_inbox_items_source_channel",
        ),
        # Email items must be staged; held-upload items keep their blobs
        # in the upload-session staging the job already owns.
        CheckConstraint(
            "source_channel <> 'email' OR staged_prefix IS NOT NULL",
            name="ck_inbox_items_email_staged",
        ),
        Index("ix_inbox_items_patient", "patient_id"),
        # The review-queue list: open items per patient, newest first.
        Index("ix_inbox_items_patient_status", "patient_id", "status", "created_at"),
        # Stale-processing recovery sweep (services/review_queue/jobs.py).
        Index("ix_inbox_items_status_updated", "status", "updated_at"),
    )


class InboxSenderAllowlist(Base, TimestampMixin):
    __tablename__ = "inbox_sender_allowlist"

    id: Mapped[uuid.UUID] = uuid_pk()
    patient_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("patients.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Stored lowercase (service-normalised). Exact-address matches only:
    # domain wildcards would silently widen the auto-accept surface.
    sender_email: Mapped[str] = mapped_column(String(320), nullable=False)
    # Auto-accept requires SPF or DKIM alignment with the From domain;
    # this flag exists so a future profile can relax it deliberately,
    # never silently (default stays strict).
    require_alignment: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    created_by_subject_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("subjects.id", ondelete="SET NULL"),
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    etag: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=False,
        server_default=text("gen_random_uuid()"),
    )

    __table_args__ = (
        UniqueConstraint("patient_id", "id", name="uq_inbox_sender_allowlist_patient_id"),
        # One live row per (patient, sender); revoked history stays.
        Index(
            "uq_inbox_sender_allowlist_active",
            "patient_id",
            "sender_email",
            unique=True,
            postgresql_where=text("active"),
        ),
        Index("ix_inbox_sender_allowlist_patient", "patient_id"),
    )


__all__ = [
    "INBOX_SOURCE_CHANNELS",
    "InboundEmail",
    "InboxItem",
    "InboxSenderAllowlist",
    "PatientInboxAddress",
]
