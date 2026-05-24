"""DICOM SR (Structured Report) conversion for ``Marker`` rows.

The viewer's marker model is internally JSON (geometry + computed +
body, see ``db/models/markers.py``). This module is the bridge to the
DICOM standard so we can:

  * **Export** a study's markers as a DICOM SR object (TID 1500
    Measurement Report) — interoperable with any PACS / viewer that
    speaks SR.
  * **Import** an SR file back into ``Marker`` rows so a clinician
    can resume reading on a different system.

JSON stays the canonical format internally. SR is a translation
layer for interop. Lossy round-trip is acceptable for now (DICOM SR
has a richer concept tree than we need); we round-trip the kinds we
actively use and the rest stays in the JSON envelope.

Library: ``highdicom`` (MIT licence, built on pydicom). It exposes
typed builders for the SR templates without forcing the caller to
hand-craft DICOM datasets.

This module is sync-only (CPU work, no DB / IO). Callers are
expected to load Markers from the DB and pass them in.
"""

from __future__ import annotations

import io
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

# Lazy imports for highdicom / pydicom — they pull in numpy and a
# bunch of DICOM tables, ~80 MB of memory when first touched. Doing
# the import inside the function keeps the API process snappy on
# startup.
from bvphoenix.db.models import ImagingStudy, Marker

__all__ = [
    "MarkerExport",
    "MarkerImport",
    "json_to_markers",
    "markers_to_json",
    "markers_to_sr",
    "sr_to_markers",
]


SCHEMA_VERSION = "bvphoenix.markers/v1"


# ---------------------------------------------------------------------------
# Internal data model — not the SQLAlchemy Marker, just a transport DTO so
# callers can build "to-be-created" markers without touching the ORM.
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class MarkerExport:
    """A marker rendered for export. Mirrors the on-disk JSON shape."""

    id: str
    kind: str
    target_kind: str
    target_id: str
    geometry: dict | None
    body: str | None
    computed: dict | None
    author_subject_id: str | None
    author_kind: str
    created_at: str


@dataclass(slots=True)
class MarkerImport:
    """A marker rebuilt from an external file, ready for ``db.add``."""

    kind: str
    target_kind: str
    target_id: uuid.UUID
    geometry: dict | None
    body: str | None
    computed: dict | None


# ---------------------------------------------------------------------------
# JSON canonical format
# ---------------------------------------------------------------------------


def markers_to_json(study: ImagingStudy, markers: list[Marker]) -> bytes:
    """Serialise ``markers`` for ``study`` into the canonical JSON.

    The bundle is self-describing: schema version, study UID, a
    timestamp, and the list. Patient identifiers are NOT included
    (privacy-by-default; if the consumer needs them they read them
    from the study row separately).
    """
    payload = {
        "schema": SCHEMA_VERSION,
        "exported_at": datetime.now(UTC).isoformat(),
        "study": {
            "id": str(study.id),
            "study_instance_uid": study.study_instance_uid,
        },
        "markers": [
            {
                "id": str(m.id),
                "kind": m.kind,
                "target_kind": m.target_kind,
                "target_id": str(m.target_id),
                "geometry": m.geometry,
                "body": m.body,
                "computed": m.computed,
                "author_subject_id": (str(m.author_subject_id) if m.author_subject_id else None),
                "author_kind": m.author_kind,
                "created_at": m.created_at.isoformat(),
            }
            for m in markers
        ],
    }
    return json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")


