"""F6: per-study training consent lifecycle.

Unit tests on ``services.consent_auto``. Exercises both the grant
side (``ensure_tier_consent[s]``) and the revoke side
(``revoke_tier_consent_for_study``). Stubs ``db.execute`` so the
tests don't need a live Postgres — the service only reads active
rows by (user, study, tier) filter + inserts through ``db.add``.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from bvphoenix.db.models import TrainingConsent
from bvphoenix.services.consent_auto import (
    CURRENT_CONSENT_VERSION,
    ensure_tier_consent,
    ensure_tier_consents,
    revoke_tier_consent_for_study,
)


class _ScalarResult:
    """Supports both ``scalar_one_or_none()`` (active-row lookup in
    ``ensure_tier_consent``) and ``scalars().all()`` (revoke-side list
    lookup). A single payload drives both shapes.
    """

    def __init__(self, rows: TrainingConsent | list[TrainingConsent] | None) -> None:
        self._rows = rows

    def scalar_one_or_none(self) -> TrainingConsent | None:
        if isinstance(self._rows, list):
            return self._rows[0] if self._rows else None
        return self._rows

    def scalars(self) -> _ScalarResult:
        return self

    def all(self) -> list[TrainingConsent]:
        if self._rows is None:
            return []
        if isinstance(self._rows, list):
            return self._rows
        return [self._rows]


class _StubDB:
    """Ordered-response fake. Each ``execute()`` pops the next entry."""

    def __init__(
        self,
        responses: list[TrainingConsent | list[TrainingConsent] | None] | None = None,
    ) -> None:
        self._responses = list(responses or [])
        self.added: list[Any] = []
        self.flushed = 0

    async def execute(self, _stmt: Any) -> _ScalarResult:
        row = self._responses.pop(0) if self._responses else None
        return _ScalarResult(row)

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        self.flushed += 1


# --- ensure_tier_consent --------------------------------------------------


@pytest.mark.asyncio
async def test_t1_is_noop() -> None:
    db = _StubDB()
    out = await ensure_tier_consent(
        db,
        user_subject_id=uuid.uuid4(),
        study_id=uuid.uuid4(),
        tier="t1",
    )
    assert out is None
    assert db.added == []
    assert db.flushed == 0


@pytest.mark.asyncio
async def test_t2_is_noop() -> None:
    db = _StubDB()
    out = await ensure_tier_consent(
        db,
        user_subject_id=uuid.uuid4(),
        study_id=uuid.uuid4(),
        tier="t2",
    )
    assert out is None
    assert db.added == []


@pytest.mark.asyncio
async def test_t3_inserts_active_row() -> None:
    db = _StubDB(responses=[None])  # no existing row
    uid = uuid.uuid4()
    sid = uuid.uuid4()
    out = await ensure_tier_consent(db, user_subject_id=uid, study_id=sid, tier="t3")
    assert out is not None
    assert out.user_subject_id == uid
    assert out.study_id == sid
    assert out.tier == "t3"
    assert out.consent_version == CURRENT_CONSENT_VERSION
    # sha256 hex = 64 chars.
    assert len(out.consent_hash) == 64
    assert out in db.added
    assert db.flushed == 1


@pytest.mark.asyncio
async def test_t4_inserts_active_row_with_t4_hash() -> None:
    db_a = _StubDB(responses=[None])
    db_b = _StubDB(responses=[None])
    uid = uuid.uuid4()
    sid = uuid.uuid4()
    t3_row = await ensure_tier_consent(db_a, user_subject_id=uid, study_id=sid, tier="t3")
    t4_row = await ensure_tier_consent(db_b, user_subject_id=uid, study_id=sid, tier="t4")
    # Different tier => different consent text => different hash.
    assert t3_row is not None and t4_row is not None
    assert t3_row.consent_hash != t4_row.consent_hash


@pytest.mark.asyncio
async def test_idempotent_reupload_reuses_active_row() -> None:
    existing = TrainingConsent(
        user_subject_id=uuid.uuid4(),
        study_id=uuid.uuid4(),
        tier="t3",
        consent_version=1,
        consent_hash="a" * 64,
    )
    db = _StubDB(responses=[existing])
    out = await ensure_tier_consent(
        db,
        user_subject_id=existing.user_subject_id,
        study_id=existing.study_id,
        tier="t3",
    )
    assert out is existing
    assert db.added == []
    assert db.flushed == 0


# --- ensure_tier_consents (batch) ----------------------------------------


@pytest.mark.asyncio
async def test_batch_t3_inserts_one_row_per_study() -> None:
    # Two studies, neither has an existing row.
    db = _StubDB(responses=[None, None])
    uid = uuid.uuid4()
    sids = [uuid.uuid4(), uuid.uuid4()]
    out = await ensure_tier_consents(db, user_subject_id=uid, tier="t3", study_ids=sids)
    assert len(out) == 2
    assert {row.study_id for row in out} == set(sids)
    assert db.flushed == 2


@pytest.mark.asyncio
async def test_batch_t1_is_noop() -> None:
    db = _StubDB()
    out = await ensure_tier_consents(
        db, user_subject_id=uuid.uuid4(), tier="t1", study_ids=[uuid.uuid4()]
    )
    assert out == []
    assert db.added == []


@pytest.mark.asyncio
async def test_batch_empty_study_ids_is_noop() -> None:
    db = _StubDB()
    out = await ensure_tier_consents(db, user_subject_id=uuid.uuid4(), tier="t3", study_ids=[])
    assert out == []
    assert db.flushed == 0


# --- revoke_tier_consent_for_study ---------------------------------------


@pytest.mark.asyncio
async def test_revoke_sets_revoked_at_on_active_row() -> None:
    uid = uuid.uuid4()
    sid = uuid.uuid4()
    row = TrainingConsent(
        user_subject_id=uid,
        study_id=sid,
        tier="t3",
        consent_version=1,
        consent_hash="a" * 64,
    )
    db = _StubDB(responses=[[row]])
    out = await revoke_tier_consent_for_study(
        db, user_subject_id=uid, study_id=sid, reason="user opt-out"
    )
    assert out == [row]
    assert row.revoked_at is not None
    assert row.revoke_reason == "user opt-out"
    assert db.flushed == 1


@pytest.mark.asyncio
async def test_revoke_is_noop_when_no_active_rows() -> None:
    db = _StubDB(responses=[[]])
    out = await revoke_tier_consent_for_study(
        db, user_subject_id=uuid.uuid4(), study_id=uuid.uuid4()
    )
    assert out == []
    assert db.flushed == 0


@pytest.mark.asyncio
async def test_revoke_touches_t3_and_t4_rows_for_same_study() -> None:
    """A study that was re-tiered without a clean revoke can end up
    with two active consent rows (t3 + t4). The revoke path must
    close both."""
    uid = uuid.uuid4()
    sid = uuid.uuid4()
    t3 = TrainingConsent(
        user_subject_id=uid,
        study_id=sid,
        tier="t3",
        consent_version=1,
        consent_hash="a" * 64,
    )
    t4 = TrainingConsent(
        user_subject_id=uid,
        study_id=sid,
        tier="t4",
        consent_version=1,
        consent_hash="b" * 64,
    )
    db = _StubDB(responses=[[t3, t4]])
    out = await revoke_tier_consent_for_study(db, user_subject_id=uid, study_id=sid)
    assert out == [t3, t4]
    assert all(row.revoked_at is not None for row in out)
