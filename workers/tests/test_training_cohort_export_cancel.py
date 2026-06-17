"""Cooperative mid-stream cancel of the training-cohort export.

Flow task a5c3f73e. ``request_cancellation`` flips the Job to ``cancelled``;
the export worker's progress poller reads that and trips a shared
``threading.Event`` so the streaming thread raises ``_ExportCancelled``
before its next member. ``upload_iter`` then aborts the multipart (no orphan
artifact) and the worker returns ``cancelled`` without clobbering the
already-terminal Job status. These tests pin the streaming core's
cooperative bail; the abort + status handling are covered by the
``upload_iter`` contract and the worker's ``except _ExportCancelled`` branch.
"""

from __future__ import annotations

import threading

import pytest

# The worker imports stream_zip at module top; skip cleanly if absent.
pytest.importorskip("stream_zip")

from bvworkers.tasks import training_cohort_export as mod


class _FakeStorage:
    """upload_iter that drives the member generator the way stream_zip + the
    real S3 multipart sink would (pull every member), then reports size."""

    def __init__(self) -> None:
        self.completed = False

    def upload_iter(self, gen, *, bucket: str, key: str, content_type: str):
        for _ in gen:
            pass
        self.completed = True

        class _R:
            size_bytes = 0

        return _R()


def _work(n: int) -> list[dict[str, str]]:
    return [
        {"bucket": "b", "key": f"k{i}", "name": f"study-0001/img-{i}.dcm", "kind": "dicom"}
        for i in range(n)
    ]


def test_cancel_before_first_member_fetches_nothing(monkeypatch):
    fetched: list[str] = []

    def fake_fetch(_storage, _bucket, key, *, deidentify):
        fetched.append(key)
        return b"x"

    storage = _FakeStorage()
    monkeypatch.setattr(mod, "_fetch_blob_bytes", fake_fetch)
    monkeypatch.setattr(mod, "get_s3_storage", lambda: storage)

    cancel = threading.Event()
    cancel.set()  # already cancelled when the stream starts

    with pytest.raises(mod._ExportCancelledError):
        mod._stream_cohort_sync(
            _work(3), b"{}", bucket="b", key="out.zip", progress_q=[0], cancel=cancel
        )

    assert fetched == []  # bailed before touching a single blob
    assert storage.completed is False  # multipart never completed


def test_cancel_mid_stream_stops_early(monkeypatch):
    cancel = threading.Event()
    fetched: list[str] = []

    def fake_fetch(_storage, _bucket, key, *, deidentify):
        fetched.append(key)
        cancel.set()  # the cancel lands while the first member is in flight
        return b"x"

    storage = _FakeStorage()
    monkeypatch.setattr(mod, "_fetch_blob_bytes", fake_fetch)
    monkeypatch.setattr(mod, "get_s3_storage", lambda: storage)

    work = _work(5)
    with pytest.raises(mod._ExportCancelledError):
        mod._stream_cohort_sync(
            work, b"{}", bucket="b", key="out.zip", progress_q=[0], cancel=cancel
        )

    # Stopped before draining the whole cohort, and never completed the upload.
    assert 0 < len(fetched) < len(work)
    assert storage.completed is False
