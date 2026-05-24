"""Unit tests for the sponsorship resolver and cap consumption.

Covers the pure-resolver code paths (specificity ordering, recency
tie-break, cap headroom filter) and the failure modes of cap
consumption (over-cap raises, atomic increment). Database-touching
tests live in tests/integration where a Postgres pool is available.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from bvphoenix.services.sponsorship import (
    BillingTarget,
    CapExceededError,
    ScopeMatch,
    SponsorshipError,
    consume_sponsorship,
    resolve_billing,
)


def _now() -> datetime:
    return datetime(2026, 5, 9, 12, 0, tzinfo=UTC)


def _make_row(
    *,
    sponsor: uuid.UUID,
    sponsored: uuid.UUID,
    scope_kind: str,
    scope_id: uuid.UUID | None,
    cap_cents: int = 1000,
    spent_cents: int = 0,
    revoked: bool = False,
    created_minutes_ago: int = 0,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        sponsor_subject_id=sponsor,
        sponsored_subject_id=sponsored,
        scope_kind=scope_kind,
        scope_id=scope_id,
        cap_cents=cap_cents,
        spent_cents=spent_cents,
        revoked_at=_now() if revoked else None,
        valid_from=_now() - timedelta(days=1),
        valid_until=None,
        created_at=_now() - timedelta(minutes=created_minutes_ago),
    )


class _ScalarsResult:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def all(self) -> list:
        return list(self._rows)


class _ExecuteResult:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def scalars(self) -> _ScalarsResult:
        return _ScalarsResult(self._rows)


class _StubDB:
    """Stub for resolve_billing: ``execute`` returns the pre-loaded rows
    without any predicate filtering — the test arranges the dataset to
    match the resolver's expected post-filter set."""

    def __init__(self, rows: list) -> None:
        self.rows = rows

    async def execute(self, _stmt):
        return _ExecuteResult(self.rows)


# ---------------------------------------------------------------------------
# resolve_billing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_no_match_returns_self_pay() -> None:
    caller = uuid.uuid4()
    db = _StubDB(rows=[])
    target = await resolve_billing(
        db,
        caller_subject_id=caller,
        scopes=[ScopeMatch(scope_kind="patient", scope_id=uuid.uuid4())],
        estimated_cents=50,
    )
    assert isinstance(target, BillingTarget)
    assert target.billed_subject_id == caller
    assert target.is_sponsored is False


@pytest.mark.asyncio
async def test_resolve_picks_consultation_over_patient() -> None:
    caller = uuid.uuid4()
    sponsor = uuid.uuid4()
    patient_id = uuid.uuid4()
    consultation_id = uuid.uuid4()
    rows = [
        _make_row(
            sponsor=sponsor,
            sponsored=caller,
            scope_kind="patient",
            scope_id=patient_id,
            created_minutes_ago=10,  # older
        ),
        _make_row(
            sponsor=sponsor,
            sponsored=caller,
            scope_kind="consultation",
            scope_id=consultation_id,
            created_minutes_ago=60,  # older but more specific
        ),
    ]
    db = _StubDB(rows=rows)
    target = await resolve_billing(
        db,
        caller_subject_id=caller,
        scopes=[
            ScopeMatch(scope_kind="consultation", scope_id=consultation_id),
            ScopeMatch(scope_kind="patient", scope_id=patient_id),
        ],
        estimated_cents=100,
    )
    assert target.is_sponsored
    assert target.sponsorship.scope_kind == "consultation"


@pytest.mark.asyncio
async def test_resolve_picks_global_only_when_nothing_else() -> None:
    caller = uuid.uuid4()
    sponsor = uuid.uuid4()
    rows = [
        _make_row(
            sponsor=sponsor,
            sponsored=caller,
            scope_kind="global",
            scope_id=None,
        ),
    ]
    db = _StubDB(rows=rows)
    target = await resolve_billing(
        db,
        caller_subject_id=caller,
        scopes=[
            ScopeMatch(scope_kind="patient", scope_id=uuid.uuid4()),
            ScopeMatch(scope_kind="global", scope_id=None),
        ],
        estimated_cents=50,
    )
    assert target.is_sponsored
    assert target.sponsorship.scope_kind == "global"


