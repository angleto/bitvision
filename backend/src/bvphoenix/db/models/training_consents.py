"""Per-study training consent (F6 + DESIGN.md §4).

A dedicated table for consent to the T3 / T4 training pool, one row
per (user, study, tier). Splits the paper trail from the GDPR-style
user-level consents in :class:`Consent`: that table tracks opt-ins
like "privacy policy accepted" or "marketing email" that are global
to the user, while this one tracks the per-upload decision that
authorises a specific study to land in assembled training datasets.

Why a dedicated table:

* DESIGN.md §4 names the columns (``study_id``, ``consent_version``,
  ``consent_hash``, ``granted_at``, ``revoked_at``). Keeping them as
  real columns instead of a ``Consent.metadata['study_ids']`` list
  means GDPR-erasure and k-anonymity assembly queries hit an index
  instead of walking JSONB.
* Revocation becomes per-row ``revoked_at`` rather than mutating a
  metadata list — matches the append-only audit posture of the rest
  of the F6 / F10 surface (credit_ledger, contributor_payouts).
* A ``consent_hash`` column lets us pin the exact consent text the
  user agreed to. When the consent copy changes, existing rows keep
  their historical hash; the new text produces new rows with a bumped
  version.

Partial unique index on (user_subject_id, study_id, tier) WHERE
``revoked_at IS NULL`` enforces "at most one active consent per
(user, study, tier)", so idempotent re-upload does not create
duplicates but a revoke-then-re-grant is allowed (new row).
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
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from bvphoenix.db.base import Base
from bvphoenix.db.models._common import uuid_pk

TRAINING_CONSENT_TIERS: tuple[str, ...] = ("t3", "t4")


class TrainingConsent(Base):
    __tablename__ = "training_consents"

    id: Mapped[uuid.UUID] = uuid_pk()
    # The owner who granted the consent. Redundant with
    # ``study.owner_subject_id`` but kept explicit so GDPR erasure by
    # user does not need a join to ``studies`` and so the row stays
    # self-contained for audit.
    user_subject_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("subjects.id", ondelete="CASCADE"),
        nullable=False,
    )
    study_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("imaging_studies.id", ondelete="CASCADE"),
        nullable=False,
    )
    tier: Mapped[str] = mapped_column(String(2), nullable=False)
    # Monotonic integer that bumps whenever the consent text the UI
    # shows to the user changes. Rows keep the version they were
    # created under so an auditor can pair a row to the exact text.
    consent_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    # SHA-256 of the consent text + tier + version. Opaque hex string;
    # the backend uses it as a sanity check that the consent surface
    # the user clicked through is the one we expected.
    consent_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Free-text reason captured when the user clicked revoke, for the
    # audit trail. Small enough to stay inline rather than a JSONB key.
    revoke_reason: Mapped[str | None] = mapped_column(Text)
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, nullable=False, server_default="{}")

    __table_args__ = (
        CheckConstraint(
            "tier IN ('t3','t4')",
            name="ck_training_consents_tier",
        ),
        CheckConstraint(
            "consent_version > 0",
            name="ck_training_consents_version_positive",
        ),
        # Partial unique: one active row per (user, study, tier).
        # Revoked rows accumulate for audit; a subsequent re-grant
        # inserts a fresh row.
        Index(
            "uq_training_consents_active",
            "user_subject_id",
            "study_id",
            "tier",
            unique=True,
            postgresql_where="revoked_at IS NULL",
        ),
        Index("ix_training_consents_study", "study_id"),
        Index("ix_training_consents_user", "user_subject_id"),
    )
