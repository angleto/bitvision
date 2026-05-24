"""End-to-end sponsorship test against a real Postgres.

Synthesises two subjects (sponsor + sponsored), tops up the sponsor's
wallet, creates a patient-scoped sponsorship, and runs ``debit_llm_call``
several times from the sponsored caller. Verifies:

* the ledger row uses ``user_subject_id = sponsor`` and
  ``caller_subject_id = sponsored`` (so audit answers "who paid" and
  "who triggered");
* ``wallet_sponsorships.spent_cents`` is advanced atomically to match
  the ledger debits;
* an over-cap call falls back to the caller's wallet (resolver pre-
  filter on cap headroom);
* a revoked sponsorship is ignored;
* specificity ordering picks consultation over patient when both match.

This bypasses the shared ``db_session`` fixture because the test needs
real commits (the sponsorship cap accounting and ledger audit are only
meaningful when the rows are visible across sessions). Cleanup deletes
the synthetic subjects via ``ON DELETE CASCADE``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from bvphoenix.config import get_settings
from bvphoenix.services import billing, credits
from bvphoenix.services import sponsorship as spon_svc
from bvphoenix.services.sponsorship import ScopeMatch
from tests.conftest import skip_if_no_db

pytestmark = [pytest.mark.asyncio, skip_if_no_db]


_USAGE_BIG = {"prompt": 1_000_000, "completion": 1_000_000}
_USAGE_SMALL = {"prompt": 1_000, "completion": 500}
_MODEL = "mistral-small-3.2-24b-instruct-2506"


# --------------------------------------------------------------------------
# Per-test engine + factory
# --------------------------------------------------------------------------


@pytest_asyncio.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Fresh engine per test so the asyncio event-loop scope matches."""
    eng = create_async_engine(get_settings().database_url, future=True, pool_pre_ping=True)
    factory = async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)
    try:
        yield factory
    finally:
        await eng.dispose()


@pytest_asyncio.fixture
async def synthetic_subjects(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]]:
    """Yield (sponsor_id, sponsored_id, fake_patient, fake_consultation)
    and cascade-delete the two subjects on teardown."""
    sponsor_id = uuid.uuid4()
    sponsored_id = uuid.uuid4()
    fake_patient = uuid.uuid4()
    fake_consultation = uuid.uuid4()

    async with session_factory() as db:
        await db.execute(
            text(
                "INSERT INTO subjects (id, kind, display_name) VALUES (:s, 'user', 'test_sponsor'), (:c, 'user', 'test_sponsored')"
            ),
            {"s": sponsor_id, "c": sponsored_id},
        )
        await db.commit()

    try:
        yield sponsor_id, sponsored_id, fake_patient, fake_consultation
    finally:
        # Sponsor has ON DELETE RESTRICT on wallet_sponsorships, so we
        # need to drop the rows owned by these subjects in the correct
        # order: ledger -> sponsorships -> subjects.
        async with session_factory() as db:
            await db.execute(
                text(
                    "DELETE FROM credit_ledger "
                    "WHERE user_subject_id IN (:s, :c) OR caller_subject_id IN (:s, :c)"
                ),
                {"s": sponsor_id, "c": sponsored_id},
            )
            await db.execute(
                text(
                    "DELETE FROM wallet_sponsorships "
                    "WHERE sponsor_subject_id IN (:s, :c) OR sponsored_subject_id IN (:s, :c)"
                ),
                {"s": sponsor_id, "c": sponsored_id},
            )
            await db.execute(
                text("DELETE FROM subjects WHERE id IN (:s, :c)"),
                {"s": sponsor_id, "c": sponsored_id},
            )
            await db.commit()


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


async def _balance(db: AsyncSession, subject_id: uuid.UUID) -> int:
    return await credits.get_balance_cents(db, user_subject_id=subject_id)


async def _spent(db: AsyncSession, sponsorship_id: uuid.UUID) -> int:
    row = (
        await db.execute(
            text("SELECT spent_cents FROM wallet_sponsorships WHERE id = :id"),
            {"id": sponsorship_id},
        )
    ).fetchone()
    return int(row[0]) if row else -1


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------


