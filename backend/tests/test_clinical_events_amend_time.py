"""``POST /api/clinical-events/{id}/amend-time`` — the missing writer.

The bug report, in the product owner's words: *"editing the date of a
not-yet-happened event returns 500; and there is no way to record the
correct date of a past event because the system stamps the insertion
time as the actual date and it cannot be changed, not even as admin."*

Both halves land on this endpoint. ``event_date`` is a DERIVED
projection of the status anchor (``fn_ce_derive_event_date``, migration
0047), so the only honest way to re-date an event is to move the anchor
and let the trigger recompute — which is what this sub-resource does,
while leaving ``event_status`` alone, because correcting *when we
recorded that something happened* is an amendment, not a lifecycle
transition. That distinction is why the endpoint is legal on terminal
rows where every FSM verb is refused.

Contract pinned here:

* the anchor moves, ``event_date`` follows **in the row's timezone**,
  and ``event_status`` / ``status_change_reason`` / ``phase_id`` do not
  move;
* exactly one ``clinical_event_transitions`` row (``action='amend_time'``)
  and one ``provenance_events`` row (``activity='transition.amend_time'``)
  per accepted amendment;
* pending reminders are re-materialised against the new anchor;
* every documented 422 code;
* ``If-Match`` + ``Idempotency-Key`` are mandatory, a replay is free,
  and ``dry_run`` persists nothing.

Error bodies are RFC 9457: a dict ``detail`` is merged into the top
level of the response (``middleware/problem_details._build_body``), so
the code lives at ``body["code"]``.

Needs a migrated Postgres (``BVP_DATABASE_URL``).
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.db.models import (
    AgentAssistant,
    AgentAssistantPatient,
    CarePhase,
    ClinicalEvent,
    ClinicalEventTransition,
    NotificationDispatch,
    Patient,
    PatientContact,
    ProvenanceEvent,
)

from .conftest import client_as, client_as_bearer, skip_if_no_db

pytestmark = skip_if_no_db

# 23:30 UTC on a January day is 00:30 of the NEXT day in Europe/Rome
# (UTC+1). Amending to this instant is the single assertion that proves
# both halves of the bug report at once: a past event can be re-dated,
# and the date that comes back is the LOCAL calendar date, not the UTC
# one. Comfortably in the past relative to any plausible run date, so
# the ``future_actual_time`` guard never interferes.
_ROME_MIDNIGHT_UTC = datetime(2026, 1, 15, 23, 30, tzinfo=UTC)
_ROME_LOCAL_DATE = date(2026, 1, 16)


def _headers(etag: str, *, key: str | None = None, wildcard: bool = False) -> dict[str, str]:
    out = {"If-Match": "*" if wildcard else f'"{etag}"'}
    out["Idempotency-Key"] = key or str(uuid.uuid4())
    return out


@pytest_asyncio.fixture
async def ctx(db_session: AsyncSession, make_user):
    """Owner + patient + a factory for events of any shape.

    The factory returns plain values (id, etag, ...) rather than the ORM
    instance: ``commit`` and ``rollback`` expire every attribute, and
    reading one back from an async session outside a greenlet raises
    ``MissingGreenlet``. Tests re-read state through explicit queries.
    """
    user = await make_user()
    patient = Patient(
        id=uuid.uuid4(),
        managed_by_subject_id=user.subject_id,
        display_name=f"Amend Patient {uuid.uuid4().hex[:8]}",
    )
    db_session.add(patient)
    await db_session.flush()
    patient_id = patient.id
    # Captured before the commit expires the instance: reading
    # ``user.subject_id`` back from an async session outside a greenlet
    # raises MissingGreenlet, and the audit assertions need the value.
    owner_subject_id = user.subject_id
    await db_session.commit()

    async def make_event(**kwargs) -> SimpleNamespace:
        kwargs.setdefault("kind", "outpatient_visit")
        kwargs.setdefault("title", "Amendable event")
        kwargs.setdefault("timezone", "Europe/Rome")
        ev = ClinicalEvent(id=uuid.uuid4(), patient_id=patient_id, **kwargs)
        db_session.add(ev)
        await db_session.flush()
        # ``etag`` and ``event_date`` are server-side (default + BEFORE
        # trigger), so read them back before the commit expires the row.
        await db_session.refresh(ev)
        snap = SimpleNamespace(
            id=ev.id,
            etag=str(ev.etag),
            event_date=ev.event_date,
            event_status=ev.event_status,
        )
        await db_session.commit()
        return snap

    yield SimpleNamespace(
        user=user,
        subject_id=owner_subject_id,
        patient_id=patient_id,
        make_event=make_event,
    )

    # ``provenance_events.agent_subject_id`` is ON DELETE SET NULL and a
    # human-authored row with a NULL subject violates
    # ck_provenance_events_human_subject_present, so the audit rows must
    # go before ``make_user`` drops the Subject.
    await db_session.rollback()
    for stmt in (
        "DELETE FROM provenance_events WHERE target_kind = 'clinical_event' "
        "AND target_id IN (SELECT id FROM clinical_events WHERE patient_id = :p)",
        "DELETE FROM notification_dispatches WHERE patient_id = :p",
        "DELETE FROM clinical_events WHERE patient_id = :p",
        "DELETE FROM care_phase_revision WHERE phase_id IN "
        "(SELECT id FROM care_phase WHERE patient_id = :p)",
        "DELETE FROM care_phase WHERE patient_id = :p",
        "DELETE FROM patient_contacts WHERE patient_id = :p",
        "DELETE FROM patients WHERE id = :p",
    ):
        await db_session.execute(text(stmt), {"p": patient_id})
    await db_session.commit()


@pytest.fixture
def notifications_on(monkeypatch: pytest.MonkeyPatch):
    """Force the two feature flags the reminder assertions depend on.

    ``materialise_event_dispatches`` returns 0 when
    ``notifications_enabled`` is off, and the per-channel gate drops
    ``email`` when ``notifications_email_enabled`` is off. Both default
    to True, but they are ordinary settings: an operator ``.env`` or a
    CI job that exports ``BVP_NOTIFICATIONS_ENABLED=false`` would turn
    the reminder test into a silent pass on an empty dispatch table.
    Pinning them here makes the test state its own preconditions instead
    of inheriting them from the ambient environment.
    """
    from bvphoenix.config import get_settings

    monkeypatch.setenv("BVP_NOTIFICATIONS_ENABLED", "true")
    monkeypatch.setenv("BVP_NOTIFICATIONS_EMAIL_ENABLED", "true")
    get_settings.cache_clear()
    try:
        yield
    finally:
        # ``monkeypatch`` restores the env; the lru_cache still holds the
        # Settings built from the patched one, so it must be dropped too.
        get_settings.cache_clear()


async def _row(db: AsyncSession, event_id: uuid.UUID) -> ClinicalEvent:
    await db.rollback()  # drop whatever the handler left on this session
    return (
        await db.execute(select(ClinicalEvent).where(ClinicalEvent.id == event_id))
    ).scalar_one()


async def _transitions(db: AsyncSession, event_id: uuid.UUID) -> list[ClinicalEventTransition]:
    rows = (
        (
            await db.execute(
                select(ClinicalEventTransition)
                .where(ClinicalEventTransition.event_id == event_id)
                .order_by(ClinicalEventTransition.created_at)
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


async def _provenance(db: AsyncSession, event_id: uuid.UUID) -> list[ProvenanceEvent]:
    rows = (
        (
            await db.execute(
                select(ProvenanceEvent)
                .where(
                    ProvenanceEvent.target_kind == "clinical_event",
                    ProvenanceEvent.target_id == event_id,
                )
                .order_by(ProvenanceEvent.recorded_at)
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


# ---------------------------------------------------------------------------
# The bug report, end to end
# ---------------------------------------------------------------------------


@skip_if_no_db
async def test_amending_a_completed_event_moves_the_date_in_the_row_timezone(
    db_session, ctx
) -> None:
    """Re-date something that already happened.

    The chosen instant (23:30 UTC, Europe/Rome) lands on the NEXT
    calendar day locally, so a trigger that cast to the UTC date would
    return 2026-01-15 and fail here. ``event_status`` must not move: the
    visit still happened, we only corrected when.
    """
    ev = await ctx.make_event(
        event_status="completed",
        actual_start_at=datetime(2026, 1, 10, 9, 0, tzinfo=UTC),
    )
    assert ev.event_date == date(2026, 1, 10), "seed should already be trigger-derived"

    async with client_as(db_session, ctx.user) as client:
        resp = await client.post(
            f"/api/clinical-events/{ev.id}/amend-time",
            json={
                "actual_start_at": _ROME_MIDNIGHT_UTC.isoformat(),
                "reason": "referto: la visita era il 16, non il 10",
            },
            headers=_headers(ev.etag),
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["event_date"] == _ROME_LOCAL_DATE.isoformat()
    assert body["actual_start_at"] == _ROME_MIDNIGHT_UTC.isoformat()
    assert body["event_status"] == "completed", "an amendment is not a transition"
    assert body["etag"] != ev.etag

    row = await _row(db_session, ev.id)
    assert row.event_date == _ROME_LOCAL_DATE
    assert row.actual_start_at == _ROME_MIDNIGHT_UTC
    assert row.event_status == "completed"

    transitions = await _transitions(db_session, ev.id)
    assert [t.action for t in transitions] == ["amend_time"]
    assert transitions[0].reason == "referto: la visita era il 16, non il 10"
    assert transitions[0].snapshot_before["event_date"] == "2026-01-10"
    assert transitions[0].snapshot_after["event_date"] == _ROME_LOCAL_DATE.isoformat()

    provenance = await _provenance(db_session, ev.id)
    assert [p.activity for p in provenance] == ["transition.amend_time"]
    assert provenance[0].diff["actual_start_at"]["to"] == _ROME_MIDNIGHT_UTC.isoformat()


@skip_if_no_db
async def test_terminal_rows_are_amendable_and_keep_their_lifecycle_fields(db_session, ctx) -> None:
    """``cancelled`` / ``rescheduled`` are terminal for the FSM but must
    stay re-datable, and the amendment must not touch the fields the
    transition that closed them wrote.

    Reusing ``_persist_transition`` here would clobber
    ``status_change_reason`` (destroying the cancellation reason) and
    clear ``phase_id``; that would corrupt the record the amendment is
    meant to fix, so it is asserted explicitly.
    """
    phase = CarePhase(
        id=uuid.uuid4(),
        patient_id=ctx.patient_id,
        slug=f"phase-{uuid.uuid4().hex[:8]}",
        name="Follow-up",
        kind="followup",
        color_hex="#3F5FBF",
        ordinal=0,
        author_kind="human",
    )
    db_session.add(phase)
    await db_session.flush()
    phase_id = phase.id
    await db_session.commit()

    for status_name in ("cancelled", "rescheduled"):
        ev = await ctx.make_event(
            event_status=status_name,
            planned_start_at=datetime(2026, 2, 3, 15, 0, tzinfo=UTC),
            status_change_reason="ambulatorio chiuso",
            status_changed_by_kind="human",
            phase_id=phase_id,
            phase_assigned_by="human",
        )
        async with client_as(db_session, ctx.user) as client:
            resp = await client.post(
                f"/api/clinical-events/{ev.id}/amend-time",
                json={"planned_start_at": "2026-02-04T15:00:00+00:00"},
                headers=_headers(ev.etag),
            )
        assert resp.status_code == 200, f"{status_name}: {resp.text}"

        row = await _row(db_session, ev.id)
        assert row.event_status == status_name, "amend must not move the status"
        assert row.status_change_reason == "ambulatorio chiuso"
        assert row.phase_id == phase_id
        # migration 0047 put ``cancelled`` in the planned anchor family;
        # before it, a cancelled row's date was frozen while every other
        # status re-derived.
        assert row.event_date == date(2026, 2, 4), (
            f"{status_name} must re-derive event_date from planned_start_at"
        )


@skip_if_no_db
async def test_amending_a_planned_anchor_rebuilds_the_pending_reminders(
    db_session, ctx, notifications_on
) -> None:
    """A moved anchor invalidates every queued reminder. The old rows are
    cancelled (kept, so the audit trail survives) and the patient must
    end up with a pending reminder anchored to the NEW time — otherwise
    moving an appointment silently switches its reminders off."""
    from bvphoenix.services.notifications.scheduling import materialise_event_dispatches

    contact = PatientContact(
        id=uuid.uuid4(),
        patient_id=ctx.patient_id,
        label="Caregiver",
        email=f"caregiver-{uuid.uuid4().hex[:8]}@example.com",
        consent_to_contact=True,
        consent_email=True,
        preferred_channels=["email"],
    )
    db_session.add(contact)
    await db_session.flush()
    await db_session.commit()

    anchor = datetime.now(UTC) + timedelta(days=30)
    new_anchor = anchor + timedelta(days=1)
    ev = await ctx.make_event(
        event_status="planned",
        planned_start_at=anchor,
        reminder_offsets_minutes=[-1440],
    )
    live = (
        await db_session.execute(select(ClinicalEvent).where(ClinicalEvent.id == ev.id))
    ).scalar_one()
    seeded = await materialise_event_dispatches(db_session, live)
    assert seeded == 1, (
        "precondition: exactly one reminder must exist before the amendment, "
        f"got {seeded}. materialise_event_dispatches returns 0 when "
        "notifications_enabled / notifications_email_enabled are off; the "
        "``notifications_on`` fixture forces both"
    )
    await db_session.commit()

    async with client_as(db_session, ctx.user) as client:
        resp = await client.post(
            f"/api/clinical-events/{ev.id}/amend-time",
            json={"planned_start_at": new_anchor.isoformat()},
            headers=_headers(ev.etag),
        )
    assert resp.status_code == 200, resp.text

    await db_session.rollback()
    dispatches = (
        (
            await db_session.execute(
                select(NotificationDispatch).where(NotificationDispatch.target_id == ev.id)
            )
        )
        .scalars()
        .all()
    )
    cancelled = [d for d in dispatches if d.status == "cancelled"]
    pending = [d for d in dispatches if d.status == "pending"]
    assert cancelled, "the reminder queued against the old anchor must be cancelled"
    assert cancelled[0].error_code == "amended"
    assert len(pending) == 1, (
        "the amended anchor must leave exactly one pending reminder; "
        f"got {[(d.status, d.scheduled_at) for d in dispatches]}"
    )
    assert pending[0].scheduled_at == new_anchor - timedelta(minutes=1440)


# ---------------------------------------------------------------------------
# Date-only rows: the DICOM StudyDate shape
# ---------------------------------------------------------------------------


@skip_if_no_db
async def test_event_date_is_writable_on_a_date_only_row_and_survives_a_patch(
    db_session, ctx
) -> None:
    """DICOM ``StudyDate`` imports and document backfills genuinely have
    no time of day: both anchors are NULL, so ``event_date`` is the row's
    own value and the trigger leaves it alone. Those are the only rows on
    which a direct ``event_date`` write is honest, and the value must
    survive a subsequent metadata PATCH (which re-fires the trigger)."""
    ev = await ctx.make_event(
        kind="imaging_study",
        title="TC torace (import)",
        event_status="completed",
        source="imaging_ingest",
        event_date=date(2019, 6, 1),
    )
    assert ev.event_date == date(2019, 6, 1)

    async with client_as(db_session, ctx.user) as client:
        resp = await client.post(
            f"/api/clinical-events/{ev.id}/amend-time",
            json={
                "event_date": "2019-05-28",
                "reason": "StudyDate del CD: 28/05, non 01/06",
            },
            headers=_headers(ev.etag),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["event_date"] == "2019-05-28"
        new_etag = resp.json()["etag"]

        patched = await client.patch(
            f"/api/clinical-events/{ev.id}",
            json={"title": "TC torace con mdc (import)"},
            headers={"If-Match": f'"{new_etag}"'},
        )
    assert patched.status_code == 200, patched.text
    assert patched.json()["event_date"] == "2019-05-28", (
        "a metadata PATCH re-fires fn_ce_derive_event_date; with both anchors "
        "NULL it must leave the standalone event_date alone"
    )

    row = await _row(db_session, ev.id)
    assert row.event_date == date(2019, 5, 28)


@skip_if_no_db
async def test_supplying_an_anchor_hands_the_date_back_to_the_trigger(db_session, ctx) -> None:
    """Once a date-only row learns its real timestamp, the DB owns the
    date again: the anchor wins over whatever ``event_date`` held."""
    ev = await ctx.make_event(
        kind="imaging_study",
        title="TC torace (import)",
        event_status="completed",
        source="imaging_ingest",
        event_date=date(2019, 6, 1),
    )
    async with client_as(db_session, ctx.user) as client:
        resp = await client.post(
            f"/api/clinical-events/{ev.id}/amend-time",
            json={
                "actual_start_at": _ROME_MIDNIGHT_UTC.isoformat(),
                "reason": "orario reale letto dall'header DICOM",
            },
            headers=_headers(ev.etag),
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["event_date"] == _ROME_LOCAL_DATE.isoformat()

    row = await _row(db_session, ev.id)
    assert row.event_date == _ROME_LOCAL_DATE

    # ...and from now on the row is anchored, so a direct event_date
    # write is refused instead of being silently reverted.
    async with client_as(db_session, ctx.user) as client:
        refused = await client.post(
            f"/api/clinical-events/{ev.id}/amend-time",
            json={"event_date": "2019-06-01", "reason": "ripensamento"},
            headers=_headers(str(row.etag)),
        )
    assert refused.status_code == 422, refused.text
    assert refused.json()["code"] == "event_date_is_derived"


# ---------------------------------------------------------------------------
# The 422 contract
# ---------------------------------------------------------------------------


@skip_if_no_db
async def test_wrong_anchor_for_status_is_refused(db_session, ctx) -> None:
    """``actual_*`` on a planned row (and vice versa) would write a
    timestamp the trigger never reads: the row's displayed date would
    not move, which reads to the user as "the edit did nothing"."""
    planned = await ctx.make_event(
        event_status="planned",
        planned_start_at=datetime.now(UTC) + timedelta(days=10),
    )
    completed = await ctx.make_event(
        event_status="completed",
        actual_start_at=datetime(2026, 1, 10, 9, 0, tzinfo=UTC),
    )
    async with client_as(db_session, ctx.user) as client:
        on_planned = await client.post(
            f"/api/clinical-events/{planned.id}/amend-time",
            json={"actual_start_at": "2026-01-10T09:00:00+00:00", "reason": "x"},
            headers=_headers(planned.etag),
        )
        on_completed = await client.post(
            f"/api/clinical-events/{completed.id}/amend-time",
            json={"planned_start_at": "2026-01-10T09:00:00+00:00"},
            headers=_headers(completed.etag),
        )
    assert on_planned.status_code == 422, on_planned.text
    assert on_planned.json()["code"] == "wrong_anchor_for_status"
    assert on_planned.json()["expected_prefix"] == "planned_"
    assert on_completed.status_code == 422, on_completed.text
    assert on_completed.json()["code"] == "wrong_anchor_for_status"
    assert on_completed.json()["expected_prefix"] == "actual_"


@skip_if_no_db
async def test_event_date_on_an_anchored_row_is_refused(db_session, ctx) -> None:
    """Refused rather than accepted-and-reverted: the old behaviour wrote
    the value and the next trigger firing silently undid it."""
    ev = await ctx.make_event(
        event_status="planned",
        planned_start_at=datetime.now(UTC) + timedelta(days=10),
    )
    async with client_as(db_session, ctx.user) as client:
        resp = await client.post(
            f"/api/clinical-events/{ev.id}/amend-time",
            json={"event_date": "2026-04-01", "reason": "x"},
            headers=_headers(ev.etag),
        )
    assert resp.status_code == 422, resp.text
    assert resp.json()["code"] == "event_date_is_derived"
    assert resp.json()["derived_from"] == "planned_start_at"


@skip_if_no_db
async def test_future_actual_time_is_refused(db_session, ctx) -> None:
    """ "It already happened" and "it happens next week" cannot both be
    true. This is the guard against the UI's date picker being used to
    push a completed event into the future."""
    ev = await ctx.make_event(
        event_status="completed",
        actual_start_at=datetime(2026, 1, 10, 9, 0, tzinfo=UTC),
    )
    async with client_as(db_session, ctx.user) as client:
        resp = await client.post(
            f"/api/clinical-events/{ev.id}/amend-time",
            json={
                "actual_start_at": (datetime.now(UTC) + timedelta(days=3)).isoformat(),
                "reason": "typo",
            },
            headers=_headers(ev.etag),
        )
    assert resp.status_code == 422, resp.text
    assert resp.json()["code"] == "future_actual_time"


@skip_if_no_db
async def test_end_before_start_is_refused(db_session, ctx) -> None:
    ev = await ctx.make_event(
        event_status="completed",
        actual_start_at=datetime(2026, 1, 10, 9, 0, tzinfo=UTC),
    )
    async with client_as(db_session, ctx.user) as client:
        resp = await client.post(
            f"/api/clinical-events/{ev.id}/amend-time",
            json={
                "actual_start_at": "2026-01-10T09:00:00+00:00",
                "actual_end_at": "2026-01-10T08:00:00+00:00",
                "reason": "durata",
            },
            headers=_headers(ev.etag),
        )
    assert resp.status_code == 422, resp.text
    assert resp.json()["code"] == "end_before_start"


@skip_if_no_db
async def test_nothing_to_amend_is_refused(db_session, ctx) -> None:
    """A reason with no temporal field is a no-op that would still write
    an audit row claiming a correction happened."""
    ev = await ctx.make_event(
        event_status="completed",
        actual_start_at=datetime(2026, 1, 10, 9, 0, tzinfo=UTC),
    )
    async with client_as(db_session, ctx.user) as client:
        resp = await client.post(
            f"/api/clinical-events/{ev.id}/amend-time",
            json={"reason": "solo una nota"},
            headers=_headers(ev.etag),
        )
    assert resp.status_code == 422, resp.text
    assert resp.json()["code"] == "nothing_to_amend"
    assert await _transitions(db_session, ev.id) == []


@skip_if_no_db
async def test_clearing_a_start_anchor_is_refused_whatever_the_status(db_session, ctx) -> None:
    """The start anchor defines the row's date and cannot be removed.

    For planned/confirmed the DB CHECK
    ``ck_clinical_events_time_required_by_status`` says so too and would
    otherwise surface as an IntegrityError-shaped 500. For a terminal
    row there is no CHECK, and clearing the anchor was worse than a
    crash: the trigger matches neither branch, so ``event_date`` stayed
    frozen at the value derived from the timestamp just deleted, i.e. a
    date on a clinical record that nobody wrote. An END anchor stays
    clearable, "we do not know when it finished" being legitimate.
    """
    scheduled = await ctx.make_event(
        event_status="confirmed",
        planned_start_at=datetime.now(UTC) + timedelta(days=10),
    )
    done = await ctx.make_event(
        event_status="completed",
        actual_start_at=datetime(2026, 1, 10, 9, 0, tzinfo=UTC),
        actual_end_at=datetime(2026, 1, 10, 10, 0, tzinfo=UTC),
    )
    async with client_as(db_session, ctx.user) as client:
        refused_plan = await client.post(
            f"/api/clinical-events/{scheduled.id}/amend-time",
            json={"planned_start_at": None},
            headers=_headers(scheduled.etag),
        )
        refused_actual = await client.post(
            f"/api/clinical-events/{done.id}/amend-time",
            json={"actual_start_at": None, "reason": "typo"},
            headers=_headers(done.etag),
        )
        cleared_end = await client.post(
            f"/api/clinical-events/{done.id}/amend-time",
            json={"actual_end_at": None, "reason": "the end time was never recorded"},
            headers=_headers(done.etag),
        )
    assert refused_plan.status_code == 422, refused_plan.text
    assert refused_plan.json()["code"] == "anchor_not_clearable"
    assert refused_plan.json()["field"] == "planned_start_at"
    assert refused_actual.status_code == 422, refused_actual.text
    assert refused_actual.json()["code"] == "anchor_not_clearable"
    assert cleared_end.status_code == 200, cleared_end.text
    assert cleared_end.json()["actual_end_at"] is None
    assert cleared_end.json()["actual_start_at"] is not None


@skip_if_no_db
async def test_clearing_event_date_on_a_date_only_row_is_refused(db_session, ctx) -> None:
    """The twin of the start-anchor rule, for the rows that have no
    anchor at all.

    On a date-only row (DICOM ``StudyDate`` import, document backfill)
    ``event_date`` IS the row's date: no trigger would recompute it, so
    an accepted ``null`` would leave a clinical event with no date
    whatsoever, which is not a state any caller needs and which the
    timeline cannot place. Refused with the same code as the anchor, and
    ``field`` says which one was refused.
    """
    ev = await ctx.make_event(
        kind="imaging_study",
        title="TC torace (import)",
        event_status="completed",
        source="imaging_ingest",
        event_date=date(2019, 6, 1),
    )
    assert ev.event_date == date(2019, 6, 1)

    async with client_as(db_session, ctx.user) as client:
        resp = await client.post(
            f"/api/clinical-events/{ev.id}/amend-time",
            json={"event_date": None, "reason": "the CD had no date"},
            headers=_headers(ev.etag),
        )
    assert resp.status_code == 422, resp.text
    assert resp.json()["code"] == "anchor_not_clearable"
    assert resp.json()["field"] == "event_date"

    # Nothing persisted: same date, same etag, no audit row claiming an
    # amendment happened.
    row = await _row(db_session, ev.id)
    assert row.event_date == date(2019, 6, 1)
    assert str(row.etag) == ev.etag
    assert await _transitions(db_session, ev.id) == []


@skip_if_no_db
@pytest.mark.parametrize("realised_status", ["completed", "missed"])
async def test_reason_is_required_for_the_actual_family_only(
    db_session, ctx, realised_status: str
) -> None:
    """Amending a realised fact is a record amendment and must say why.
    Correcting a plan that has not happened yet is ordinary editing, so
    demanding a justification there would just train users to type "x".

    The rule keys on the ROW'S STATUS, not on which key the payload
    carries: both statuses of the actual family (``completed`` and
    ``missed``) demand a reason, and they demand it even for a
    ``timezone``-only body, which carries no timestamp at all yet still
    relabels the recorded date. The same ``timezone``-only body on a
    planned row is accepted without one. An implementation that keyed on
    the payload instead would pass the moved-anchor case and fail here.
    """
    # 23:30 UTC stored under ``UTC`` reads as the 15th; the same instant
    # relabelled Europe/Rome reads as the 16th. That is what makes a
    # timezone-only amendment a change to the recorded clinical date.
    realised = await ctx.make_event(
        event_status=realised_status,
        actual_start_at=_ROME_MIDNIGHT_UTC,
        timezone="UTC",
    )
    assert realised.event_date == date(2026, 1, 15)
    planned = await ctx.make_event(
        event_status="planned",
        planned_start_at=datetime.now(UTC) + timedelta(days=10),
        timezone="UTC",
    )
    async with client_as(db_session, ctx.user) as client:
        without_reason = await client.post(
            f"/api/clinical-events/{realised.id}/amend-time",
            json={"actual_start_at": _ROME_MIDNIGHT_UTC.isoformat()},
            headers=_headers(realised.etag),
        )
        tz_without_reason = await client.post(
            f"/api/clinical-events/{realised.id}/amend-time",
            json={"timezone": "Europe/Rome"},
            headers=_headers(realised.etag),
        )
        tz_with_reason = await client.post(
            f"/api/clinical-events/{realised.id}/amend-time",
            json={
                "timezone": "Europe/Rome",
                "reason": "the report is stamped in Italian local time",
            },
            headers=_headers(realised.etag),
        )
        plan_move = await client.post(
            f"/api/clinical-events/{planned.id}/amend-time",
            json={"planned_start_at": (datetime.now(UTC) + timedelta(days=11)).isoformat()},
            headers=_headers(planned.etag),
        )
        plan_tz = await client.post(
            f"/api/clinical-events/{planned.id}/amend-time",
            json={"timezone": "Europe/Rome"},
            # Wildcard: this row's etag moved with ``plan_move`` just above,
            # and the assertion under test is the reason rule, not concurrency.
            headers=_headers(planned.etag, wildcard=True),
        )
    assert without_reason.status_code == 422, without_reason.text
    assert without_reason.json()["code"] == "reason_required"
    assert tz_without_reason.status_code == 422, tz_without_reason.text
    assert tz_without_reason.json()["code"] == "reason_required", (
        "a timezone-only body on a realised row carries no timestamp but still "
        "moves the recorded date: the rule keys on the row's status"
    )
    assert tz_with_reason.status_code == 200, tz_with_reason.text
    assert tz_with_reason.json()["event_date"] == _ROME_LOCAL_DATE.isoformat()
    assert plan_move.status_code == 200, (
        f"moving a plan must not require a reason: {plan_move.text}"
    )
    assert plan_tz.status_code == 200, (
        f"the same timezone-only body on a planned row needs no reason: {plan_tz.text}"
    )

    row = await _row(db_session, realised.id)
    assert row.event_date == _ROME_LOCAL_DATE
    assert row.event_status == realised_status
    assert [t.action for t in await _transitions(db_session, realised.id)] == ["amend_time"], (
        "only the amendment that carried a reason may have been recorded"
    )


@skip_if_no_db
async def test_invalid_timezone_is_refused(db_session, ctx) -> None:
    """``timezone`` feeds ``AT TIME ZONE`` inside the trigger. An IANA
    name that Postgres does not know aborts the UPDATE, so it has to be
    caught in the handler and answered with the documented 422 rather
    than surfacing as a 500."""
    ev = await ctx.make_event(
        event_status="planned",
        planned_start_at=datetime.now(UTC) + timedelta(days=10),
    )
    async with client_as(db_session, ctx.user) as client:
        resp = await client.post(
            f"/api/clinical-events/{ev.id}/amend-time",
            json={"timezone": "Mars/Olympus_Mons"},
            headers=_headers(ev.etag),
        )
    assert resp.status_code == 422, resp.text
    assert resp.json()["code"] == "invalid_timezone"


# ---------------------------------------------------------------------------
# Concurrency + idempotency + dry-run
# ---------------------------------------------------------------------------


@skip_if_no_db
async def test_if_match_and_idempotency_key_are_mandatory(db_session, ctx) -> None:
    """Both preconditions are 428, not 400: the request is well-formed,
    it is the caller's contract that is incomplete."""
    ev = await ctx.make_event(
        event_status="planned",
        planned_start_at=datetime.now(UTC) + timedelta(days=10),
    )
    payload = {"planned_start_at": (datetime.now(UTC) + timedelta(days=11)).isoformat()}
    async with client_as(db_session, ctx.user) as client:
        no_if_match = await client.post(
            f"/api/clinical-events/{ev.id}/amend-time",
            json=payload,
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
        no_key = await client.post(
            f"/api/clinical-events/{ev.id}/amend-time",
            json=payload,
            headers={"If-Match": f'"{ev.etag}"'},
        )
        stale = await client.post(
            f"/api/clinical-events/{ev.id}/amend-time",
            json=payload,
            headers=_headers(str(uuid.uuid4())),
        )
    assert no_if_match.status_code == 428, no_if_match.text
    assert no_key.status_code == 428, no_key.text
    assert stale.status_code == 412, stale.text
    assert await _transitions(db_session, ev.id) == []


@skip_if_no_db
async def test_replaying_the_idempotency_key_returns_the_first_response(db_session, ctx) -> None:
    """A retried POST (flaky network, agent retry loop) must not double
    the amendment. The replay is answered from the stored
    ``snapshot_after``, and no second transition row appears.

    The retry uses ``If-Match: *``: the first call bumped the etag, so a
    client replaying its original token would be told 412 before the
    idempotency cache is even consulted. RFC 9110 §13.1.1 wildcard is
    the documented opt-out for a deliberately idempotent mutation.
    """
    ev = await ctx.make_event(
        event_status="completed",
        actual_start_at=datetime(2026, 1, 10, 9, 0, tzinfo=UTC),
    )
    key = str(uuid.uuid4())
    payload = {
        "actual_start_at": _ROME_MIDNIGHT_UTC.isoformat(),
        "reason": "referto: la visita era il 16",
    }
    async with client_as(db_session, ctx.user) as client:
        first = await client.post(
            f"/api/clinical-events/{ev.id}/amend-time",
            json=payload,
            headers=_headers(ev.etag, key=key),
        )
        assert first.status_code == 200, first.text
        replay = await client.post(
            f"/api/clinical-events/{ev.id}/amend-time",
            json=payload,
            headers=_headers(ev.etag, key=key, wildcard=True),
        )
    assert replay.status_code == 200, replay.text
    assert replay.json()["etag"] == first.json()["etag"]
    assert replay.json()["event_date"] == first.json()["event_date"]
    assert len(await _transitions(db_session, ev.id)) == 1, "a replay must not amend twice"
    assert len(await _provenance(db_session, ev.id)) == 1


@skip_if_no_db
async def test_dry_run_previews_the_derived_date_and_persists_nothing(db_session, ctx) -> None:
    """The preview has to show the date the trigger WOULD derive — a
    preview that echoed the submitted timestamp without projecting it
    onto the local calendar would mislead exactly where the timezone
    matters."""
    ev = await ctx.make_event(
        event_status="completed",
        actual_start_at=datetime(2026, 1, 10, 9, 0, tzinfo=UTC),
    )
    async with client_as(db_session, ctx.user) as client:
        resp = await client.post(
            f"/api/clinical-events/{ev.id}/amend-time?dry_run=true",
            json={"actual_start_at": _ROME_MIDNIGHT_UTC.isoformat(), "reason": "prova"},
            headers=_headers(ev.etag),
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["event_date"] == _ROME_LOCAL_DATE.isoformat()

    row = await _row(db_session, ev.id)
    assert row.event_date == date(2026, 1, 10), "dry_run must not persist"
    assert row.actual_start_at == datetime(2026, 1, 10, 9, 0, tzinfo=UTC)
    assert str(row.etag) == ev.etag, "dry_run must not bump the etag"
    assert await _transitions(db_session, ev.id) == []
    assert await _provenance(db_session, ev.id) == []


# ---------------------------------------------------------------------------
# Who amended it: author_kind on both audit rows
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def assistant_secret(db_session: AsyncSession, ctx):
    """A per-assistant ``client_secret`` (the modern MCP credential)
    granted on ``ctx``'s patient.

    No teardown on purpose. The assistant cascades away with the
    owner's Subject in ``make_user``, which runs AFTER ``ctx`` has
    deleted the provenance rows; ``provenance_events.agent_assistant_id``
    is ON DELETE SET NULL, so dropping the assistant first would leave
    an agent-authored row with no identity at all and trip
    ``ck_provenance_events_agent_identified``.
    """
    secret = f"bvps_{uuid.uuid4().hex}{uuid.uuid4().hex}"
    assistant = AgentAssistant(
        id=uuid.uuid4(),
        owner_subject_id=ctx.subject_id,
        label=f"amend-agent-{uuid.uuid4().hex[:8]}",
        permissions=["patient:read", "consultation:write"],
        client_id=f"cid-{uuid.uuid4().hex[:24]}",
        client_secret_hash=hashlib.sha256(secret.encode("utf-8")).hexdigest(),
        client_secret_prefix=secret[:8],
    )
    db_session.add(assistant)
    await db_session.flush()
    db_session.add(
        AgentAssistantPatient(
            assistant_id=assistant.id,
            patient_id=ctx.patient_id,
            granted_by_subject_id=ctx.subject_id,
        )
    )
    await db_session.flush()
    assistant_id = assistant.id
    await db_session.commit()
    yield SimpleNamespace(secret=secret, assistant_id=assistant_id)


@skip_if_no_db
async def test_a_human_amendment_is_stamped_human_on_both_audit_rows(db_session, ctx) -> None:
    """``memoria: feedback_ai_provenance_must_be_visible``.

    An amendment rewrites a clinical date, so "who says so" is part of
    the record. Both audit rows carry it and both are asserted: the
    transition's ``author_kind`` is what the Undo/history UI reads, the
    provenance row's ``agent_kind`` is what the provenance chain and the
    GDPR export read. A regression that stamped one correctly and the
    other from a stale default would otherwise pass.
    """
    ev = await ctx.make_event(
        event_status="completed",
        actual_start_at=datetime(2026, 1, 10, 9, 0, tzinfo=UTC),
    )
    async with client_as(db_session, ctx.user) as client:
        resp = await client.post(
            f"/api/clinical-events/{ev.id}/amend-time",
            json={
                "actual_start_at": _ROME_MIDNIGHT_UTC.isoformat(),
                "reason": "referto: la visita era il 16",
            },
            headers=_headers(ev.etag),
        )
    assert resp.status_code == 200, resp.text

    transition = (await _transitions(db_session, ev.id))[0]
    assert transition.author_kind == "human"
    assert transition.actor_subject_id == ctx.subject_id

    provenance = (await _provenance(db_session, ev.id))[0]
    assert provenance.activity == "transition.amend_time"
    assert provenance.agent_kind == "human"
    assert provenance.agent_subject_id == ctx.subject_id
    assert provenance.agent_assistant_id is None
    assert provenance.agent_token_id is None


@skip_if_no_db
async def test_an_agent_amendment_is_stamped_agent_and_identifies_the_assistant(
    db_session, ctx, assistant_secret
) -> None:
    """The same amendment through an agent credential must NOT look
    human.

    Goes through the real auth chain (``client_as_bearer``) rather than
    a ``require_user`` override, because ``request.state.is_agent`` is
    what both writers key on: overriding the dependency would leave the
    flag unset and the test would pass against a handler that hard-coded
    ``'human'``. The assistant id is asserted too — an agent row whose
    identity is NULL is an unattributable write, and the table CHECK
    ``ck_provenance_events_agent_identified`` exists for exactly that.
    """
    ev = await ctx.make_event(
        event_status="completed",
        actual_start_at=datetime(2026, 1, 10, 9, 0, tzinfo=UTC),
    )
    async with client_as_bearer(db_session, assistant_secret.secret) as client:
        resp = await client.post(
            f"/api/clinical-events/{ev.id}/amend-time",
            json={
                "actual_start_at": _ROME_MIDNIGHT_UTC.isoformat(),
                "reason": "data corretta dal referto allegato",
            },
            headers=_headers(ev.etag),
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["event_date"] == _ROME_LOCAL_DATE.isoformat()

    transition = (await _transitions(db_session, ev.id))[0]
    assert transition.author_kind == "agent", (
        "an amendment made through an agent credential must not be filed as human"
    )
    # The agent acts under its owner, so the subject stays the owner's.
    assert transition.actor_subject_id == ctx.subject_id

    provenance = (await _provenance(db_session, ev.id))[0]
    assert provenance.agent_kind == "agent"
    assert provenance.agent_assistant_id == assistant_secret.assistant_id
    assert provenance.agent_subject_id is None, (
        "agent-authored provenance carries the assistant identity, not the human subject"
    )
