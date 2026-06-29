"""DICOMweb read-surface helpers (PS3.18): QIDO-RS DICOM-JSON serialization,
WADO-RS multipart/related framing, and WADO-RS metadata extraction.

Pure logic, no FastAPI. The API layer (``api/dicomweb.py``) does auth +
patient scoping (``visible_studies_filter``) + storage I/O and calls these
to shape responses. Storage isolation is the API layer's job; nothing here
emits a bucket name or presigned URL.

DICOM JSON model (PS3.18 F.2): an object is a map of 8-hex-digit tag ->
``{"vr": VR}`` (empty) or ``{"vr": VR, "Value": [...]}``; PN values are
``[{"Alphabetic": "..."}]``; IS values are JSON numbers.
"""

from __future__ import annotations

import io
import uuid
from collections.abc import Callable, Iterator
from datetime import date
from typing import Any

import pydicom

# Keyword -> 8-hex-digit tag for every attribute we serialize or match on.
TAG: dict[str, str] = {
    "SpecificCharacterSet": "00080005",
    "SOPClassUID": "00080016",
    "SOPInstanceUID": "00080018",
    "StudyDate": "00080020",
    "StudyTime": "00080030",
    "AccessionNumber": "00080050",
    "InstanceAvailability": "00080056",
    "Modality": "00080060",
    "ModalitiesInStudy": "00080061",
    "StudyDescription": "00081030",
    "SeriesDescription": "0008103E",
    "RetrieveURL": "00081190",
    "PatientName": "00100010",
    "PatientID": "00100020",
    "BodyPartExamined": "00180015",
    "StudyInstanceUID": "0020000D",
    "SeriesInstanceUID": "0020000E",
    "StudyID": "00200010",
    "SeriesNumber": "00200011",
    "InstanceNumber": "00200013",
    "NumberOfStudyRelatedSeries": "00201206",
    "NumberOfStudyRelatedInstances": "00201208",
    "NumberOfSeriesRelatedInstances": "00201209",
}

DICOM_JSON_MEDIA_TYPE = "application/dicom+json"
DICOM_MEDIA_TYPE = "application/dicom"


# ---- DICOM JSON attribute builders ----------------------------------------


def _str(vr: str, value: str | None) -> dict[str, Any]:
    """A single-valued string attribute; empty when value is falsy."""
    if value is None or value == "":
        return {"vr": vr}
    return {"vr": vr, "Value": [str(value)]}


def _int(vr: str, value: int | None) -> dict[str, Any]:
    if value is None:
        return {"vr": vr}
    return {"vr": vr, "Value": [int(value)]}


def _multi(vr: str, values: list[str] | None) -> dict[str, Any]:
    vals = [v for v in (values or []) if v]
    if not vals:
        return {"vr": vr}
    return {"vr": vr, "Value": vals}


def _pn(name: str | None) -> dict[str, Any]:
    if not name:
        return {"vr": "PN"}
    return {"vr": "PN", "Value": [{"Alphabetic": str(name)}]}


def _fmt_date(d: date | None) -> str | None:
    return d.strftime("%Y%m%d") if d is not None else None


# ---- QIDO-RS serializers (DB row -> DICOM JSON) ---------------------------
#
# PatientID carries the platform's opaque patient UUID (already public in the
# REST API; not PHI) so DICOMweb clients group studies by patient correctly.
# PatientName is left empty — no name leaks through the read surface.


def study_to_json(
    *,
    study_instance_uid: str,
    study_date: date | None,
    study_description: str | None,
    modalities: list[str] | None,
    patient_id: uuid.UUID | None,
    num_series: int,
    num_instances: int,
    retrieve_url: str,
) -> dict[str, Any]:
    return {
        TAG["SpecificCharacterSet"]: _str("CS", "ISO_IR 192"),
        TAG["StudyDate"]: _str("DA", _fmt_date(study_date)),
        TAG["AccessionNumber"]: _str("SH", None),
        TAG["InstanceAvailability"]: _str("CS", "ONLINE"),
        TAG["ModalitiesInStudy"]: _multi("CS", modalities),
        TAG["StudyDescription"]: _str("LO", study_description),
        TAG["RetrieveURL"]: _str("UR", retrieve_url),
        TAG["PatientName"]: _pn(None),
        TAG["PatientID"]: _str("LO", str(patient_id) if patient_id else None),
        TAG["StudyInstanceUID"]: _str("UI", study_instance_uid),
        TAG["StudyID"]: _str("SH", None),
        TAG["NumberOfStudyRelatedSeries"]: _int("IS", num_series),
        TAG["NumberOfStudyRelatedInstances"]: _int("IS", num_instances),
    }


