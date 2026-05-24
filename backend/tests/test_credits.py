"""F7.3: credit ledger unit tests (topup / debit / refund + idempotency).

Stubbed AsyncSession — the advisory-lock call is routed to a no-op so
we do not need a real Postgres. The idempotency-replay path is
exercised because it is the thing most likely to break under retry.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from bvphoenix.db.models import CreditLedger
from bvphoenix.services import credits


class _ScalarResult:
    def __init__(self, value: Any) -> None:
        self._v = value

    def scalar_one_or_none(self) -> Any:
        return self._v


class _StubSession:
    """Advisory-lock calls go through ``execute(text(...))``; we check
    the SQL text and skip the queue for those. Every other execute
    pops the next configured response.

    ``responses`` is evaluated left-to-right and maps to the sequence
    of calls the service makes:
    idempotency lookup → latest-row lookup (for balance)."""

    def __init__(self, responses: list[Any] | None = None) -> None:
        self.responses = list(responses or [])
        self.added: list[Any] = []
        self.flushed = 0

    async def execute(self, stmt: Any, *_args: Any, **_kwargs: Any) -> _ScalarResult:
        s = str(stmt)
        if "pg_advisory" in s.lower():
            return _ScalarResult(None)
        if not self.responses:
            return _ScalarResult(None)
        return _ScalarResult(self.responses.pop(0))

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        self.flushed += 1


@pytest.mark.asyncio
async def test_get_balance_is_zero_on_empty_ledger() -> None:
    db = _StubSession(responses=[None])
    bal = await credits.get_balance_cents(db, user_subject_id=uuid.uuid4())
    assert bal == 0


@pytest.mark.asyncio
async def test_topup_creates_row_and_updates_balance() -> None:
    # responses: [idempotency-lookup=None, latest-row=None for balance read]
    db = _StubSession(responses=[None, None])
    uid = uuid.uuid4()
    out = await credits.topup(
        db,
        user_subject_id=uid,
        amount_cents=500,
        idempotency_key="onboarding-1",
    )
    assert out.kind == "topup"
    assert out.amount_cents == 500
    assert out.balance_after_cents == 500
    assert len(db.added) == 1
    row = db.added[0]
    assert isinstance(row, CreditLedger)
    assert row.user_subject_id == uid
    assert row.kind == "topup"
    assert row.idempotency_key == "onboarding-1"


@pytest.mark.asyncio
async def test_topup_replays_on_idempotency_key() -> None:
    existing = CreditLedger(
        user_subject_id=uuid.uuid4(),
        kind="topup",
        amount_cents=700,
        balance_after_cents=700,
        idempotency_key="already-ran",
    )
    existing.id = uuid.uuid4()
    db = _StubSession(responses=[existing])
    out = await credits.topup(
        db,
        user_subject_id=existing.user_subject_id,
        amount_cents=999,  # would NOT be 700 if we wrote a fresh row
        idempotency_key="already-ran",
    )
    assert out.row_id == existing.id
    assert out.balance_after_cents == 700  # existing, untouched
    assert db.added == []  # no new row created


@pytest.mark.asyncio
async def test_debit_refuses_when_insufficient() -> None:
    prev = CreditLedger(
        user_subject_id=uuid.uuid4(),
        kind="topup",
        amount_cents=100,
        balance_after_cents=100,
        idempotency_key="prior-topup",
    )
    # idempotency-lookup=None, latest-row=prev (balance=100)
    db = _StubSession(responses=[None, prev])
    with pytest.raises(credits.InsufficientCreditsError) as exc:
        await credits.debit(
            db,
            user_subject_id=prev.user_subject_id,
            amount_cents=200,
            idempotency_key="overdraft-attempt",
        )
    assert exc.value.balance_cents == 100
    assert exc.value.requested_cents == 200


@pytest.mark.asyncio
async def test_debit_on_sufficient_balance_records_negative_delta() -> None:
    prev = CreditLedger(
        user_subject_id=uuid.uuid4(),
        kind="topup",
        amount_cents=500,
        balance_after_cents=500,
        idempotency_key="prior-topup",
    )
    db = _StubSession(responses=[None, prev])
    out = await credits.debit(
        db,
        user_subject_id=prev.user_subject_id,
        amount_cents=120,
        idempotency_key="llm-call-xyz",
        reference_kind="consultation",
        reference_id=uuid.uuid4(),
    )
    assert out.kind == "debit"
    # Deltas are signed: debit is stored negative so SUM(deltas)
    # reconstructs the balance.
    assert out.amount_cents == -120
    assert out.balance_after_cents == 380


@pytest.mark.asyncio
async def test_refund_adds_back_credit() -> None:
    prev = CreditLedger(
        user_subject_id=uuid.uuid4(),
        kind="debit",
        amount_cents=-200,
        balance_after_cents=300,
        idempotency_key="prior-debit",
    )
    db = _StubSession(responses=[None, prev])
    out = await credits.refund(
        db,
        user_subject_id=prev.user_subject_id,
        amount_cents=200,
        idempotency_key="llm-call-xyz:refund",
    )
    assert out.kind == "refund"
    assert out.amount_cents == 200
    assert out.balance_after_cents == 500


@pytest.mark.asyncio
async def test_negative_or_zero_amount_rejected() -> None:
    db = _StubSession()
    with pytest.raises(ValueError):
        await credits.topup(db, user_subject_id=uuid.uuid4(), amount_cents=0, idempotency_key="x")
    with pytest.raises(ValueError):
        await credits.debit(db, user_subject_id=uuid.uuid4(), amount_cents=-1, idempotency_key="y")
    with pytest.raises(ValueError):
        await credits.refund(db, user_subject_id=uuid.uuid4(), amount_cents=0, idempotency_key="z")
