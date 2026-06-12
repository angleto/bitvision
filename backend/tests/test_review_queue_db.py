"""DB-backed checks for migration 0024 (shared review-queue primitives).

Verifies against the real Postgres that:

* the ``review_status`` ENUM exists with exactly the engine's statuses
  in order;
* the widened ``provenance_events`` CHECKs accept the review-queue
  activities/target kinds the engine emits (and still reject garbage).

Everything runs inside the rolled-back ``db_session`` so no rows leak.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.services.provenance_log import record_provenance_event
from bvphoenix.services.review_queue.states import REVIEW_STATUSES
from tests.conftest import skip_if_no_db


@skip_if_no_db
@pytest.mark.asyncio
async def test_review_status_enum_matches_states(db_session: AsyncSession) -> None:
    rows = (
        await db_session.execute(
            text(
                "SELECT e.enumlabel FROM pg_enum e "
                "JOIN pg_type t ON t.oid = e.enumtypid "
                "WHERE t.typname = 'review_status' ORDER BY e.enumsortorder"
            )
        )
    ).scalars()
    assert tuple(rows) == REVIEW_STATUSES


@skip_if_no_db
@pytest.mark.asyncio
async def test_provenance_checks_accept_review_transitions(db_session: AsyncSession) -> None:
    record_provenance_event(
        db_session,
        target_kind="inbox_item",
        target_id=uuid.uuid4(),
        activity="transition.accepted",
        agent_kind="system",
        agent_subject_id=None,
        diff={"from": "needs_review", "to": "accepted"},
    )
    record_provenance_event(
        db_session,
        target_kind="submission",
        target_id=uuid.uuid4(),
        activity="transition.rejected",
        agent_kind="system",
        agent_subject_id=None,
    )
    await db_session.flush()  # CHECK constraints evaluate here


@skip_if_no_db
@pytest.mark.asyncio
async def test_provenance_checks_still_reject_unknown_activity(
    db_session: AsyncSession,
) -> None:
    from sqlalchemy.exc import DBAPIError

    record_provenance_event(
        db_session,
        target_kind="inbox_item",
        target_id=uuid.uuid4(),
        activity="transition.not_a_state",
        agent_kind="system",
        agent_subject_id=None,
    )
    with pytest.raises(DBAPIError):
        await db_session.flush()
    await db_session.rollback()
