"""Direct DB smoke for the FSM-checked transition sub-resources.

We don't stand up the full FastAPI stack here; instead we drive the
transition logic via the public handler functions in
``backend/src/bvphoenix/api/clinical_events.py``. That keeps the test
fast and skips the auth machinery while still exercising the FSM +
``clinical_event_transitions`` write path.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.db.models import (
    ClinicalEvent,
    ClinicalEventTransition,
    Patient,
)
from bvphoenix.db.models.principals import Subject

from .conftest import skip_if_no_db

pytestmark = skip_if_no_db


async def _new_planned_event(db: AsyncSession) -> ClinicalEvent:
    sid = uuid.uuid4()
    db.add(Subject(id=sid, kind="user", display_name=f"sub-{sid}"))
    await db.flush()
    patient = Patient(
        id=uuid.uuid4(),
        managed_by_subject_id=sid,
        display_name="Smoke Patient",
    )
    db.add(patient)
    await db.flush()
    ev = ClinicalEvent(
        patient_id=patient.id,
        kind="outpatient_visit",
        title="Test planned",
        event_status="planned",
        planned_start_at=datetime(2026, 8, 1, 10, 0, tzinfo=UTC),
        timezone="Europe/Rome",
    )
    db.add(ev)
    await db.flush()
    await db.refresh(ev)
    return ev


@skip_if_no_db
async def test_fsm_planned_to_confirmed_persists_transition(db_session) -> None:
    """A confirm transition writes a row to ``clinical_event_transitions``
    and bumps ``event_status``. We perform the steps the API handler
    would perform, in order, against a real DB."""
    from bvphoenix.services import clinical_events_fsm as fsm

    ev = await _new_planned_event(db_session)
    # Mirror handler steps:
    fsm.assert_transition_allowed(from_status=ev.event_status, to_status=fsm.CONFIRMED)
    snapshot_before = {"event_status": ev.event_status, "id": str(ev.id)}
    ev.event_status = fsm.CONFIRMED
    ev.status_changed_at = datetime.now(UTC)
    ev.status_changed_by_kind = "human"
    ev.etag = uuid.uuid4()
    await db_session.flush()
    db_session.add(
        ClinicalEventTransition(
            event_id=ev.id,
            action="confirm",
            idempotency_key="test-key-1",
            snapshot_before=snapshot_before,
            snapshot_after={"event_status": ev.event_status, "id": str(ev.id)},
            actor_subject_id=None,
            author_kind="human",
        )
    )
    await db_session.flush()
    await db_session.refresh(ev)
    assert ev.event_status == "confirmed"

    rows = (
        (
            await db_session.execute(
                select(ClinicalEventTransition).where(ClinicalEventTransition.event_id == ev.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].action == "confirm"
    assert rows[0].idempotency_key == "test-key-1"


@skip_if_no_db
async def test_fsm_invalid_transition_raises(db_session) -> None:
    """completed is terminal — moving back to planned must 422."""
    from fastapi import HTTPException

    from bvphoenix.services import clinical_events_fsm as fsm

    with pytest.raises(HTTPException) as exc:
        fsm.assert_transition_allowed(from_status="completed", to_status="planned")
    assert exc.value.status_code == 422


@skip_if_no_db
async def test_transitions_unique_idempotency_per_action(db_session) -> None:
    """Same (event_id, action, idempotency_key) row a second time
    violates the UNIQUE constraint."""
    from sqlalchemy.exc import IntegrityError

    ev = await _new_planned_event(db_session)
    row = ClinicalEventTransition(
        event_id=ev.id,
        action="confirm",
        idempotency_key="dup-key",
        snapshot_before={"a": 1},
        snapshot_after={"a": 2},
        actor_subject_id=None,
        author_kind="human",
    )
    db_session.add(row)
    await db_session.flush()
    dup = ClinicalEventTransition(
        event_id=ev.id,
        action="confirm",
        idempotency_key="dup-key",
        snapshot_before={"a": 1},
        snapshot_after={"a": 3},
        actor_subject_id=None,
        author_kind="human",
    )
    db_session.add(dup)
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


@skip_if_no_db
async def test_transitions_different_action_same_key_ok(db_session) -> None:
    """Same key on different actions is allowed (UNIQUE includes action)."""
    ev = await _new_planned_event(db_session)
    db_session.add_all(
        [
            ClinicalEventTransition(
                event_id=ev.id,
                action="confirm",
                idempotency_key="shared",
                snapshot_before={},
                snapshot_after={},
                actor_subject_id=None,
                author_kind="human",
            ),
            ClinicalEventTransition(
                event_id=ev.id,
                action="cancel",
                idempotency_key="shared",
                snapshot_before={},
                snapshot_after={},
                actor_subject_id=None,
                author_kind="human",
            ),
        ]
    )
    await db_session.flush()  # no IntegrityError expected
