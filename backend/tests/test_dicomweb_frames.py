"""DICOMweb WADO-RS frames + bulkdata + QIDO relational roots (PS3.18).

Follow-up to the v1 read surface (test_dicomweb.py). Same two-layer style:

* pure service tests — frame extraction byte-identity across native +
  encapsulated transfer syntaxes (the headline correctness risk), the media-
  type mapping, and the BulkDataURI wiring;
* API tests — frames/bulkdata retrieve through the patient-scoped, storage-
  isolated handlers, and the relational roots /series and /instances.

Fixtures are built in-process (encapsulate raw frame payloads) so the suite
never depends on downloading pydicom's external test-data repo in CI.
"""

from __future__ import annotations

import hashlib
import io
import uuid

import pydicom
import pytest
from fastapi import HTTPException
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.encaps import encapsulate, get_frame
from pydicom.uid import (
    CTImageStorage,
    ExplicitVRLittleEndian,
    JPEG2000Lossless,
)
from starlette.requests import Request

from bvphoenix.api import dicomweb as api_dw
from bvphoenix.api.dicomweb import (
    _wado_base,
    qido_relational_instances,
    qido_relational_series,
    wado_bulkdata,
    wado_frames,
)
from bvphoenix.main import app
from bvphoenix.services import dicomweb as dw
from tests.conftest import skip_if_no_db
from tests.test_dicomweb import (
    _add_instance,
    _drain,
    _FakeStorage,
    _multipart_payloads,
    _req,
    _StubAudit,
)

_ROWS = _COLS = 4
_FRAME_BYTES = _ROWS * _COLS * 2  # 16-bit mono => 32 bytes/frame


def _native_frames(n: int) -> list[bytes]:
    # Distinctive, decoder-free payloads so a mis-sliced frame is obvious.
    return [bytes([i + 1]) * _FRAME_BYTES for i in range(n)]


def _base_ds(sop: str) -> Dataset:
    fm = FileMetaDataset()
    fm.MediaStorageSOPClassUID = CTImageStorage
    fm.MediaStorageSOPInstanceUID = sop
    ds = Dataset()
    ds.file_meta = fm
    ds.SOPClassUID = CTImageStorage
    ds.SOPInstanceUID = sop
    ds.StudyInstanceUID = "1.2.3"
    ds.SeriesInstanceUID = "1.2.4"
    ds.Rows = _ROWS
    ds.Columns = _COLS
    ds.BitsAllocated = 16
    ds.BitsStored = 16
    ds.HighBit = 15
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.PixelRepresentation = 0
    return ds


def _native_dicom(frames: list[bytes], sop: str = "1.2.5") -> bytes:
    ds = _base_ds(sop)
    ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    if len(frames) > 1:
        ds.NumberOfFrames = len(frames)
    ds.PixelData = b"".join(frames)
    ds["PixelData"].VR = "OW"
    buf = io.BytesIO()
    pydicom.dcmwrite(buf, ds, enforce_file_format=True)
    return buf.getvalue()


def _encapsulated_dicom(frames: list[bytes], sop: str = "1.2.6") -> bytes:
    ds = _base_ds(sop)
    ds.file_meta.TransferSyntaxUID = JPEG2000Lossless
    if len(frames) > 1:
        ds.NumberOfFrames = len(frames)
    ds.PixelData = encapsulate(frames)
    ds["PixelData"].VR = "OB"
    buf = io.BytesIO()
    pydicom.dcmwrite(buf, ds, enforce_file_format=True)
    return buf.getvalue()


# ---- pure service tests ----------------------------------------------------


def test_frame_media_type_per_transfer_syntax() -> None:
    assert dw.frame_media_type("1.2.840.10008.1.2.1") == "application/octet-stream"
    assert dw.frame_media_type("1.2.840.10008.1.2") == "application/octet-stream"
    assert dw.frame_media_type("1.2.840.10008.1.2.4.50") == "image/jpeg"
    assert dw.frame_media_type("1.2.840.10008.1.2.4.80") == "image/jls"
    assert dw.frame_media_type("1.2.840.10008.1.2.4.90") == "image/jp2"
    assert dw.frame_media_type("1.2.840.10008.1.2.5") == "image/dicom-rle"