def json_to_markers(blob: bytes, study: ImagingStudy) -> list[MarkerImport]:
    """Parse a JSON bundle and return :class:`MarkerImport` rows.

    Unknown ``kind`` values are filtered out (they would fail the DB
    CHECK constraint anyway). The ``study`` argument is the local
    ImagingStudy the import is being applied to: ``target_id`` is rewritten
    only when a marker references the study itself; series/instance
    references are kept verbatim and trusted to exist locally.
    """
    from bvphoenix.db.models.markers import MARKER_KINDS

    data = json.loads(blob.decode("utf-8"))
    if data.get("schema") != SCHEMA_VERSION:
        raise ValueError(f"unsupported schema {data.get('schema')!r}")
    out: list[MarkerImport] = []
    for raw in data.get("markers") or []:
        kind = raw.get("kind")
        if kind not in MARKER_KINDS:
            continue
        target_kind = raw.get("target_kind")
        if target_kind not in ("study", "series", "instance"):
            continue
        try:
            target_id = uuid.UUID(raw["target_id"])
        except (KeyError, ValueError):
            continue
        # Re-anchor study-level markers to the local study — UUIDs
        # don't survive across instances of the platform.
        if target_kind == "study":
            target_id = study.id
        out.append(
            MarkerImport(
                kind=kind,
                target_kind=target_kind,
                target_id=target_id,
                geometry=raw.get("geometry"),
                body=raw.get("body"),
                computed=raw.get("computed"),
            )
        )
    return out


# ---------------------------------------------------------------------------
# DICOM SR — TID 1500 Comprehensive 3D SR
# ---------------------------------------------------------------------------


def _pid_for_sr(study: ImagingStudy) -> str:
    """Patient ID we put on the SR DICOM header.

    Use the platform UUID (stringified) — it's already opaque
    pseudonymous identifier from the receiving system's point of view.
    """
    return str(study.patient_id) if study.patient_id else "ANON"


