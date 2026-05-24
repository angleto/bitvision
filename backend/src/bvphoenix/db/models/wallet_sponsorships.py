"""Wallet sponsorship: cross-subject billing authorisation.

A sponsorship row authorises one subject (``sponsored_subject_id``) to
debit the wallet of another (``sponsor_subject_id``) up to ``cap_cents``,
within a given scope (a specific patient, a consultation, an
organisation, or globally).

This is orthogonal to ``Grant`` (which gates *access* to a resource):
a consultant invited to read a patient's record may also be sponsored
to spend from the patient's wallet for AI calls on that record. Without
a sponsorship the consultant pays from their own wallet (or BYOK).

Cap mechanics: ``spent_cents`` is incremented atomically inside the
same transaction that writes the ledger debit row. A debit is refused
when ``spent_cents + amount > cap_cents``.

Cap edits are logged in ``wallet_sponsorship_audit`` so a sponsor's
later cap reduction does not erase the historical authorisation that
allowed prior spend.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from bvphoenix.db.base import Base
from bvphoenix.db.models._common import uuid_pk

SCOPE_KINDS: tuple[str, ...] = ("patient", "consultation", "organization", "global")
# Specificity ranking used by the resolver: lower index = more specific.
# A consultation-scoped sponsorship is preferred over a patient-scoped
# one when both match the same call.
SCOPE_SPECIFICITY: dict[str, int] = {
    "consultation": 0,
    "patient": 1,
    "organization": 2,
    "global": 3,
}
PERIODS: tuple[str, ...] = ("weekly", "monthly")
AUDIT_ACTIONS: tuple[str, ...] = (
    "created",
    "cap_raised",
    "cap_lowered",
    "revoked",
    "expired",
)


class WalletSponsorship(Base):
    __tablename__ = "wallet_sponsorships"

    id: Mapped[uuid.UUID] = uuid_pk()
    sponsor_subject_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("subjects.id", ondelete="RESTRICT"),
        nullable=False,
    )
    sponsored_subject_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("subjects.id", ondelete="CASCADE"),
        nullable=False,
    )
    scope_kind: Mapped[str] = mapped_column(Text, nullable=False)
    scope_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    cap_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    spent_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    period: Mapped[str | None] = mapped_column(Text)
    valid_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_by_subject_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("subjects.id", ondelete="SET NULL"),
    )
    purpose: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "sponsor_subject_id <> sponsored_subject_id",
            name="ck_wallet_sponsorships_distinct_subjects",
        ),
        CheckConstraint("cap_cents > 0", name="ck_wallet_sponsorships_cap_positive"),
        CheckConstraint("spent_cents >= 0", name="ck_wallet_sponsorships_spent_nonneg"),
        CheckConstraint(
            "scope_kind IN ('patient','consultation','organization','global')",
            name="ck_wallet_sponsorships_scope_kind",
        ),
        CheckConstraint(
            "(scope_kind = 'global' AND scope_id IS NULL) "
            "OR (scope_kind <> 'global' AND scope_id IS NOT NULL)",
            name="ck_wallet_sponsorships_scope_id_match",
        ),
        CheckConstraint(
            "period IS NULL OR period IN ('weekly','monthly')",
            name="ck_wallet_sponsorships_period",
        ),
        Index(
            "ix_wallet_sponsorships_lookup_active",
            "sponsored_subject_id",
            "scope_kind",
            "scope_id",
            postgresql_where=text("revoked_at IS NULL"),
        ),
    )


class WalletSponsorshipAudit(Base):
    __tablename__ = "wallet_sponsorship_audit"

    id: Mapped[uuid.UUID] = uuid_pk()
    sponsorship_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("wallet_sponsorships.id", ondelete="CASCADE"),
        nullable=False,
    )
    actor_subject_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("subjects.id", ondelete="SET NULL"),
    )
    action: Mapped[str] = mapped_column(Text, nullable=False)
    before_cap_cents: Mapped[int | None] = mapped_column(BigInteger)
    after_cap_cents: Mapped[int | None] = mapped_column(BigInteger)
    notes: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "action IN ('created','cap_raised','cap_lowered','revoked','expired')",
            name="ck_wallet_sponsorship_audit_action",
        ),
    )
