"""F7.3 follow-up: LLM wallet debit wiring.

Unit tests for :func:`bvphoenix.services.billing.debit_llm_call`. The
credit-ledger plumbing has its own coverage in ``test_credits.py``; this
file focuses on the decision logic *around* the debit call: skip BYOK,
skip zero-usage, skip unknown models, use a stable idempotency key.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from bvphoenix.db.models import CreditLedger
from bvphoenix.services import credits
from bvphoenix.services.billing import debit_llm_call


class _ScalarResult:
    def __init__(self, value: Any) -> None:
        self._v = value

    def scalar_one_or_none(self) -> Any:
        return self._v


class _StubSession:
    """Mirrors ``test_credits._StubSession``. The advisory-lock call goes
    through ``execute(text(...))`` and must be a no-op; every other
    ``execute`` pops the next scripted response."""

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


# Usage shaped like what LLMUsage.as_dict() produces. Chosen so that
# billed_cents() comes out well above the ceil(0) degenerate case.
_USAGE_CANONICAL = {
    "prompt": 1000,
    "completion": 500,
    "cache_read_tokens": 0,
    "cache_creation_tokens": 0,
}


@pytest.mark.asyncio
async def test_debit_skipped_when_no_user() -> None:
    db = _StubSession()
    out = await debit_llm_call(
        db,
        user_subject_id=None,
        model_id="claude-sonnet-4-6",
        token_usage=_USAGE_CANONICAL,
        is_byok=False,
        reference_kind="summary",
        reference_id=uuid.uuid4(),
    )
    assert out is None
    assert db.added == []


@pytest.mark.asyncio
async def test_debit_skipped_for_byok_calls() -> None:
    db = _StubSession()
    out = await debit_llm_call(
        db,
        user_subject_id=uuid.uuid4(),
        model_id="claude-sonnet-4-6",
        token_usage=_USAGE_CANONICAL,
        is_byok=True,
        reference_kind="summary",
        reference_id=uuid.uuid4(),
    )
    assert out is None
    assert db.added == []


@pytest.mark.asyncio
async def test_debit_skipped_when_usage_is_zero() -> None:
    db = _StubSession()
    out = await debit_llm_call(
        db,
        user_subject_id=uuid.uuid4(),
        model_id="claude-sonnet-4-6",
        token_usage={"prompt": 0, "completion": 0},
        is_byok=False,
        reference_kind="summary",
        reference_id=uuid.uuid4(),
    )
    assert out is None
    assert db.added == []


@pytest.mark.asyncio
async def test_debit_skipped_for_missing_model_id() -> None:
    db = _StubSession()
    out = await debit_llm_call(
        db,
        user_subject_id=uuid.uuid4(),
        model_id=None,
        token_usage=_USAGE_CANONICAL,
        is_byok=False,
        reference_kind="summary",
        reference_id=uuid.uuid4(),
    )
    assert out is None
    assert db.added == []


@pytest.mark.asyncio
async def test_debit_skipped_for_unknown_model() -> None:
    # Not in _MODEL_RATES: the helper logs + returns None rather than
    # refusing the response after the LLM has already run.
    db = _StubSession()
    out = await debit_llm_call(
        db,
        user_subject_id=uuid.uuid4(),
        model_id="claude-unknown-v0",
        token_usage=_USAGE_CANONICAL,
        is_byok=False,
        reference_kind="summary",
        reference_id=uuid.uuid4(),
    )
    assert out is None
    assert db.added == []


@pytest.mark.asyncio
async def test_debit_records_negative_delta_on_platform_call() -> None:
    uid = uuid.uuid4()
    prev = CreditLedger(
        user_subject_id=uid,
        kind="topup",
        amount_cents=10000,
        balance_after_cents=10000,
        idempotency_key="prior-topup",
    )
    # responses: [idempotency-lookup=None, latest-row=prev for balance]
    db = _StubSession(responses=[None, prev])
    ref = uuid.uuid4()
    out = await debit_llm_call(
        db,
        user_subject_id=uid,
        model_id="claude-sonnet-4-6",
        token_usage=_USAGE_CANONICAL,
        is_byok=False,
        reference_kind="summary",
        reference_id=ref,
    )
    assert out is not None
    assert out.kind == "debit"
    assert out.amount_cents < 0  # debits are stored as negative deltas
    assert len(db.added) == 1
    row = db.added[0]
    assert isinstance(row, CreditLedger)
    # The default idempotency key is "{reference_kind}:{reference_id}".
    assert row.idempotency_key == f"summary:{ref}"
    assert row.reference_kind == "summary"
    assert row.reference_id == ref


@pytest.mark.asyncio
async def test_debit_is_idempotent_on_reference() -> None:
    uid = uuid.uuid4()
    ref = uuid.uuid4()
    existing = CreditLedger(
        user_subject_id=uid,
        kind="debit",
        amount_cents=-42,
        balance_after_cents=9958,
        idempotency_key=f"summary:{ref}",
    )
    existing.id = uuid.uuid4()
    # First execute() returns the existing row; the helper short-circuits
    # before any balance read, so no second response is needed.
    db = _StubSession(responses=[existing])
    out = await debit_llm_call(
        db,
        user_subject_id=uid,
        model_id="claude-sonnet-4-6",
        token_usage=_USAGE_CANONICAL,
        is_byok=False,
        reference_kind="summary",
        reference_id=ref,
    )
    assert out is not None
    assert out.row_id == existing.id
    assert db.added == []  # replay did not write a new row


@pytest.mark.asyncio
async def test_debit_propagates_insufficient_credits() -> None:
    uid = uuid.uuid4()
    prev = CreditLedger(
        user_subject_id=uid,
        kind="topup",
        amount_cents=1,
        balance_after_cents=1,
        idempotency_key="prior-topup",
    )
    db = _StubSession(responses=[None, prev])
    with pytest.raises(credits.InsufficientCreditsError):
        await debit_llm_call(
            db,
            user_subject_id=uid,
            model_id="claude-opus-4-7",  # expensive: easily exceeds 1 cent
            token_usage=_USAGE_CANONICAL,
            is_byok=False,
            reference_kind="annotation",
            reference_id=uuid.uuid4(),
        )


@pytest.mark.asyncio
async def test_debit_accepts_anthropic_style_usage_keys() -> None:
    """An older row may carry ``input_tokens`` / ``output_tokens`` keys
    (raw provider payload). The helper should still be able to bill it."""
    uid = uuid.uuid4()
    prev = CreditLedger(
        user_subject_id=uid,
        kind="topup",
        amount_cents=10000,
        balance_after_cents=10000,
        idempotency_key="prior-topup",
    )
    db = _StubSession(responses=[None, prev])
    out = await debit_llm_call(
        db,
        user_subject_id=uid,
        model_id="claude-sonnet-4-6",
        token_usage={"input_tokens": 1000, "output_tokens": 500},
        is_byok=False,
        reference_kind="summary",
        reference_id=uuid.uuid4(),
    )
    assert out is not None
    assert out.amount_cents < 0