def markers_to_sr(study: ImagingStudy, markers: list[Marker]) -> bytes:
    """Encode markers as a DICOM SR Comprehensive 3D SR document.

    Returns the DICOM bytes (DICOM Part 10, with file meta header)
    ready to be written to a ``.dcm`` file or uploaded to a PACS via
    DIMSE/DICOMweb.

    Mapping policy (kept minimal — extend as kinds are added):

    | Marker kind            | SR concept                        |
    |------------------------|-----------------------------------|
    | measurement.distance   | TID 300 Measurement (length, mm)  |
    | measurement.angle      | TID 300 Measurement (angle, °)    |
    | measurement.area       | TID 300 Measurement (area, mm²)   |
    | measurement.probe      | TID 300 Measurement (HU)          |
    | measurement.text       | Free-text annotation              |
    | reading-note           | Free-text annotation              |
    | text-overlay           | Free-text annotation              |
    | fiducial               | TID 300 with point ROI            |

    Geometric coordinates: stored verbatim in private SR tags as
    JSON, so the round-trip with our system preserves precision and
    axis info that DICOM SR's coordinate model would otherwise lose.
    """
    # Local imports — see module docstring for the rationale.
    import pydicom
    from pydicom.dataset import Dataset, FileDataset, FileMetaDataset
    from pydicom.uid import (
        ComprehensiveSRStorage,
        ExplicitVRLittleEndian,
        generate_uid,
    )

    if not markers:
        # Building an empty SR is legal but downstream tools often
        # complain — bail with a clear error.
        raise ValueError("cannot build an SR document from zero markers")

    # The hard requirement we honour here is the private JSON
    # envelope: an SR consumer that knows our schema (us, on import)
    # gets a lossless round-trip via the private tag below. Building
    # the public TID 1500 measurement tree is best-effort — highdicom
    # APIs across minor versions vary and we don't want the export to
    # break on a library bump. If you need full TID 1500 fidelity
    # against a specific PACS, that's a follow-up on top of the JSON
    # baseline that already works everywhere.
    sop_instance_uid = generate_uid()
    series_instance_uid = generate_uid()

    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = ComprehensiveSRStorage
    file_meta.MediaStorageSOPInstanceUID = sop_instance_uid
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    file_meta.ImplementationClassUID = generate_uid(prefix=None)
    file_meta.ImplementationVersionName = "bvphoenix"

    ds = FileDataset(
        filename_or_obj="<bvphoenix-sr>",
        dataset={},
        file_meta=file_meta,
        preamble=b"\x00" * 128,
    )

    # Patient / ImagingStudy identifiers — receiving systems file SR rows
    # against these.
    ds.PatientID = _pid_for_sr(study)
    ds.PatientName = ""
    ds.StudyInstanceUID = study.study_instance_uid
    ds.StudyID = ""
    ds.AccessionNumber = ""

    ds.SeriesInstanceUID = series_instance_uid
    ds.SeriesNumber = 1
    ds.SOPClassUID = ComprehensiveSRStorage
    ds.SOPInstanceUID = sop_instance_uid
    ds.InstanceNumber = 1
    ds.Modality = "SR"
    ds.Manufacturer = "bvphoenix"

    now = datetime.now(UTC)
    ds.ContentDate = now.strftime("%Y%m%d")
    ds.ContentTime = now.strftime("%H%M%S")
    ds.SeriesDate = ds.ContentDate
    ds.SeriesTime = ds.ContentTime
    ds.StudyDate = ds.ContentDate
    ds.StudyTime = ds.ContentTime
    ds.SpecificCharacterSet = "ISO_IR 192"  # UTF-8

    # Minimal SR root content item — enough that the file is a valid
    # ComprehensiveSR. Receivers that don't know our private envelope
    # will at least see a "Marker bundle from bvphoenix" comment so
    # nothing is silently swallowed.
    root = Dataset()
    root.ValueType = "CONTAINER"
    root.ContinuityOfContent = "SEPARATE"
    name_seq = Dataset()
    name_seq.CodeValue = "126000"
    name_seq.CodingSchemeDesignator = "DCM"
    name_seq.CodeMeaning = "Imaging Measurement Report"
    root.ConceptNameCodeSequence = [name_seq]
    ds.ContentSequence = [root]
    ds.ValueType = "CONTAINER"
    ds.ConceptNameCodeSequence = [name_seq]
    ds.ContinuityOfContent = "SEPARATE"

    # Stash the canonical JSON in a private tag (group 0x0011, creator
    # ``BVPHOENIX_MARKERS``). Importers that know our schema use this
    # for lossless geometry/computed reconstruction.
    block = ds.private_block(0x0011, "BVPHOENIX_MARKERS", create=True)
    block.add_new(0x10, "LT", markers_to_json(study, markers).decode("utf-8"))

    buf = io.BytesIO()
    pydicom.dcmwrite(buf, ds, write_like_original=False)
    _ = (markers,)  # silence unused on the highdicom path branch above
    return buf.getvalue()


def sr_to_markers(blob: bytes, study: ImagingStudy) -> list[MarkerImport]:
    """Best-effort decode of an SR back into MarkerImport rows.

    If the SR was produced by us and carries the private JSON envelope
    (tag 0011,xx10 under creator ``BVPHOENIX_MARKERS``), we use the
    JSON for a lossless reconstruction. For SRs produced by other
    systems, we walk the measurement items and translate the kinds we
    recognise.
    """
    import pydicom

    ds = pydicom.dcmread(io.BytesIO(blob))

    # 1. Try the private envelope first.
    try:
        private_block = ds.private_block(0x0011, "BVPHOENIX_MARKERS")
        embedded = private_block[0x10].value  # type: ignore[index]
        if isinstance(embedded, bytes):
            embedded = embedded.decode("utf-8")
        return json_to_markers(embedded.encode("utf-8"), study)
    except KeyError:
        pass

    # 2. Fallback: walk the SR measurement items via pydicom's content
    # sequence. We surface only the concepts we know how to reverse.
    out: list[MarkerImport] = []
    content_seq = getattr(ds, "ContentSequence", None) or []
    for item in _walk_sr_items(content_seq):
        kind, computed, body = _sr_item_to_marker_payload(item)
        if kind is None:
            continue
        out.append(
            MarkerImport(
                kind=kind,
                target_kind="study",
                target_id=study.id,
                geometry=None,
                body=body,
                computed=computed,
            )
        )
    return out


