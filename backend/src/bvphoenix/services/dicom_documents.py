"""Render the non-image DICOM SOP classes the viewer can show.

The 2D viewer can fall through to this service when
:func:`services.thumbnails.dicom_to_jpeg` raises ``NoPixelDataError``.
Two cases are useful clinically:

* **Encapsulated PDF** (SOP class ``1.2.840.10008.5.1.4.1.1.104.1``) —
  the bytes wrap a complete PDF in the ``EncapsulatedDocument`` field.
  We hand the PDF straight back to the browser.

* **Structured Report** (any of the ``88.x`` SOP classes) — a tree of
  coded concepts + values living under ``ContentSequence``. We flatten
  it into a plain-text rendering that the viewer can show in a
  scrollable panel. This is enough for radiologists to *read* the
  report; round-tripping back to a real SR object is out of scope (we
  already have :mod:`bvphoenix.services.markers_sr` for that on the
  measurements path).

Encapsulated CDA + Presentation State + Key Object are intentionally
unsupported here — the first two need an HTML/CDA stylesheet, the
last two refer to other instances and are best rendered as overlays
on top of the referenced image (a future viewer feature).
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Any

import pydicom

SOP_ENCAPSULATED_PDF = "1.2.840.10008.5.1.4.1.1.104.1"
SOP_ENCAPSULATED_CDA = "1.2.840.10008.5.1.4.1.1.104.2"

# All SR SOP classes we know how to flatten to plain text. Includes the
# common 88.* family and the radiation-dose / mammography CAD variants.
SR_SOP_CLASSES = frozenset(
    {
        "1.2.840.10008.5.1.4.1.1.88.11",  # Basic Text SR
        "1.2.840.10008.5.1.4.1.1.88.22",  # Enhanced SR
        "1.2.840.10008.5.1.4.1.1.88.33",  # Comprehensive SR
        "1.2.840.10008.5.1.4.1.1.88.34",  # Comprehensive 3D SR
        "1.2.840.10008.5.1.4.1.1.88.40",  # Procedure Log
        "1.2.840.10008.5.1.4.1.1.88.50",  # Mammography CAD SR
        "1.2.840.10008.5.1.4.1.1.88.65",  # Chest CAD SR
        "1.2.840.10008.5.1.4.1.1.88.67",  # X-Ray Radiation Dose SR
    }
)


class UnsupportedDocumentError(Exception):
    """Raised when a DICOM instance is non-image but the document
    flavour is one we don't yet render (Presentation State, Key Object,
    Encapsulated CDA)."""


@dataclass
class DicomDocument:
    """Result of decoding a non-image DICOM instance.

    ``kind`` is the discriminant for the viewer:
      * ``"pdf"`` → ``data`` is the raw PDF bytes, ``mime_type`` is
        ``application/pdf``.
      * ``"text"`` → ``data`` is the rendered text encoded as UTF-8
        bytes, ``mime_type`` is ``text/plain; charset=utf-8``.

    ``title`` is a short human label the viewer uses as a header
    (e.g. the SR's ``ConceptNameCodeSequence`` meaning, or the
    Encapsulated PDF's ``DocumentTitle`` when present).
    """

    kind: str
    mime_type: str
    data: bytes
    title: str | None = None


def read_dicom_document(dcm_bytes: bytes) -> DicomDocument:
    """Decode a DICOM instance into a viewer-ready document.

    Raises :class:`UnsupportedDocumentError` for SOP classes we don't
    yet flatten (Presentation State, Key Object Selection,
    Encapsulated CDA). The endpoint translates that to a 415.
    """
    ds = pydicom.dcmread(io.BytesIO(dcm_bytes))
    sop = str(getattr(ds, "SOPClassUID", "") or "")

    if sop == SOP_ENCAPSULATED_PDF:
        payload = getattr(ds, "EncapsulatedDocument", None)
        if not payload:
            raise UnsupportedDocumentError("encapsulated PDF has no document bytes")
        title = _str_or_none(getattr(ds, "DocumentTitle", None))
        return DicomDocument(
            kind="pdf",
            mime_type="application/pdf",
            data=bytes(payload),
            title=title,
        )

    if sop in SR_SOP_CLASSES:
        text = _render_sr_to_text(ds)
        title = _sr_title(ds)
        return DicomDocument(
            kind="text",
            mime_type="text/plain; charset=utf-8",
            data=text.encode("utf-8"),
            title=title,
        )

    # Encapsulated CDA, Presentation State, Key Object — out of scope
    # for the current viewer. The endpoint returns 415 so the frontend
    # can show "format not yet supported" instead of a blank canvas.
    raise UnsupportedDocumentError(f"SOP class {sop or 'unknown'} not supported")


# ---- SR rendering ----------------------------------------------------


def _render_sr_to_text(ds: pydicom.Dataset) -> str:
    """Flatten an SR's ContentSequence into an indented text outline.

    The DICOM SR tree mixes node types (CONTAINER, TEXT, NUM, DATE,
    CODE, IMAGE, etc.) under a ``ConceptNameCodeSequence`` + a
    type-specific value field. The renderer is intentionally lossy —
    the goal is "the radiologist can read the report at a glance",
    not round-trip fidelity. Coded concepts that don't carry text
    (PNAME, IMAGE, SCOORD references) appear as a one-line summary.
    """
    out: list[str] = []
    title = _sr_title(ds)
    if title:
        out.append(title)
        out.append("")

    # Root-level metadata that's useful to surface even before the
    # ContentSequence walk.
    completion = _str_or_none(getattr(ds, "CompletionFlag", None))
    verification = _str_or_none(getattr(ds, "VerificationFlag", None))
    if completion or verification:
        meta_bits = []
        if completion:
            meta_bits.append(f"completion={completion}")
        if verification:
            meta_bits.append(f"verification={verification}")
        out.append("[" + ", ".join(meta_bits) + "]")
        out.append("")

    seq = getattr(ds, "ContentSequence", None)
    if seq:
        _walk(seq, depth=0, out=out)
    else:
        out.append("(empty ContentSequence)")
    return "\n".join(out).rstrip() + "\n"


def _walk(items: Any, *, depth: int, out: list[str]) -> None:
    indent = "  " * depth
    for item in items:
        rel = _str_or_none(getattr(item, "RelationshipType", None))
        vt = _str_or_none(getattr(item, "ValueType", None))
        name = _concept_name(getattr(item, "ConceptNameCodeSequence", None))
        prefix = f"{indent}- "
        if rel and rel != "CONTAINS":
            prefix = f"{indent}- ({rel}) "
        if vt == "CONTAINER":
            header = f"{prefix}{name or '(container)'}"
            out.append(header)
            sub = getattr(item, "ContentSequence", None)
            if sub:
                _walk(sub, depth=depth + 1, out=out)
            continue
        if vt == "TEXT":
            value = _str_or_none(getattr(item, "TextValue", None))
            out.append(f"{prefix}{name + ': ' if name else ''}{value or ''}")
            continue
        if vt == "NUM":
            mv = getattr(item, "MeasuredValueSequence", None)
            num = "?"
            unit = ""
            if mv:
                first = mv[0]
                num = _str_or_none(getattr(first, "NumericValue", None)) or "?"
                unit_seq = getattr(first, "MeasurementUnitsCodeSequence", None)
                if unit_seq:
                    unit = (
                        _str_or_none(getattr(unit_seq[0], "CodeValue", None))
                        or _str_or_none(getattr(unit_seq[0], "CodeMeaning", None))
                        or ""
                    )
            out.append(f"{prefix}{name + ': ' if name else ''}{num} {unit}".rstrip())
            continue
        if vt == "CODE":
            cv = _concept_name(getattr(item, "ConceptCodeSequence", None))
            out.append(f"{prefix}{name + ': ' if name else ''}{cv or ''}")
            continue
        if vt == "DATE":
            v = _str_or_none(getattr(item, "Date", None))
            out.append(f"{prefix}{name + ': ' if name else ''}{v or ''}")
            continue
        if vt == "DATETIME":
            v = _str_or_none(getattr(item, "DateTime", None))
            out.append(f"{prefix}{name + ': ' if name else ''}{v or ''}")
            continue
        if vt == "TIME":
            v = _str_or_none(getattr(item, "Time", None))
            out.append(f"{prefix}{name + ': ' if name else ''}{v or ''}")
            continue
        if vt == "UIDREF":
            v = _str_or_none(getattr(item, "UID", None))
            out.append(f"{prefix}{name + ': ' if name else ''}{v or ''}")
            continue
        if vt == "PNAME":
            v = _str_or_none(getattr(item, "PersonName", None))
            out.append(f"{prefix}{name + ': ' if name else ''}{v or ''}")
            continue
        # Unknown / unhandled value type: best-effort summary so the
        # walk keeps going.
        out.append(f"{prefix}{name + ': ' if name else ''}({vt or 'unknown'})")
        sub = getattr(item, "ContentSequence", None)
        if sub:
            _walk(sub, depth=depth + 1, out=out)


def _sr_title(ds: pydicom.Dataset) -> str | None:
    """Return the SR's top-level concept name or document title."""
    seq = getattr(ds, "ConceptNameCodeSequence", None)
    if seq:
        meaning = _str_or_none(getattr(seq[0], "CodeMeaning", None))
        if meaning:
            return meaning
    desc = _str_or_none(getattr(ds, "SeriesDescription", None))
    return desc


def _concept_name(seq: Any) -> str | None:
    if not seq:
        return None
    try:
        item = seq[0]
    except (IndexError, TypeError):
        return None
    return _str_or_none(getattr(item, "CodeMeaning", None)) or _str_or_none(
        getattr(item, "CodeValue", None)
    )


def _str_or_none(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s if s else None
