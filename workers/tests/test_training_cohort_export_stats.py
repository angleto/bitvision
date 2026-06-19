"""Per-study byte/hash accumulation + burned-in-pixel gate in the cohort streamer.

Flow task a5c3f73e (Option 3 producer). ``_stream_cohort_sync`` tracks, per real
``study_id``, the de-identified byte count + a running SHA-256 (the DatasetStudy
payout weight + integrity). The M0 burned-in-PHI gate then EXCLUDES high-risk-
pixel instances from the public artifact. The trailing labels.json carries no
study_id and must not pollute the stats.
"""

from __future__ import annotations

import hashlib
import threading
import uuid
from io import BytesIO

import pydicom
import pytest
from pydicom.dataset import FileMetaDataset
from pydicom.uid import UID, ExplicitVRLittleEndian, generate_uid

pytest.importorskip("stream_zip")

from bvworkers.tasks import training_cohort_export as mod

_CT = "1.2.840.10008.5.1.4.1.1.2"  # CT Image Storage (low risk)
_US = "1.2.840.10008.5.1.4.1.1.6.1"  # US Image Storage (high burned-in risk)


def _dcm(sop_class: str, **attrs: object) -> bytes:
    ds = pydicom.Dataset()
    ds.StudyInstanceUID = generate_uid()
    ds.SeriesInstanceUID = generate_uid()
    ds.SOPInstanceUID = generate_uid()
    ds.SOPClassUID = UID(sop_class)
    for k, v in attrs.items():
        setattr(ds, k, v)
    fm = FileMetaDataset()
    fm.MediaStorageSOPClassUID = UID(sop_class)
    fm.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
    fm.TransferSyntaxUID = ExplicitVRLittleEndian
    fm.ImplementationClassUID = generate_uid()
    ds.file_meta = fm
    buf = BytesIO()
    ds.save_as(buf, write_like_original=False)
    return buf.getvalue()


class _FakeStorage:
    def upload_iter(self, gen, *, bucket: str, key: str, content_type: str):
        for _ in gen:
            pass

        class _R:
            size_bytes = 123

        return _R()


def test_per_study_stats_accumulated(monkeypatch):
    s1, s2 = uuid.uuid4(), uuid.uuid4()
    # Low-risk DICOM (CT) flows through the burned-in-PHI gate; mask is raw.
    dcm1 = _dcm(_CT, Modality="CT", ImageType=["ORIGINAL", "PRIMARY"], BodyPartExamined="CHEST")
    mask = b"BBBB"
    dcm3 = _dcm(_CT, Modality="CT", ImageType=["ORIGINAL", "PRIMARY"], BodyPartExamined="ABDOMEN")
    bodies = {"k1": dcm1, "k2": mask, "k3": dcm3}

    def fake_fetch(_storage, _bucket, key, *, deidentify):
        return bodies[key]

    monkeypatch.setattr(mod, "_fetch_blob_bytes", fake_fetch)
    monkeypatch.setattr(mod, "get_s3_storage", lambda: _FakeStorage())

    work = [
        {"kind": "dicom", "name": "study-0001/a.dcm", "bucket": "b", "key": "k1", "study_id": s1},
        {"kind": "mask", "name": "study-0001/m.bin", "bucket": "b", "key": "k2", "study_id": s1},
        {"kind": "dicom", "name": "study-0002/a.dcm", "bucket": "b", "key": "k3", "study_id": s2},
    ]
    total, stats, skipped = mod._stream_cohort_sync(
        work, b"{}", bucket="b", key="out.zip", progress_q=[0], cancel=threading.Event()
    )

    assert total == 123  # the upload sink reports the streamed ZIP size
    assert skipped == []  # nothing high-risk
    assert set(stats) == {s1, s2}  # labels.json (no study_id) does not appear
    assert stats[s1]["size_bytes"] == len(dcm1) + len(mask)
    assert stats[s1]["content_sha256"] == hashlib.sha256(dcm1 + mask).hexdigest()
    assert stats[s2]["size_bytes"] == len(dcm3)
    assert stats[s2]["content_sha256"] == hashlib.sha256(dcm3).hexdigest()


def test_high_risk_pixel_instance_excluded(monkeypatch):
    s1 = uuid.uuid4()
    us = _dcm(_US, Modality="US")  # ultrasound → high burned-in-pixel risk
    ct = _dcm(_CT, Modality="CT", ImageType=["ORIGINAL", "PRIMARY"], BodyPartExamined="CHEST")
    bodies = {"k_us": us, "k_ct": ct}

    monkeypatch.setattr(mod, "_fetch_blob_bytes", lambda _s, _b, key, *, deidentify: bodies[key])
    monkeypatch.setattr(mod, "get_s3_storage", lambda: _FakeStorage())

    work = [
        {"kind": "dicom", "name": "s/us.dcm", "bucket": "b", "key": "k_us", "study_id": s1},
        {"kind": "dicom", "name": "s/ct.dcm", "bucket": "b", "key": "k_ct", "study_id": s1},
    ]
    _total, stats, skipped = mod._stream_cohort_sync(
        work, b"{}", bucket="b", key="out.zip", progress_q=[0], cancel=threading.Event()
    )

    # The US instance is excluded from the public artifact; only the CT counts.
    assert [s["name"] for s in skipped] == ["s/us.dcm"]
    assert stats[s1]["size_bytes"] == len(ct)
    assert stats[s1]["content_sha256"] == hashlib.sha256(ct).hexdigest()