async def test_sponsored_call_debits_sponsor_wallet(
    session_factory: async_sessionmaker[AsyncSession],
    synthetic_subjects: tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID],
) -> None:
    sponsor_id, sponsored_id, fake_patient, _ = synthetic_subjects

    async with session_factory() as db:
        await credits.topup(
            db,
            user_subject_id=sponsor_id,
            amount_cents=500,
            idempotency_key=f"test-topup-{uuid.uuid4()}",
        )
        sp = await spon_svc.create_sponsorship(
            db,
            sponsor_subject_id=sponsor_id,
            sponsored_subject_id=sponsored_id,
            scope_kind="patient",
            scope_id=fake_patient,
            cap_cents=200,
            purpose="e2e test",
        )
        await db.commit()
        sp_id = sp.id

    scopes = [ScopeMatch(scope_kind="patient", scope_id=fake_patient)]
    ref = uuid.uuid4()
    async with session_factory() as db:
        movement = await billing.debit_llm_call(
            db,
            user_subject_id=sponsored_id,
            model_id=_MODEL,
            token_usage=_USAGE_SMALL,
            is_byok=False,
            reference_kind="qna",
            reference_id=ref,
            idempotency_key=f"test-debit-{ref}",
            scopes=scopes,
        )
        await db.commit()

    assert movement is not None
    assert movement.amount_cents < 0

    async with session_factory() as db:
        sponsor_balance = await _balance(db, sponsor_id)
        sponsored_balance = await _balance(db, sponsored_id)
        spent = await _spent(db, sp_id)
        row = (
            await db.execute(
                text(
                    "SELECT user_subject_id, caller_subject_id, sponsorship_id "
                    "FROM credit_ledger WHERE idempotency_key = :k"
                ),
                {"k": f"test-debit-{ref}"},
            )
        ).fetchone()

    assert sponsor_balance == 500 + movement.amount_cents
    assert sponsored_balance == 0
    assert spent == abs(movement.amount_cents)
    assert row is not None
    assert row[0] == sponsor_id
    assert row[1] == sponsored_id
    assert row[2] == sp_id


async def test_overcap_falls_back_to_caller_wallet(
    session_factory: async_sessionmaker[AsyncSession],
    synthetic_subjects: tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID],
) -> None:
    """Cap 10 cents, call costs > 100 cents: the resolver pre-filter
    rejects the sponsorship for lack of headroom and the caller's own
    wallet is debited instead."""
    sponsor_id, sponsored_id, fake_patient, _ = synthetic_subjects

    async with session_factory() as db:
        await credits.topup(
            db,
            user_subject_id=sponsor_id,
            amount_cents=1000,
            idempotency_key=f"sponsor-topup-{uuid.uuid4()}",
        )
        await credits.topup(
            db,
            user_subject_id=sponsored_id,
            amount_cents=500,
            idempotency_key=f"caller-topup-{uuid.uuid4()}",
        )
        sp = await spon_svc.create_sponsorship(
            db,
            sponsor_subject_id=sponsor_id,
            sponsored_subject_id=sponsored_id,
            scope_kind="patient",
            scope_id=fake_patient,
            cap_cents=10,
            purpose="overcap e2e",
        )
        await db.commit()
        sp_id = sp.id

    scopes = [ScopeMatch(scope_kind="patient", scope_id=fake_patient)]
    ref = uuid.uuid4()
    async with session_factory() as db:
        movement = await billing.debit_llm_call(
            db,
            user_subject_id=sponsored_id,
            model_id=_MODEL,
            token_usage=_USAGE_BIG,
            is_byok=False,
            reference_kind="qna",
            reference_id=ref,
            idempotency_key=f"test-overcap-{ref}",
            scopes=scopes,
        )
        await db.commit()

    assert movement is not None
    async with session_factory() as db:
        assert await _balance(db, sponsor_id) == 1000
        assert await _balance(db, sponsored_id) == 500 + movement.amount_cents
        assert await _spent(db, sp_id) == 0
        row = (
            await db.execute(
                text(
                    "SELECT user_subject_id, caller_subject_id, sponsorship_id "
                    "FROM credit_ledger WHERE idempotency_key = :k"
                ),
                {"k": f"test-overcap-{ref}"},
            )
        ).fetchone()
        assert row is not None
        assert row[0] == sponsored_id
        assert row[1] == sponsored_id
        assert row[2] is None


