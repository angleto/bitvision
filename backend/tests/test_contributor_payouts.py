"""F10.4: contributor payout assembly tests.

Same stub-session pattern as the DUC / credits tests. The responses
queue is keyed to the service's read order: license lookup,
existing-payout count, contributions aggregate. The dataset is reached
via ``TrainingLicense.dataset_id`` (Option 3), not a separate lookup."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from bvphoenix.db.models import (
    ContributorPayout,
    TrainingLicense,
)
from bvphoenix.services import contributor_payouts as svc


class _Result:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalar_one_or_none(self) -> Any:
        return self._rows[0] if self._rows else None

    def scalar_one(self) -> Any:
        return self._rows[0]

    def scalars(self) -> _Result:
        return self

    def all(self) -> list[Any]:
        return list(self._rows)


class _Session:
    def __init__(self, responses: list[list[Any]]) -> None:
        self._responses = list(responses)
        self.added: list[Any] = []
        self.flushed = 0

    async def execute(self, _stmt: Any, *_args: Any, **_kwargs: Any) -> _Result:
        if not self._responses:
            return _Result([])
        return _Result(self._responses.pop(0))

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        self.flushed += 1


def _license(
    *, price_cents: int = 10_000, status: str = "signed", with_dataset: bool = True
) -> TrainingLicense:
    lic = TrainingLicense(
        licensee_name="Test",
        licensee_email="x@example",
        price_usd_cents=price_cents,
    )
    lic.id = uuid.uuid4()
    lic.status = status
    lic.dataset_id = uuid.uuid4() if with_dataset else None
    return lic


@pytest.mark.asyncio
async def test_assemble_rejects_unsigned_license() -> None:
    lic = _license(status="draft")
    db = _Session(responses=[[lic]])
    with pytest.raises(svc.PayoutAssemblyError):
        await svc.assemble_payouts(db, license_id=lic.id)


@pytest.mark.asyncio
async def test_assemble_rejects_missing_license() -> None:
    db = _Session(responses=[[]])
    with pytest.raises(svc.PayoutAssemblyError):
        await svc.assemble_payouts(db, license_id=uuid.uuid4())


@pytest.mark.asyncio
async def test_assemble_refuses_replay() -> None:
    lic = _license()
    # license exists, 3 existing payout rows -> refuse
    db = _Session(responses=[[lic], [3]])
    with pytest.raises(svc.PayoutAssemblyError) as exc:
        await svc.assemble_payouts(db, license_id=lic.id)
    assert "refusing to replay" in str(exc.value)


@pytest.mark.asyncio
async def test_assemble_splits_50_50_by_bytes() -> None:
    lic = _license(price_cents=10_000)  # $100
    alice = uuid.uuid4()
    bob = uuid.uuid4()
    # alice contributed 75%, bob 25% of the bytes
    contributions = [(alice, 7_500), (bob, 2_500)]
    # responses: license, existing count (0), contributions
    db = _Session(responses=[[lic], [0], contributions])
    summary = await svc.assemble_payouts(db, license_id=lic.id)

    assert summary.pool_cents == 5_000  # 50% of $100
    assert summary.contributor_count == 2
    assert summary.total_bytes == 10_000
    assert summary.payouts_created == 2
    # Alice: 5000 * 7500 / 10000 = 3750, Bob: 5000 * 2500 / 10000 = 1250
    total_paid = sum(r.amount_cents for r in db.added)
    assert total_paid == 5_000
    rows = {r.user_subject_id: r for r in db.added}
    assert rows[alice].amount_cents == 3_750
    assert rows[bob].amount_cents == 1_250
    # Platform keeps 50% + any integer-division leftover (zero here).
    assert summary.platform_remainder_cents == 5_000


@pytest.mark.asyncio
async def test_assemble_handles_no_contributors() -> None:
    lic = _license(price_cents=10_000)
    db = _Session(responses=[[lic], [0], []])  # no contributions
    summary = await svc.assemble_payouts(db, license_id=lic.id)
    assert summary.payouts_created == 0
    assert summary.contributor_count == 0
    assert summary.total_bytes == 0
    assert summary.platform_remainder_cents == 10_000


@pytest.mark.asyncio
async def test_assemble_without_dataset_bound_raises() -> None:
    lic = _license(with_dataset=False)  # dataset_id is None
    db = _Session(responses=[[lic], [0]])  # license, count; contributions never reached
    with pytest.raises(svc.PayoutAssemblyError):
        await svc.assemble_payouts(db, license_id=lic.id)


@pytest.mark.asyncio
async def test_integer_rounding_leftover_goes_to_platform() -> None:
    """A 3-way split of a prime-ish pool leaves crumbs: verify the
    platform absorbs them so SUM(rows) + platform = price."""
    lic = _license(price_cents=1000)  # $10
    # 3 contributors with equal bytes → pool=500, each = 500//3 = 166.
    # Sum = 498; platform gets 502 (500 + leftover 2).
    contributors = [uuid.uuid4() for _ in range(3)]
    contributions = [(c, 100) for c in contributors]
    db = _Session(responses=[[lic], [0], contributions])
    summary = await svc.assemble_payouts(db, license_id=lic.id)
    assert summary.payouts_created == 3
    total_paid = sum(r.amount_cents for r in db.added)
    assert total_paid == 498
    assert summary.platform_remainder_cents == 502
    # Sum of everything still equals the license price.
    assert total_paid + summary.platform_remainder_cents == 1000


def test_pool_fraction_is_50_percent() -> None:
    assert svc.CONTRIBUTOR_POOL_FRACTION == 0.50


@pytest.mark.asyncio
async def test_list_user_payouts_is_user_scoped() -> None:
    """Smoke test: the service goes through ``db.execute`` and
    returns whatever the session yielded — so we can confirm the
    shape of the call and the return type."""
    uid = uuid.uuid4()
    payout = ContributorPayout(
        license_id=uuid.uuid4(),
        user_subject_id=uid,
        amount_cents=500,
        bytes_contributed=1000,
    )
    db = _Session(responses=[[payout]])
    out = await svc.list_user_payouts(db, user_subject_id=uid)
    assert out == [payout]
