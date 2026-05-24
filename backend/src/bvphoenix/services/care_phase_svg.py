"""Server-side SVG renderer for the care timeline.

Pure stdlib implementation that mirrors the visual style of
`the local reference SVG (not committed)`: a vertical dashed
spine with colored phase chips on the left and event dots + date +
title on the right.

The function is intentionally pure: no I/O, no globals, deterministic
given the input. It is reused by:

* the REST endpoint ``GET /care-timeline?format=svg``;
* the MCP tool ``render_care_timeline_svg``;
* the print stylesheet on the frontend (`@media print`).

Layout constants are tuned to match the reference SVG. The formula
for ``total_height`` keeps the spine 40 px short of the bottom edge.
"""

from __future__ import annotations

from datetime import date
from xml.sax.saxutils import escape as _xml_escape

from bvphoenix.services.care_phase_schemas import (
    CarePhaseDetailOut,
    CareTimelineOut,
    TimelineEventOut,
)

# ---------------------------------------------------------------------------
# Layout constants (px). Values picked to reproduce the reference SVG.
# ---------------------------------------------------------------------------

_HEADER_Y = 40
_FIRST_PHASE_Y = 60
_PHASE_X = 20
_PHASE_W = 90
_PHASE_LABEL_X = _PHASE_X + _PHASE_W // 2  # 65
_SPINE_X = 120
_DATE_X = 140
_TITLE_X = 140
_EVENT_PITCH = 30  # vertical distance between event dots
_PHASE_GAP = 20  # gap below a phase chip before the next one starts
_FIRST_EVENT_OFFSET = 20  # first event dot lives PHASE_TOP + 20

_NEUTRAL = "#888780"  # unassigned chip / spine color
_DARK_BG = "#1a1a1a"
_LIGHT_BG = "#FFFFFF"
_DARK_HEADER = "#ECECEA"
_DARK_BODY = "#D9D8D2"
_LIGHT_HEADER = "#141413"
_LIGHT_BODY = "#3D3D3A"

# Font stack — multi-word family names are wrapped with the XML entity
# ``&quot;`` so the value is well-formed both as an XML attribute (the
# enclosing ``style="..."`` is double-quoted) and as CSS (the browser
# unescapes ``&quot;`` to a real double quote before parsing the rule).
_FONT_FAMILY = (
    "&quot;Anthropic Sans&quot;, -apple-system, "
    "&quot;system-ui&quot;, &quot;Segoe UI&quot;, sans-serif"
)

# Italian / English month abbreviations (lowercase, 3 letters, no dot)
# matching the reference SVG ("20 mag 2024", "29 lug 2024").
_MONTHS_IT = (
    "gen",
    "feb",
    "mar",
    "apr",
    "mag",
    "giu",
    "lug",
    "ago",
    "set",
    "ott",
    "nov",
    "dic",
)
_MONTHS_EN = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)

_HEADERS = {
    "it": ("Fase", "Data", "Evento", "Non assegnati"),
    "en": ("Phase", "Date", "Event", "Unassigned"),
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _hex_to_rgb(h: str) -> tuple[int, int, int]:
    s = h.lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    return int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)


def _rgb_to_hex(r: int, g: int, b: int) -> str:
    return f"#{r:02X}{g:02X}{b:02X}"


def _lighten(h: str, factor: float = 0.92) -> str:
    """Blend the color toward white by ``factor`` (0=keep, 1=white).

    Used to derive the soft chip fill from the chip stroke; ``0.92``
    matches the pastel tones in the reference SVG (``#185FA5`` →
    ``#E6F1FB``, ``#993C1D`` → ``#FAECE7``, etc.).
    """
    r, g, b = _hex_to_rgb(h)
    nr = round(r + (255 - r) * factor)
    ng = round(g + (255 - g) * factor)
    nb = round(b + (255 - b) * factor)
    return _rgb_to_hex(nr, ng, nb)


