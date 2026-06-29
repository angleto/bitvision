"""DICOMweb read surface (PS3.18): QIDO-RS query + WADO-RS retrieve/metadata.

Two layers, mirroring the de-id-provenance test style (call handlers directly
on the test's event loop; TestClient's separate loop corrupts ``db_session``):

* pure service tests — DICOM-JSON serialization + the multipart/related
  framing (the headline engineering risk) + metadata extraction, no infra;
* API tests — QIDO patient-scoping (cross-patient = 404, inexpressible) and
  WADO retrieve byte-identity, with storage mocked in-memory so the round
  trip runs without MinIO.
"""

from __future__ import annotations

import hashlib
import io
import json
import uuid

import pydicom
import pytest
from fastapi import HTTPException
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian
from starlette.requests import Request

from bvphoenix.api import dicomweb as api_dw
from bvphoenix.api.dicomweb import (
    _resolve_study,
    qido_series,
    qido_series_instances,
    qido_studies,
    wado_instance,
    wado_instance_metadata,
    wado_series,
)
from bvphoenix.db.models import Instance
from bvphoenix.main import app
from bvphoenix.services import dicomweb as dw
from tests.conftest import skip_if_no_db


def _dicom_bytes(
    study_uid: str, series_uid: str, sop_uid: str, *, instance_number: int = 1
) -> bytes:
    fm = FileMetaDataset()
    fm.MediaStorageSOPClassUID = CTImageStorage
    fm.MediaStorageSOPInstanceUID = sop_uid
    fm.TransferSyntaxUID = ExplicitVRLittleEndian
    ds = Dataset()
    ds.file_meta = fm
    ds.PatientID = "TEST123"
    ds.PatientName = "Test^Patient"
    ds.StudyInstanceUID = study_uid
    ds.SeriesInstanceUID = series_uid
    ds.SOPInstanceUID = sop_uid
    ds.SOPClassUID = CTImageStorage
    ds.Modality = "CT"
    ds.StudyDate = "20260401"
    ds.InstanceNumber = instance_number
    buf = io.BytesIO()
    pydicom.dcmwrite(buf, ds, enforce_file_format=True)
    return buf.getvalue()


def _req(query: str = "", accept: str = "") -> Request:
    headers = [(b"accept", accept.encode())] if accept else []
    return Request(
        {
            "type": "http",
            "method": "GET",
            "scheme": "http",
            "server": ("testserver", 80),
            "path": "/",
            "query_string": query.encode(),
            "headers": headers,
        }
    )


class _StubAudit:
    async def log(self, **_kw) -> None:
        return None


class _FakeStorage:
    """In-memory stand-in for S3Storage: just enough for WADO retrieve."""

    def __init__(self, blobs: dict[str, bytes]) -> None:
        self.blobs = blobs

    def get_object_bytes(self, *, bucket: str, key: str) -> bytes:
        return self.blobs[key]

    def iter_object(self, *, bucket: str, key: str, chunk_size: int = 65536):
        data = self.blobs[key]

        def _it():
            for i in range(0, len(data), chunk_size):
                yield data[i : i + chunk_size]

        return _it(), len(data), "application/dicom"


def _multipart_payloads(body: bytes, content_type: str) -> list[bytes]:
    boundary = content_type.split("boundary=")[1].split(";")[0].strip().strip('"')
    delim = b"--" + boundary.encode("ascii")
    out: list[bytes] = []
    for seg in body.split(delim):
        if b"\r\n\r\n" not in seg:
            continue  # preamble / closing "--"
        _head, payload = seg.split(b"\r\n\r\n", 1)
        if payload.endswith(b"\r\n"):
            payload = payload[:-2]  # drop only the framing CRLF, not payload bytes
        out.append(payload)
    return out


async def _drain(resp) -> bytes:
    return b"".join([chunk async for chunk in resp.body_iterator])


# ---- pure service tests ----------------------------------------------------


def test_study_json_shape_and_pii_safety() -> None:
    pid = uuid.uuid4()
    j = dw.study_to_json(
        study_instance_uid="1.2.3",
        study_date=None,
        study_description="Thorax",
        modalities=["CT", "PT"],
        patient_id=pid,
        num_series=2,
        num_instances=10,
        retrieve_url="http://x/api/dicom/studies/1.2.3",
    )
    assert j["0020000D"] == {"vr": "UI", "Value": ["1.2.3"]}
    assert j["00080061"] == {"vr": "CS", "Value": ["CT", "PT"]}
    assert j["00201206"] == {"vr": "IS", "Value": [2]}
    assert j["00201208"] == {"vr": "IS", "Value": [10]}
    # PatientID carries the opaque platform UUID (grouping key, not PHI);
    # PatientName never leaks.
    assert j["00100020"] == {"vr": "LO", "Value": [str(pid)]}
    assert j["00100010"] == {"vr": "PN"}