def test_extract_frames_native_byte_identical() -> None:
    frames = _native_frames(10)
    raw = _native_dicom(frames)
    ts, got = dw.extract_frames(raw, [1, 5, 10])
    assert ts == str(ExplicitVRLittleEndian)
    assert got == [frames[0], frames[4], frames[9]]


def test_extract_frames_encapsulated_byte_identical() -> None:
    frames = _native_frames(3)
    raw = _encapsulated_dicom(frames)
    ts, got = dw.extract_frames(raw, [1, 2, 3])
    assert ts == str(JPEG2000Lossless)
    # Cross-check against pydicom's own frame reader.
    ds = pydicom.dcmread(io.BytesIO(raw), force=True)
    expected = [get_frame(ds.PixelData, i, number_of_frames=3) for i in range(3)]
    assert got == expected


def test_extract_frames_out_of_range_and_bad_layout() -> None:
    raw = _native_dicom(_native_frames(2))
    with pytest.raises(dw.FrameError):
        dw.extract_frames(raw, [3])
    with pytest.raises(dw.FrameError):
        dw.extract_frames(raw, [0])


def test_header_to_json_wires_pixeldata_bulkdata_uri() -> None:
    raw = _native_dicom(_native_frames(1))
    url = "https://h/api/dicom/studies/1.2.3/series/1.2.4/instances/1.2.5"
    meta = dw.header_to_json(raw, instance_url=url)
    px = meta[dw.TAG["PixelData"]]
    assert px["vr"] in ("OB", "OW")
    assert px["BulkDataURI"] == f"{url}/frames/1"
    # Without instance_url, nothing dangles.
    assert dw.TAG["PixelData"] not in dw.header_to_json(raw)


def test_extract_bulkdata_returns_value_bytes_or_none() -> None:
    raw = _native_dicom(_native_frames(1))
    pixeldata_tag = 0x7FE00010
    assert dw.extract_bulkdata(raw, pixeldata_tag) == _native_frames(1)[0]
    # Absent tag => None (API maps to 404).
    assert dw.extract_bulkdata(raw, 0x00187050) is None


def test_wado_base_honours_x_forwarded_proto() -> None:
    def _r(headers: list[tuple[bytes, bytes]]) -> Request:
        return Request(
            {
                "type": "http",
                "method": "GET",
                "scheme": "http",
                "server": ("bitvision.xeno.garden", 80),
                "path": "/",
                "query_string": b"",
                "headers": headers,
            }
        )

    https = _wado_base(_r([(b"x-forwarded-proto", b"https")]))
    assert https.startswith("https://") and https.endswith("/api/dicom")
    # No header: scheme stays whatever base_url reports (http in this scope).
    plain = _wado_base(_r([]))
    assert plain.startswith("http://")


def test_new_routes_registered() -> None:
    paths = {getattr(r, "path", "") for r in app.routes}
    for expected in (
        "/api/dicom/series",
        "/api/dicom/instances",
        "/api/dicom/studies/{study_uid}/series/{series_uid}/instances/{sop_uid}/frames/{frame_list}",
        "/api/dicom/studies/{study_uid}/series/{series_uid}/instances/{sop_uid}/bulkdata/{tag}",
    ):
        assert expected in paths, f"missing route {expected}"


# ---- API tests -------------------------------------------------------------


@skip_if_no_db
async def test_wado_frames_native_byte_identical(db_session, make_user, make_study, monkeypatch):
    owner = await make_user()
    study, series = await make_study(owner)
    sop = f"1.2.{uuid.uuid4().int}"[:48]
    await _add_instance(db_session, series, sop, key="kf")
    frames = _native_frames(10)
    monkeypatch.setattr(
        api_dw, "get_s3_storage", lambda: _FakeStorage({"kf": _native_dicom(frames)})
    )

    resp = await wado_frames(
        study.study_instance_uid,
        series.series_instance_uid,
        sop,
        "1,5,10",
        _req(accept="multipart/related"),
        db_session,
        owner,
        _StubAudit(),
        grant=None,
    )
    assert "transfer-syntax=1.2.840.10008.1.2.1" not in resp.media_type  # bare type param
    payloads = _multipart_payloads(await _drain(resp), resp.headers["content-type"])
    assert payloads == [frames[0], frames[4], frames[9]]


