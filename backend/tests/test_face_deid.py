"""M6: de-facing seam + scan-text preservation mode.

Covers the defacer Protocol (Null records absence, heuristic masks the anterior
band and refuses ROI-bearing regions), the low-risk wiring in clean_pixel_data
(disabled => unchanged; enabled => masked + flagged for review, provenance only
on accept), and the over_redact vs selective redaction mode.
"""

from __future__ import annotations

import io
from types import SimpleNamespace

import numpy as np
import pydicom
import pytest

import bvphoenix.config as config_mod
import bvphoenix.services.pixel_deid as pixel_deid
from bvphoenix.services.face_deid import (
    HeuristicFaceMasker,
    NullDefacer,
    get_defacer,
)
from bvphoenix.services.pixel_deid import (
    TextBox,
    classify_pixel_risk,
    classify_text_phi,
    clean_pixel_data,
    mark_visual_features_removed,
)
from bvphoenix.services.pixel_deid_eval import synthesize_case


def _low_risk_ct(body: str = "HEAD", rows: int = 40, cols: int = 40, fill: int = 100) -> bytes:
    from pydicom.dataset import Dataset, FileMetaDataset
    from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian, generate_uid

    ds = Dataset()
    ds.Modality = "CT"
    ds.BodyPartExamined = body
    ds.SOPClassUID = CTImageStorage
    ds.SOPInstanceUID = generate_uid()
    ds.Rows = rows
    ds.Columns = cols
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.BitsAllocated = 8
    ds.BitsStored = 8
    ds.HighBit = 7
    ds.PixelRepresentation = 0
    ds.PixelData = np.full((rows, cols), fill, dtype=np.uint8).tobytes()
    fm = FileMetaDataset()
    fm.MediaStorageSOPClassUID = CTImageStorage
    fm.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
    fm.TransferSyntaxUID = ExplicitVRLittleEndian
    ds.file_meta = fm
    ds.is_little_endian = True
    ds.is_implicit_VR = False
    buf = io.BytesIO()
    ds.save_as(buf, write_like_original=False)
    return buf.getvalue()


# -- defacer Protocol ------------------------------------------------------


def test_null_defacer_records_absence():
    res = NullDefacer().deface([np.zeros((10, 10))], body_part="HEAD")
    assert res.applied is False
    assert res.reason == "null_defacer_no_op"


def test_heuristic_masks_anterior_band_for_head():
    res = HeuristicFaceMasker(anterior_fraction=0.4).deface([np.zeros((40, 30))], body_part="HEAD")
    assert res.applied is True
    assert res.boxes_per_frame == [[(0, 0, 30, 16)]]


def test_heuristic_refuses_roi_body_part():
    res = HeuristicFaceMasker().deface([np.zeros((40, 30))], body_part="ORBIT")
    assert res.applied is False
    assert res.reason.startswith("face_is_roi")


def test_heuristic_refuses_ineligible_body_part():
    res = HeuristicFaceMasker().deface([np.zeros((40, 30))], body_part="CHEST")
    assert res.applied is False
    assert res.reason.startswith("body_part_not_eligible")


# -- get_defacer config resolution -----------------------------------------


def _settings(**over):
    base = {"face_deid_enabled": False, "face_deid_mode": "null"}
    base.update(over)
    return SimpleNamespace(**base)


def test_get_defacer_disabled_by_default(monkeypatch):
    monkeypatch.setattr(config_mod, "get_settings", lambda: _settings())
    assert get_defacer() is None


def test_get_defacer_null_mode(monkeypatch):
    monkeypatch.setattr(config_mod, "get_settings", lambda: _settings(face_deid_enabled=True))
    assert isinstance(get_defacer(), NullDefacer)


def test_get_defacer_heuristic_mode(monkeypatch):
    monkeypatch.setattr(
        config_mod,
        "get_settings",
        lambda: _settings(face_deid_enabled=True, face_deid_mode="heuristic"),
    )
    assert isinstance(get_defacer(), HeuristicFaceMasker)


# -- clean_pixel_data low-risk wiring --------------------------------------


def test_low_risk_classified():
    assert classify_pixel_risk(pydicom.dcmread(io.BytesIO(_low_risk_ct()))).level == "low"


