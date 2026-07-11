"""Fail-closed contract of pixelphi-svc WITHOUT a model present.

The backend's HttpPixelPhiEngine over-redacts every returned box and, on an
empty/absent detector, falls closed to whole-frame masking. This service must
therefore make its no-model state UNMISTAKABLE: ``model_loaded`` False and
``/detect`` returns zero boxes rather than raising or inventing regions. The
model-present path is validated out-of-band on ARM (see scripts/export_onnx.py);
these tests need no ONNX graph.
"""

from __future__ import annotations

import base64
import io

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from bvpixelphi.app import app
from bvpixelphi.config import Settings
from bvpixelphi.engine import DetectorEngine


def _png_b64(w: int = 64, h: int = 48) -> str:
    arr = np.zeros((h, w), dtype=np.uint8)
    arr[10:20, 5:40] = 255  # a bright bar (would-be text)
    buf = io.BytesIO()
    Image.fromarray(arr, mode="L").save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def test_engine_reports_no_model(tmp_path):
    eng = DetectorEngine(Settings(model_dir=str(tmp_path)))
    assert eng.model_loaded is False


def test_engine_detect_returns_empty_without_model(tmp_path):
    eng = DetectorEngine(Settings(model_dir=str(tmp_path)))
    boxes = eng.detect(Image.new("L", (64, 48)))
    assert boxes == []  # fail closed to over-redaction on the backend side


def test_healthz_advertises_missing_model():
    with TestClient(app) as client:
        r = client.get("/healthz")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert body["model_loaded"] is False  # no graph baked / synced in tests


def test_detect_returns_empty_boxes_without_model():
    with TestClient(app) as client:
        r = client.post("/detect", json={"image_png_b64": _png_b64()})
        assert r.status_code == 200
        body = r.json()
        assert body["model_loaded"] is False
        assert body["boxes"] == []


def test_detect_rejects_invalid_image():
    with TestClient(app) as client:
        r = client.post("/detect", json={"image_png_b64": "not-base64!!"})
        assert r.status_code == 422


@pytest.mark.parametrize(
    "model_name,expected",
    [("inference.json", "inference.json"), ("inference.pdmodel", "inference.pdmodel")],
)
def test_export_autodetects_pir_vs_legacy(tmp_path, model_name, expected):
    # The export helper must pick the right graph file for Paddle 3.x (PIR
    # inference.json) vs 2.x (inference.pdmodel) — the format drift the design
    # flagged as the top ARM risk.
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "export_onnx", Path(__file__).resolve().parents[1] / "scripts" / "export_onnx.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    (tmp_path / model_name).write_text("x")
    (tmp_path / "inference.pdiparams").write_text("x")
    args = mod._resolve_model_files(tmp_path)
    assert expected in args
