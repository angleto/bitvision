"""GDPR consent + erasure-request models.

See docs/security-gdpr.md for the regulatory context. Key points:

* ``Consent`` rows are append-only. A consent is *active* when
  ``revoked_at IS NULL`` and a matching ``granted_at`` row exists for the
  (user, kind) pair — the history is kept so a DPO can always reconstruct
  exactly who consented to what and when (Art. 7 GDPR: proof of consent).
* ``DataErasureRequest`` tracks Art. 17 (right to be forgotten) flows.
  Two-step: ``pending`` after user submits, transitions to ``completed``
  once the anonymization service finishes. Admins can ``reject`` (legal
  hold, ongoing investigation) by flipping ``status``.
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
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from bvphoenix.db.base import Base
from bvphoenix.db.models._common import uuid_pk

# Recognised consent kinds. Keep in sync with docs/security-gdpr.md §3.
CONSENT_KINDS: tuple[str, ...] = (
    "terms_of_service",
    "privacy_policy",
    "marketing_email",
    "research_use",
    "commercial_use",
    "ai_training",
    "third_party_sharing",
)

# Consents whose presence is a precondition to using the platform.
# Acceptance is implicit in account creation; ``api/auth.register``
# persists explicit rows on signup, and ``api/gdpr.list_consents``
# synthesises ``granted=true`` for these kinds when no row exists yet
# (covers users registered before the persistence was wired up).
REQUIRED_CONSENT_KINDS: tuple[str, ...] = (
    "terms_of_service",
    "privacy_policy",
)

ERASURE_SCOPES: tuple[str, ...] = (
    "self",
    "studies",
    "annotations",
    "consents_only",
)

ERASURE_STATUSES: tuple[str, ...] = (
    "pending",
    "approved",
    "rejected",
    "completed",
    "cancelled",
)


class Consent(Base):
    __tablename__ = "consents"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_subject_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("subjects.id", ondelete="CASCADE"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, nullable=False, server_default="{}")

    __table_args__ = (
        Index("ix_consents_user_kind", "user_subject_id", "kind"),
        Index("ix_consents_active", "user_subject_id", "kind", "revoked_at"),
    )


class DataErasureRequest(Base):
    __tablename__ = "data_erasure_requests"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_subject_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("subjects.id", ondelete="CASCADE"),
        nullable=False,
    )
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reason: Mapped[str | None] = mapped_column(Text)
    scope: Mapped[str] = mapped_column(Text, nullable=False, server_default="self")
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pending")
    notes: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','approved','rejected','completed','cancelled')",
            name="ck_erasure_status",
        ),
        CheckConstraint(
            "scope IN ('self','studies','annotations','consents_only')",
            name="ck_erasure_scope",
        ),
        Index("ix_erasure_user_time", "user_subject_id", "requested_at"),
    )
