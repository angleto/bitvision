"""Parse ``media_label`` text files shipped on Italian DICOM DVDs.

Several Italian DICOM DVD authoring tools bundle a ``label.txt`` (or
similar) alongside the DICOM payload. Format is key=value,
CRLF-delimited, with ``Field1..Field9`` carrying the clinically
interesting metadata (Modality, StudyDate, InstitutionName,
StudyDescription, etc.). Such files otherwise land in the system as
``document_type='media_label'`` opaque blobs; this module extracts
the fields so the downstream pipelines can prepopulate
``imaging_study.metadata`` and the timeline title.

Wired into the ingestion pipeline: TODO — keep this helper standalone
for now. An internal MCP session report flagged the format as worth
recognising; that's a P3 polish, not a P0, so the integration lands
in a follow-up sprint.

Reference fixture (synthetic — no real patient data)::

    Id=0000000000
    FirstName=
    LastName=ROSSI MARIO
    BirthDate=01/01/1970
    Sex=M
    Field1=DVD 1 di 1
    Field2=TC ADDOME COMPLETO con  MDC
    Field3=16/09/2024 13:13
    Field4=16/09/2024
    Field5=13:13
    Field6=SR
    Field7=TC ADDOME COMPLETO con  MDC
    Field8=TC ADDOME COMPLETO con  MDC
    Field9=OSPEDALE ESEMPIO

Italian hospital workflows often concatenate ``cognome+nome`` into
``LastName`` because the local RIS uses one PN component, leaving
``FirstName`` blank. The parser is lenient about this: callers who
need a clean split should resolve via ``decode_codice_fiscale`` or a
manual review.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime


@dataclass(frozen=True, slots=True)
class MediaLabel:
    """Structured view of a parsed media_label file.

    All fields are optional — a malformed or partial label still
    parses to a row with the recognised keys filled and the rest
    ``None``. Callers compose against ``ClinicalEvent`` /
    ``ImagingStudy`` and skip ``None`` values, so an empty parse is
    a no-op for the ingestion pipeline.
    """

    patient_id_external: str | None
    first_name: str | None
    last_name: str | None
    birth_date: date | None
    sex: str | None
    modality: str | None
    study_date: date | None
    study_description: str | None
    institution_name: str | None
    raw_fields: dict[str, str]


_DATE_PATTERNS: tuple[str, ...] = (
    "%d/%m/%Y",
    "%Y-%m-%d",
    "%d.%m.%Y",
)


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    # Strip a trailing time component (``16/09/2024 13:13`` → ``16/09/2024``).
    cleaned = re.split(r"\s+", cleaned, maxsplit=1)[0]
    for pat in _DATE_PATTERNS:
        try:
            return datetime.strptime(cleaned, pat).date()
        except ValueError:
            continue
    return None


def _normalise_modality(value: str | None) -> str | None:
    """DICOM Modality is uppercase, 2–4 chars (CT, MR, PT, SR, US…).
    The label often uses the exact code; trim and uppercase to be
    safe."""
    if not value:
        return None
    code = value.strip().upper()
    if 1 < len(code) <= 8:
        return code
    return None


def parse_media_label(blob: bytes) -> MediaLabel | None:
    """Parse a media_label blob into a :class:`MediaLabel`.

    Returns ``None`` when the blob does not look like a media_label
    (no ``Field1..Field9`` keys recognised) so callers can chain with
    a generic text fallback. Encoding is best-effort: tries UTF-8,
    falls back to Latin-1 for the ANSI Italian variants emitted by
    older Windows-only authoring tools.
    """
    if not blob:
        return None
    try:
        text = blob.decode("utf-8")
    except UnicodeDecodeError:
        try:
            text = blob.decode("latin-1")
        except UnicodeDecodeError:
            return None

    raw: dict[str, str] = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if key:
            raw[key] = value

    has_field_keys = any(k.startswith("Field") for k in raw)
    if not has_field_keys and "LastName" not in raw:
        return None

    return MediaLabel(
        patient_id_external=raw.get("Id") or None,
        first_name=raw.get("FirstName") or None,
        last_name=raw.get("LastName") or None,
        birth_date=_parse_date(raw.get("BirthDate")),
        sex=(raw.get("Sex") or None),
        # Field6 is the DICOM Modality on the IRST/IST template; if
        # the source uses a different field index a more sophisticated
        # parser will need to map per-vendor.
        modality=_normalise_modality(raw.get("Field6")),
        study_date=_parse_date(raw.get("Field4")),
        study_description=(raw.get("Field2") or raw.get("Field7") or None),
        institution_name=raw.get("Field9") or None,
        raw_fields=raw,
    )


__all__ = ["MediaLabel", "parse_media_label"]
