"""Verified-clean substitution in the cohort export cores.

A human-accepted contribution leaves a ``clean_bucket``/``clean_key`` pointer
on the plan item (``cohort_blob_plan`` / ``cohort_series_plan``). The export
must ship that exact rendition — no re-scrub, no re-classify (the classifier
distrusts ``BurnedInAnnotation=NO`` and would re-flag it) — and must NEVER
fall back to the raw high-risk bytes when the clean blob is missing.
"""

from __future__ import annotations

import threading
import uuid
from io import BytesIO

import pydicom
import pytest
from pydicom.dataset import FileMetaDataset
from pydicom.uid import UID, ExplicitVRLittleEndian, generate_uid

pytest.importorskip("stream_zip")

from bvworkers.tasks import training_cohort_export as mod

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


def _run(work, monkeypatch, bodies):
    fetch_calls: list[tuple[str, bool]] = []

    def fake_fetch(_storage, _bucket, key, *, deidentify):
        fetch_calls.append((key, deidentify))
        return bodies.get(key, b"")

    monkeypatch.setattr(mod, "_fetch_blob_bytes", fake_fetch)
    monkeypatch.setattr(mod, "get_s3_storage", lambda: _FakeStorage())
    result = mod._stream_cohort_sync(
        work, b"{}", bucket="b", key="out.zip", progress_q=[0], cancel=threading.Event()
    )
    return result, fetch_calls


def test_clean_pointer_ships_verified_blob(monkeypatch):
    # A verified-clean US rendition (still classifies 'high' — the classifier
    # distrusts BurnedInAnnotation=NO) MUST ship via the pointer, un-re-scrubbed.
    s1 = uuid.uuid4()
    clean = _dcm(_US, Modality="US", BurnedInAnnotation="NO")
    work = [
        {
            "kind": "dicom",
            "name": "s/us.dcm",
            "bucket": "b",
            "key": "k_raw",
            "clean_bucket": "b",
            "clean_key": "k_clean",
            "study_id": s1,
        }
    ]
    (_total, stats, skipped), fetch_calls = _run(work, monkeypatch, {"k_clean": clean})

    assert skipped == []
    assert stats[s1]["size_bytes"] == len(clean)
    # The clean blob was fetched WITHOUT the header re-scrub, and the raw
    # bytes were never touched.
    assert fetch_calls == [("k_clean", False)]


def test_clean_pointer_missing_blob_skips_never_falls_back(monkeypatch):
    s1 = uuid.uuid4()
    raw = _dcm(_US, Modality="US")
    work = [
        {
            "kind": "dicom",
            "name": "s/us.dcm",
            "bucket": "b",
            "key": "k_raw",
            "clean_bucket": "b",
            "clean_key": "k_gone",
            "study_id": s1,
        }
    ]
    (_total, stats, skipped), fetch_calls = _run(work, monkeypatch, {"k_raw": raw})

    assert [s["risk"] for s in skipped] == ["clean_blob_unavailable"]
    assert stats == {}  # nothing shipped
    assert ("k_raw", True) not in fetch_calls  # the raw bytes were never fetched


def test_volume_series_dropped_when_clean_blob_missing(monkeypatch):
    # Volume core: a slice whose verified-clean blob is missing drops the whole
    # series (a hole in the volume is not an option), recorded — never raw.
    s1 = uuid.uuid4()
    plan = [
        {
            "study_syn": "study-0001",
            "study_id": s1,
            "series_idx": 1,
            "dicom": [{"bucket": "b", "key": "k_raw", "clean_bucket": "b", "clean_key": "k_gone"}],
            "masks": [],
        }
    ]
    fetch_calls: list[tuple[str, bool]] = []

    def fake_fetch(_storage, _bucket, key, *, deidentify):
        fetch_calls.append((key, deidentify))
        return b""

    monkeypatch.setattr(mod, "_fetch_blob_bytes", fake_fetch)
    monkeypatch.setattr(mod, "get_s3_storage", lambda: _FakeStorage())
    _total, stats, skipped = mod._stream_cohort_volumes_sync(
        plan,
        b"{}",
        "nnunet",
        {},
        bucket="b",
        key="out.zip",
        progress_q=[0],
        cancel=threading.Event(),
    )

    assert [s["risk"] for s in skipped] == ["clean_blob_unavailable"]
    assert stats == {}
    assert fetch_calls == [("k_gone", False)]
