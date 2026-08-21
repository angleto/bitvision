"""``POST /api/clinical-events`` — recording a past event in one call.

The bug report has two halves. ``/amend-time`` covers the corrective
one ("there is no way to fix the date afterwards"); this file covers
the constructive one, which the contract calls the supported fix:
a caller who already knows when something happened must be able to say
so at creation time instead of having the insertion moment stamped as a
clinical fact.

What is pinned here:

* ``event_status='completed'`` + ``actual_start_at`` produces the row
  with the LOCAL calendar date, so an evening appointment in
  ``Europe/Rome`` is not filed on the previous day;
* a caller may still send ``event_date`` alongside an anchor, but only
  if the pair agrees — otherwise the trigger would discard their
  ``event_date`` and hand back a date they never asked for, which is
  the silent-revert behaviour the amendment endpoint exists to end;
* the timezone is validated here too, not only on the amend path: it is
  fed to ``AT TIME ZONE`` inside ``fn_ce_derive_event_date`` and an
  unknown name aborts the INSERT as a bare 500;
* the start anchor is required by the status on the way IN, the same
  invariant ``anchor_not_clearable`` enforces on the way through;
* rows with no anchor at all (the DICOM ``StudyDate`` shape) keep the
  ``event_date`` they were created with — those are exactly the rows
  ``/amend-time`` later lets a caller re-date directly.

The legacy ``POST /api/consultations`` shim is covered at the end: it
mints its own ``consultation_event`` and used to stamp
``datetime.now()`` with no way back.

Error bodies are RFC 9457 problem+json: a dict ``detail`` is merged into
the top level of the response (``middleware/problem_details._build_body``),
so the assertions read ``body["code"]``.

Needs a migrated Postgres (``BVP_DATABASE_URL``): the derive trigger is
the substance of the test.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.db.models import ClinicalEvent, Patient, ReportContent

from .conftest import client_as, skip_if_no_db

pytestmark = skip_if_no_db

# 23:30 UTC on a January day is 00:30 of the NEXT day in Europe/Rome
# (UTC+1). Every assertion about the derived date uses this pair, so a
# handler or trigger that projected onto the UTC calendar would return
# 2026-01-15 and fail loudly instead of being off by one day in
# production. Safely in the past, so ``future_actual_time`` never
# interferes.
_ROME_EVENING_UTC = datetime(2026, 1, 15, 23, 30, tzinfo=UTC)
_ROME_LOCAL_DATE = date(2026, 1, 16)


@pytest_asyncio.fixture
async def ctx(db_session: AsyncSession, make_user):
    """Owner user + one patient. The events are created through HTTP,
    which is the point of the file, so there is no event factory here.

    Plain values are yielded rather than ORM instances: ``commit`` and
    ``rollback`` expire every attribute and re-reading one from an async
    session outside a greenlet raises ``MissingGreenlet``.
    """
    user = await make_user()
    patient = Patient(
        id=uuid.uuid4(),
        managed_by_subject_id=user.subject_id,
        display_name=f"Create Patient {uuid.uuid4().hex[:8]}",
    )
    db_session.add(patient)
    await db_session.flush()
    patient_id = patient.id
    await db_session.commit()

    yield SimpleNamespace(user=user, patient_id=patient_id)

    # ``provenance_events.agent_subject_id`` is ON DELETE SET NULL and a
    # human-authored row with a NULL subject violates
    # ck_provenance_events_human_subject_present, so the audit rows must
    # go before ``make_user`` drops the Subject.
    await db_session.rollback()
    for stmt in (
        "DELETE FROM provenance_events WHERE target_kind = 'clinical_event' "
        "AND target_id IN (SELECT id FROM clinical_events WHERE patient_id = :p)",
        "DELETE FROM report_contents WHERE clinical_event_id IN "
        "(SELECT id FROM clinical_events WHERE patient_id = :p)",
        "DELETE FROM notification_dispatches WHERE patient_id = :p",
        "DELETE FROM clinical_events WHERE patient_id = :p",
        "DELETE FROM care_phase_revision WHERE phase_id IN "
        "(SELECT id FROM care_phase WHERE patient_id = :p)",
        "DELETE FROM care_phase WHERE patient_id = :p",
        "DELETE FROM patients WHERE id = :p",
    ):
        await db_session.execute(text(stmt), {"p": patient_id})
    await db_session.commit()


def _body(patient_id: uuid.UUID, **overrides) -> dict:
    payload: dict = {
        "patient_id": str(patient_id),
        "kind": "outpatient_visit",
        "title": "Visita cardiologica",
    }
    payload.update(overrides)
    return payload


async def _event_count(db: AsyncSession, patient_id: uuid.UUID) -> int:
    await db.rollback()
    return (
        await db.execute(
            select(func.count())
            .select_from(ClinicalEvent)
            .where(ClinicalEvent.patient_id == patient_id)
        )
    ).scalar_one()


# ---------------------------------------------------------------------------
# The supported fix: record a past event in ONE call
# ---------------------------------------------------------------------------


@skip_if_no_db
async def test_creating_a_completed_event_with_an_actual_anchor_files_the_local_date(
    db_session, ctx
) -> None:
    """The half of the bug report the contract calls the supported fix.

    Nothing is amended here: the caller states the real instant at
    creation, and the row comes back dated 2026-01-16 because that is
    the calendar day in the event's own timezone. Before the derive
    trigger owned the value, this call recorded the insertion date.
    """
    async with client_as(db_session, ctx.user) as client:
        resp = await client.post(
            "/api/clinical-events",
            json=_body(
                ctx.patient_id,
                event_status="completed",
                actual_start_at=_ROME_EVENING_UTC.isoformat(),
                timezone="Europe/Rome",
            ),
        )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["event_date"] == _ROME_LOCAL_DATE.isoformat()
    assert body["actual_start_at"] == _ROME_EVENING_UTC.isoformat()
    assert body["event_status"] == "completed"
    assert body["timezone"] == "Europe/Rome"

    await db_session.rollback()
    row = (
        await db_session.execute(select(ClinicalEvent).where(ClinicalEvent.id == body["id"]))
    ).scalar_one()
    assert row.event_date == _ROME_LOCAL_DATE, "the DB row, not just the response"
    assert row.actual_start_at == _ROME_EVENING_UTC


@pytest.mark.parametrize(
    ("event_status", "anchor_field", "anchor"),
    [
        ("completed", "actual_start_at", _ROME_EVENING_UTC),
        ("planned", "planned_start_at", datetime(2026, 9, 15, 23, 30, tzinfo=UTC)),
    ],
)
@skip_if_no_db
async def test_creating_with_an_event_date_that_agrees_with_the_anchor_is_accepted(
    db_session, ctx, event_status, anchor_field, anchor
) -> None:
    """A client that computes the local date itself is not refused for
    saying out loud what the trigger would derive anyway.

    Both anchor families are covered because the conflict check picks
    the anchor from the status (``_PLANNED_ANCHOR_STATUSES``); reading
    the wrong one would let a mismatched pair through on one family.
    """
    local_date = date(anchor.year, anchor.month, anchor.day) + timedelta(days=1)
    async with client_as(db_session, ctx.user) as client:
        resp = await client.post(
            "/api/clinical-events",
            json=_body(
                ctx.patient_id,
                event_status=event_status,
                timezone="Europe/Rome",
                event_date=local_date.isoformat(),
                **{anchor_field: anchor.isoformat()},
            ),
        )
    assert resp.status_code == 201, resp.text
    assert resp.json()["event_date"] == local_date.isoformat()


@skip_if_no_db
async def test_creating_with_an_event_date_that_contradicts_the_anchor_is_refused(
    db_session, ctx
) -> None:
    """The trap this 422 exists for: a UTC-thinking client sends the
    anchor's UTC date while the row's timezone puts it on the next day.

    Accepting it would store 2026-01-15 and the trigger would return
    2026-01-16 in the same response — a value nobody asked for. The body
    echoes the derived date so the client can fix the payload without
    guessing.
    """
    async with client_as(db_session, ctx.user) as client:
        resp = await client.post(
            "/api/clinical-events",
            json=_body(
                ctx.patient_id,
                event_status="completed",
                actual_start_at=_ROME_EVENING_UTC.isoformat(),
                timezone="Europe/Rome",
                event_date="2026-01-15",
            ),
        )
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["code"] == "event_date_conflicts_with_anchor"
    assert body["event_date"] == "2026-01-15"
    assert body["derived"] == _ROME_LOCAL_DATE.isoformat()

    assert await _event_count(db_session, ctx.patient_id) == 0, "a refused create must not persist"


@skip_if_no_db
async def test_creating_with_an_unknown_timezone_is_refused(db_session, ctx) -> None:
    """``timezone`` reaches ``AT TIME ZONE`` inside the derive trigger.
    An IANA name Postgres does not know aborts the INSERT, so it has to
    be caught in the handler and answered with the documented 422 rather
    than surfacing as a bare 500 — the same guard the amend path has."""
    async with client_as(db_session, ctx.user) as client:
        resp = await client.post(
            "/api/clinical-events",
            json=_body(
                ctx.patient_id,
                event_status="completed",
                actual_start_at=_ROME_EVENING_UTC.isoformat(),
                timezone="Mars/Olympus_Mons",
            ),
        )
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["code"] == "invalid_timezone"
    assert body["timezone"] == "Mars/Olympus_Mons"

    assert await _event_count(db_session, ctx.patient_id) == 0, "a refused create must not persist"


@pytest.mark.parametrize("event_status", ["planned", "confirmed"])
@skip_if_no_db
async def test_creating_a_scheduled_event_without_its_start_anchor_is_refused(
    db_session, ctx, event_status
) -> None:
    """The CREATE-side twin of ``anchor_not_clearable``.

    The start anchor defines the row's date for its whole life: the
    amend path refuses to remove it, and this path refuses to omit it.
    Without the handler check the DB CHECK
    ``ck_clinical_events_time_required_by_status`` fires instead and the
    caller gets an IntegrityError-shaped 500 for a plain input error.
    """
    async with client_as(db_session, ctx.user) as client:
        resp = await client.post(
            "/api/clinical-events",
            json=_body(ctx.patient_id, event_status=event_status),
        )
    assert resp.status_code == 422, resp.text
    assert "planned_start_at" in resp.text

    assert await _event_count(db_session, ctx.patient_id) == 0, "a refused create must not persist"


@skip_if_no_db
async def test_creating_a_date_only_event_keeps_the_supplied_event_date(db_session, ctx) -> None:
    """The legacy shape, still the majority of imported history: a date
    and no time of day. With both anchors NULL the trigger leaves
    ``event_date`` alone, which is precisely why ``/amend-time`` accepts
    a direct ``event_date`` write on these rows and refuses it on the
    others."""
    async with client_as(db_session, ctx.user) as client:
        resp = await client.post(
            "/api/clinical-events",
            json=_body(ctx.patient_id, event_status="completed", event_date="2019-06-01"),
        )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["event_date"] == "2019-06-01"
    assert body["actual_start_at"] is None

    await db_session.rollback()
    row = (
        await db_session.execute(select(ClinicalEvent).where(ClinicalEvent.id == body["id"]))
    ).scalar_one()
    assert row.event_date == date(2019, 6, 1)


# ---------------------------------------------------------------------------
# The legacy consultations shim
# ---------------------------------------------------------------------------


async def _event_date_of_consultation(db: AsyncSession, consultation_id: str) -> date | None:
    await db.rollback()
    return (
        await db.execute(
            select(ClinicalEvent.event_date)
            .join(ReportContent, ReportContent.clinical_event_id == ClinicalEvent.id)
            .where(ReportContent.id == uuid.UUID(consultation_id))
        )
    ).scalar_one()


@skip_if_no_db
async def test_consultation_create_honours_an_explicit_past_event_date(db_session, ctx) -> None:
    """``POST /api/consultations`` mints its own ``consultation_event``.

    It used to stamp ``datetime.now()`` unconditionally, so importing an
    older consultation recorded the insertion moment as a clinical fact
    with no way back — the exact sentence in the bug report. The caller
    can now state the real date up front.
    """
    async with client_as(db_session, ctx.user) as client:
        resp = await client.post(
            "/api/consultations",
            json={
                "patient_id": str(ctx.patient_id),
                "title": "Consulto oncologico (import)",
                "summary_md": "sintesi",
                "event_date": "2024-11-03",
            },
        )
    assert resp.status_code == 201, resp.text
    assert await _event_date_of_consultation(db_session, resp.json()["id"]) == date(2024, 11, 3)


@skip_if_no_db
async def test_consultation_create_still_defaults_to_today(db_session, ctx) -> None:
    """The default is unchanged: a synthesis produced now genuinely
    happened now. Pinned so the new optional field cannot quietly turn
    into "no date at all" for every existing caller."""
    async with client_as(db_session, ctx.user) as client:
        resp = await client.post(
            "/api/consultations",
            json={
                "patient_id": str(ctx.patient_id),
                "title": "Consulto odierno",
                "summary_md": "sintesi",
            },
        )
    assert resp.status_code == 201, resp.text
    assert (
        await _event_date_of_consultation(db_session, resp.json()["id"]) == datetime.now(UTC).date()
    )
