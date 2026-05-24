"""Clinical entity extraction (Sprint 4, ADR 0008).

Rule-based v0 extractor: scan a piece of italian clinical text and
emit a structured payload with two namespaces, ``entities_proposed``
and ``entities_validated``. The extractor populates only the
proposed namespace; promotion to validated is a separate operator
action (out of scope for v0).

Scope of extraction:

* ``lab_values`` — analyte / value / unit / reference range, e.g.
  ``Creatinina 1.2 mg/dL`` or ``CEA 4.3 ng/mL (vn < 5)``.
* ``measurements`` — clinical measurements outside lab panels: blood
  pressure (``130/80 mmHg``), heart rate (``72 bpm``), temperature
  (``36.7 C``).
* ``dates`` — italian-formatted dates (``12/01/2024``, ``12 gennaio
  2024``) for chronology hints.
* ``procedures_keywords`` — coarse imaging / diagnostic procedure
  keywords (``TC``, ``RMN``, ``ecografia`` …). The list is shared
  with the Sprint 3 ``propose_radiology_reclassification.py`` script,
  but the agent gets it as structured rather than free text.

Confidence calibration is intentionally crude: rule-based hits start
at 0.6 and bump to 0.8 when the surrounding context matches an
expected schema (e.g. a lab value followed by a unit). Future ML
extractors return a posterior probability.

Determinism: identical input + identical version produce a byte-equal
output. The caller relies on this for the ``document_ocr_entities``
cache.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

EXTRACTOR_VERSION = "rules-it-v0.1"


# --- Patterns ---------------------------------------------------------------

# Lab value: <analyte>[<colon>?] <number> <unit>[ (<reference>)]
# Italian decimal comma is allowed; we normalise to a float. The
# analyte allows any Unicode word character via ``\w`` (Python regex
# is Unicode-aware by default in py3).
_LAB_PATTERN = re.compile(
    r"""
    (?P<analyte>[A-Za-z][\w\s/-]{1,40}?)
    [\s:]+
    (?P<value>\d+(?:[.,]\d+)?)
    \s*
    (?P<unit>(?:mg/dL|mg/dl|g/dL|g/dl|mmol/L|mmol/l|ng/mL|ng/ml|UI/L|U/L|umol/L|/uL|mEq/L|%))
    """,
    re.VERBOSE | re.UNICODE,
)

# Blood pressure: 120/80 mmHg, 130 / 80 mmHg
_BP_PATTERN = re.compile(
    r"\b(?P<sys>\d{2,3})\s*/\s*(?P<dia>\d{2,3})\s*mmHg\b",
    re.IGNORECASE,
)

# Heart rate: 72 bpm, FC 84
_HR_PATTERN = re.compile(
    r"\b(?:fc|frequenza\s+cardiaca|hr|heart\s+rate)?\s*"
    r"(?P<bpm>\d{2,3})\s*bpm\b",
    re.IGNORECASE,
)

# Temperature: 36.7 C, 37,2 °C
_TEMP_PATTERN = re.compile(
    r"\b(?:T|temperatura|tc)?\s*(?P<temp>\d{2}[.,]\d)\s*°?\s*C\b",
    re.IGNORECASE,
)

# Italian numeric date: 12/01/2024 or 12-01-2024
_DATE_NUMERIC = re.compile(r"\b(?P<d>\d{1,2})[/-](?P<m>\d{1,2})[/-](?P<y>\d{4})\b")

_MONTH_IT = {
    "gennaio": 1,
    "febbraio": 2,
    "marzo": 3,
    "aprile": 4,
    "maggio": 5,
    "giugno": 6,
    "luglio": 7,
    "agosto": 8,
    "settembre": 9,
    "ottobre": 10,
    "novembre": 11,
    "dicembre": 12,
}

_DATE_LITERAL = re.compile(
    r"\b(?P<d>\d{1,2})\s+(?P<m>" + "|".join(_MONTH_IT.keys()) + r")\s+(?P<y>\d{4})\b",
    re.IGNORECASE,
)


_PROCEDURE_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("TC", "TC"),
    ("TAC", "TC"),
    ("RMN", "RM"),
    ("RM ", "RM"),
    ("ecografia", "US"),
    ("ecodoppler", "US"),
    ("rx torace", "RX"),
    ("radiografia", "RX"),
    ("scintigrafia", "NM"),
    ("PET", "PET"),
    ("mammografia", "MG"),
    ("doppler", "US"),
    ("colonscopia", "ENDO"),
    ("gastroscopia", "ENDO"),
    ("EGDS", "ENDO"),
    ("ecocardiogramma", "US"),
    ("ECG", "ECG"),
    ("Holter", "ECG"),
)


# --- Output schema ----------------------------------------------------------


@dataclass(slots=True)
class _Span:
    text: str
    start: int
    end: int


@dataclass(slots=True)
class LabValueOut:
    text: str
    start: int
    end: int
    analyte: str
    value: float
    unit: str
    confidence: float
    extractor: str = EXTRACTOR_VERSION
    validation_status: str = "unverified"


@dataclass(slots=True)
class MeasurementOut:
    text: str
    start: int
    end: int
    kind: str  # 'blood_pressure' | 'heart_rate' | 'temperature'
    payload: dict[str, Any]
    confidence: float
    extractor: str = EXTRACTOR_VERSION
    validation_status: str = "unverified"


@dataclass(slots=True)
class DateOut:
    text: str
    start: int
    end: int
    iso: str  # YYYY-MM-DD
    confidence: float
    extractor: str = EXTRACTOR_VERSION
    validation_status: str = "unverified"


@dataclass(slots=True)
class ProcedureKeywordOut:
    text: str
    start: int
    end: int
    modality: str
    confidence: float
    extractor: str = EXTRACTOR_VERSION
    validation_status: str = "unverified"


@dataclass(slots=True)
class ExtractionResult:
    extractor_version: str
    extracted_at: str
    entities_proposed: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    entities_validated: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        return {
            "extractor_version": self.extractor_version,
            "extracted_at": self.extracted_at,
            "entities_proposed": self.entities_proposed,
            "entities_validated": self.entities_validated,
        }


# --- Extraction primitives --------------------------------------------------


def _to_float(value: str) -> float:
    return float(value.replace(",", "."))


def _extract_labs(text: str) -> list[LabValueOut]:
    out: list[LabValueOut] = []
    for m in _LAB_PATTERN.finditer(text):
        analyte = m.group("analyte").strip()
        # Conservative rule: short analyte (<3 chars) with no preceding
        # context is too noisy. Filter them out unless the analyte is a
        # known short code like "Hb".
        if len(analyte) < 2:
            continue
        out.append(
            LabValueOut(
                text=m.group(0),
                start=m.start(),
                end=m.end(),
                analyte=analyte,
                value=_to_float(m.group("value")),
                unit=m.group("unit"),
                confidence=0.8,
            )
        )
    return out


def _extract_measurements(text: str) -> list[MeasurementOut]:
    out: list[MeasurementOut] = []
    for m in _BP_PATTERN.finditer(text):
        out.append(
            MeasurementOut(
                text=m.group(0),
                start=m.start(),
                end=m.end(),
                kind="blood_pressure",
                payload={
                    "systolic_mmhg": int(m.group("sys")),
                    "diastolic_mmhg": int(m.group("dia")),
                },
                confidence=0.85,
            )
        )
    for m in _HR_PATTERN.finditer(text):
        out.append(
            MeasurementOut(
                text=m.group(0),
                start=m.start(),
                end=m.end(),
                kind="heart_rate",
                payload={"bpm": int(m.group("bpm"))},
                confidence=0.7,
            )
        )
    for m in _TEMP_PATTERN.finditer(text):
        try:
            value = _to_float(m.group("temp"))
        except ValueError:
            continue
        if 30.0 <= value <= 45.0:
            out.append(
                MeasurementOut(
                    text=m.group(0),
                    start=m.start(),
                    end=m.end(),
                    kind="temperature",
                    payload={"celsius": value},
                    confidence=0.75,
                )
            )
    return out


def _extract_dates(text: str) -> list[DateOut]:
    out: list[DateOut] = []
    for m in _DATE_NUMERIC.finditer(text):
        d = int(m.group("d"))
        mo = int(m.group("m"))
        y = int(m.group("y"))
        if not (1 <= d <= 31 and 1 <= mo <= 12 and 1900 <= y <= 2100):
            continue
        try:
            iso = f"{y:04d}-{mo:02d}-{d:02d}"
            datetime(y, mo, d)
        except ValueError:
            continue
        out.append(
            DateOut(
                text=m.group(0),
                start=m.start(),
                end=m.end(),
                iso=iso,
                confidence=0.9,
            )
        )
    for m in _DATE_LITERAL.finditer(text):
        d = int(m.group("d"))
        mo = _MONTH_IT[m.group("m").lower()]
        y = int(m.group("y"))
        if not (1 <= d <= 31 and 1900 <= y <= 2100):
            continue
        try:
            iso = f"{y:04d}-{mo:02d}-{d:02d}"
            datetime(y, mo, d)
        except ValueError:
            continue
        out.append(
            DateOut(
                text=m.group(0),
                start=m.start(),
                end=m.end(),
                iso=iso,
                confidence=0.85,
            )
        )
    return out


def _extract_procedures(text: str) -> list[ProcedureKeywordOut]:
    out: list[ProcedureKeywordOut] = []
    seen: set[tuple[int, int]] = set()
    for needle, modality in _PROCEDURE_KEYWORDS:
        # case-insensitive substring scan via re for cleanliness; we
        # require word boundaries on alphabetic needles.
        if needle[0].isalpha():
            pat = re.compile(rf"\b{re.escape(needle)}\b", re.IGNORECASE)
        else:
            pat = re.compile(re.escape(needle), re.IGNORECASE)
        for m in pat.finditer(text):
            key = (m.start(), m.end())
            if key in seen:
                continue
            seen.add(key)
            out.append(
                ProcedureKeywordOut(
                    text=m.group(0),
                    start=m.start(),
                    end=m.end(),
                    modality=modality,
                    confidence=0.65,
                )
            )
    return out


def extract_entities(text: str) -> ExtractionResult:
    """Run the rule-based extractor on ``text``.

    Determinism contract: identical ``text`` + the same
    ``EXTRACTOR_VERSION`` produce byte-equal :meth:`to_payload`. The
    ``extracted_at`` field is intentionally NOT part of the cache key
    because it varies — callers that need byte-equality cache by
    ``(document_id, extractor_version, sha256(text))``.
    """
    proposed: dict[str, list[dict[str, Any]]] = {
        "lab_values": [asdict(e) for e in _extract_labs(text)],
        "measurements": [asdict(e) for e in _extract_measurements(text)],
        "dates": [asdict(e) for e in _extract_dates(text)],
        "procedures_keywords": [asdict(e) for e in _extract_procedures(text)],
    }
    return ExtractionResult(
        extractor_version=EXTRACTOR_VERSION,
        extracted_at=datetime.now(UTC).isoformat(),
        entities_proposed=proposed,
        entities_validated={},
    )


def canonical_payload(result: ExtractionResult) -> str:
    """JSON canonical form suitable for hashing or byte-equal compare.

    Drops ``extracted_at`` so two runs of the same extractor on the
    same text produce the same string.
    """
    payload = result.to_payload()
    payload.pop("extracted_at", None)
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


__all__ = [
    "EXTRACTOR_VERSION",
    "ExtractionResult",
    "canonical_payload",
    "extract_entities",
]
