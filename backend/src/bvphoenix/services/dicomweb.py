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
from pydicom import encaps

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
    "Rows": "00280010",
    "NumberOfFrames": "00280008",
    "BitsAllocated": "00280100",
    "PixelData": "7FE00010",
}

DICOM_JSON_MEDIA_TYPE = "application/dicom+json"
DICOM_MEDIA_TYPE = "application/dicom"
OCTET_STREAM_MEDIA_TYPE = "application/octet-stream"

# WADO-RS frame media type per transfer syntax (PS3.18 6.5.3). Encapsulated
# syntaxes carry their codec's media type; everything uncompressed (implicit /
# explicit LE, explicit BE, deflated) streams the raw octets. v1 serves the
# stored transfer syntax verbatim — no transcoding — so the part media type is
# a pure function of the file's TransferSyntaxUID.
_FRAME_MEDIA_BY_TS: dict[str, str] = {
    "1.2.840.10008.1.2.4.50": "image/jpeg",  # JPEG Baseline
    "1.2.840.10008.1.2.4.51": "image/jpeg",  # JPEG Extended
    "1.2.840.10008.1.2.4.57": "image/jpeg",  # JPEG Lossless
    "1.2.840.10008.1.2.4.70": "image/jpeg",  # JPEG Lossless SV1
    "1.2.840.10008.1.2.4.80": "image/jls",  # JPEG-LS Lossless
    "1.2.840.10008.1.2.4.81": "image/jls",  # JPEG-LS Near-Lossless
    "1.2.840.10008.1.2.4.90": "image/jp2",  # JPEG 2000 Lossless
    "1.2.840.10008.1.2.4.91": "image/jp2",  # JPEG 2000
    "1.2.840.10008.1.2.4.92": "image/jpx",  # JPEG 2000 Part 2 multi-component
    "1.2.840.10008.1.2.4.93": "image/jpx",
    "1.2.840.10008.1.2.5": "image/dicom-rle",  # RLE Lossless
}


def frame_media_type(transfer_syntax_uid: str) -> str:
    """Media type for a WADO-RS frame part, keyed on the stored transfer
    syntax. Unknown / uncompressed → ``application/octet-stream``."""
    return _FRAME_MEDIA_BY_TS.get(transfer_syntax_uid, OCTET_STREAM_MEDIA_TYPE)


class FrameError(ValueError):
    """Frame request the instance cannot satisfy (out of range, unsupported
    pixel layout, or no pixel data). The API layer maps it to 404/400."""


