"""M2 dataset + M4 redaction: OCR detect → blackout → re-encode, scored against
the synthetic ground truth.

The recall gate (every burned-in PHI box masked) runs end-to-end through
Tesseract when the binary is available, and is skipped otherwise; the
deterministic parts (blackout, re-encode round-trip, PHI classification, the
none-risk passthrough) always run.
"""

from __future__ import annotations

import io
import os
import shutil

import numpy as np
import pydicom
import pytest

from bvphoenix.services.pixel_deid import (
    PixelDeidResult,
    TextBox,
    classify_text_phi,
    clean_pixel_data,
    redact_frames,
    reencode_pixel_data,
)
from bvphoenix.services.pixel_deid_eval import (
    US_SOP,
    load_public_corpus,
    score_redaction,
    synthesize_case,
)

_HAS_TESSERACT = shutil.which("tesseract") is not None
_needs_ocr = pytest.mark.skipif(not _HAS_TESSERACT, reason="tesseract binary not installed")
_CT = "1.2.840.10008.5.1.4.1.1.2"


def test_ocr_gate_not_silently_skipped():
    """CI arms ``BVP_REQUIRE_OCR=1`` after installing tesseract: if the binary
    ever goes missing there, this FAILS instead of letting the recall==1.0
    gate above evaporate into a skip. Local runs without the flag skip."""
    if os.environ.get("BVP_REQUIRE_OCR") != "1":
        pytest.skip("BVP_REQUIRE_OCR unset (local run)")
    assert _HAS_TESSERACT, (
        "BVP_REQUIRE_OCR=1 but tesseract is not installed - "
        "the pixel-PHI recall gate would silently skip"
    )


# --- M4 recall gate (OCR end-to-end) ---------------------------------------


@_needs_ocr
@pytest.mark.parametrize("seed", [1, 2, 3, 7, 11])
def test_clean_pixel_data_masks_all_burned_in_phi(seed):
    case = synthesize_case(seed=seed, modality="US", sop_class=US_SOP)
    result = clean_pixel_data(case.dicom)
    assert result.residual_suspect is True  # high-risk always needs human review
    masked = [(r["x"], r["y"], r["w"], r["h"]) for r in result.redactions]
    score = score_redaction(case.gt, masked, coverage=0.8)
    assert score.recall == 1.0, f"unmasked PHI: {score.missed}"


@_needs_ocr
def test_redacted_output_pixels_are_black_over_phi():
    case = synthesize_case(seed=5)
    out = pydicom.dcmread(io.BytesIO(clean_pixel_data(case.dicom).out_bytes))
    arr = out.pixel_array
    for g in case.gt:
        region = arr[g.y : g.y + g.h, g.x : g.x + g.w]
        assert region.mean() < 8.0, f"PHI region for {g.text!r} not blacked out"


# --- deterministic (no OCR) -------------------------------------------------


def test_none_risk_passthrough():
    # A CT chest (none-risk) is returned untouched, not redacted.
    case = synthesize_case(seed=1, modality="CT", sop_class=_CT, phi_items=[])
    # Force a clearly none-risk header by using CT + ORIGINAL/PRIMARY-style.
    ds = pydicom.dcmread(io.BytesIO(case.dicom))
    ds.ImageType = ["ORIGINAL", "PRIMARY"]
    ds.BodyPartExamined = "CHEST"
    buf = io.BytesIO()
    ds.save_as(buf, write_like_original=False)
    result = clean_pixel_data(buf.getvalue())
    assert result.risk.level == "none"
    assert result.residual_suspect is False
    assert result.out_bytes == buf.getvalue()  # untouched


def test_reencode_roundtrip_preserves_modified_pixels():
    case = synthesize_case(seed=9, phi_items=[("ROSSI MARIO", "name")])
    ds = pydicom.dcmread(io.BytesIO(case.dicom))
    arr = np.asarray(ds.pixel_array).copy()
    arr[0:20, 0:20] = 0  # blackout a corner
    reencode_pixel_data(ds, arr)
    buf = io.BytesIO()
    ds.save_as(buf, write_like_original=False)
    back = pydicom.dcmread(io.BytesIO(buf.getvalue())).pixel_array
    assert back.shape == arr.shape
    assert int(back[0:20, 0:20].sum()) == 0  # the blackout survived the round-trip


def test_redact_frames_blackout_monochrome2():
    frame = np.full((40, 40), 200, dtype=np.uint8)
    redact_frames([frame], [[(5, 5, 10, 10)]], black=0)
    assert int(frame[5:15, 5:15].sum()) == 0
    assert frame[0, 0] == 200  # outside the box untouched


def test_classify_text_phi_keeps_phi_drops_clinical():
    boxes = [
        TextBox(0, 0, 10, 10, "RSSMRA85T10A562S", 90),  # codice fiscale shape
        TextBox(0, 0, 10, 10, "15/05/1950", 90),  # date
        TextBox(0, 0, 10, 10, "MRN-123456", 90),  # mrn
        TextBox(0, 0, 10, 10, "ROSSI", 90),  # name
        TextBox(0, 0, 10, 10, "4.2cm", 90),  # clinical measurement (not PHI-shaped)
        TextBox(0, 0, 10, 10, "LT", 90),  # laterality token
    ]
    kept = {b.text for b in classify_text_phi(boxes)}
    assert "RSSMRA85T10A562S" in kept
    assert "15/05/1950" in kept
    assert "MRN-123456" in kept
    assert "4.2cm" not in kept  # measurement preserved in selective mode


def test_load_public_corpus_absent_is_empty(tmp_path):
    # Skip-if-absent: CI without the synced corpus yields nothing (no error).
    assert list(load_public_corpus(tmp_path / "not-synced")) == []


@pytest.mark.skipif(
    not os.environ.get("BVP_PIXEL_DEID_CORPUS"),
    reason="public corpus not synced (BVP_PIXEL_DEID_CORPUS)",
)
@_needs_ocr
def test_public_corpus_recall_tracked():
    # Tracked, NOT gated: TCIA pixel labels are noisy. Reports mean recall.
    recalls = []
    for case in load_public_corpus(os.environ["BVP_PIXEL_DEID_CORPUS"]):
        if not case.gt:
            continue
        masked = [(r["x"], r["y"], r["w"], r["h"]) for r in clean_pixel_data(case.dicom).redactions]
        recalls.append(score_redaction(case.gt, masked).recall)
    if recalls:
        print(f"public corpus mean pixel-redaction recall: {sum(recalls) / len(recalls):.3f}")


def test_clean_pixel_data_undecodable_blocks():
    # A high-risk SOP class but with no/garbage pixel data → can't redact → block.
    case = synthesize_case(seed=1, phi_items=[("X", "name")])
    ds = pydicom.dcmread(io.BytesIO(case.dicom))
    ds.PixelData = b"\x00\x01\x02"  # too short for Rows*Columns
    buf = io.BytesIO()
    ds.save_as(buf, write_like_original=False)
    result: PixelDeidResult = clean_pixel_data(buf.getvalue())
    assert result.decode_failed is True
    assert result.residual_suspect is True
