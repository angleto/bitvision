"""Per-study byte/hash accumulation in the cohort export streamer.

Flow task a5c3f73e (Option 3 producer). ``_stream_cohort_sync`` tracks, per
real ``study_id``, the de-identified byte count + a running SHA-256 as it
streams, so the dataset producer can write one ``DatasetStudy`` per study
with an accurate ``size_bytes`` (the payout weight) and ``content_sha256``.
The trailing labels.json carries no study_id and must not pollute the stats.
"""

from __future__ import annotations

import hashlib
import threading
import uuid

import pytest

pytest.importorskip("stream_zip")

from bvworkers.tasks import training_cohort_export as mod


class _FakeStorage:
    def upload_iter(self, gen, *, bucket: str, key: str, content_type: str):
        for _ in gen:
            pass

        class _R:
            size_bytes = 123

        return _R()


def test_per_study_stats_accumulated(monkeypatch):
    s1, s2 = uuid.uuid4(), uuid.uuid4()
    bodies = {"k1": b"AAA", "k2": b"BBBB", "k3": b"CC"}

    def fake_fetch(_storage, _bucket, key, *, deidentify):
        return bodies[key]

    monkeypatch.setattr(mod, "_fetch_blob_bytes", fake_fetch)
    monkeypatch.setattr(mod, "get_s3_storage", lambda: _FakeStorage())

    work = [
        {"kind": "dicom", "name": "study-0001/a.dcm", "bucket": "b", "key": "k1", "study_id": s1},
        {"kind": "mask", "name": "study-0001/m.bin", "bucket": "b", "key": "k2", "study_id": s1},
        {"kind": "dicom", "name": "study-0002/a.dcm", "bucket": "b", "key": "k3", "study_id": s2},
    ]
    total, stats = mod._stream_cohort_sync(
        work, b"{}", bucket="b", key="out.zip", progress_q=[0], cancel=threading.Event()
    )

    assert total == 123  # the upload sink reports the streamed ZIP size
    assert set(stats) == {s1, s2}  # labels.json (no study_id) does not appear
    assert stats[s1]["size_bytes"] == 7  # 3 + 4
    assert stats[s1]["content_sha256"] == hashlib.sha256(b"AAA" + b"BBBB").hexdigest()
    assert stats[s2]["size_bytes"] == 2
    assert stats[s2]["content_sha256"] == hashlib.sha256(b"CC").hexdigest()
