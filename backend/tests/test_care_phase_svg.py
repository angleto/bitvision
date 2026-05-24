"""Snapshot-style tests for the care-timeline SVG renderer.

Builds a synthetic ``CareTimelineOut`` mirroring the seven phases /
events of `the local reference SVG (not committed)` and asserts
that the rendered SVG:

* parses as XML;
* contains every phase name, every formatted event date, and every
  distinct phase color;
* honors ``lang="en"`` (English month abbreviations) and
  ``theme="dark"`` (dark background fill).
"""

from __future__ import annotations

import uuid
import xml.etree.ElementTree as ET
from datetime import UTC, date, datetime

from bvphoenix.services.care_phase_schemas import (
    CarePhaseCounts,
    CarePhaseDetailOut,
    CareTimelineOut,
    GenericEventTarget,
    TimelineEventOut,
)
from bvphoenix.services.care_phase_svg import render_svg

# Reference data extracted from `the local reference SVG (not committed)`.
PATIENT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
NOW = datetime(2026, 5, 3, 12, 0, tzinfo=UTC)

# (slug, name, kind, color_hex, [(yyyy-mm-dd, title)])
_PHASES = [
    (
        "imaging-pre-op",
        "Imaging pre-op",
        "imaging",
        "#185FA5",
        [(date(2024, 5, 20), "RM addome superiore senza/con MDC")],
    ),
    (
        "intervento-chirurgico",
        "Intervento chirurgico",
        "surgery",
        "#993C1D",
        [
            (date(2024, 7, 29), "Fine procedura chirurgica"),
            (date(2024, 8, 6), "Relazione di dimissione"),
            (date(2024, 8, 13), "Biopsia post-operatoria"),
        ],
    ),
    (
        "followup-post-op",
        "Follow-up post-op",
        "followup",
        "#185FA5",
        [
            (date(2024, 9, 16), "TC addome completo con MDC"),
            (date(2024, 9, 30), "RM addome superiore con MDC"),
            (date(2024, 10, 25), "PET total body"),
        ],
    ),
    (
        "inizio-followup-oncologico",
        "Inizio follow-up oncologico",
        "followup",
        "#534AB7",
        [
            (date(2024, 10, 29), "Visita oncologica n.1"),
            (date(2024, 11, 15), "Ecografia con mezzo di contrasto"),
            (date(2024, 11, 19), "Visita oncologica n.2"),
        ],
    ),
    (
        "sorveglianza-imaging-2025",
        "Sorveglianza imaging 2025",
        "surveillance",
        "#185FA5",
        [
            (date(2025, 2, 3), "TC addome superiore"),
            (date(2025, 5, 21), "TC addome completo"),
        ],
    ),
    (
        "visita-2025",
        "Visita 2025",
        "visit",
        "#534AB7",
        [(date(2025, 11, 14), "Visita oncologica n.3")],
    ),
    (
        "rivalutazione-primavera-2026",
        "Rivalutazione primavera 2026",
        "reassessment",
        "#854F0B",
        [
            (date(2026, 3, 9), "TC addome completo"),
            (date(2026, 3, 18), "Visita oncologica n.4"),
            (date(2026, 3, 25), "Esami ematologici + ECG"),
            (date(2026, 4, 3), "PET FDG total body"),
            (date(2026, 4, 7), "Ecoendoscopia + visite onc"),
        ],
    ),
]


def _make_target(ev_id: uuid.UUID) -> GenericEventTarget:
    return GenericEventTarget(
        kind="event",
        id=ev_id,
        url=f"/events/{ev_id}",
        mcp_uri=f"mcp://event/{ev_id}",
    )


def _make_event(d: date, title: str, phase_id: uuid.UUID) -> TimelineEventOut:
    eid = uuid.uuid4()
    return TimelineEventOut(
        id=eid,
        patient_id=PATIENT_ID,
        kind="generic",
        event_date=d,
        title=title,
        phase_id=phase_id,
        target=_make_target(eid),
        etag=uuid.uuid4(),
    )