def test_multipart_framing_is_byte_identical() -> None:
    """The headline risk: multipart/related must reproduce each part's bytes
    exactly, including a payload that itself ends in CRLF."""
    a = b"\x00\x01DICOM-A\r\n"  # trailing CRLF in the payload on purpose
    b = b"DICOM-B-no-crlf"
    parts: list[dw.MultipartPart] = [
        ("loc/a", (lambda: iter([a]))),
        ("loc/b", (lambda: iter([b[:3], b[3:]]))),  # chunked source
    ]
    boundary = dw.new_boundary()
    body = b"".join(dw.iter_multipart(parts, boundary))
    ctype = dw.multipart_content_type(boundary)
    payloads = _multipart_payloads(body, ctype)
    assert payloads == [a, b]
    assert 'type="application/dicom"' in ctype


def test_header_to_json_excludes_pixels() -> None:
    raw = _dicom_bytes("1.2.10", "1.2.11", "1.2.12")
    meta = dw.header_to_json(raw)
    assert meta["0020000D"]["Value"] == ["1.2.10"]
    assert meta["00080018"]["Value"] == ["1.2.12"]
    assert "7FE00010" not in meta  # PixelData excluded


def test_routes_are_registered() -> None:
    paths = {getattr(r, "path", "") for r in app.routes}
    for expected in (
        "/api/dicom/studies",
        "/api/dicom/studies/{study_uid}",
        "/api/dicom/studies/{study_uid}/series",
        "/api/dicom/studies/{study_uid}/series/{series_uid}",
        "/api/dicom/studies/{study_uid}/series/{series_uid}/instances/{sop_uid}",
        "/api/dicom/studies/{study_uid}/metadata",
    ):
        assert expected in paths, f"missing DICOMweb route {expected}"


# ---- API tests -------------------------------------------------------------


async def _add_instance(db, series, sop_uid, *, key, instance_number=1) -> Instance:
    inst = Instance(
        id=uuid.uuid4(),
        series_id=series.id,
        sop_instance_uid=sop_uid,
        sop_class_uid=str(CTImageStorage),
        instance_number=instance_number,
        s3_bucket="bv-test",
        s3_key=key,
    )
    db.add(inst)
    await db.flush()
    return inst


@skip_if_no_db
async def test_qido_lists_visible_studies_and_hierarchy(db_session, make_user, make_study):
    owner = await make_user()
    study, series = await make_study(owner, description="Thorax", modality="CT")
    await _add_instance(db_session, series, f"1.2.{uuid.uuid4().int}"[:48], key="k1")

    # study level
    resp = await qido_studies(_req(), db_session, owner)
    studies = json.loads(resp.body)
    mine = [s for s in studies if s["0020000D"]["Value"] == [study.study_instance_uid]]
    assert len(mine) == 1
    assert mine[0]["00201206"]["Value"] == [1]  # one series
    assert mine[0]["00201208"]["Value"] == [1]  # one instance

    # series level
    resp = await qido_series(study.study_instance_uid, _req(), db_session, owner)
    series_items = json.loads(resp.body)
    assert series_items[0]["0020000E"]["Value"] == [series.series_instance_uid]
    assert series_items[0]["00201209"]["Value"] == [1]

    # instance level
    resp = await qido_series_instances(
        study.study_instance_uid, series.series_instance_uid, _req(), db_session, owner
    )
    inst_items = json.loads(resp.body)
    assert inst_items[0]["00080016"]["Value"] == [str(CTImageStorage)]


@skip_if_no_db
async def test_cross_patient_uid_is_404_not_visible(db_session, make_user, make_study):
    owner = await make_user()
    other = await make_user()
    other_study, _series = await make_study(other)  # private, owned by someone else

    # owner1 cannot even resolve owner2's study UID — inexpressible, not refused.
    with pytest.raises(HTTPException) as ei:
        await _resolve_study(db_session, owner, other_study.study_instance_uid)
    assert ei.value.status_code == 404

    # And it never appears in owner1's QIDO study list.
    import json

    resp = await qido_studies(_req(), db_session, owner)
    bodies = json.loads(resp.body) if resp.status_code == 200 else []
    assert all(s["0020000D"]["Value"] != [other_study.study_instance_uid] for s in bodies)


