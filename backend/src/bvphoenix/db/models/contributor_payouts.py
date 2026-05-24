"""Contributor payout ORM model (F10.4).

One row per (license, contributor). Records the contributor's share
of the revenue on a training licence at the moment the licence was
signed, plus the lifecycle of the outbound payment. The actual
payment-rail integration (Stripe Connect / SEPA) lands in F9; until
then the row sits in ``pending`` status, an admin reconciles out of
band, and flips it to ``paid`` with the reference filled in.
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
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from bvphoenix.db.base import Base
from bvphoenix.db.models._common import uuid_pk

CONTRIBUTOR_PAYOUT_STATUSES: tuple[str, ...] = (
    "pending",
    "paid",
    "failed",
    "cancelled",
)


class ContributorPayout(Base):
    __tablename__ = "contributor_payouts"

    id: Mapped[uuid.UUID] = uuid_pk()
    license_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("training_licenses.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_subject_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("subjects.id", ondelete="CASCADE"),
        nullable=False,
    )
    amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    bytes_contributed: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pending")
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    payout_reference: Mapped[str | None] = mapped_column(String(255))
    notes: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','paid','failed','cancelled')",
            name="ck_contributor_payouts_status",
        ),
        CheckConstraint(
            "amount_cents >= 0",
            name="ck_contributor_payouts_amount_nonneg",
        ),
        CheckConstraint(
            "bytes_contributed >= 0",
            name="ck_contributor_payouts_bytes_nonneg",
        ),
        Index(
            "uq_contributor_payouts_license_user",
            "license_id",
            "user_subject_id",
            unique=True,
        ),
        Index(
            "ix_contributor_payouts_user_status",
            "user_subject_id",
            "status",
        ),
    )