def _build_timeline() -> CareTimelineOut:
    phases: list[CarePhaseDetailOut] = []
    for ordinal, (slug, name, kind, color, evs) in enumerate(_PHASES):
        phase_id = uuid.uuid4()
        events = [_make_event(d, t, phase_id) for d, t in evs]
        phases.append(
            CarePhaseDetailOut(
                id=phase_id,
                patient_id=PATIENT_ID,
                slug=slug,
                name=name,
                name_i18n={},
                kind=kind,
                color_hex=color,
                start_date=None,
                end_date=None,
                ordinal=ordinal,
                narrative_md=None,
                author_kind="human",
                proposed_by_agent_id=None,
                confirmed_by_user_id=None,
                confirmed_at=None,
                etag=uuid.uuid4(),
                created_at=NOW,
                updated_at=NOW,
                counts=CarePhaseCounts(n_events=len(evs)),
                events=events,
            )
        )
    return CareTimelineOut(
        patient_id=PATIENT_ID,
        phases=phases,
        unassigned_events=[],
        generated_at=NOW,
        lang="it",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_render_svg_italian_snapshot() -> None:
    timeline = _build_timeline()
    svg = render_svg(timeline, lang="it")

    assert svg.startswith("<svg")

    # Round-trip parse as XML.
    root = ET.fromstring(svg)
    assert root.tag.endswith("svg")

    # All seven phase names present.
    for _, name, _, _, _ in _PHASES:
        # Phase names are split into multiple text lines (cap 14 chars),
        # so check the first word at minimum.
        first_word = name.split()[0]
        assert first_word in svg, f"missing phase first word: {first_word}"

    # Italian month abbreviations & dates.
    expected_dates = [
        "20 mag 2024",
        "29 lug 2024",
        "6 ago 2024",
        "13 ago 2024",
        "16 set 2024",
        "30 set 2024",
        "25 ott 2024",
        "29 ott 2024",
        "15 nov 2024",
        "19 nov 2024",
        "3 feb 2025",
        "21 mag 2025",
        "14 nov 2025",
        "9 mar 2026",
        "18 mar 2026",
        "25 mar 2026",
        "3 apr 2026",
        "7 apr 2026",
    ]
    for ds in expected_dates:
        assert ds in svg, f"missing date: {ds}"

    # All distinct color codes present (case-insensitive).
    distinct_colors = {c for _, _, _, c, _ in _PHASES}
    upper_svg = svg.upper()
    for c in distinct_colors:
        assert c.upper() in upper_svg, f"missing color: {c}"


def test_render_svg_english_months() -> None:
    timeline = _build_timeline()
    svg = render_svg(timeline, lang="en")

    root = ET.fromstring(svg)
    assert root.tag.endswith("svg")

    # English short month abbreviations should appear instead of Italian ones.
    for token in (
        "May 2024",
        "Jul 2024",
        "Aug 2024",
        "Sep 2024",
        "Nov 2024",
        "Feb 2025",
        "Mar 2026",
        "Apr 2026",
    ):
        assert token in svg, f"missing english date token: {token}"

    # Italian abbreviations must NOT appear.
    for it_token in ("mag 2024", "lug 2024", "ott 2024"):
        assert it_token not in svg, f"unexpected italian token: {it_token}"

    # Header label should be English.
    assert "Phase" in svg
    assert "Event" in svg


def test_render_svg_dark_theme_background() -> None:
    timeline = _build_timeline()
    svg = render_svg(timeline, theme="dark")

    root = ET.fromstring(svg)
    assert root.tag.endswith("svg")

    # Dark background fill must be present (background rect + style hint).
    assert "#1a1a1a" in svg or "#1A1A1A" in svg


def test_render_svg_unassigned_events_chip() -> None:
    timeline = _build_timeline()
    extra_id = uuid.uuid4()
    timeline.unassigned_events.append(
        TimelineEventOut(
            id=extra_id,
            patient_id=PATIENT_ID,
            kind="generic",
            event_date=date(2026, 4, 30),
            title="Evento orfano",
            phase_id=None,
            target=_make_target(extra_id),
            etag=uuid.uuid4(),
        )
    )
    svg = render_svg(timeline, lang="it")
    assert "Non assegnati" in svg
    assert "Evento orfano" in svg
    assert "30 apr 2026" in svg

    svg_en = render_svg(timeline, lang="en")
    assert "Unassigned" in svg_en
