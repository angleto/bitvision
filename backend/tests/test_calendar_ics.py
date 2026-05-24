"""Unit tests for ``services.calendar_ics.render_ics``.

Pure unit tests, no DB. We feed synthetic ``ClinicalEvent`` instances
(SQLAlchemy declarative objects can be instantiated freely without a
session) and assert the rendered iCal text contains the expected
RFC 5545 properties + the status mapping.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

from bvphoenix.db.models import ClinicalEvent
from bvphoenix.services.calendar_ics import render_ics


def _ev(**overrides) -> ClinicalEvent:
    """Construct an in-memory ClinicalEvent for ICS rendering."""
    base = {
        "id": uuid.uuid4(),
        "patient_id": uuid.uuid4(),
        "kind": "outpatient_visit",
        "title": "Test event",
        "event_status": "completed",
    }
    base.update(overrides)
    return ClinicalEvent(**base)


def test_render_ics_envelope() -> None:
    ev = _ev(
        title="Hello",
        event_status="completed",
        actual_start_at=datetime(2026, 6, 1, 9, 0, tzinfo=UTC),
        timezone="Europe/Rome",
    )
    text = render_ics([ev])
    assert "BEGIN:VCALENDAR" in text
    assert "END:VCALENDAR" in text
    assert "PRODID:-//bitvision//calendar//EN" in text
    assert "BEGIN:VEVENT" in text
    assert "END:VEVENT" in text
    assert "STATUS:CONFIRMED" in text
    assert f"UID:event-{ev.id}@bitvision" in text


def test_render_ics_status_mapping_planned() -> None:
    ev = _ev(
        event_status="planned",
        planned_start_at=datetime(2026, 6, 1, 9, 0, tzinfo=UTC),
        timezone="UTC",
    )
    text = render_ics([ev])
    assert "STATUS:TENTATIVE" in text
    assert "X-BV-STATUS:planned" in text


def test_render_ics_status_mapping_cancelled() -> None:
    ev = _ev(
        event_status="cancelled",
        planned_start_at=datetime(2026, 6, 1, 9, 0, tzinfo=UTC),
    )
    text = render_ics([ev])
    assert "STATUS:CANCELLED" in text
    assert "X-BV-STATUS:cancelled" in text


def test_render_ics_uses_planned_start_for_planned() -> None:
    ev = _ev(
        event_status="planned",
        planned_start_at=datetime(2026, 6, 1, 9, 0, tzinfo=UTC),
        # actual_start_at intentionally None for a planned event
    )
    text = render_ics([ev])
    assert "DTSTART" in text


def test_render_ics_all_day_fallback() -> None:
    """A historical event with only event_date (no timestamps) renders
    as an all-day VEVENT, not skipped."""
    ev = _ev(
        event_status="completed",
        event_date=date(2024, 1, 15),
        actual_start_at=None,
        planned_start_at=None,
    )
    text = render_ics([ev])
    assert "BEGIN:VEVENT" in text
    assert "DTSTART;VALUE=DATE:20240115" in text


def test_render_ics_no_patient_id_leak() -> None:
    """The rendered ICS MUST NOT include the patient_id. The recipient
    learns who the calendar belongs to from the subscription URL /
    download dialog, not from the file body — keeps cross-patient
    leak impossible if the ICS is shared / cached."""
    ev = _ev(
        event_status="planned",
        planned_start_at=datetime(2026, 6, 1, 9, 0, tzinfo=UTC),
    )
    text = render_ics([ev])
    assert "X-BV-PATIENT" not in text
    assert str(ev.patient_id) not in text


def test_render_ics_summary_escapes_commas() -> None:
    ev = _ev(
        title="Visita, controllo; verifica\\test",
        event_status="completed",
        actual_start_at=datetime(2026, 6, 1, 9, 0, tzinfo=UTC),
    )
    text = render_ics([ev])
    assert "SUMMARY:Visita\\, controllo\\; verifica\\\\test" in text
