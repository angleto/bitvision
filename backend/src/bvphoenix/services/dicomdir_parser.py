"""DICOMDIR parser — turn a CD's index file into a PATIENT→STUDY→SERIES→IMAGE tree.

A DICOMDIR is a plain DICOM Media Storage Directory dataset whose
``DirectoryRecordSequence`` holds flat directory records linked by byte
offsets:

* ``OffsetOfTheFirstDirectoryRecordOfTheRootDirectoryEntity`` — offset of
  the first top-level (PATIENT) record.
* ``OffsetOfTheNextDirectoryRecord`` — sibling pointer.
* ``OffsetOfReferencedLowerLevelDirectoryEntity`` — child pointer.

We walk those offsets to rebuild the logical hierarchy and return a
:class:`DicomdirTree` that upper layers can use to ingest the referenced
files from the CD's folder tree.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from datetime import date, datetime

import pydicom
from pydicom.errors import InvalidDicomError


class DicomdirParseError(Exception):
    """Raised when the DICOMDIR bytes can't be interpreted as a directory."""


@dataclass
class DicomdirImage:
    relative_path: str  # e.g. "DICOM/IMG0001/IM000001.dcm"
    sop_instance_uid: str | None = None
    sop_class_uid: str | None = None
    instance_number: int | None = None


@dataclass
class DicomdirSeries:
    series_instance_uid: str
    modality: str | None = None
    series_number: int | None = None
    series_description: str | None = None
    images: list[DicomdirImage] = field(default_factory=list)


@dataclass
class DicomdirStudy:
    study_instance_uid: str
    study_description: str | None = None
    study_date: date | None = None
    series: list[DicomdirSeries] = field(default_factory=list)


@dataclass
class DicomdirPatient:
    patient_id: str | None = None
    patient_name: str | None = None
    studies: list[DicomdirStudy] = field(default_factory=list)


@dataclass
class DicomdirTree:
    patients: list[DicomdirPatient] = field(default_factory=list)
    source_file_size: int = 0


def _as_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_dicom_date(value: str | None) -> date | None:
    if not value or len(value) != 8:
        return None
    try:
        return datetime.strptime(value, "%Y%m%d").date()
    except ValueError:
        return None


def _normalize_relative_path(file_id: object) -> str:
    """ReferencedFileID is a multi-value string (path components).

    Old CDs sometimes ship backslash-separated strings; normalize both
    shapes to forward-slash-joined POSIX-style paths so callers can use
    them with ``zipfile`` / ``os.path`` without platform gotchas.
    """
    if file_id is None:
        return ""
    if isinstance(file_id, str):
        parts = file_id.replace("\\", "/").split("/")
    else:
        # pydicom returns a MultiValue for VR=CS repeating. Coerce each.
        try:
            parts = [str(p) for p in file_id]
        except TypeError:
            parts = [str(file_id)]
    cleaned = [p.strip() for p in parts if p and str(p).strip()]
    return "/".join(cleaned)


def _record_type(record: pydicom.Dataset) -> str:
    raw = getattr(record, "DirectoryRecordType", "") or ""
    return str(raw).strip().upper()


def _patient_from(record: pydicom.Dataset) -> DicomdirPatient:
    raw_name = getattr(record, "PatientName", None)
    name = str(raw_name) if raw_name is not None else None
    pid = getattr(record, "PatientID", None)
    return DicomdirPatient(
        patient_id=str(pid) if pid else None,
        patient_name=name or None,
    )


def _study_from(record: pydicom.Dataset) -> DicomdirStudy:
    uid = getattr(record, "StudyInstanceUID", None)
    if not uid:
        raise DicomdirParseError("STUDY record missing StudyInstanceUID")
    return DicomdirStudy(
        study_instance_uid=str(uid),
        study_description=getattr(record, "StudyDescription", None) or None,
        study_date=_parse_dicom_date(getattr(record, "StudyDate", None)),
    )


def _series_from(record: pydicom.Dataset) -> DicomdirSeries:
    uid = getattr(record, "SeriesInstanceUID", None)
    if not uid:
        raise DicomdirParseError("SERIES record missing SeriesInstanceUID")
    return DicomdirSeries(
        series_instance_uid=str(uid),
        modality=getattr(record, "Modality", None) or None,
        series_number=_as_int(getattr(record, "SeriesNumber", None)),
        series_description=getattr(record, "SeriesDescription", None) or None,
    )


def _image_from(record: pydicom.Dataset) -> DicomdirImage:
    rel = _normalize_relative_path(getattr(record, "ReferencedFileID", None))
    sop_uid = getattr(record, "ReferencedSOPInstanceUIDInFile", None)
    sop_class = getattr(record, "ReferencedSOPClassUIDInFile", None)
    return DicomdirImage(
        relative_path=rel,
        sop_instance_uid=str(sop_uid) if sop_uid else None,
        sop_class_uid=str(sop_class) if sop_class else None,
        instance_number=_as_int(getattr(record, "InstanceNumber", None)),
    )


def _build_offset_map(records: list[pydicom.Dataset]) -> dict[int, pydicom.Dataset]:
    """Map sequence-item byte offsets to their records.

    pydicom stores the original file offset of each ``DirectoryRecordSequence``
    item on the Dataset as ``seq_item_tell`` (in 3.x) or ``file_tell`` (older
    builds). We try both so this works across the versions we support.
    """
    mapping: dict[int, pydicom.Dataset] = {}
    for rec in records:
        offset = getattr(rec, "seq_item_tell", None)
        if offset is None:
            offset = getattr(rec, "file_tell", None)
        if offset is None:
            continue
        mapping[int(offset)] = rec
    return mapping