def test_low_risk_no_defacer_ships_unchanged():
    src = _low_risk_ct()
    res = clean_pixel_data(src, face_defacer=None)
    assert res.out_bytes == src
    assert res.residual_suspect is False
    assert res.face_deid_reason is None


def test_low_risk_null_defacer_flags_review_unchanged_pixels():
    src = _low_risk_ct()
    res = clean_pixel_data(src, face_defacer=NullDefacer())
    assert res.out_bytes == src  # nothing masked
    assert res.residual_suspect is True  # but flagged: we did NOT remove features
    assert res.face_deid_reason == "null_defacer_no_op"


def test_low_risk_heuristic_masks_anterior_band():
    src = _low_risk_ct(fill=100, rows=40, cols=40)
    res = clean_pixel_data(src, face_defacer=HeuristicFaceMasker(anterior_fraction=0.4))
    assert res.residual_suspect is True
    assert res.face_deid_reason == "heuristic_anterior_band"
    out = pydicom.dcmread(io.BytesIO(res.out_bytes)).pixel_array
    assert int(out[:16].max()) == 0  # anterior band blacked
    assert int(out[16:].min()) == 100  # rest preserved
    assert any(r["text"] == "<face>" for r in res.redactions)


def test_low_risk_heuristic_refuses_orbit_flags_review():
    src = _low_risk_ct(body="ORBIT")
    res = clean_pixel_data(src, face_defacer=HeuristicFaceMasker())
    assert res.out_bytes == src
    assert res.residual_suspect is True
    assert res.face_deid_reason.startswith("face_is_roi")


# -- provenance (only on accept) -------------------------------------------


def test_mark_visual_features_removed_writes_113102_idempotent():
    ds = pydicom.dcmread(io.BytesIO(_low_risk_ct()))
    mark_visual_features_removed(ds)
    mark_visual_features_removed(ds)  # idempotent
    assert ds.RecognizableVisualFeatures == "NO"
    codes = [e.CodeValue for e in ds.DeidentificationMethodCodeSequence]
    assert codes.count("113102") == 1


# -- scan-text preservation mode (over_redact vs selective) ----------------


def test_classify_text_phi_keeps_only_phi_shaped():
    boxes = [
        TextBox(0, 0, 10, 8, "Rossi", 90.0),  # pure-letter -> name-shaped -> PHI
        TextBox(0, 10, 10, 8, "HR72", 90.0),  # alphanumeric clinical -> kept
    ]
    kept = classify_text_phi(boxes)
    assert [b.text for b in kept] == ["Rossi"]


def _patch_ocr(monkeypatch):
    boxes = [
        TextBox(2, 2, 12, 8, "Rossi", 90.0),
        TextBox(2, 14, 12, 8, "HR72", 90.0),
    ]
    monkeypatch.setattr(pixel_deid, "detect_text_boxes", lambda img, **k: list(boxes))


def test_over_redact_masks_all_detected(monkeypatch):
    _patch_ocr(monkeypatch)
    case = synthesize_case(seed=1, modality="US", phi_items=[])
    res = clean_pixel_data(case.dicom, selective=False, vlm_engine=None)
    texts = {r["text"] for r in res.redactions}
    assert texts == {"Rossi", "HR72"}


def test_selective_preserves_clinical_scan_text(monkeypatch):
    _patch_ocr(monkeypatch)
    case = synthesize_case(seed=1, modality="US", phi_items=[])
    res = clean_pixel_data(case.dicom, selective=True, vlm_engine=None)
    texts = {r["text"] for r in res.redactions}
    assert texts == {"Rossi"}  # HR72 (clinical) preserved


@pytest.mark.parametrize(
    "mode,expected", [("over_redact", {"Rossi", "HR72"}), ("selective", {"Rossi"})]
)
def test_redaction_mode_resolved_from_config(monkeypatch, mode, expected):
    _patch_ocr(monkeypatch)
    monkeypatch.setattr(
        config_mod, "get_settings", lambda: SimpleNamespace(pixel_deid_redaction_mode=mode)
    )
    case = synthesize_case(seed=1, modality="US", phi_items=[])
    res = clean_pixel_data(case.dicom, selective=None, vlm_engine=None)
    assert {r["text"] for r in res.redactions} == expected
