"""Append-only credit ledger (F7.3).

Each movement (top-up / debit / refund) is its own row. The balance at
any point in time is ``balance_after_cents`` on the most recent row
for that user; that materialised value is stored alongside the delta
so balance reads are O(1) and the chain of rows can be re-audited.
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
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from bvphoenix.db.base import Base
from bvphoenix.db.models._common import uuid_pk

LEDGER_KINDS: tuple[str, ...] = ("topup", "debit", "refund")


class CreditLedger(Base):
    __tablename__ = "credit_ledger"

    id: Mapped[uuid.UUID] = uuid_pk()
    # ``user_subject_id`` is the wallet owner: the subject whose balance
    # the row applies to. For sponsored debits this differs from the
    # caller (see ``caller_subject_id`` below).
    user_subject_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("subjects.id", ondelete="CASCADE"),
        nullable=False,
    )
    # ``caller_subject_id`` is who originated the spend. Equal to
    # ``user_subject_id`` for self-pay; differs when a debit is booked
    # against a sponsor's wallet (consultation, organisation pool, ...).
    caller_subject_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("subjects.id", ondelete="SET NULL"),
        nullable=False,
    )
    sponsorship_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("wallet_sponsorships.id", ondelete="SET NULL"),
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    # ``amount_cents`` is signed: top-ups and refunds are positive,
    # debits are negative. ``balance_after_cents`` is unsigned (we
    # refuse debits that would go negative).
    amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    balance_after_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reference_kind: Mapped[str | None] = mapped_column(String(32))
    reference_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    notes: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "kind IN ('topup','debit','refund')",
            name="ck_credit_ledger_kind",
        ),
        Index("uq_credit_ledger_idempotency", "idempotency_key", unique=True),
        Index("ix_credit_ledger_user_time", "user_subject_id", "created_at"),
    )