def _walk_offsets(
    first_offset: int,
    offset_map: dict[int, pydicom.Dataset],
) -> list[pydicom.Dataset]:
    """Follow ``OffsetOfTheNextDirectoryRecord`` siblings starting at the
    given offset, returning them in encounter order. Returns ``[]`` for 0
    / missing offsets. Cycles are guarded with a visited set so malformed
    CDs can't make us loop forever.
    """
    out: list[pydicom.Dataset] = []
    seen: set[int] = set()
    cur = int(first_offset or 0)
    while cur and cur not in seen:
        seen.add(cur)
        rec = offset_map.get(cur)
        if rec is None:
            break
        out.append(rec)
        nxt = getattr(rec, "OffsetOfTheNextDirectoryRecord", 0) or 0
        cur = int(nxt)
    return out


def _children_of(
    record: pydicom.Dataset,
    offset_map: dict[int, pydicom.Dataset],
) -> list[pydicom.Dataset]:
    first = getattr(record, "OffsetOfReferencedLowerLevelDirectoryEntity", 0) or 0
    return _walk_offsets(int(first), offset_map)


def _walk_via_offsets(ds: pydicom.Dataset) -> list[DicomdirPatient]:
    """Preferred walk using the offset pointers in the DICOMDIR."""
    records: list[pydicom.Dataset] = list(getattr(ds, "DirectoryRecordSequence", []) or [])
    if not records:
        return []

    offset_map = _build_offset_map(records)
    first_offset = int(
        getattr(ds, "OffsetOfTheFirstDirectoryRecordOfTheRootDirectoryEntity", 0) or 0
    )
    if not first_offset or not offset_map:
        return []

    patients: list[DicomdirPatient] = []
    for patient_rec in _walk_offsets(first_offset, offset_map):
        if _record_type(patient_rec) != "PATIENT":
            # Some CDs skip the PATIENT level; handled by the fallback.
            return []
        patient = _patient_from(patient_rec)
        for study_rec in _children_of(patient_rec, offset_map):
            if _record_type(study_rec) != "STUDY":
                continue
            study = _study_from(study_rec)
            for series_rec in _children_of(study_rec, offset_map):
                if _record_type(series_rec) != "SERIES":
                    continue
                series = _series_from(series_rec)
                for img_rec in _children_of(series_rec, offset_map):
                    if _record_type(img_rec) != "IMAGE":
                        continue
                    series.images.append(_image_from(img_rec))
                study.series.append(series)
            patient.studies.append(study)
        patients.append(patient)
    return patients


def _walk_sequential(ds: pydicom.Dataset) -> list[DicomdirPatient]:
    """Fallback walk for CDs where offset pointers are unusable.

    Assumes records are emitted in tree-preorder (PATIENT, then its
    STUDYs, each followed by SERIES, each followed by IMAGEs). This is
    true for the vast majority of writer implementations.
    """
    patients: list[DicomdirPatient] = []
    cur_patient: DicomdirPatient | None = None
    cur_study: DicomdirStudy | None = None
    cur_series: DicomdirSeries | None = None

    for rec in getattr(ds, "DirectoryRecordSequence", []) or []:
        rtype = _record_type(rec)
        if rtype == "PATIENT":
            cur_patient = _patient_from(rec)
            patients.append(cur_patient)
            cur_study = None
            cur_series = None
        elif rtype == "STUDY":
            if cur_patient is None:
                # CD with no explicit PATIENT record — synthesize one so
                # nothing is dropped.
                cur_patient = DicomdirPatient()
                patients.append(cur_patient)
            cur_study = _study_from(rec)
            cur_patient.studies.append(cur_study)
            cur_series = None
        elif rtype == "SERIES":
            if cur_study is None:
                raise DicomdirParseError("SERIES record without a parent STUDY")
            cur_series = _series_from(rec)
            cur_study.series.append(cur_series)
        elif rtype == "IMAGE":
            if cur_series is None:
                raise DicomdirParseError("IMAGE record without a parent SERIES")
            cur_series.images.append(_image_from(rec))
        # Other record types (PRIVATE, RT PLAN, HANGING PROTOCOL, ...) are
        # silently ignored — callers only care about the imaging tree.

    return patients


async def parse_dicomdir(dicomdir_bytes: bytes) -> DicomdirTree:
    """Parse a DICOMDIR file's binary content into a navigable tree.

    ``dicomdir_bytes`` is the raw contents of the ``DICOMDIR`` file found
    at the root of a typical imaging CD. Raises :class:`DicomdirParseError`
    with a readable message if the file is malformed.
    """
    if not dicomdir_bytes:
        raise DicomdirParseError("DICOMDIR is empty")

    try:
        ds = pydicom.dcmread(io.BytesIO(dicomdir_bytes), force=True)
    except (InvalidDicomError, OSError, EOFError) as exc:
        raise DicomdirParseError(f"not a readable DICOM file: {exc}") from exc
    except Exception as exc:  # pydicom occasionally raises ValueError on junk
        raise DicomdirParseError(f"failed to parse DICOMDIR: {exc}") from exc

    if "DirectoryRecordSequence" not in ds:
        raise DicomdirParseError("not a DICOMDIR (missing DirectoryRecordSequence)")

    # Prefer offset-based traversal (semantically correct) and fall back
    # to in-order scanning only if that produced nothing usable.
    patients = _walk_via_offsets(ds)
    if not patients:
        patients = _walk_sequential(ds)

    return DicomdirTree(patients=patients, source_file_size=len(dicomdir_bytes))
