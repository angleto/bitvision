"""Migration 0098 — clinical_events planning + calendar v1.

Pins the invariants introduced by ``0098_clinical_events_calendar_v1``:

1. ``event_status`` defaults to ``'completed'`` for back-compat with
   pre-0098 callers.
2. CHECK ``ck_clinical_events_time_required_by_status`` rejects
   ``event_status='planned'`` / ``'confirmed'`` rows without a
   ``planned_start_at`` timestamp.
3. The ``fn_ce_derive_event_date()`` trigger derives ``event_date``
   from ``planned_start_at`` / ``actual_start_at`` honouring the
   ``timezone`` IANA name (so an event at 23:30 Europe/Rome lands
   on the local DATE, not on the UTC-next-day).
4. Composite FK ``fk_clinical_events_parent`` makes cross-patient
   ``parent_event_id`` impossible at the DB level — the seventh
   layer of cross-patient defense.
5. The list endpoint accepts ``?statuses=...`` (multi-value) and
   filters server-side.

All tests are gated on ``skip_if_no_db`` because the trigger / CHECK
behaviour only exists in a real Postgres with the migration applied.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timezone

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.db.models import ClinicalEvent, Patient
from bvphoenix.db.models.principals import Subject

from .conftest import skip_if_no_db

pytestmark = skip_if_no_db


async def _new_patient(db_session: AsyncSession) -> Patient:
    """Create one Patient + its owning Subject, flushed.

    Inline helper rather than a fixture so each test owns the
    creation and there is no fixture-teardown coroutine left
    hanging on the event loop between tests (which was producing
    "got Future attached to a different loop" errors when an
    async fixture was reused).
    """
    sid = uuid.uuid4()
    db_session.add(Subject(id=sid, kind="user", display_name=f"sub-{sid}"))
    await db_session.flush()
    patient = Patient(
        id=uuid.uuid4(),
        managed_by_subject_id=sid,
        display_name=f"Test Patient {uuid.uuid4().hex[:8]}",
    )
    db_session.add(patient)
    await db_session.flush()
    return patient


@skip_if_no_db
async def test_event_status_default_is_completed(db_session) -> None:
    """A new event without an explicit ``event_status`` inherits the
    server_default ``'completed'`` — keeps pre-0098 callers working."""
    patient = await _new_patient(db_session)
    ev = ClinicalEvent(
        patient_id=patient.id,
        kind="outpatient_visit",
        title="Default-status visit",
    )
    db_session.add(ev)
    await db_session.flush()
    await db_session.refresh(ev)
    assert ev.event_status == "completed"


@skip_if_no_db
async def test_planned_requires_planned_start_at(db_session) -> None:
    """``event_status='planned'`` without ``planned_start_at`` violates
    ``ck_clinical_events_time_required_by_status``."""
    patient = await _new_patient(db_session)
    ev = ClinicalEvent(
        patient_id=patient.id,
        kind="outpatient_visit",
        title="Planned without when",
        event_status="planned",
        # planned_start_at intentionally omitted → CHECK fires.
    )
    db_session.add(ev)
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


@skip_if_no_db
async def test_planned_with_planned_start_at_passes(db_session) -> None:
    """The same insert with a ``planned_start_at`` succeeds."""
    patient = await _new_patient(db_session)
    ev = ClinicalEvent(
        patient_id=patient.id,
        kind="outpatient_visit",
        title="Planned visit",
        event_status="planned",
        planned_start_at=datetime(2026, 6, 1, 10, 0, tzinfo=UTC),
        timezone="Europe/Rome",
    )
    db_session.add(ev)
    await db_session.flush()
    await db_session.refresh(ev)
    assert ev.event_status == "planned"
    assert ev.planned_start_at is not None


@skip_if_no_db
async def test_trigger_derives_event_date_for_planned(db_session) -> None:
    """For a ``planned`` event, ``event_date`` is derived from
    ``planned_start_at`` at the supplied timezone — Europe/Rome at 00:30
    UTC-next-day is still the local previous day, so the trigger must
    respect the TZ rather than naively cast to UTC date."""
    patient = await _new_patient(db_session)
    # 22:30 UTC on 2026-06-15 == 00:30 Rome on 2026-06-16 (DST: +2).
    # Without the timezone clause the trigger would compute event_date
    # = 2026-06-15. The trigger we ship must compute 2026-06-16.
    ev = ClinicalEvent(
        patient_id=patient.id,
        kind="outpatient_visit",
        title="Late-evening visit",
        event_status="planned",
        planned_start_at=datetime(2026, 6, 15, 22, 30, tzinfo=UTC),
        timezone="Europe/Rome",
    )
    db_session.add(ev)
    await db_session.flush()
    await db_session.refresh(ev)
    assert ev.event_date == date(2026, 6, 16), (
        f"trigger should derive 2026-06-16 (Rome local DATE for 22:30 UTC + 2h DST), "
        f"got {ev.event_date}"
    )


@skip_if_no_db
async def test_trigger_derives_event_date_for_completed(db_session) -> None:
    """For a ``completed`` event with an explicit ``actual_start_at``,
    the trigger derives ``event_date`` from the actual timestamp."""
    patient = await _new_patient(db_session)
    ev = ClinicalEvent(
        patient_id=patient.id,
        kind="surgical_procedure",
        title="Past surgery",
        event_status="completed",
        actual_start_at=datetime(2024, 3, 5, 8, 15, tzinfo=UTC),
        timezone="Europe/Rome",
    )
    db_session.add(ev)
    await db_session.flush()
    await db_session.refresh(ev)
    assert ev.event_date == date(2024, 3, 5)


@skip_if_no_db
async def test_composite_fk_parent_cross_patient_blocks(db_session) -> None:
    """Cross-patient ``parent_event_id`` is rejected at the DB level
    by the composite FK ``fk_clinical_events_parent``. This is the
    seventh layer of cross-patient defense (DB-enforced, not just
    application-enforced)."""
    patient_a = await _new_patient(db_session)
    patient_b = await _new_patient(db_session)

    # An event under patient A — the would-be parent.
    parent = ClinicalEvent(
        patient_id=patient_a.id,
        kind="outpatient_visit",
        title="Parent event A",
        event_status="completed",
        actual_start_at=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
    )
    db_session.add(parent)
    await db_session.commit()
    await db_session.refresh(parent)

    # Try to insert a child under patient B pointing at patient A's
    # event. The composite FK (patient_id, parent_event_id) → (patient_id,
    # id) cannot match: patient B + parent_id-of-A is not a row in
    # clinical_events.
    child = ClinicalEvent(
        patient_id=patient_b.id,
        kind="outpatient_visit",
        title="Child event B with cross-patient parent",
        event_status="completed",
        actual_start_at=datetime(2026, 1, 2, 12, 0, tzinfo=UTC),
        parent_event_id=parent.id,
    )
    db_session.add(child)
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


@skip_if_no_db
async def test_same_patient_parent_succeeds(db_session) -> None:
    """Same-patient reschedule chain is allowed — the FK is composite
    on (patient_id, parent_event_id), so as long as the parent shares
    the same patient_id the link goes through."""
    patient = await _new_patient(db_session)
    parent = ClinicalEvent(
        patient_id=patient.id,
        kind="outpatient_visit",
        title="Original appointment",
        event_status="rescheduled",
        planned_start_at=datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
    )
    db_session.add(parent)
    await db_session.commit()
    await db_session.refresh(parent)

    child = ClinicalEvent(
        patient_id=patient.id,
        kind="outpatient_visit",
        title="Rescheduled appointment",
        event_status="planned",
        planned_start_at=datetime(2026, 1, 8, 9, 0, tzinfo=UTC),
        parent_event_id=parent.id,
    )
    db_session.add(child)
    await db_session.commit()
    await db_session.refresh(child)
    assert child.parent_event_id == parent.id


@skip_if_no_db
async def test_list_filter_statuses_returns_only_matching(db_session) -> None:
    """The list query filters by ``event_status`` IN (...). We exercise
    the same SQL the API endpoint runs to avoid stubbing the whole
    FastAPI dispatch — what we care about is the WHERE clause behaving
    correctly for the new column + indices."""
    patient = await _new_patient(db_session)
    # Two planned, one completed, one cancelled.
    rows = [
        ClinicalEvent(
            patient_id=patient.id,
            kind="outpatient_visit",
            title=f"Planned {i}",
            event_status="planned",
            planned_start_at=datetime(2026, 7, i + 1, 10, 0, tzinfo=UTC),
        )
        for i in range(2)
    ]
    rows.append(
        ClinicalEvent(
            patient_id=patient.id,
            kind="outpatient_visit",
            title="Done",
            event_status="completed",
            actual_start_at=datetime(2025, 12, 1, 10, 0, tzinfo=UTC),
        )
    )
    rows.append(
        ClinicalEvent(
            patient_id=patient.id,
            kind="outpatient_visit",
            title="Cancelled",
            event_status="cancelled",
        )
    )
    db_session.add_all(rows)
    await db_session.commit()

    stmt = (
        select(ClinicalEvent)
        .where(ClinicalEvent.patient_id == patient.id)
        .where(ClinicalEvent.event_status.in_(["planned", "confirmed"]))
    )
    found = (await db_session.execute(stmt)).scalars().all()
    assert len(found) == 2
    assert all(e.event_status == "planned" for e in found)


@skip_if_no_db
async def test_status_changed_by_kind_check(db_session) -> None:
    """``status_changed_by_kind`` accepts only human/agent/system."""
    patient = await _new_patient(db_session)
    ev = ClinicalEvent(
        patient_id=patient.id,
        kind="outpatient_visit",
        title="Bad kind",
        event_status="planned",
        planned_start_at=datetime(2026, 8, 1, 10, 0, tzinfo=UTC),
        status_changed_by_kind="robot",  # invalid
    )
    db_session.add(ev)
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


@skip_if_no_db
async def test_partial_index_for_planned_present(db_session) -> None:
    """The partial index ``ix_clinical_events_patient_status_planned``
    is created by migration 0098 and must be present in the schema.
    Catches a regression where the migration silently down-grades."""
    result = await db_session.execute(
        text(
            """
            SELECT indexname FROM pg_indexes
            WHERE schemaname = 'public'
              AND tablename = 'clinical_events'
              AND indexname IN (
                  'ix_clinical_events_patient_status_planned',
                  'ix_clinical_events_patient_actual',
                  'ix_clinical_events_parent',
                  'ix_clinical_events_external_calendar'
              )
            ORDER BY indexname;
            """
        )
    )
    names = sorted(r[0] for r in result.fetchall())
    assert names == [
        "ix_clinical_events_external_calendar",
        "ix_clinical_events_parent",
        "ix_clinical_events_patient_actual",
        "ix_clinical_events_patient_status_planned",
    ]