def _luminance(h: str) -> float:
    r, g, b = _hex_to_rgb(h)
    return (0.299 * r + 0.587 * g + 0.114 * b) / 255.0


def _contrast_text(bg_hex: str) -> str:
    """Return ``#141413`` on light backgrounds and ``#FFFFFF`` on dark.

    Simple WCAG-AA-friendly heuristic: perceived luminance on the
    [0, 1] range. The threshold ``0.5`` keeps white text only on
    genuinely dark fills (which is what the reference SVG uses on
    the saturated chip strokes when projected on a dark theme).
    """
    return "#141413" if _luminance(bg_hex) > 0.5 else "#FFFFFF"


def _format_date(d: date | None, lang: str) -> str:
    if d is None:
        return ""
    months = _MONTHS_EN if lang == "en" else _MONTHS_IT
    return f"{d.day} {months[d.month - 1]} {d.year}"


def _split_phase_label(name: str, max_lines: int = 3) -> list[str]:
    """Wrap a phase name into 1..``max_lines`` lines for the chip.

    The reference SVG chips are 90 px wide and use 12 px text; we cap
    each line at ~14 chars to avoid overflow. Words longer than the
    cap are kept on their own line (better truncated visually than
    silently dropped).
    """
    words = name.split()
    if not words:
        return [""]
    lines: list[str] = []
    current = ""
    cap = 14
    for idx, w in enumerate(words):
        candidate = (current + " " + w).strip()
        if len(candidate) <= cap or not current:
            current = candidate
        else:
            lines.append(current)
            current = w
            if len(lines) == max_lines - 1:
                # last line gets whatever remains
                current = " ".join([current, *words[idx + 1 :]])
                break
    if current:
        lines.append(current)
    return lines[:max_lines]


