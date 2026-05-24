"""Credit ledger service (F7.3).

Append-only accounting for the LLM wallet: ``topup``, ``debit``, and
``refund`` are the three kinds of row. The current balance is the
``balance_after_cents`` column on the most recent row for the user; we
carry the balance forward on every insert so balance reads are O(1)
and an auditor can walk the history to verify the chain.

Concurrency
-----------

Two concurrent calls to :func:`debit` would both read the same "last
row" and both decide their insert is fine — that race lets a
well-timed user spend their balance twice. We close it with a
PostgreSQL advisory lock keyed on the user's subject id: every
ledger movement first grabs ``pg_advisory_xact_lock(hash(user_id))``
so the read-compute-write sequence is serialised per user, without
locking the whole table.

Idempotency
-----------

Every write takes an ``idempotency_key``. The column is ``UNIQUE`` so
a retried request (network blip, client retry) lands on the same row
instead of double-debiting. Callers are responsible for deriving a
stable key — ``consultation:{id}`` or ``topup:{stripe_charge_id}``
work fine.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.db.models import CreditLedger

logger = logging.getLogger(__name__)


class InsufficientCreditsError(RuntimeError):
    """Raised when a debit would take the balance below zero."""

    def __init__(self, *, balance_cents: int, requested_cents: int) -> None:
        super().__init__(
            f"insufficient credits: balance {balance_cents} cents, "
            f"requested debit {requested_cents} cents"
        )
        self.balance_cents = balance_cents
        self.requested_cents = requested_cents


@dataclass(frozen=True, slots=True)
class LedgerMovement:
    row_id: uuid.UUID
    kind: str
    amount_cents: int
    balance_after_cents: int
    idempotency_key: str


def _user_lock_id(user_subject_id: uuid.UUID) -> int:
    """Map a user UUID to a 64-bit signed int for advisory locks.

    Postgres' ``pg_advisory_xact_lock(bigint)`` keys on a bigint, so we
    hash the UUID into the int64 range. Collisions mean two distinct
    users occasionally serialise on the same lock — acceptable:
    correctness is unchanged, we just pay a tiny amount of extra
    contention at the accounting path (not a hot loop).
    """
    digest = hashlib.blake2b(user_subject_id.bytes, digest_size=8).digest()
    value = int.from_bytes(digest, byteorder="big", signed=False)
    # Squeeze into int64 range for postgres bigint.
    return value - (1 << 63) if value >= (1 << 63) else value


async def _acquire_user_lock(db: AsyncSession, user_subject_id: uuid.UUID) -> None:
    await db.execute(
        text("SELECT pg_advisory_xact_lock(:lock_id)"),
        {"lock_id": _user_lock_id(user_subject_id)},
    )


async def _latest_row(db: AsyncSession, user_subject_id: uuid.UUID) -> CreditLedger | None:
    return (
        await db.execute(
            select(CreditLedger)
            .where(CreditLedger.user_subject_id == user_subject_id)
            .order_by(CreditLedger.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _existing_by_idempotency(db: AsyncSession, idempotency_key: str) -> CreditLedger | None:
    return (
        await db.execute(
            select(CreditLedger).where(CreditLedger.idempotency_key == idempotency_key)
        )
    ).scalar_one_or_none()


async def get_balance_cents(db: AsyncSession, *, user_subject_id: uuid.UUID) -> int:
    """Return the user's current wallet balance in cents (0 when no
    ledger rows exist yet)."""
    latest = await _latest_row(db, user_subject_id)
    return int(latest.balance_after_cents) if latest else 0


async def topup(
    db: AsyncSession,
    *,
    user_subject_id: uuid.UUID,
    amount_cents: int,
    idempotency_key: str,
    notes: dict | None = None,
) -> LedgerMovement:
    """Add credit to the user's wallet. Positive ``amount_cents``.

    Idempotent on ``idempotency_key``: a replay returns the existing
    row unchanged."""
    if amount_cents <= 0:
        raise ValueError("top-up amount must be positive")

    await _acquire_user_lock(db, user_subject_id)

    existing = await _existing_by_idempotency(db, idempotency_key)
    if existing is not None:
        return LedgerMovement(
            row_id=existing.id,
            kind=existing.kind,
            amount_cents=int(existing.amount_cents),
            balance_after_cents=int(existing.balance_after_cents),
            idempotency_key=existing.idempotency_key,
        )

    previous_balance = await get_balance_cents(db, user_subject_id=user_subject_id)
    new_balance = previous_balance + int(amount_cents)

    row = CreditLedger(
        user_subject_id=user_subject_id,
        caller_subject_id=user_subject_id,
        kind="topup",
        amount_cents=int(amount_cents),
        balance_after_cents=new_balance,
        idempotency_key=idempotency_key,
        notes=notes or {},
    )
    db.add(row)
    await db.flush()

    return LedgerMovement(
        row_id=row.id,
        kind="topup",
        amount_cents=int(amount_cents),
        balance_after_cents=new_balance,
        idempotency_key=idempotency_key,
    )


async def debit(
    db: AsyncSession,
    *,
    user_subject_id: uuid.UUID,
    amount_cents: int,
    idempotency_key: str,
    reference_kind: str | None = None,
    reference_id: uuid.UUID | None = None,
    notes: dict | None = None,
    caller_subject_id: uuid.UUID | None = None,
    sponsorship_id: uuid.UUID | None = None,
) -> LedgerMovement:
    """Take credit from the user's wallet. Positive ``amount_cents``.

    Raises :class:`InsufficientCreditsError` when the projected balance
    would go below zero. Idempotent on ``idempotency_key``.

    ``caller_subject_id`` is who originated the spend (defaults to
    ``user_subject_id`` for self-pay). ``sponsorship_id`` is set when
    the debit is booked against a sponsor's wallet via a
    ``WalletSponsorship`` row; the cap accounting on that row must be
    advanced *before* this function is called (see
    ``services.sponsorship.consume_sponsorship``)."""
    if amount_cents <= 0:
        raise ValueError("debit amount must be positive")

    await _acquire_user_lock(db, user_subject_id)

    existing = await _existing_by_idempotency(db, idempotency_key)
    if existing is not None:
        return LedgerMovement(
            row_id=existing.id,
            kind=existing.kind,
            amount_cents=int(existing.amount_cents),
            balance_after_cents=int(existing.balance_after_cents),
            idempotency_key=existing.idempotency_key,
        )

    previous_balance = await get_balance_cents(db, user_subject_id=user_subject_id)
    if previous_balance < amount_cents:
        raise InsufficientCreditsError(
            balance_cents=previous_balance, requested_cents=int(amount_cents)
        )
    new_balance = previous_balance - int(amount_cents)

    row = CreditLedger(
        user_subject_id=user_subject_id,
        caller_subject_id=caller_subject_id or user_subject_id,
        sponsorship_id=sponsorship_id,
        kind="debit",
        # Stored as a negative delta so SUM(amount_cents) over the
        # history equals the current balance — an auditor's sanity check.
        amount_cents=-int(amount_cents),
        balance_after_cents=new_balance,
        reference_kind=reference_kind,
        reference_id=reference_id,
        idempotency_key=idempotency_key,
        notes=notes or {},
    )
    db.add(row)
    await db.flush()

    return LedgerMovement(
        row_id=row.id,
        kind="debit",
        amount_cents=-int(amount_cents),
        balance_after_cents=new_balance,
        idempotency_key=idempotency_key,
    )


async def refund(
    db: AsyncSession,
    *,
    user_subject_id: uuid.UUID,
    amount_cents: int,
    idempotency_key: str,
    reference_kind: str | None = None,
    reference_id: uuid.UUID | None = None,
    notes: dict | None = None,
) -> LedgerMovement:
    """Credit back a previously-debited amount. Positive
    ``amount_cents``. Idempotent on ``idempotency_key``."""
    if amount_cents <= 0:
        raise ValueError("refund amount must be positive")

    await _acquire_user_lock(db, user_subject_id)

    existing = await _existing_by_idempotency(db, idempotency_key)
    if existing is not None:
        return LedgerMovement(
            row_id=existing.id,
            kind=existing.kind,
            amount_cents=int(existing.amount_cents),
            balance_after_cents=int(existing.balance_after_cents),
            idempotency_key=existing.idempotency_key,
        )

    previous_balance = await get_balance_cents(db, user_subject_id=user_subject_id)
    new_balance = previous_balance + int(amount_cents)

    row = CreditLedger(
        user_subject_id=user_subject_id,
        caller_subject_id=user_subject_id,
        kind="refund",
        amount_cents=int(amount_cents),
        balance_after_cents=new_balance,
        reference_kind=reference_kind,
        reference_id=reference_id,
        idempotency_key=idempotency_key,
        notes=notes or {},
    )
    db.add(row)
    await db.flush()

    return LedgerMovement(
        row_id=row.id,
        kind="refund",
        amount_cents=int(amount_cents),
        balance_after_cents=new_balance,
        idempotency_key=idempotency_key,
    )


__all__ = [
    "InsufficientCreditsError",
    "LedgerMovement",
    "debit",
    "get_balance_cents",
    "refund",
    "topup",
]