@skip_if_no_db
async def test_wado_instance_retrieve_is_byte_identical(
    db_session, make_user, make_study, monkeypatch
):
    owner = await make_user()
    study, series = await make_study(owner)
    sop = f"1.2.{uuid.uuid4().int}"[:48]
    await _add_instance(db_session, series, sop, key="k-inst")
    raw = _dicom_bytes(study.study_instance_uid, series.series_instance_uid, sop)
    monkeypatch.setattr(api_dw, "get_s3_storage", lambda: _FakeStorage({"k-inst": raw}))

    resp = await wado_instance(
        study.study_instance_uid,
        series.series_instance_uid,
        sop,
        _req(accept='multipart/related; type="application/dicom"'),
        db_session,
        owner,
        _StubAudit(),
        grant=None,
    )
    assert resp.media_type.startswith("multipart/related")
    body = await _drain(resp)
    payloads = _multipart_payloads(body, resp.headers["content-type"])
    assert len(payloads) == 1
    assert hashlib.sha256(payloads[0]).hexdigest() == hashlib.sha256(raw).hexdigest()


@skip_if_no_db
async def test_wado_series_retrieve_streams_all_instances(
    db_session, make_user, make_study, monkeypatch
):
    owner = await make_user()
    study, series = await make_study(owner)
    sop1 = f"1.2.{uuid.uuid4().int}"[:48]
    sop2 = f"1.2.{uuid.uuid4().int}"[:48]
    await _add_instance(db_session, series, sop1, key="k1", instance_number=1)
    await _add_instance(db_session, series, sop2, key="k2", instance_number=2)
    raw1 = _dicom_bytes(
        study.study_instance_uid, series.series_instance_uid, sop1, instance_number=1
    )
    raw2 = _dicom_bytes(
        study.study_instance_uid, series.series_instance_uid, sop2, instance_number=2
    )
    monkeypatch.setattr(api_dw, "get_s3_storage", lambda: _FakeStorage({"k1": raw1, "k2": raw2}))

    resp = await wado_series(
        study.study_instance_uid,
        series.series_instance_uid,
        _req(),
        db_session,
        owner,
        _StubAudit(),
        grant=None,
    )
    payloads = _multipart_payloads(await _drain(resp), resp.headers["content-type"])
    got = {hashlib.sha256(p).hexdigest() for p in payloads}
    assert got == {hashlib.sha256(raw1).hexdigest(), hashlib.sha256(raw2).hexdigest()}


@skip_if_no_db
async def test_wado_instance_metadata_json(db_session, make_user, make_study, monkeypatch):
    owner = await make_user()
    study, series = await make_study(owner)
    sop = f"1.2.{uuid.uuid4().int}"[:48]
    await _add_instance(db_session, series, sop, key="k-meta")
    raw = _dicom_bytes(study.study_instance_uid, series.series_instance_uid, sop)
    monkeypatch.setattr(api_dw, "get_s3_storage", lambda: _FakeStorage({"k-meta": raw}))

    resp = await wado_instance_metadata(
        study.study_instance_uid, series.series_instance_uid, sop, db_session, owner, grant=None
    )
    items = json.loads(resp.body)
    assert resp.media_type == dw.DICOM_JSON_MEDIA_TYPE
    assert len(items) == 1
    assert items[0]["00080018"]["Value"] == [sop]
    assert "7FE00010" not in items[0]


@skip_if_no_db
async def test_wado_retrieve_deidentifies_t3_study(db_session, make_user, make_study, monkeypatch):
    owner = await make_user()
    study, series = await make_study(owner)
    # T3 = training opt-in => served scrubbed even without a share grant.
    study.contribution_tier = "t3"
    db_session.add(study)
    await db_session.flush()
    sop = f"1.2.{uuid.uuid4().int}"[:48]
    await _add_instance(db_session, series, sop, key="k-deid")
    raw = _dicom_bytes(study.study_instance_uid, series.series_instance_uid, sop)
    monkeypatch.setattr(api_dw, "get_s3_storage", lambda: _FakeStorage({"k-deid": raw}))

    resp = await wado_instance(
        study.study_instance_uid,
        series.series_instance_uid,
        sop,
        _req(),
        db_session,
        owner,
        _StubAudit(),
        grant=None,
    )
    # The de-identification branch ran: no-store cache + a still-parseable part.
    assert resp.headers["cache-control"] == "no-store"
    payloads = _multipart_payloads(await _drain(resp), resp.headers["content-type"])
    served = pydicom.dcmread(io.BytesIO(payloads[0]), force=True)
    assert (
        served.PatientName in ("", "Anonymous", "Anonymized")
        or str(served.PatientName) != "Test^Patient"
    )