def extract_frames(dcm_bytes: bytes, frame_numbers: list[int]) -> tuple[str, list[bytes]]:
    """Return ``(transfer_syntax_uid, [encoded frame bytes])`` for the given
    1-based ``frame_numbers``, WITHOUT decoding/transcoding pixels.

    * Encapsulated syntaxes: the codec bitstream of each frame is read back via
      :func:`pydicom.encaps.get_frame` (the JPEG / JLS / JP2 / RLE bytes
      exactly as stored).
    * Uncompressed syntaxes: ``PixelData`` is a contiguous buffer, so frame *n*
      is the stride slice ``rows * cols * samples * (bits_allocated // 8)``.

    The caller has already scrubbed the bytes when de-identification is
    required (PS3.15), so frames reflect whatever the served instance contains
    — including any burned-in-pixel redaction the de-id engine applied.
    """
    ds = pydicom.dcmread(io.BytesIO(dcm_bytes), force=True)
    ts = ds.file_meta.TransferSyntaxUID
    n_frames = int(getattr(ds, "NumberOfFrames", 1) or 1)
    for fn in frame_numbers:
        if fn < 1 or fn > n_frames:
            raise FrameError(f"frame {fn} out of range (1..{n_frames})")
    if "PixelData" not in ds:
        raise FrameError("instance has no pixel data")
    pixel_data = ds.PixelData

    if ts.is_encapsulated:
        frames = [
            encaps.get_frame(pixel_data, fn - 1, number_of_frames=n_frames) for fn in frame_numbers
        ]
        return str(ts), frames

    bits_allocated = int(getattr(ds, "BitsAllocated", 0) or 0)
    if bits_allocated < 8 or bits_allocated % 8 != 0:
        # Bit-packed (<8-bit) native pixel data is not byte-aligned per frame;
        # the full-instance WADO retrieve still serves it correctly.
        raise FrameError(f"native frame extraction unsupported for BitsAllocated={bits_allocated}")
    frame_size = (
        int(ds.Rows)
        * int(ds.Columns)
        * int(getattr(ds, "SamplesPerPixel", 1))
        * (bits_allocated // 8)
    )
    frames = [pixel_data[(fn - 1) * frame_size : fn * frame_size] for fn in frame_numbers]
    return str(ts), frames


def extract_bulkdata(dcm_bytes: bytes, tag: int) -> bytes | None:
    """Raw value bytes of a top-level binary element (for WADO-RS bulkdata).
    ``None`` when the tag is absent or its value is not a byte payload (so the
    API maps it to a 404). Nested-sequence bulkdata is a tracked follow-up."""
    from pydicom.tag import Tag

    ds = pydicom.dcmread(io.BytesIO(dcm_bytes), force=True)
    t = Tag(tag)
    if t not in ds:
        return None
    value = ds[t].value
    if isinstance(value, bytes | bytearray):
        return bytes(value)
    return None


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


def header_to_json(dcm_bytes: bytes, *, instance_url: str | None = None) -> dict[str, Any]:
    """Parse a stored DICOM Part-10 blob and return its DICOM-JSON metadata,
    pixel data excluded (``stop_before_pixels``). The caller scrubs the bytes
    first when de-identification is required, so this reflects whatever the
    served bytes contain.

    When ``instance_url`` (the instance's WADO-RS retrieve URL) is given, bulk
    elements are referenced by ``BulkDataURI`` instead of inlined: ``PixelData``
    points at the frames resource (``{instance_url}/frames/1``) and every other
    bulk binary element at ``{instance_url}/bulkdata/{tag}``. Without it (pure
    serialization, e.g. unit tests) no URI is emitted so nothing dangles.
    """
    ds = pydicom.dcmread(io.BytesIO(dcm_bytes), stop_before_pixels=True, force=True)
    if instance_url is None:
        # bulk_data_threshold huge => any residual binary inlines as
        # InlineBinary rather than a BulkDataURI we couldn't serve.
        return ds.to_json_dict(bulk_data_threshold=10**12, suppress_invalid_tags=True)

    base = instance_url.rstrip("/")

    def _bulk_uri(elem: pydicom.DataElement) -> str:
        return f"{base}/bulkdata/{elem.tag.group:04X}{elem.tag.element:04X}"

    out = ds.to_json_dict(
        bulk_data_element_handler=_bulk_uri,
        bulk_data_threshold=1024,
        suppress_invalid_tags=True,
    )
    # PixelData was skipped by stop_before_pixels; advertise it explicitly so a
    # pixel-streaming client (OHIF) finds the frames resource. VR mirrors the
    # stored encoding (OB encapsulated / ≤8-bit, OW otherwise).
    if TAG["Rows"] in out:
        ts = ds.file_meta.TransferSyntaxUID
        vr = (
            "OB"
            if (ts.is_encapsulated or int(getattr(ds, "BitsAllocated", 16) or 16) <= 8)
            else "OW"
        )
        out[TAG["PixelData"]] = {"vr": vr, "BulkDataURI": f"{base}/frames/1"}
    return out


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
    part_content_type: str | None = None,
) -> Iterator[bytes]:
    """Yield a ``multipart/related`` body. Sync generator: hand it to
    ``StreamingResponse`` so Starlette runs the blocking storage reads in its
    threadpool, off the event loop.

    ``part_content_type`` overrides the per-part ``Content-Type`` header when it
    must differ from the multipart ``type`` parameter — e.g. frame parts whose
    header carries a ``transfer-syntax`` parameter the bare ``type`` omits.
    """
    bnd = boundary.encode("ascii")
    ctype = f"Content-Type: {part_content_type or part_type}\r\n".encode("ascii")
    for content_location, body in parts:
        yield _CRLF + b"--" + bnd + _CRLF
        yield ctype
        yield b"Content-Location: " + content_location.encode("ascii") + _CRLF
        yield _CRLF
        yield from body()
    yield _CRLF + b"--" + bnd + b"--" + _CRLF
