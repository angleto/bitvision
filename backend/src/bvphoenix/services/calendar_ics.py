"""ICS (iCalendar / RFC 5545) renderer for the calendar feed.

This module is the single source-of-truth for emitting iCalendar
documents from ``clinical_events`` rows when the request comes
through the **calendar** endpoint
(``/api/patients/{pid}/calendar?format=ics``).

Distinct from ``_render_timeline_ics`` in
``backend/src/bvphoenix/api/care_phases.py``, which emits the
phase-categorised timeline (DATE-only, no STATUS mapping) and stays
the canonical renderer for the **care-timeline** endpoint. We keep
the two separate because their contracts are different:

- Timeline ICS: phase-grouped, ALL-DAY events, no lifecycle status,
  audience = patient's calendar overview.
- Calendar ICS: status-aware (TENTATIVE / CONFIRMED / CANCELLED),
  timed events (DTSTART;TZID=...), audience = appointment
  subscription.

Mapping ``event_status`` -> RFC 5545 ``STATUS``::

    planned     -> TENTATIVE
    confirmed   -> CONFIRMED
    completed   -> CONFIRMED      (already happened; not omitted so
                                   the row stays on imported calendars)
    cancelled   -> CANCELLED
    missed      -> CANCELLED      (closest standard mapping for "no-show")
    rescheduled -> CANCELLED      (parent is closed; the successor row
                                   has its own UID + STATUS)

UID strategy: ``event-{event_id}@bitvision``. Stable across re-exports
so a re-import updates the existing entry instead of duplicating.

We intentionally do NOT emit ``X-BV-PATIENT-ID`` or any other patient
identifier inside the ICS body. If the file ends up in a shared
calendar feed the recipient must learn the patient from the
subscription context, not from the file contents.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta

from bvphoenix.db.models import ClinicalEvent, PatientTask

_PRODID = "-//bitvision//calendar//EN"

# Cap on VALARM blocks per VEVENT. Mirrors the backend Pydantic
# validator on ``reminder_offsets_minutes`` (max 5 entries) so a single
# event can never balloon the resulting ICS file with dozens of
# alarms. The dispatcher worker (sprint C) consumes the same list.
_MAX_VALARM_PER_EVENT = 5

# Localised default DESCRIPTION used inside VALARM blocks when no
# specific copy is supplied. Calendar apps fall back to SUMMARY when
# DESCRIPTION is missing, so emitting one is good UX practice.
_VALARM_DESCRIPTION_IT = "Promemoria"
_VALARM_DESCRIPTION_EN = "Reminder"

# RFC 5545 STATUS values are limited; we collapse the bitvision lifecycle
# onto the three standard buckets. The original lifecycle is preserved
# verbatim in ``X-BV-STATUS`` for round-trip fidelity (re-import knows
# the precise state without losing information to the collapse).
_STATUS_MAP = {
    "planned": "TENTATIVE",
    "confirmed": "CONFIRMED",
    "completed": "CONFIRMED",
    "cancelled": "CANCELLED",
    "missed": "CANCELLED",
    "rescheduled": "CANCELLED",
}

# RFC 5545 text values must escape commas, semicolons, backslashes and
# newlines. The grammar is forgiving on order; we apply backslash-escape
# first so we don't double-escape values that already contain a backslash.
_ESCAPE_RE = re.compile(r"([\\,;])")


def _escape(s: str) -> str:
    """RFC 5545 §3.3.11 text-value escaping. CRLF becomes ``\\n``."""
    return _ESCAPE_RE.sub(r"\\\1", s).replace("\r\n", "\\n").replace("\n", "\\n")


def _fmt_utc(dt: datetime) -> str:
    """Render a datetime as RFC 5545 UTC form (``YYYYMMDDTHHMMSSZ``).

    Naive datetimes are treated as UTC by convention. The DB columns
    are ``timestamptz`` so we shouldn't see naive values, but we
    guard against an accidental naive object slipping through."""
    from datetime import timezone as _tz

    if dt.tzinfo is None:
        return dt.strftime("%Y%m%dT%H%M%SZ")
    return dt.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def _fmt_local(dt: datetime) -> str:
    """``YYYYMMDDTHHMMSS`` (no Z, used with TZID parameter)."""
    return dt.strftime("%Y%m%dT%H%M%S")


def _fmt_trigger(offset_minutes: int) -> str:
    """Format a VALARM ``TRIGGER`` value relative to DTSTART.

    Negative offsets fire BEFORE the event (the only direction we use
    in BitVision — reminders are pre-event). Positive offsets fire
    AFTER, which RFC 5545 permits but our scheduler never emits.

    Returns the duration form ``-PTnH``, ``-PTnHnM``, ``-PnD``, etc.
    per RFC 5545 §3.3.6 / §3.8.6.3. Examples::

        offset_minutes=-15    → "-PT15M"
        offset_minutes=-60    → "-PT1H"
        offset_minutes=-90    → "-PT1H30M"
        offset_minutes=-1440  → "-P1D"
        offset_minutes=-1500  → "-P1DT1H"
    """
    if offset_minutes == 0:
        return "PT0S"
    sign = "-" if offset_minutes < 0 else ""
    total = abs(int(offset_minutes))
    days, rem = divmod(total, 1440)
    hours, minutes = divmod(rem, 60)
    # Days portion uses the date form ``P<n>D``; the time portion (if
    # any) uses ``T<h>H<m>M``. Combine accordingly.
    out = "P"
    if days:
        out += f"{days}D"
    if hours or minutes or not days:
        out += "T"
        if hours:
            out += f"{hours}H"
        if minutes:
            out += f"{minutes}M"
        if not hours and not minutes and not days:
            # Should not happen given the offset==0 guard above, but
            # keeps the function total.
            out += "0M"
    return f"{sign}{out}"


def _emit_valarm(offset_minutes: int, *, description: str, summary: str | None = None) -> list[str]:
    """Emit one VALARM block for the given offset (negative = before).

    ACTION:DISPLAY is the most broadly supported across calendar apps
    (iOS / macOS / Google Calendar / Thunderbird all render it as a
    pop-up notification). ACTION:EMAIL would require a SMTP envelope
    inside the file that we're not in a position to authoritatively
    fill in, and ACTION:AUDIO needs an ATTACH:// to a sound file we
    don't host.
    """
    lines = [
        "BEGIN:VALARM",
        "ACTION:DISPLAY",
        f"TRIGGER:{_fmt_trigger(offset_minutes)}",
        f"DESCRIPTION:{_escape(description)}",
    ]
    if summary:
        # Some apps surface the SUMMARY when the alarm pops up; keeping
        # the parent event title there improves the notification text.
        lines.append(f"X-BV-ALARM-SUMMARY:{_escape(summary)}")
    lines.append("END:VALARM")
    return lines


def _valarm_default_description(lang: str) -> str:
    return _VALARM_DESCRIPTION_EN if lang.startswith("en") else _VALARM_DESCRIPTION_IT


def _coerce_offsets(
    raw: Sequence[int] | None,
) -> list[int]:
    """Normalise ``reminder_offsets_minutes`` for VALARM emission.

    - Filter to integers only (JSONB may carry stale types from earlier
      schema versions).
    - Cap at ``_MAX_VALARM_PER_EVENT`` to bound the output size.
    - De-duplicate while preserving order (a user editing the field
      may accidentally repeat ``-60``; emitting one alarm is better
      than two notifications fire-on-fire).
    """
    if not raw:
        return []
    seen: set[int] = set()
    out: list[int] = []
    for v in raw:
        try:
            n = int(v)
        except (TypeError, ValueError):
            continue
        if n in seen:
            continue
        seen.add(n)
        out.append(n)
        if len(out) >= _MAX_VALARM_PER_EVENT:
            break
    return out


def _emit_vevent(
    ev: ClinicalEvent,
    *,
    now_utc: datetime,
    with_valarm: bool = False,
    valarm_lang: str = "it",
) -> list[str]:
    """Emit the VEVENT lines for a single ClinicalEvent.

    When ``with_valarm=True`` AND the row's ``reminder_offsets_minutes``
    is non-empty, one VALARM block per offset is appended before the
    ``END:VEVENT`` marker. The subscription feed leaves the default
    ``False`` so subscribers don't get a notification per offset per
    event every time their client polls the feed."""
    # Pick the timestamp for DTSTART based on lifecycle stage:
    # planned/confirmed -> planned_start_at; completed/missed ->
    # actual_start_at; cancelled/rescheduled may carry only planned_*
    # (already entered the calendar before being cancelled).
    if ev.event_status in ("planned", "confirmed") and ev.planned_start_at:
        dt_start = ev.planned_start_at
        dt_end = ev.planned_end_at or (ev.planned_start_at + timedelta(hours=1))
    elif ev.event_status in ("completed", "missed") and ev.actual_start_at:
        dt_start = ev.actual_start_at
        dt_end = ev.actual_end_at or (ev.actual_start_at + timedelta(hours=1))
    elif ev.planned_start_at:
        dt_start = ev.planned_start_at
        dt_end = ev.planned_end_at or (ev.planned_start_at + timedelta(hours=1))
    elif ev.event_date:
        # All-day fallback for historical rows that have only DATE.
        date_s = ev.event_date.strftime("%Y%m%d")
        date_e = (ev.event_date + timedelta(days=1)).strftime("%Y%m%d")
        all_day_lines = [
            "BEGIN:VEVENT",
            f"UID:event-{ev.id}@bitvision",
            f"DTSTAMP:{_fmt_utc(now_utc)}",
            f"DTSTART;VALUE=DATE:{date_s}",
            f"DTEND;VALUE=DATE:{date_e}",
            f"SUMMARY:{_escape(ev.title)}",
            f"STATUS:{_STATUS_MAP.get(ev.event_status, 'CONFIRMED')}",
            f"CATEGORIES:{_escape(ev.kind)}",
            f"X-BV-STATUS:{ev.event_status}",
            f"X-BV-KIND:{ev.kind}",
        ]
        if with_valarm:
            offsets = _coerce_offsets(ev.reminder_offsets_minutes)
            desc = _valarm_default_description(valarm_lang)
            for off in offsets:
                all_day_lines.extend(_emit_valarm(off, description=desc, summary=ev.title))
        all_day_lines.append("END:VEVENT")
        return all_day_lines
    else:
        # No anchor timestamp; we cannot place this on a calendar.
        return []

    # Timed event with TZID. Falls back to UTC ``Z`` form when no TZ
    # was set on the row.
    tz = ev.timezone or "UTC"
    if tz == "UTC":
        dtstart_line = f"DTSTART:{_fmt_utc(dt_start)}"
        dtend_line = f"DTEND:{_fmt_utc(dt_end)}"
    else:
        # For TZID form RFC 5545 wants local-time (no Z). Convert to
        # the timezone explicitly so the rendered string is consistent
        # with the TZID parameter.
        try:
            from zoneinfo import ZoneInfo

            local_start = dt_start.astimezone(ZoneInfo(tz))
            local_end = dt_end.astimezone(ZoneInfo(tz))
            dtstart_line = f"DTSTART;TZID={tz}:{_fmt_local(local_start)}"
            dtend_line = f"DTEND;TZID={tz}:{_fmt_local(local_end)}"
        except Exception:
            # Unknown TZ name → fall back to UTC Z form so we don't
            # emit a malformed file.
            dtstart_line = f"DTSTART:{_fmt_utc(dt_start)}"
            dtend_line = f"DTEND:{_fmt_utc(dt_end)}"

    lines = [
        "BEGIN:VEVENT",
        f"UID:event-{ev.id}@bitvision",
        f"DTSTAMP:{_fmt_utc(now_utc)}",
        dtstart_line,
        dtend_line,
        f"SUMMARY:{_escape(ev.title)}",
        f"STATUS:{_STATUS_MAP.get(ev.event_status, 'CONFIRMED')}",
        f"CATEGORIES:{_escape(ev.kind)}",
        f"X-BV-STATUS:{ev.event_status}",
        f"X-BV-KIND:{ev.kind}",
    ]
    if ev.narrative:
        # Truncate to a sensible length; the FE shows the full
        # narrative anyway when the event is opened in bitvision.
        desc = ev.narrative[:1000]
        lines.append(f"DESCRIPTION:{_escape(desc)}")
    if ev.location_struct:
        # Render a compact human-readable location string from the
        # struct. Order: facility, room, address, city.
        loc_parts = [
            str(v)
            for v in (
                ev.location_struct.get("facility"),
                ev.location_struct.get("room"),
                ev.location_struct.get("address"),
                ev.location_struct.get("city"),
            )
            if v
        ]
        if loc_parts:
            lines.append(f"LOCATION:{_escape(', '.join(loc_parts))}")
    if with_valarm:
        offsets = _coerce_offsets(ev.reminder_offsets_minutes)
        desc = _valarm_default_description(valarm_lang)
        for off in offsets:
            lines.extend(_emit_valarm(off, description=desc, summary=ev.title))
    lines.append("END:VEVENT")
    return lines


def render_ics(
    events: Iterable[ClinicalEvent],
    *,
    lang: str = "it",
    calendar_name: str = "BitVision",
    now_utc: datetime | None = None,
    with_valarm: bool = False,
) -> str:
    """Render the calendar feed as an iCalendar document.

    ``lang`` controls only the X-WR-CALNAME suffix (the rest of the
    document is language-neutral). ``calendar_name`` is the visible
    name shown by the recipient's calendar app.

    ``with_valarm`` defaults to ``False`` because the subscription feed
    is consumed every few minutes by the recipient's calendar app; if
    the feed carried VALARM blocks each refresh would re-arm the same
    notifications and produce duplicate pops. Single-event exports
    (``render_single_event_ics``) flip the default to ``True``: those
    files are imported once and the VALARMs become local reminders the
    user actually wants.
    """
    if now_utc is None:
        now_utc = datetime.now(UTC)
    suffix = "Calendar" if lang == "en" else "Calendario"
    lines: list[str] = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{_PRODID}",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{_escape(calendar_name)} - {suffix}",
    ]
    for ev in events:
        lines.extend(
            _emit_vevent(
                ev,
                now_utc=now_utc,
                with_valarm=with_valarm,
                valarm_lang=lang,
            )
        )
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


def render_single_event_ics(
    event: ClinicalEvent,
    *,
    lang: str = "it",
    with_valarm: bool = True,
    now_utc: datetime | None = None,
) -> str:
    """Render a single ClinicalEvent as a standalone .ics file.

    Audience: a one-shot "send me the appointment" download, an
    email attachment dispatched by the notifier worker (sprint C+D),
    or a calendar invite the user forwards to a caregiver. Because
    these files are imported once and not re-polled, VALARM blocks
    are opt-in by default (``with_valarm=True``) so the recipient's
    calendar app fires the reminders locally without any server-side
    push.
    """
    if now_utc is None:
        now_utc = datetime.now(UTC)
    suffix = "Calendar" if lang == "en" else "Calendario"
    lines: list[str] = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{_PRODID}",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:BitVision - {suffix}",
    ]
    lines.extend(
        _emit_vevent(
            event,
            now_utc=now_utc,
            with_valarm=with_valarm,
            valarm_lang=lang,
        )
    )
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


# ---------------------------------------------------------------------------
# PatientTask renderer (operational checklist, v3.4)
# ---------------------------------------------------------------------------
#
# A task is rendered as a VEVENT anchored on ``due_at`` (when set),
# falling back to ``completed_at`` for already-done rows. Tasks
# without a date are skipped — calendar apps can't place them.
# STATUS mapping:
#
#     pending     -> TENTATIVE
#     in_progress -> TENTATIVE
#     snoozed     -> TENTATIVE      (the user paused it; still pending)
#     done        -> CONFIRMED      (acts like a completed event)
#     dropped     -> CANCELLED
#
# UID: ``task-{task_id}@bitvision``. Disjoint from event UIDs so the
# two surfaces don't collide on import.

_TASK_STATUS_MAP = {
    "pending": "TENTATIVE",
    "in_progress": "TENTATIVE",
    "snoozed": "TENTATIVE",
    "done": "CONFIRMED",
    "dropped": "CANCELLED",
}


def _emit_task_vevent(
    task: PatientTask,
    *,
    now_utc: datetime,
    with_valarm: bool = False,
    valarm_lang: str = "it",
) -> list[str]:
    """VEVENT block for a single PatientTask.

    Tasks borrow the calendar surface but they're not clinical events;
    we keep the rendering deliberately compact (no LOCATION, no kind
    category) so a recipient's calendar view doesn't conflate the two.
    """
    # Pick the anchor: due_at when present, completed_at for done
    # tasks otherwise. Tasks with neither are not on a calendar.
    dt_start: datetime | None = task.due_at
    if dt_start is None and task.status == "done" and task.completed_at:
        dt_start = task.completed_at
    if dt_start is None:
        return []
    # Tasks have no native end time; default to a 30-minute slot so
    # the calendar app renders a visible block. Calendar apps treat
    # zero-duration events inconsistently (some drop them entirely).
    dt_end = dt_start + timedelta(minutes=30)

    tz = task.timezone or "UTC"
    if tz == "UTC":
        dtstart_line = f"DTSTART:{_fmt_utc(dt_start)}"
        dtend_line = f"DTEND:{_fmt_utc(dt_end)}"
    else:
        try:
            from zoneinfo import ZoneInfo

            local_start = dt_start.astimezone(ZoneInfo(tz))
            local_end = dt_end.astimezone(ZoneInfo(tz))
            dtstart_line = f"DTSTART;TZID={tz}:{_fmt_local(local_start)}"
            dtend_line = f"DTEND;TZID={tz}:{_fmt_local(local_end)}"
        except Exception:
            dtstart_line = f"DTSTART:{_fmt_utc(dt_start)}"
            dtend_line = f"DTEND:{_fmt_utc(dt_end)}"

    lines = [
        "BEGIN:VEVENT",
        f"UID:task-{task.id}@bitvision",
        f"DTSTAMP:{_fmt_utc(now_utc)}",
        dtstart_line,
        dtend_line,
        f"SUMMARY:{_escape(task.title)}",
        f"STATUS:{_TASK_STATUS_MAP.get(task.status, 'TENTATIVE')}",
        f"CATEGORIES:{_escape(task.category)}",
        f"X-BV-STATUS:{task.status}",
        "X-BV-KIND:patient_task",
        f"X-BV-PRIORITY:{task.priority}",
    ]
    if task.description:
        lines.append(f"DESCRIPTION:{_escape(task.description[:1000])}")
    if with_valarm:
        offsets = _coerce_offsets(task.reminder_offsets_minutes)
        desc = _valarm_default_description(valarm_lang)
        for off in offsets:
            lines.extend(_emit_valarm(off, description=desc, summary=task.title))
    lines.append("END:VEVENT")
    return lines


def render_single_task_ics(
    task: PatientTask,
    *,
    lang: str = "it",
    with_valarm: bool = True,
    now_utc: datetime | None = None,
) -> str:
    """Render a single PatientTask as a standalone .ics file.

    Returns the empty ICS skeleton when the task has no anchor date
    (neither ``due_at`` nor ``completed_at``); the caller should
    decide whether to refuse with 422 or to emit the placeholder.
    """
    if now_utc is None:
        now_utc = datetime.now(UTC)
    suffix = "Calendar" if lang == "en" else "Calendario"
    lines: list[str] = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{_PRODID}",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:BitVision - {suffix}",
    ]
    lines.extend(
        _emit_task_vevent(
            task,
            now_utc=now_utc,
            with_valarm=with_valarm,
            valarm_lang=lang,
        )
    )
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


__all__ = [
    "render_ics",
    "render_single_event_ics",
    "render_single_task_ics",
]
