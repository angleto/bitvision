"""Contrast / acquisition phase classifier for multiphase CT.

A contrast-enhanced CT study acquires the same anatomy several times after
a single IV bolus: non-contrast (basal), arterial (~20-40s), portal-venous
(~60-80s), delayed/equilibrium (~3-5min+), and — for liver Gd-EOB or renal
protocols — hepatobiliary / corticomedullary / nephrographic / excretory.
Each phase is acquired as its OWN ``SeriesInstanceUID`` (a full stack at
its own time), so a *phase* is a property of a *series* and classifying a
study means labelling each of its CT series.

This module is the pure, side-effect-free core. It takes the metadata the
ingest path already persists on the series row (``acquisition_time_of_day``,
``contrast_bolus_agent``, ``contrast_bolus_start_time``, description,
body part) plus optionally the ProtocolName (read from the header) and
returns a confidence-scored label per series. The API/MCP layer turns ORM
rows into :class:`SeriesPhaseInput`, calls :func:`classify_study_phases`,
and persists ``acquisition_phase`` / ``phase_confidence`` /
``phase_source='auto'`` back onto the rows.

Design philosophy (matches the product decision "candidate + human
confirm"): **never be confidently wrong**. The description text is the
strong signal; acquisition timing confirms it or, alone, yields a
low-confidence candidate. Anything below :data:`CONFIRM_THRESHOLD` is
surfaced as needing human confirmation rather than silently trusted — a
mislabelled phase silently corrupts washout (labelling portal as arterial
inverts the curve), so ambiguity must degrade to "ask a human", not to a
wrong number.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import time

from bvphoenix.db.models.dicom import ACQUISITION_PHASES
from bvphoenix.services.series_kind import is_non_reviewable_desc

# Confidence a label needs to be trusted without human confirmation. Below
# this, the UI/MCP surfaces the phase as a candidate to confirm.
CONFIRM_THRESHOLD = 0.70

# Confidence bands. These are HEURISTIC trust levels (how much we trust a
# signal combination), NOT clinical constants — they are to be calibrated
# against the real-data golden set. What IS a deliberate safety choice is
# that any combination that could be wrong sits below CONFIRM_THRESHOLD so
# it surfaces for human confirmation instead of being trusted silently.
_CONF_DESC_TIMING_AGREE = 0.95  # description + true-delay timing agree
_CONF_DESC_STRONG = 0.88  # unambiguous description, no timing to confirm
_CONF_DESC_TIMING_CONFLICT = 0.50  # description wins but timing disagrees -> confirm
_CONF_TIMING_TRUE = 0.66  # true post-injection delay, no description match
_CONF_TIMING_RELATIVE = 0.42  # ordinal-only (no bolus-start anchor) -> confirm
# Agent-absence alone is vendor-dependent (not every scanner writes
# ContrastBolusAgent per-phase), so on its own it stays BELOW the confirm
# threshold: a candidate to confirm, never a trusted label. When it merely
# corroborates a description that already says "unenhanced", the description
# branch above scores it high instead.
_CONF_AGENT_UNENHANCED = 0.60

# ---- phase description patterns ---------------------------------------
# Ordered most-specific first; the first match wins. Italian + English
# radiology vocabulary (the platform is bilingual). Word boundaries guard
# against substring collisions (e.g. "art" inside "artifact").
_PHASE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "hepatobiliary",
        re.compile(
            r"hepatobiliary|\bhbp\b|epatobiliare|\beob\b|primovist|gd-?eob|\b20\s*min",
            re.IGNORECASE,
        ),
    ),
    (
        "excretory",
        re.compile(
            r"excretory|urograph|pyelograph|escretor|urograf|pielograf|\bep\b|\buro\b",
            re.IGNORECASE,
        ),
    ),
    (
        "nephrographic",
        re.compile(r"nephrograph|nefrograf|\bnp\b", re.IGNORECASE),
    ),
    (
        "corticomedullary",
        re.compile(
            r"cortico-?medullary|cortico-?midollare|\bcmp\b|corticomidollare", re.IGNORECASE
        ),
    ),
    (
        "delayed",
        re.compile(
            r"delay|tardiv|equilib|wash-?out|\beq\b|\b(3|4|5|10|15)\s*min|late\s*phase",
            re.IGNORECASE,
        ),
    ),
    (
        "portal_venous",
        re.compile(
            r"portal[\s-]*ven|porto[\s-]*ven|portale|venous|venosa|\bpv\b|\bpvp\b|hepatic\s*ven",
            re.IGNORECASE,
        ),
    ),
    (
        "arterial",
        re.compile(
            r"arterial|arterios|\bart\b|\bhap\b|\blap\b|late\s*arter|early\s*arter|hepatic\s*arter",
            re.IGNORECASE,
        ),
    ),
    (
        "unenhanced",
        re.compile(
            r"non[\s-]*contrast|pre[\s-]*contrast|unenhanced|precontrast|"
            r"basale|diretta|senza\s*m\.?d\.?c|senza\s*contrast|nativ|\bnc\b|\bw/?o\s*contrast",
            re.IGNORECASE,
        ),
    ),
    (
        "dynamic",
        re.compile(r"dynamic|perfusion|dinamic|perfusion|\b4d\b|time[\s-]*resolved", re.IGNORECASE),
    ),
)

# ---- timing windows (seconds since injection) -------------------------
# Region-dependent windows for IODINATED-CONTRAST CT, grounded in published
# protocols (NOT invented). Used only when a TRUE delay
# (AcquisitionTime - ContrastBolusStartTime) is available. Order matters:
# first window that contains the delay wins; an UNCOVERED delay returns no
# timing label (conservative: defer to description / human rather than guess).
#
# Hepatic (liver multiphase CT): late-arterial ~35s (bolus-trigger + 15-20s),
# portal-venous ~60-90s, delayed/equilibrium ~3-5 min. Refs: Murakami et al.,
# AJR 2005 (MDCT bolus-tracking hepatic arterial/portal delays); Liver CT
# techniques review, PMC8388239. NOTE: the 'hepatobiliary' phase is a
# gadoxetate (Gd-EOB) MR concept (~20 min), not an iodinated-CT timing
# window — it is therefore description-only here and, since this classifier
# is CT-scoped, effectively assigned only when a series description names it.
# Dynamic contrast-enhanced MR phase timing is a deliberate future extension.
_HEPATIC_WINDOWS: tuple[tuple[float, float, str], ...] = (
    (-1e9, 15.0, "unenhanced"),
    (15.0, 50.0, "arterial"),
    (50.0, 100.0, "portal_venous"),
    (100.0, 1e9, "delayed"),
)
# Renal mass CT (Society of Abdominal Radiology RCC protocol; Radiopaedia):
# corticomedullary 40-70s, nephrographic 100-120s (clinically 80-180s),
# excretory 7-10 min. The 200-420s gap is intentionally uncovered: no
# standard renal acquisition lands there, so a delay in that gap is left
# unlabelled by timing rather than mis-called excretory.
_RENAL_WINDOWS: tuple[tuple[float, float, str], ...] = (
    (-1e9, 15.0, "unenhanced"),
    (15.0, 80.0, "corticomedullary"),
    (80.0, 200.0, "nephrographic"),
    (420.0, 1e9, "excretory"),
)

_RENAL_RE = re.compile(r"kidney|renal|rene|urogr|uro|nephro|nefro", re.IGNORECASE)


@dataclass(frozen=True)
class SeriesPhaseInput:
    """Per-series metadata the classifier reasons over (ORM-decoupled)."""

    series_id: uuid.UUID
    modality: str | None = None
    series_number: int | None = None
    body_part_examined: str | None = None
    series_description: str | None = None
    protocol_name: str | None = None
    acquisition_time_of_day: time | None = None
    contrast_bolus_agent: str | None = None
    contrast_bolus_start_time: time | None = None


@dataclass
class SeriesPhaseResult:
    series_id: uuid.UUID
    acquisition_phase: str | None
    confidence: float | None
    time_offset_s: float | None = None
    offset_is_relative: bool = False
    rationale: list[str] = field(default_factory=list)

    @property
    def needs_confirmation(self) -> bool:
        """A classified-but-uncertain label the human should confirm. An
        unclassified series (``None``) is not a *candidate* — it's simply
        unknown — so it does not need confirmation."""
        return self.acquisition_phase is not None and (
            self.confidence is None or self.confidence < CONFIRM_THRESHOLD
        )


def _seconds_of_day(t: time) -> int:
    return t.hour * 3600 + t.minute * 60 + t.second


def _match_description(text: str | None) -> tuple[str | None, str | None]:
    """Return (phase, matched_pattern_phase_label) for the first hit."""
    if not text:
        return None, None
    for phase, pattern in _PHASE_PATTERNS:
        if pattern.search(text):
            return phase, phase
    return None, None


def _distinct_phase_matches(text: str | None) -> set[str]:
    """Every distinct phase whose pattern matches anywhere in ``text``."""
    if not text:
        return set()
    return {phase for phase, pattern in _PHASE_PATTERNS if pattern.search(text)}


def _unambiguous_protocol_phase(protocol_name: str | None) -> str | None:
    """Phase from a ProtocolName, but ONLY when the protocol names exactly
    one phase.

    A study-level ProtocolName is shared by every series and frequently
    enumerates the *whole* multiphase protocol — e.g. GE's
    "Torace Addome Pelvi (Basale/Arteriosa-Venosa)" names unenhanced +
    arterial + portal at once. Matching the first pattern would stamp that
    single label onto every scout, reformat and dose report in the study.
    So a protocol that resolves to more than one distinct phase is treated
    as a non-discriminating exam descriptor and ignored; only a protocol
    that points at a single phase is trusted as a weak fallback signal.
    """
    matches = _distinct_phase_matches(protocol_name)
    return next(iter(matches)) if len(matches) == 1 else None


def _is_renal(s: SeriesPhaseInput) -> bool:
    for txt in (s.body_part_examined, s.series_description, s.protocol_name):
        if txt and _RENAL_RE.search(txt):
            return True
    return False


def _norm_region(s: SeriesPhaseInput) -> str:
    return "renal" if _is_renal(s) else "hepatic"


def _timing_phase(delay_s: float, region: str) -> str | None:
    windows = _RENAL_WINDOWS if region == "renal" else _HEPATIC_WINDOWS
    for lo, hi, phase in windows:
        if lo <= delay_s < hi:
            return phase
    return None


def classify_study_phases(inputs: list[SeriesPhaseInput]) -> list[SeriesPhaseResult]:
    """Classify every series of a study into a contrast phase.

    Returns one :class:`SeriesPhaseResult` per input, in input order.
    Non-CT series and series the classifier cannot place return
    ``acquisition_phase=None`` (unknown, not a candidate).
    """
    results: dict[uuid.UUID, SeriesPhaseResult] = {}

    ct = [s for s in inputs if (s.modality or "").upper() == "CT"]

    # Group sibling phases by coarse anatomical region so timing windows
    # and the "this series alone lacks contrast" check compare like with
    # like (an abdomen study may also carry a chest series).
    groups: dict[str, list[SeriesPhaseInput]] = {}
    for s in ct:
        groups.setdefault(_norm_region(s), []).append(s)

    for region, series_list in groups.items():
        results.update(_classify_group(series_list, region))

    # Everything not classified (non-CT, or CT the group pass skipped)
    # gets an explicit "unknown".
    for s in inputs:
        results.setdefault(
            s.series_id,
            SeriesPhaseResult(
                series_id=s.series_id,
                acquisition_phase=None,
                confidence=None,
                rationale=[
                    "not a CT series" if (s.modality or "").upper() != "CT" else "unclassified"
                ],
            ),
        )
    return [results[s.series_id] for s in inputs]


def _classify_group(
    group: list[SeriesPhaseInput], region: str
) -> dict[uuid.UUID, SeriesPhaseResult]:
    out: dict[uuid.UUID, SeriesPhaseResult] = {}

    has_any_agent = any((s.contrast_bolus_agent or "").strip() for s in group)
    acq_secs = {
        s.series_id: _seconds_of_day(s.acquisition_time_of_day)
        for s in group
        if s.acquisition_time_of_day is not None
    }
    earliest_acq = min(acq_secs.values()) if acq_secs else None

    for s in group:
        rationale: list[str] = []

        # 0) Localizer / capture / dose report / bolus-prep series are not a
        #    contrast phase at all — never label them (a stale "portal" on a
        #    Scout would otherwise leak into the viewer's phase panes).
        if is_non_reviewable_desc(s.series_description):
            out[s.series_id] = SeriesPhaseResult(
                series_id=s.series_id,
                acquisition_phase=None,
                confidence=None,
                rationale=["non-reviewable series (localizer / capture / dose / prep)"],
            )
            continue

        # 1) Description — the strong signal. ProtocolName is a guarded
        #    fallback: used only when it names exactly one phase (a
        #    multiphase protocol descriptor shared by every series, e.g.
        #    "Basale/Arteriosa-Venosa", is ambiguous and ignored).
        desc_phase, _ = _match_description(s.series_description)
        if desc_phase is None:
            desc_phase = _unambiguous_protocol_phase(s.protocol_name)
        if desc_phase is not None:
            rationale.append(f"description matched '{desc_phase}'")

        # 2) Timing — true delay if the bolus-start tag is present, else an
        #    ordinal anchored to the earliest acquisition in the group.
        delay_s: float | None = None
        offset_relative = False
        if s.contrast_bolus_start_time is not None and s.acquisition_time_of_day is not None:
            delay_s = float(
                _seconds_of_day(s.acquisition_time_of_day)
                - _seconds_of_day(s.contrast_bolus_start_time)
            )
            rationale.append(f"true delay {delay_s:.0f}s post-injection")
        elif s.series_id in acq_secs and earliest_acq is not None:
            delay_s = float(acq_secs[s.series_id] - earliest_acq)
            offset_relative = True
            rationale.append(f"relative offset {delay_s:.0f}s from earliest series")

        timing_phase = (
            _timing_phase(delay_s, region)
            if (delay_s is not None and not offset_relative)
            else None
        )

        # 3) Per-series contrast-agent signal: in a study that used contrast,
        #    a series whose own ContrastBolusAgent is empty is very likely the
        #    pre-contrast acquisition.
        lacks_agent = not (s.contrast_bolus_agent or "").strip()
        agent_unenhanced = has_any_agent and lacks_agent

        phase, confidence = _combine(
            desc_phase=desc_phase,
            timing_phase=timing_phase,
            offset_relative=offset_relative,
            delay_s=delay_s,
            agent_unenhanced=agent_unenhanced,
            rationale=rationale,
        )

        out[s.series_id] = SeriesPhaseResult(
            series_id=s.series_id,
            acquisition_phase=phase,
            confidence=confidence,
            time_offset_s=delay_s,
            offset_is_relative=offset_relative,
            rationale=rationale,
        )
    return out


def _combine(
    *,
    desc_phase: str | None,
    timing_phase: str | None,
    offset_relative: bool,
    delay_s: float | None,
    agent_unenhanced: bool,
    rationale: list[str],
) -> tuple[str | None, float | None]:
    """Fuse the three signals into a (phase, confidence). Description is
    authoritative; timing confirms or, alone, gives a low-confidence
    candidate; the agent-absence signal backs up 'unenhanced'."""
    if desc_phase is not None:
        if timing_phase is not None and timing_phase == desc_phase:
            rationale.append("timing agrees")
            return desc_phase, _CONF_DESC_TIMING_AGREE
        if timing_phase is not None and timing_phase != desc_phase:
            rationale.append("timing disagrees -> confirm")
            return desc_phase, _CONF_DESC_TIMING_CONFLICT
        if desc_phase == "unenhanced" and agent_unenhanced:
            rationale.append("no contrast agent on this series")
            return desc_phase, _CONF_DESC_TIMING_AGREE
        return desc_phase, _CONF_DESC_STRONG

    # No description match — rely on timing / agent.
    if timing_phase is not None:
        # Pre-contrast from a true sub-15s delay is only credible if the
        # series also lacks its own agent; otherwise prefer arterial-or-later.
        if timing_phase == "unenhanced" and not agent_unenhanced:
            rationale.append("sub-15s delay but agent present -> ambiguous")
            return "unenhanced", _CONF_TIMING_RELATIVE
        rationale.append("timing-only candidate")
        return timing_phase, _CONF_TIMING_TRUE

    if agent_unenhanced:
        rationale.append("no contrast agent while siblings have it")
        return "unenhanced", _CONF_AGENT_UNENHANCED

    if offset_relative and delay_s is not None:
        # Ordinal-only: we can order but not name reliably. Emit the
        # earliest as a low-confidence unenhanced/arterial candidate only
        # when it also lacks an agent; otherwise leave unknown.
        rationale.append("ordinal-only, insufficient to name -> unknown")
        return None, None

    return None, None


# Validate the pattern table can only ever emit known phases (guards a
# typo from shipping an out-of-vocabulary label that the CHECK constraint
# would later reject at write time).
assert {p for p, _ in _PHASE_PATTERNS}.issubset(set(ACQUISITION_PHASES))
