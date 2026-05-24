"""F11.3: storage quota unit tests.

The production query is a single SUM over ``instances.size_bytes``
joined to ``series`` and ``studies``. We stub ``db.execute`` at the
session level so the tests do not need a live Postgres, and exercise
the decision tree on the pure ``check_quota_or_raise`` helper:

* T3/T4 skip the cap.
* T1/T2 under cap is a no-op.
* T1/T2 that would tip over raises 413.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi import HTTPException

from bvphoenix.services.quota import (
    STORAGE_FREE_TIER_BYTES,
    StorageUsage,
    check_quota_or_raise,
    get_user_storage_usage,
)


class _FakeScalarResult:
    def __init__(self, value: int | None) -> None:
        self._value = value

    def scalar_one(self) -> int | None:
        return self._value

    def scalar_one_or_none(self) -> int | None:
        # ``ef24601`` (per-user quota override) added a second SELECT on
        # ``users.storage_quota_bytes`` that uses ``scalar_one_or_none``;
        # the fake mirrors the new shape so the existing single-fixture
        # mock continues to work without parameterising the result per
        # call.
        return self._value


class _FakeSession:
    """Minimal AsyncSession stand-in. ``fixed_sum`` is what the first
    stubbed query (``SUM(instances.size_bytes)``) returns; subsequent
    calls return ``None`` so the per-user quota-override SELECT
    introduced by ef24601 falls through to the static cap."""

    def __init__(self, fixed_sum: int) -> None:
        self._fixed = fixed_sum
        self.execute_calls = 0

    async def execute(self, *_: Any, **__: Any) -> _FakeScalarResult:
        self.execute_calls += 1
        if self.execute_calls == 1:
            return _FakeScalarResult(self._fixed)
        # Subsequent calls (quota override) — None means "no override,
        # use the default tier cap".
        return _FakeScalarResult(None)


@pytest.mark.asyncio
async def test_usage_reports_sum_and_constant_quota() -> None:
    session = _FakeSession(fixed_sum=1_500)
    usage = await get_user_storage_usage(session, uuid.uuid4())
    assert usage.used_bytes == 1_500
    assert usage.quota_bytes == STORAGE_FREE_TIER_BYTES
    assert usage.remaining_bytes == STORAGE_FREE_TIER_BYTES - 1_500
    assert not usage.exceeded


@pytest.mark.asyncio
async def test_quota_check_is_noop_for_t3() -> None:
    session = _FakeSession(fixed_sum=STORAGE_FREE_TIER_BYTES)  # already at cap
    usage = await check_quota_or_raise(
        session,
        user_subject_id=uuid.uuid4(),
        tier="t3",
        incoming_bytes=10**9,  # would blow the cap for T1/T2
    )
    assert usage.used_bytes == STORAGE_FREE_TIER_BYTES  # no raise


@pytest.mark.asyncio
async def test_quota_check_is_noop_for_t4() -> None:
    session = _FakeSession(fixed_sum=STORAGE_FREE_TIER_BYTES * 2)
    usage = await check_quota_or_raise(
        session,
        user_subject_id=uuid.uuid4(),
        tier="t4",
        incoming_bytes=10**9,
    )
    assert usage.used_bytes == STORAGE_FREE_TIER_BYTES * 2


@pytest.mark.asyncio
async def test_quota_check_allows_under_cap_t1() -> None:
    session = _FakeSession(fixed_sum=100)
    usage = await check_quota_or_raise(
        session,
        user_subject_id=uuid.uuid4(),
        tier="t1",
        incoming_bytes=200,
    )
    assert usage.used_bytes == 100  # reported, not raised


@pytest.mark.asyncio
async def test_quota_check_raises_413_when_over_cap_t1() -> None:
    session = _FakeSession(fixed_sum=STORAGE_FREE_TIER_BYTES - 50)
    with pytest.raises(HTTPException) as exc:
        await check_quota_or_raise(
            session,
            user_subject_id=uuid.uuid4(),
            tier="t1",
            incoming_bytes=1_000,
        )
    assert exc.value.status_code == 413
    assert isinstance(exc.value.detail, dict)
    assert exc.value.detail["error"] == "storage_quota_exceeded"
    assert exc.value.detail["used_bytes"] == STORAGE_FREE_TIER_BYTES - 50
    assert exc.value.detail["incoming_bytes"] == 1_000


@pytest.mark.asyncio
async def test_quota_check_raises_413_for_t2() -> None:
    session = _FakeSession(fixed_sum=STORAGE_FREE_TIER_BYTES + 1)
    with pytest.raises(HTTPException) as exc:
        await check_quota_or_raise(
            session,
            user_subject_id=uuid.uuid4(),
            tier="t2",
            incoming_bytes=1,
        )
    assert exc.value.status_code == 413


def test_storage_usage_remaining_floors_at_zero() -> None:
    """Sanity: if somehow the usage reported exceeds the quota (race /
    retroactive change), ``remaining_bytes`` must be 0, never negative."""
    usage = StorageUsage(
        used_bytes=STORAGE_FREE_TIER_BYTES + 100, quota_bytes=STORAGE_FREE_TIER_BYTES
    )
    assert usage.remaining_bytes == 0
    assert usage.exceeded
