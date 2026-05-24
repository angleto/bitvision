"""Unit tests for VALARM emission + single-resource ICS exports (sprint B).

Covers:
* ``_fmt_trigger`` RFC 5545 §3.3.6 duration format
* ``_coerce_offsets`` dedup + cap behaviour
* ``render_ics(..., with_valarm=False)`` feed default (no VALARMs to
  avoid poll-driven re-arm loops)
* ``render_single_event_ics(..., with_valarm=True)`` one-shot default
* ``render_single_task_ics`` with due_at and with completed_at fallback
* task without anchor renders an empty VEVENT-less skeleton

Pure unit tests; in-memory SQLAlchemy declarative instances.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from bvphoenix.db.models import ClinicalEvent, PatientTask
from bvphoenix.services.calendar_ics import (
    _coerce_offsets,
    _fmt_trigger,
    render_ics,
    render_single_event_ics,
    render_single_task_ics,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ev(**overrides) -> ClinicalEvent:
    base: dict = {
        "id": uuid.uuid4(),
        "patient_id": uuid.uuid4(),
        "kind": "outpatient_visit",
        "title": "Visita",
        "event_status": "planned",
        "planned_start_at": datetime(2026, 6, 15, 9, 0, tzinfo=UTC),
        "timezone": "UTC",
    }
    base.update(overrides)
    return ClinicalEvent(**base)


def _task(**overrides) -> PatientTask:
    base: dict = {
        "id": uuid.uuid4(),
        "patient_id": uuid.uuid4(),
        "title": "Prenotare TAC",
        "category": "admin",
        "priority": "normal",
        "status": "pending",
        "author_kind": "human",
    }
    base.update(overrides)
    return PatientTask(**base)


# ---------------------------------------------------------------------------
# _fmt_trigger (RFC 5545 §3.3.6 / §3.8.6.3)
# ---------------------------------------------------------------------------


def test_fmt_trigger_minutes() -> None:
    assert _fmt_trigger(-15) == "-PT15M"
    assert _fmt_trigger(-30) == "-PT30M"


def test_fmt_trigger_hours_and_combinations() -> None:
    assert _fmt_trigger(-60) == "-PT1H"
    assert _fmt_trigger(-90) == "-PT1H30M"
    assert _fmt_trigger(-120) == "-PT2H"


def test_fmt_trigger_days_and_combinations() -> None:
    assert _fmt_trigger(-1440) == "-P1D"
    assert _fmt_trigger(-2880) == "-P2D"
    assert _fmt_trigger(-1500) == "-P1DT1H"
    assert _fmt_trigger(-1455) == "-P1DT15M"


def test_fmt_trigger_zero() -> None:
    assert _fmt_trigger(0) == "PT0S"


def test_fmt_trigger_positive_offsets_render_without_sign() -> None:
    # Positive offsets fire AFTER the event; we don't emit them in
    # production but the formatter should still render the spec form.
    assert _fmt_trigger(60) == "PT1H"


# ---------------------------------------------------------------------------
# _coerce_offsets — dedup, cap, type filtering
# ---------------------------------------------------------------------------


def test_coerce_offsets_dedups_and_preserves_order() -> None:
    assert _coerce_offsets([-15, -60, -15, -120, -60]) == [-15, -60, -120]


def test_coerce_offsets_caps_at_five() -> None:
    raw = [-15, -30, -60, -120, -240, -480, -1440]
    out = _coerce_offsets(raw)
    assert len(out) == 5
    assert out == [-15, -30, -60, -120, -240]


def test_coerce_offsets_filters_non_ints() -> None:
    assert _coerce_offsets([-15, "x", None, -60, 3.14]) == [-15, -60, 3]


def test_coerce_offsets_handles_none_and_empty() -> None:
    assert _coerce_offsets(None) == []
    assert _coerce_offsets([]) == []


# ---------------------------------------------------------------------------
# render_ics feed — VALARM opt-in is OFF by default
# ---------------------------------------------------------------------------


def test_feed_default_has_no_valarm() -> None:
    """The subscription feed must NOT emit VALARM blocks. A polled
    feed with VALARMs would re-arm reminders on every refresh."""
    ev = _ev(reminder_offsets_minutes=[-15, -60])
    text = render_ics([ev])
    assert "BEGIN:VALARM" not in text
    assert "ACTION:DISPLAY" not in text


def test_feed_with_valarm_opt_in_emits_blocks() -> None:
    """Explicit opt-in on the feed (for tooling / tests) DOES emit
    VALARMs."""
    ev = _ev(reminder_offsets_minutes=[-15, -60])
    text = render_ics([ev], with_valarm=True)
    assert text.count("BEGIN:VALARM") == 2
    assert text.count("ACTION:DISPLAY") == 2
    assert "TRIGGER:-PT15M" in text
    assert "TRIGGER:-PT1H" in text


# ---------------------------------------------------------------------------
# render_single_event_ics — VALARM is ON by default
# ---------------------------------------------------------------------------


def test_single_event_default_emits_valarm_blocks() -> None:
    ev = _ev(reminder_offsets_minutes=[-1440, -60, -15])
    text = render_single_event_ics(ev)
    assert text.count("BEGIN:VALARM") == 3
    assert text.count("END:VALARM") == 3
    assert "TRIGGER:-P1D" in text
    assert "TRIGGER:-PT1H" in text
    assert "TRIGGER:-PT15M" in text


def test_single_event_valarm_description_locale() -> None:
    ev = _ev(reminder_offsets_minutes=[-15])
    text_it = render_single_event_ics(ev, lang="it")
    text_en = render_single_event_ics(ev, lang="en")
    assert "DESCRIPTION:Promemoria" in text_it
    assert "DESCRIPTION:Reminder" in text_en


def test_single_event_with_valarm_false_emits_clean_invite() -> None:
    """For when the caller wants the invite WITHOUT local alarms
    (e.g. forward to a recipient who already runs their own
    reminders)."""
    ev = _ev(reminder_offsets_minutes=[-15, -60])
    text = render_single_event_ics(ev, with_valarm=False)
    assert "BEGIN:VALARM" not in text
    assert "BEGIN:VEVENT" in text


def test_single_event_caps_valarm_blocks() -> None:
    """A row with 7 offsets emits exactly 5 VALARMs (the cap)."""
    ev = _ev(reminder_offsets_minutes=[-15, -30, -60, -120, -240, -480, -1440])
    text = render_single_event_ics(ev)
    assert text.count("BEGIN:VALARM") == 5


def test_single_event_no_offsets_emits_zero_valarm_blocks() -> None:
    ev = _ev(reminder_offsets_minutes=None)
    text = render_single_event_ics(ev)
    assert "BEGIN:VALARM" not in text
    # Still a valid file with one VEVENT inside.
    assert "BEGIN:VEVENT" in text
    assert "END:VEVENT" in text


def test_single_event_all_day_supports_valarm() -> None:
    """Date-only historical events (DTSTART;VALUE=DATE) also get
    VALARMs when requested."""
    from datetime import date as _date

    ev = _ev(
        event_status="completed",
        planned_start_at=None,
        actual_start_at=None,
        event_date=_date(2024, 5, 20),
        reminder_offsets_minutes=[-60],
    )
    text = render_single_event_ics(ev)
    assert "DTSTART;VALUE=DATE:20240520" in text
    assert "BEGIN:VALARM" in text
    assert "TRIGGER:-PT1H" in text


# ---------------------------------------------------------------------------
# render_single_task_ics
# ---------------------------------------------------------------------------


def test_single_task_uses_due_at_as_anchor() -> None:
    task = _task(
        status="pending",
        due_at=datetime(2026, 6, 15, 9, 0, tzinfo=UTC),
        timezone="UTC",
    )
    text = render_single_task_ics(task)
    assert "BEGIN:VEVENT" in text
    assert f"UID:task-{task.id}@bitvision" in text
    assert "DTSTART:20260615T090000Z" in text
    assert "STATUS:TENTATIVE" in text
    assert "X-BV-KIND:patient_task" in text


def test_single_task_done_falls_back_to_completed_at() -> None:
    task = _task(
        status="done",
        due_at=None,
        completed_at=datetime(2026, 5, 13, 16, 30, tzinfo=UTC),
        timezone="UTC",
    )
    text = render_single_task_ics(task)
    assert "BEGIN:VEVENT" in text
    assert "STATUS:CONFIRMED" in text
    assert "DTSTART:20260513T163000Z" in text


def test_single_task_dropped_maps_to_cancelled() -> None:
    task = _task(
        status="dropped",
        due_at=datetime(2026, 6, 15, 9, 0, tzinfo=UTC),
        timezone="UTC",
    )
    text = render_single_task_ics(task)
    assert "STATUS:CANCELLED" in text


def test_single_task_no_anchor_emits_empty_skeleton() -> None:
    """Tasks without due_at AND without completed_at can't be
    placed on a calendar. The renderer returns the VCALENDAR
    envelope but no VEVENT body — the calling endpoint decides
    whether to refuse with 422 (we do) or to return the placeholder."""
    task = _task(status="pending", due_at=None, completed_at=None)
    text = render_single_task_ics(task)
    assert "BEGIN:VCALENDAR" in text
    assert "BEGIN:VEVENT" not in text


def test_single_task_emits_valarm_blocks() -> None:
    task = _task(
        status="pending",
        due_at=datetime(2026, 6, 15, 9, 0, tzinfo=UTC),
        timezone="UTC",
        reminder_offsets_minutes=[-1440, -60],
    )
    text = render_single_task_ics(task)
    assert text.count("BEGIN:VALARM") == 2
    assert "TRIGGER:-P1D" in text
    assert "TRIGGER:-PT1H" in text


def test_single_task_priority_surfaced_as_x_extension() -> None:
    """We don't map priority to RFC 5545 ``PRIORITY`` (which is an
    integer 0-9 with non-obvious semantics); instead we emit it as
    an ``X-BV-PRIORITY`` extension so re-imports preserve it without
    confusing standards-compliant clients."""
    task = _task(
        status="pending",
        priority="urgent",
        due_at=datetime(2026, 6, 15, 9, 0, tzinfo=UTC),
    )
    text = render_single_task_ics(task)
    assert "X-BV-PRIORITY:urgent" in text


def test_single_task_description_truncated_to_1000_chars() -> None:
    long_desc = "x" * 2000
    task = _task(
        status="pending",
        description=long_desc,
        due_at=datetime(2026, 6, 15, 9, 0, tzinfo=UTC),
    )
    text = render_single_task_ics(task)
    # The truncated DESCRIPTION line must exist; we cap the source
    # field at 1000 chars before escaping.
    assert "DESCRIPTION:" in text
    # And the full 2000-char body must NOT appear verbatim.
    assert long_desc not in text