def series_to_json(
    *,
    study_instance_uid: str,
    series_instance_uid: str,
    modality: str | None,
    series_number: int | None,
    series_description: str | None,
    body_part: str | None,
    num_instances: int,
    retrieve_url: str,
) -> dict[str, Any]:
    return {
        TAG["SpecificCharacterSet"]: _str("CS", "ISO_IR 192"),
        TAG["Modality"]: _str("CS", modality),
        TAG["InstanceAvailability"]: _str("CS", "ONLINE"),
        TAG["RetrieveURL"]: _str("UR", retrieve_url),
        TAG["BodyPartExamined"]: _str("CS", body_part),
        TAG["SeriesDescription"]: _str("LO", series_description),
        TAG["StudyInstanceUID"]: _str("UI", study_instance_uid),
        TAG["SeriesInstanceUID"]: _str("UI", series_instance_uid),
        TAG["SeriesNumber"]: _int("IS", series_number),
        TAG["NumberOfSeriesRelatedInstances"]: _int("IS", num_instances),
    }


def instance_to_json(
    *,
    study_instance_uid: str,
    series_instance_uid: str,
    sop_instance_uid: str,
    sop_class_uid: str | None,
    instance_number: int | None,
    retrieve_url: str,
) -> dict[str, Any]:
    return {
        TAG["SpecificCharacterSet"]: _str("CS", "ISO_IR 192"),
        TAG["SOPClassUID"]: _str("UI", sop_class_uid),
        TAG["SOPInstanceUID"]: _str("UI", sop_instance_uid),
        TAG["InstanceAvailability"]: _str("CS", "ONLINE"),
        TAG["RetrieveURL"]: _str("UR", retrieve_url),
        TAG["StudyInstanceUID"]: _str("UI", study_instance_uid),
        TAG["SeriesInstanceUID"]: _str("UI", series_instance_uid),
        TAG["InstanceNumber"]: _int("IS", instance_number),
    }


# ---- WADO-RS metadata (stored bytes -> DICOM JSON, no pixel data) ----------


def header_to_json(dcm_bytes: bytes) -> dict[str, Any]:
    """Parse a stored DICOM Part-10 blob and return its DICOM-JSON metadata,
    pixel data excluded (``stop_before_pixels``). The caller scrubs the bytes
    first when de-identification is required, so this reflects whatever the
    served bytes contain.

    No ``BulkDataURI`` is emitted: bulk retrieve (frames / bulkdata) is not
    part of this surface yet, so a dangling URI is never produced. Clients
    that need pixels use WADO-RS instance retrieve.
    """
    ds = pydicom.dcmread(io.BytesIO(dcm_bytes), stop_before_pixels=True, force=True)
    # bulk_data_threshold huge => any residual binary inlines as InlineBinary
    # rather than producing a BulkDataURI we can't yet serve.
    return ds.to_json_dict(bulk_data_threshold=10**12, suppress_invalid_tags=True)


# ---- WADO-RS multipart/related framing -------------------------------------

_CRLF = b"\r\n"


def new_boundary() -> str:
    """A fresh multipart boundary token (unguessable, header-safe)."""
    return uuid.uuid4().hex


def multipart_content_type(boundary: str, *, part_type: str = DICOM_MEDIA_TYPE) -> str:
    return f'multipart/related; type="{part_type}"; boundary={boundary}'


# A part is (Content-Location, a no-arg callable yielding the body chunks).
# The callable is invoked lazily inside the generator so only one instance is
# in flight at a time — a multi-GB study streams in bounded memory.
MultipartPart = tuple[str, Callable[[], Iterator[bytes]]]


def iter_multipart(
    parts: list[MultipartPart],
    boundary: str,
    *,
    part_type: str = DICOM_MEDIA_TYPE,
) -> Iterator[bytes]:
    """Yield a ``multipart/related`` body. Sync generator: hand it to
    ``StreamingResponse`` so Starlette runs the blocking storage reads in its
    threadpool, off the event loop."""
    bnd = boundary.encode("ascii")
    ctype = f"Content-Type: {part_type}\r\n".encode("ascii")
    for content_location, body in parts:
        yield _CRLF + b"--" + bnd + _CRLF
        yield ctype
        yield b"Content-Location: " + content_location.encode("ascii") + _CRLF
        yield _CRLF
        yield from body()
    yield _CRLF + b"--" + bnd + b"--" + _CRLF