@pytest.mark.asyncio
async def test_resolve_recency_breaks_tie_within_same_scope() -> None:
    """Two patient-scoped sponsorships, same patient: most recent wins."""
    caller = uuid.uuid4()
    sponsor_a = uuid.uuid4()
    sponsor_b = uuid.uuid4()
    patient_id = uuid.uuid4()
    older = _make_row(
        sponsor=sponsor_a,
        sponsored=caller,
        scope_kind="patient",
        scope_id=patient_id,
        created_minutes_ago=120,
    )
    newer = _make_row(
        sponsor=sponsor_b,
        sponsored=caller,
        scope_kind="patient",
        scope_id=patient_id,
        created_minutes_ago=5,
    )
    # Service receives them in DESC order by created_at thanks to its
    # ORDER BY; emulate that here.
    db = _StubDB(rows=[newer, older])
    target = await resolve_billing(
        db,
        caller_subject_id=caller,
        scopes=[ScopeMatch(scope_kind="patient", scope_id=patient_id)],
        estimated_cents=10,
    )
    assert target.is_sponsored
    assert target.sponsorship.sponsor_subject_id == sponsor_b


@pytest.mark.asyncio
async def test_resolve_no_scopes_skips_db_and_returns_self_pay() -> None:
    caller = uuid.uuid4()
    db = _StubDB(
        rows=[
            _make_row(
                sponsor=uuid.uuid4(),
                sponsored=caller,
                scope_kind="patient",
                scope_id=uuid.uuid4(),
            )
        ]
    )
    target = await resolve_billing(db, caller_subject_id=caller, scopes=[], estimated_cents=10)
    assert target.billed_subject_id == caller
    assert target.is_sponsored is False


# ---------------------------------------------------------------------------
# consume_sponsorship
# ---------------------------------------------------------------------------


class _ConsumingDB:
    """Stub matching the API of ``consume_sponsorship``: ``get`` returns
    a row, ``execute`` is a no-op (we assert spent_cents was advanced via
    the row mutation in the SUT)."""

    def __init__(self, row: SimpleNamespace | None) -> None:
        self.row = row
        self.executed_updates: list = []

    async def get(self, _model, _id, with_for_update: bool = False):
        return self.row

    async def execute(self, stmt):
        self.executed_updates.append(stmt)
        return None


@pytest.mark.asyncio
async def test_consume_advances_spent() -> None:
    sid = uuid.uuid4()
    row = _make_row(
        sponsor=uuid.uuid4(),
        sponsored=uuid.uuid4(),
        scope_kind="patient",
        scope_id=uuid.uuid4(),
        cap_cents=1000,
        spent_cents=200,
    )
    row.id = sid
    db = _ConsumingDB(row=row)
    new_total = await consume_sponsorship(db, sponsorship_id=sid, amount_cents=300)
    assert new_total == 500
    assert len(db.executed_updates) == 1


@pytest.mark.asyncio
async def test_consume_raises_when_over_cap() -> None:
    sid = uuid.uuid4()
    row = _make_row(
        sponsor=uuid.uuid4(),
        sponsored=uuid.uuid4(),
        scope_kind="patient",
        scope_id=uuid.uuid4(),
        cap_cents=1000,
        spent_cents=950,
    )
    row.id = sid
    db = _ConsumingDB(row=row)
    with pytest.raises(CapExceededError) as exc:
        await consume_sponsorship(db, sponsorship_id=sid, amount_cents=100)
    assert exc.value.cap_cents == 1000
    assert exc.value.spent_cents == 950
    assert exc.value.requested_cents == 100
    assert db.executed_updates == []  # no UPDATE issued on rejection


@pytest.mark.asyncio
async def test_consume_raises_when_revoked() -> None:
    sid = uuid.uuid4()
    row = _make_row(
        sponsor=uuid.uuid4(),
        sponsored=uuid.uuid4(),
        scope_kind="patient",
        scope_id=uuid.uuid4(),
        revoked=True,
    )
    row.id = sid
    db = _ConsumingDB(row=row)
    with pytest.raises(SponsorshipError):
        await consume_sponsorship(db, sponsorship_id=sid, amount_cents=10)


@pytest.mark.asyncio
async def test_consume_rejects_zero_amount() -> None:
    db = _ConsumingDB(row=None)
    with pytest.raises(ValueError):
        await consume_sponsorship(db, sponsorship_id=uuid.uuid4(), amount_cents=0)


@pytest.mark.asyncio
async def test_consume_raises_when_not_found() -> None:
    db = _ConsumingDB(row=None)
    with pytest.raises(LookupError):
        await consume_sponsorship(db, sponsorship_id=uuid.uuid4(), amount_cents=10)