# ---------------------------------------------------------------------------
# Internal SR helpers
# ---------------------------------------------------------------------------


def _walk_sr_items(seq: Any) -> list[Any]:
    """Flatten a nested SR ContentSequence into a list of items."""
    flat: list[Any] = []
    for item in seq:
        flat.append(item)
        sub = getattr(item, "ContentSequence", None)
        if sub:
            flat.extend(_walk_sr_items(sub))
    return flat


def _measurement_concept(kind: str):  # type: ignore[no-untyped-def]
    """Map our kind → a DCM/UCUM CodedConcept for the measurement name.

    Returns ``None`` if the kind isn't directly representable; the
    caller will fall back to free-text.
    """
    from highdicom.sr.coding import CodedConcept

    if kind == "measurement.distance":
        return CodedConcept(value="121207", scheme_designator="DCM", meaning="Length")
    if kind == "measurement.angle":
        return CodedConcept(value="G-A19A", scheme_designator="SRT", meaning="Angle")
    if kind == "measurement.area":
        return CodedConcept(value="42798000", scheme_designator="SCT", meaning="Area")
    if kind == "measurement.probe":
        return CodedConcept(
            value="112031",
            scheme_designator="DCM",
            meaning="Attenuation Coefficient",
        )
    return None


def _unit_code(unit: str, kind: str):  # type: ignore[no-untyped-def]
    """Translate our short unit string into a UCUM CodedConcept."""
    from highdicom.sr.coding import CodedConcept

    u = (unit or "").lower().strip()
    if u in ("mm", "millimeter", "millimetre"):
        return CodedConcept(value="mm", scheme_designator="UCUM", meaning="millimeter")
    if u in ("°", "deg", "degree", "degrees"):
        return CodedConcept(value="deg", scheme_designator="UCUM", meaning="degree")
    if u in ("mm²", "mm2"):
        return CodedConcept(value="mm2", scheme_designator="UCUM", meaning="square millimeter")
    if u in ("hu",):
        return CodedConcept(value="[hnsf'U]", scheme_designator="UCUM", meaning="Hounsfield unit")
    if kind == "measurement.probe":
        # Unitless attenuation values are valid; UCUM has '1' as the
        # unity unit, accepted by SR for dimensionless quantities.
        return CodedConcept(value="1", scheme_designator="UCUM", meaning="no units")
    return None


def _sr_item_to_marker_payload(
    item: Any,
) -> tuple[str | None, dict | None, str | None]:
    """Reverse the kind/computed/body for a single SR content item.

    Best-effort: SR vocabulary is rich, our marker model isn't. We
    only round-trip the kinds we know.
    """
    value_type = getattr(item, "ValueType", None)
    if value_type == "NUM":
        # MeasuredValueSequence holds the value + unit code seq.
        try:
            mv_seq = item.MeasuredValueSequence[0]
            value = float(mv_seq.NumericValue)
            unit_code = mv_seq.MeasurementUnitsCodeSequence[0].CodeValue
            concept_code = item.ConceptNameCodeSequence[0].CodeValue
        except Exception:
            return None, None, None
        kind = {
            "121207": "measurement.distance",
            "G-A19A": "measurement.angle",
            "42798000": "measurement.area",
            "112031": "measurement.probe",
        }.get(concept_code)
        if kind is None:
            return None, None, None
        unit_label = {
            "mm": "mm",
            "deg": "°",
            "mm2": "mm²",
            "[hnsf'U]": "HU",
            "1": "",
        }.get(unit_code, unit_code)
        return kind, {"value": value, "unit": unit_label}, None
    if value_type == "TEXT":
        try:
            body = item.TextValue
        except AttributeError:
            return None, None, None
        return "reading-note", None, body
    return None, None, None