@skip_if_no_db
async def test_wado_frames_encapsulated(db_session, make_user, make_study, monkeypatch):
    owner = await make_user()
    study, series = await make_study(owner)
    sop = f"1.2.{uuid.uuid4().int}"[:48]
    await _add_instance(db_session, series, sop, key="ke")
    frames = _native_frames(4)
    monkeypatch.setattr(
        api_dw, "get_s3_storage", lambda: _FakeStorage({"ke": _encapsulated_dicom(frames)})
    )

    resp = await wado_frames(
        study.study_instance_uid,
        series.series_instance_uid,
        sop,
        "1,4",
        _req(accept='multipart/related; type="image/jp2"'),
        db_session,
        owner,
        _StubAudit(),
        grant=None,
    )
    assert 'type="image/jp2"' in resp.headers["content-type"]
    payloads = _multipart_payloads(await _drain(resp), resp.headers["content-type"])
    assert [hashlib.sha256(p).hexdigest() for p in payloads] == [
        hashlib.sha256(frames[0]).hexdigest(),
        hashlib.sha256(frames[3]).hexdigest(),
    ]


@skip_if_no_db
async def test_wado_frames_out_of_range_404(db_session, make_user, make_study, monkeypatch):
    owner = await make_user()
    study, series = await make_study(owner)
    sop = f"1.2.{uuid.uuid4().int}"[:48]
    await _add_instance(db_session, series, sop, key="kr")
    monkeypatch.setattr(
        api_dw, "get_s3_storage", lambda: _FakeStorage({"kr": _native_dicom(_native_frames(2))})
    )
    with pytest.raises(HTTPException) as ei:
        await wado_frames(
            study.study_instance_uid,
            series.series_instance_uid,
            sop,
            "5",
            _req(accept="multipart/related"),
            db_session,
            owner,
            _StubAudit(),
            grant=None,
        )
    assert ei.value.status_code == 404


@skip_if_no_db
async def test_wado_bulkdata_pixeldata_round_trip(db_session, make_user, make_study, monkeypatch):
    owner = await make_user()
    study, series = await make_study(owner)
    sop = f"1.2.{uuid.uuid4().int}"[:48]
    await _add_instance(db_session, series, sop, key="kb")
    frames = _native_frames(1)
    monkeypatch.setattr(
        api_dw, "get_s3_storage", lambda: _FakeStorage({"kb": _native_dicom(frames)})
    )

    resp = await wado_bulkdata(
        study.study_instance_uid,
        series.series_instance_uid,
        sop,
        "7FE00010",
        _req(accept="multipart/related"),
        db_session,
        owner,
        _StubAudit(),
        grant=None,
    )
    payloads = _multipart_payloads(await _drain(resp), resp.headers["content-type"])
    assert payloads == [frames[0]]


@skip_if_no_db
async def test_relational_roots_are_patient_scoped(db_session, make_user, make_study):
    owner = await make_user()
    other = await make_user()
    mine, my_series = await make_study(owner, description="Mine", modality="CT")
    await _add_instance(db_session, my_series, f"1.2.{uuid.uuid4().int}"[:48], key="r1")
    _theirs, their_series = await make_study(other, description="Theirs", modality="MR")
    await _add_instance(db_session, their_series, f"1.2.{uuid.uuid4().int}"[:48], key="r2")

    import json

    resp = await qido_relational_series(_req(), db_session, owner)
    series_uids = {s["0020000E"]["Value"][0] for s in json.loads(resp.body)}
    assert my_series.series_instance_uid in series_uids
    assert their_series.series_instance_uid not in series_uids

    resp = await qido_relational_instances(_req(), db_session, owner)
    studies = {i["0020000D"]["Value"][0] for i in json.loads(resp.body)}
    assert mine.study_instance_uid in studies
    assert _theirs.study_instance_uid not in studies
