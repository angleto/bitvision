"""M5: pluggable VLM hard-case engine, storage-isolation guard + over-redaction.

The security-critical assertions: a PHI-bearing crop is never POSTed to a host
outside the in-cluster allowlist, and any failure (disallowed host, dead service)
fails CLOSED toward over-redaction, never toward a clean image.
"""

from __future__ import annotations

import io
import shutil

import httpx
import numpy as np
import pydicom
import pytest

from bvphoenix.services.pixel_deid import clean_pixel_data
from bvphoenix.services.pixel_deid_eval import synthesize_case
from bvphoenix.services.pixel_phi_engine import (
    HttpPixelPhiEngine,
    NullPixelPhiEngine,
    get_pixel_phi_engine,
)

_HAS_TESSERACT = shutil.which("tesseract") is not None
_needs_ocr = pytest.mark.skipif(not _HAS_TESSERACT, reason="tesseract binary not installed")
_ALLOWED = frozenset({"localhost", "bvphoenix-pixelphi-svc"})


def test_null_engine_over_redacts_whole_frame():
    boxes = NullPixelPhiEngine().detect_boxes(np.zeros((20, 30), dtype=np.uint8))
    assert boxes == [(0, 0, 30, 20)]


def test_disallowed_host_over_redacts_without_network(monkeypatch):
    # A PHI crop must NEVER reach a non-allowlisted host, the guard returns
    # before any httpx call.
    def _boom(*a, **k):
        raise AssertionError("network must not be attempted for a disallowed host")

    monkeypatch.setattr(httpx, "Client", _boom)
    eng = HttpPixelPhiEngine(url="http://evil.example.com/detect", allowed_hosts=_ALLOWED)
    assert eng.detect_boxes(np.zeros((20, 30), dtype=np.uint8)) == [(0, 0, 30, 20)]


def test_allowed_host_error_fails_closed():
    # Allowed host but the service is down → fail CLOSED (over-redact), never
    # fall through to "no boxes / clean".
    eng = HttpPixelPhiEngine(url="http://localhost:1/detect", allowed_hosts=_ALLOWED, timeout=0.2)
    assert eng.detect_boxes(np.zeros((10, 10), dtype=np.uint8)) == [(0, 0, 10, 10)]


def test_allowed_host_returns_model_boxes(monkeypatch):
    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"boxes": [{"x": 1, "y": 2, "w": 3, "h": 4}]}

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, *a, **k):
            return _Resp()

    monkeypatch.setattr(httpx, "Client", _Client)
    eng = HttpPixelPhiEngine(url="http://bvphoenix-pixelphi-svc/detect", allowed_hosts=_ALLOWED)
    assert eng.detect_boxes(np.zeros((20, 30), dtype=np.uint8)) == [(1, 2, 3, 4)]


def test_engine_disabled_by_default():
    # pixel_phi_vlm_enabled defaults False → no engine, callers rely on the
    # Tesseract tier + human review.
    assert get_pixel_phi_engine() is None


@_needs_ocr
def test_clean_pixel_data_escalates_ocr_blank_high_risk_frame():
    # A high-risk US frame the cheap tier can't read (blank here) is escalated:
    # NullEngine over-redacts the whole frame.
    case = synthesize_case(seed=1, modality="US", phi_items=[])
    res = clean_pixel_data(case.dicom, vlm_engine=NullPixelPhiEngine())
    out = pydicom.dcmread(io.BytesIO(res.out_bytes))
    assert int(out.pixel_array.max()) == 0  # whole frame masked
    assert any(r["text"] == "<vlm>" for r in res.redactions)


@_needs_ocr
def test_clean_pixel_data_no_engine_leaves_blank_frame():
    case = synthesize_case(seed=1, modality="US", phi_items=[])
    res = clean_pixel_data(case.dicom, vlm_engine=None)
    out = pydicom.dcmread(io.BytesIO(res.out_bytes))
    assert int(out.pixel_array.max()) > 0  # nothing detected, nothing masked
    assert not any(r["text"] == "<vlm>" for r in res.redactions)