def _esc(s: str | None) -> str:
    return _xml_escape(s or "")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def render_svg(
    timeline: CareTimelineOut,
    *,
    lang: str = "it",
    theme: str = "light",
    width: int = 680,
) -> str:
    """Return an SVG string reproducing the reference timeline style.

    Parameters
    ----------
    timeline
        Care timeline projection (phases + unassigned events).
    lang
        ``"it"`` or ``"en"`` — controls header labels and date format.
    theme
        ``"light"`` or ``"dark"``.
    width
        Canvas width in px; height is computed from the content.
    """
    if lang not in ("it", "en"):
        lang = "it"
    if theme not in ("light", "dark"):
        theme = "light"

    headers = _HEADERS[lang]
    bg = _DARK_BG if theme == "dark" else _LIGHT_BG
    header_color = _DARK_HEADER if theme == "dark" else _LIGHT_HEADER
    body_color = _DARK_BODY if theme == "dark" else _LIGHT_BODY

    # ------------------------------------------------------------------
    # Geometry: per-phase rect + per-event row.
    # ------------------------------------------------------------------

    # Build a flat list of (phase_or_None, events) groups; the trailing
    # group with phase=None hosts the unassigned events under a neutral
    # chip (only emitted when there is at least one unassigned event).
    groups: list[tuple[CarePhaseDetailOut | None, list[TimelineEventOut]]] = []
    for ph in timeline.phases:
        groups.append((ph, list(ph.events)))
    if timeline.unassigned_events:
        groups.append((None, list(timeline.unassigned_events)))

    # Compute y-positions for each group and event.
    y_cursor = _FIRST_PHASE_Y
    layout: list[
        tuple[
            CarePhaseDetailOut | None,
            int,  # phase_top
            int,  # phase_height
            list[tuple[TimelineEventOut, int]],  # (event, dot_y)
        ]
    ] = []
    for phase, events in groups:
        n = max(1, len(events))
        height = max(40, n * _EVENT_PITCH + 10)
        rows: list[tuple[TimelineEventOut, int]] = []
        for idx, ev in enumerate(events):
            rows.append((ev, y_cursor + _FIRST_EVENT_OFFSET + idx * _EVENT_PITCH))
        layout.append((phase, y_cursor, height, rows))
        y_cursor += height + _PHASE_GAP

    total_height = y_cursor + 20  # bottom padding
    spine_y2 = total_height - 40

    # ------------------------------------------------------------------
    # Emit SVG.
    # ------------------------------------------------------------------

    title_text = _esc("Care timeline" if lang == "en" else "Timeline clinica")
    desc_text = _esc(
        "Vertical chronology of clinical events grouped by care phase."
        if lang == "en"
        else "Cronologia verticale degli eventi clinici raggruppati per fase."
    )

    parts: list[str] = []
    parts.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" role="img" '
        f'width="100%" viewBox="0 0 {width} {total_height}" '
        f'style="font-family:{_FONT_FAMILY};background:{bg}">'
    )
    parts.append(f"<title>{title_text}</title>")
    parts.append(f"<desc>{desc_text}</desc>")

    # Background rect (so the bg color is part of the raster export).
    parts.append(f'<rect x="0" y="0" width="{width}" height="{total_height}" fill="{bg}"/>')

    # Header labels.
    parts.append(
        f'<text x="40" y="{_HEADER_Y}" font-size="14" font-weight="500" '
        f'fill="{header_color}">{_esc(headers[0])}</text>'
    )
    parts.append(
        f'<text x="{_DATE_X}" y="{_HEADER_Y}" font-size="14" '
        f'font-weight="500" fill="{header_color}">{_esc(headers[1])}</text>'
    )
    parts.append(
        f'<text x="260" y="{_HEADER_Y}" font-size="14" font-weight="500" '
        f'fill="{header_color}">{_esc(headers[2])}</text>'
    )

    # Spine.
    parts.append(
        f'<line x1="{_SPINE_X}" y1="50" x2="{_SPINE_X}" y2="{spine_y2}" '
        f'stroke="{_NEUTRAL}" stroke-width="0.5" stroke-dasharray="3 3"/>'
    )

    # Phases / unassigned group.
    for phase, phase_top, phase_h, rows in layout:
        if phase is None:
            color = _NEUTRAL
            label = headers[3]
        else:
            color = phase.color_hex or _NEUTRAL
            label = phase.name

        fill = _lighten(color, 0.92)
        stroke = color

        # Chip rect.
        parts.append(
            f"<g>"
            f'<rect x="{_PHASE_X}" y="{phase_top}" width="{_PHASE_W}" '
            f'height="{phase_h}" rx="6" fill="{fill}" stroke="{stroke}" '
            f'stroke-width="0.5"/>'
        )
        # Chip label (centered, 1..3 lines).
        lines = _split_phase_label(label)
        # Vertically center the block of lines inside the chip.
        line_h = 16
        block_h = len(lines) * line_h
        first_baseline = phase_top + (phase_h - block_h) // 2 + 12
        for i, ln in enumerate(lines):
            parts.append(
                f'<text x="{_PHASE_LABEL_X}" '
                f'y="{first_baseline + i * line_h}" '
                f'text-anchor="middle" font-size="12" fill="{stroke}">'
                f"{_esc(ln)}</text>"
            )
        parts.append("</g>")

        # Event rows.
        for ev, dot_y in rows:
            date_str = _format_date(ev.event_date, lang)
            parts.append(f'<circle cx="{_SPINE_X}" cy="{dot_y}" r="4" fill="{color}"/>')
            parts.append(
                f'<text x="{_DATE_X}" y="{dot_y - 4}" font-size="14" '
                f'fill="{header_color}">{_esc(date_str)}</text>'
            )
            parts.append(
                f'<text x="{_TITLE_X}" y="{dot_y + 12}" font-size="12" '
                f'fill="{body_color}">{_esc(ev.title)}</text>'
            )

    parts.append("</svg>")
    return "".join(parts)


__all__ = ["render_svg"]
