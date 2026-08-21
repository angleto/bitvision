"""``PATCH /api/clinical-events/{id}`` — the endpoint the bug report hit.

Two defects met here, and neither had an executed test:

1. the handler builds an audit ``diff`` from raw model attributes and
   writes it to ``provenance_events.diff`` (JSONB). A ``date`` in that
   diff raised ``TypeError`` inside the JSON bind and escaped as a bare
   500 (fixed at the engine, guarded in
   ``tests/test_db_engine_factory.py``);
2. the endpoint used to accept the temporal fields, which the DB
   trigger ``fn_ce_derive_event_date`` immediately overwrote — two
   writers for one value. They now belong to
   ``POST /clinical-events/{id}/amend-time``.

The refusal has to be *narrow*: the edit dialog and the MCP
``update_clinical_event`` tool both send whole objects back, so echoing
an unchanged timestamp must stay a 200. Only a real move is refused.
Both halves are pinned below.

Error bodies are RFC 9457 problem+json: when a handler raises
``HTTPException(detail={...})`` the dict is merged into the top level of
the response body (``middleware/problem_details._build_body``), so the
assertions read ``body["code"]``, not ``body["detail"]["code"]``.

Needs a migrated Postgres (``BVP_DATABASE_URL``); the trigger and the
JSONB CHECKs are the substance of the test.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.db.models import ClinicalEvent, Patient, ProvenanceEvent

from .conftest import client_as, skip_if_no_db

pytestmark = skip_if_no_db


@pytest_asyncio.fixture
async def env(db_session: AsyncSession, make_user):
    """Owner user + patient + one planned event with a known anchor.

    Committed (not just flushed) because the handler runs its own
    ``db.commit()`` on the same session; leaving the seed uncommitted
    would make the first handler commit publish it anyway, just less
    obviously.
    """
    user = await make_user()
    patient = Patient(
        id=uuid.uuid4(),
        managed_by_subject_id=user.subject_id,
        display_name=f"Patch Patient {uuid.uuid4().hex[:8]}",
    )
    db_session.add(patient)
    await db_session.flush()
    event = ClinicalEvent(
        id=uuid.uuid4(),
        patient_id=patient.id,
        kind="outpatient_visit",
        title="Cardiology follow-up",
        event_status="planned",
        planned_start_at=datetime(2026, 9, 10, 8, 0, tzinfo=UTC),
        timezone="Europe/Rome",
    )
    db_session.add(event)
    await db_session.flush()
    await db_session.commit()
    # Yield plain values, never the ORM instances: ``commit`` /
    # ``rollback`` expire every attribute, and re-reading one from an
    # async session outside a greenlet raises MissingGreenlet. The
    # tests re-read state through explicit queries instead.
    patient_id, event_id, etag = patient.id, event.id, str(event.etag)

    yield SimpleNamespace(user=user, patient_id=patient_id, event_id=event_id, etag=etag)

    # Teardown order matters: ``provenance_events.agent_subject_id`` is
    # ON DELETE SET NULL, and a human-authored row with a NULL subject
    # violates ck_provenance_events_human_subject_present. So the audit
    # rows must go before ``make_user`` drops the Subject.
    await db_session.rollback()
    for stmt in (
        "DELETE FROM provenance_events WHERE target_kind = 'clinical_event' AND target_id = :e",
        "DELETE FROM clinical_events WHERE patient_id = :p",
        "DELETE FROM care_phase_revision WHERE phase_id IN "
        "(SELECT id FROM care_phase WHERE patient_id = :p)",
        "DELETE FROM care_phase WHERE patient_id = :p",
        "DELETE FROM patients WHERE id = :p",
    ):
        await db_session.execute(text(stmt), {"p": patient_id, "e": event_id})
    await db_session.commit()


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


@skip_if_no_db
async def test_patch_title_returns_200_and_records_a_json_clean_diff(db_session, env) -> None:
    """The happy path, and the audit row it must leave behind.

    ``json.dumps`` with NO ``default=`` hook is the assertion that
    matters: it proves the value that reached ``provenance_events.diff``
    is JSON-native, i.e. the column round-trips instead of exploding at
    bind time.
    """
    async with client_as(db_session, env.user) as client:
        resp = await client.patch(
            f"/api/clinical-events/{env.event_id}",
            json={"title": "Cardiology follow-up (Dr. Rossi)"},
            headers={"If-Match": f'"{env.etag}"'},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["title"] == "Cardiology follow-up (Dr. Rossi)"
    assert body["etag"] != env.etag, "a real change must mint a new etag"

    rows = await _provenance(db_session, env.event_id)
    assert [r.activity for r in rows] == ["update"]
    diff = rows[0].diff
    assert diff == {
        "title": {"from": "Cardiology follow-up", "to": "Cardiology follow-up (Dr. Rossi)"}
    }
    json.dumps(diff)  # no default= hook: raises if anything non-native slipped in


@skip_if_no_db
async def test_patch_recurrence_exdates_round_trips(db_session, env) -> None:
    """``recurrence_exdates`` is ``list[date]`` on the wire and JSONB in
    the DB — the second instance of the date-in-JSONB defect, on a field
    that has nothing to do with the event's anchor. Read-after-write must
    return the same ISO strings, and the audit diff must be JSON-native.
    """
    exdates = ["2026-09-17", "2026-09-24"]
    async with client_as(db_session, env.user) as client:
        resp = await client.patch(
            f"/api/clinical-events/{env.event_id}",
            json={"recurrence_rule": "FREQ=WEEKLY;BYDAY=TH", "recurrence_exdates": exdates},
            headers={"If-Match": f'"{env.etag}"'},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["recurrence_exdates"] == exdates

        read_back = await client.get(f"/api/clinical-events/{env.event_id}")
    assert read_back.status_code == 200, read_back.text
    assert read_back.json()["recurrence_exdates"] == exdates

    stored = (
        await db_session.execute(
            select(ClinicalEvent.recurrence_exdates).where(ClinicalEvent.id == env.event_id)
        )
    ).scalar_one()
    assert stored == exdates

    diff = (await _provenance(db_session, env.event_id))[0].diff
    json.dumps(diff)
    assert diff["recurrence_exdates"]["to"] == exdates


@skip_if_no_db
async def test_patch_moving_planned_start_at_is_refused_with_use_amend_time(
    db_session, env
) -> None:
    """A real move of the anchor is refused, and the refusal has to be
    actionable: the client is told which endpoint owns the change."""
    async with client_as(db_session, env.user) as client:
        resp = await client.patch(
            f"/api/clinical-events/{env.event_id}",
            json={"planned_start_at": "2026-09-11T08:00:00+00:00"},
            headers={"If-Match": f'"{env.etag}"'},
        )
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["code"] == "use_amend_time"
    assert body["fields"] == ["planned_start_at"]
    assert body["endpoint"].endswith("/amend-time")
    assert str(env.event_id) in body["endpoint"]

    await db_session.rollback()
    stored = (
        await db_session.execute(
            select(ClinicalEvent.planned_start_at).where(ClinicalEvent.id == env.event_id)
        )
    ).scalar_one()
    assert stored == datetime(2026, 9, 10, 8, 0, tzinfo=UTC), "refused patch must not persist"


@skip_if_no_db
async def test_patch_echoing_the_same_planned_start_at_is_a_200_no_op(db_session, env) -> None:
    """The UI edit dialog and MCP full-object patches send every field
    back, including the ones they did not touch. Refusing those would
    make the feature unusable, so an unchanged echo stays a 200 AND
    leaves the etag alone (no phantom version for a no-op)."""
    async with client_as(db_session, env.user) as client:
        current = await client.get(f"/api/clinical-events/{env.event_id}")
        assert current.status_code == 200, current.text
        before = current.json()

        resp = await client.patch(
            f"/api/clinical-events/{env.event_id}",
            json={
                "title": before["title"],
                "planned_start_at": before["planned_start_at"],
                "timezone": before["timezone"],
                "event_date": before["event_date"],
            },
            headers={"If-Match": f'"{before["etag"]}"'},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["etag"] == before["etag"], "a no-op patch must not bump the etag"
    assert resp.headers.get("ETag") == f'"{before["etag"]}"'
    assert await _provenance(db_session, env.event_id) == [], "a no-op must not write an audit row"


@skip_if_no_db
async def test_patch_moving_event_date_is_refused_with_use_amend_time(db_session, env) -> None:
    """The literal bug report: "there is no way to record the correct
    date of a past event". PATCH must answer with the pointer to
    ``/amend-time``, NOT with a 500 and not by writing a date the
    trigger will silently revert on the next update."""
    async with client_as(db_session, env.user) as client:
        resp = await client.patch(
            f"/api/clinical-events/{env.event_id}",
            json={"event_date": "2026-09-11"},
            headers={"If-Match": f'"{env.etag}"'},
        )
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["code"] == "use_amend_time"
    assert body["fields"] == ["event_date"]
    assert body["endpoint"].endswith("/amend-time")

    await db_session.rollback()
    stored = (
        await db_session.execute(
            select(ClinicalEvent.event_date).where(ClinicalEvent.id == env.event_id)
        )
    ).scalar_one()
    assert stored == date(2026, 9, 10), "refused patch must not persist"


@skip_if_no_db
async def test_patch_moving_timezone_is_refused_with_use_amend_time(db_session, env) -> None:
    """``timezone`` is one of the trigger's watched columns: moving it
    moves the derived date by up to a day, so it belongs to the same
    amendment path as the anchor itself."""
    async with client_as(db_session, env.user) as client:
        resp = await client.patch(
            f"/api/clinical-events/{env.event_id}",
            json={"timezone": "America/New_York"},
            headers={"If-Match": f'"{env.etag}"'},
        )
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["code"] == "use_amend_time"
    assert body["fields"] == ["timezone"]