async def test_revoked_sponsorship_is_ignored(
    session_factory: async_sessionmaker[AsyncSession],
    synthetic_subjects: tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID],
) -> None:
    sponsor_id, sponsored_id, fake_patient, _ = synthetic_subjects

    async with session_factory() as db:
        await credits.topup(
            db,
            user_subject_id=sponsored_id,
            amount_cents=500,
            idempotency_key=f"caller-topup-{uuid.uuid4()}",
        )
        sp = await spon_svc.create_sponsorship(
            db,
            sponsor_subject_id=sponsor_id,
            sponsored_subject_id=sponsored_id,
            scope_kind="patient",
            scope_id=fake_patient,
            cap_cents=200,
        )
        await spon_svc.revoke_sponsorship(db, sponsorship_id=sp.id, actor_subject_id=sponsor_id)
        await db.commit()
        sp_id = sp.id

    scopes = [ScopeMatch(scope_kind="patient", scope_id=fake_patient)]
    ref = uuid.uuid4()
    async with session_factory() as db:
        movement = await billing.debit_llm_call(
            db,
            user_subject_id=sponsored_id,
            model_id=_MODEL,
            token_usage=_USAGE_SMALL,
            is_byok=False,
            reference_kind="qna",
            reference_id=ref,
            idempotency_key=f"test-revoked-{ref}",
            scopes=scopes,
        )
        await db.commit()

    assert movement is not None
    async with session_factory() as db:
        assert await _balance(db, sponsored_id) == 500 + movement.amount_cents
        assert await _spent(db, sp_id) == 0


async def test_specificity_consultation_beats_patient(
    session_factory: async_sessionmaker[AsyncSession],
    synthetic_subjects: tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID],
) -> None:
    sponsor_a, sponsored_id, fake_patient, fake_consultation = synthetic_subjects
    sponsor_b = uuid.uuid4()

    async with session_factory() as db:
        await db.execute(
            text(
                "INSERT INTO subjects (id, kind, display_name) VALUES (:s, 'user', 'test_sponsor_b')"
            ),
            {"s": sponsor_b},
        )
        await db.commit()

    try:
        async with session_factory() as db:
            await credits.topup(
                db,
                user_subject_id=sponsor_a,
                amount_cents=500,
                idempotency_key=f"a-topup-{uuid.uuid4()}",
            )
            await credits.topup(
                db,
                user_subject_id=sponsor_b,
                amount_cents=500,
                idempotency_key=f"b-topup-{uuid.uuid4()}",
            )
            await spon_svc.create_sponsorship(
                db,
                sponsor_subject_id=sponsor_a,
                sponsored_subject_id=sponsored_id,
                scope_kind="patient",
                scope_id=fake_patient,
                cap_cents=300,
            )
            sp_consult = await spon_svc.create_sponsorship(
                db,
                sponsor_subject_id=sponsor_b,
                sponsored_subject_id=sponsored_id,
                scope_kind="consultation",
                scope_id=fake_consultation,
                cap_cents=300,
            )
            await db.commit()
            sp_consult_id = sp_consult.id

        scopes = [
            ScopeMatch(scope_kind="consultation", scope_id=fake_consultation),
            ScopeMatch(scope_kind="patient", scope_id=fake_patient),
        ]
        ref = uuid.uuid4()
        async with session_factory() as db:
            movement = await billing.debit_llm_call(
                db,
                user_subject_id=sponsored_id,
                model_id=_MODEL,
                token_usage=_USAGE_SMALL,
                is_byok=False,
                reference_kind="qna",
                reference_id=ref,
                idempotency_key=f"test-spec-{ref}",
                scopes=scopes,
            )
            await db.commit()

        assert movement is not None
        async with session_factory() as db:
            assert await _balance(db, sponsor_a) == 500
            assert await _balance(db, sponsor_b) == 500 + movement.amount_cents
            assert await _spent(db, sp_consult_id) == abs(movement.amount_cents)
    finally:
        async with session_factory() as db:
            await db.execute(
                text(
                    "DELETE FROM credit_ledger WHERE user_subject_id = :s OR caller_subject_id = :s"
                ),
                {"s": sponsor_b},
            )
            await db.execute(
                text("DELETE FROM wallet_sponsorships WHERE sponsor_subject_id = :s"),
                {"s": sponsor_b},
            )
            await db.execute(text("DELETE FROM subjects WHERE id = :s"), {"s": sponsor_b})
            await db.commit()
