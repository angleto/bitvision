"""Burned-in pixel PHI risk classifier (M0) — truth table.

Builds in-memory pydicom datasets (header only, no pixels) and asserts the
gate decision for the modalities / SOP classes / ImageType markers the
public-egress path must block before shipping to OpenData.
"""

from __future__ import annotations

from io import BytesIO

import pydicom
import pytest
from pydicom.dataset import FileMetaDataset
from pydicom.uid import UID, ExplicitVRLittleEndian, generate_uid

from bvphoenix.services.pixel_deid import classify_pixel_risk, classify_pixel_risk_bytes

# SOP class UIDs used below.
SC_IMAGE = "1.2.840.10008.5.1.4.1.1.7"  # Secondary Capture Image Storage
SC_MULTIFRAME_GRAY = "1.2.840.10008.5.1.4.1.1.7.2"
CT_IMAGE = "1.2.840.10008.5.1.4.1.1.2"
MR_IMAGE = "1.2.840.10008.5.1.4.1.1.4"
US_IMAGE = "1.2.840.10008.5.1.4.1.1.6.1"
ENCAPSULATED_PDF = "1.2.840.10008.5.1.4.1.1.104.1"
XRAY_DOSE_SR = "1.2.840.10008.5.1.4.1.1.88.67"
BASIC_TEXT_SR = "1.2.840.10008.5.1.4.1.1.88.11"
PRES_STATE = "1.2.840.10008.5.1.4.1.1.11.1"


def _ds(**attrs: object) -> pydicom.Dataset:
    ds = pydicom.Dataset()
    for k, v in attrs.items():
        setattr(ds, k, v)
    return ds


def test_ultrasound_is_high():
    risk = classify_pixel_risk(_ds(Modality="US", SOPClassUID=US_IMAGE))
    assert risk.level == "high"
    assert "high_risk_modality:US" in risk.reasons


def test_secondary_capture_sop_is_high():
    for sop in (SC_IMAGE, SC_MULTIFRAME_GRAY):
        risk = classify_pixel_risk(_ds(Modality="OT", SOPClassUID=sop))
        assert risk.level == "high"
        assert "secondary_capture_sop" in risk.reasons


def test_encapsulated_pdf_is_high():
    risk = classify_pixel_risk(_ds(Modality="DOC", SOPClassUID=ENCAPSULATED_PDF))
    assert risk.level == "high"
    assert "encapsulated_document_sop" in risk.reasons


def test_xray_dose_report_sr_is_high():
    risk = classify_pixel_risk(_ds(Modality="SR", SOPClassUID=XRAY_DOSE_SR))
    assert risk.level == "high"
    assert "xray_dose_report_sr" in risk.reasons


def test_burned_in_annotation_yes_forces_high_even_on_ct():
    risk = classify_pixel_risk(_ds(Modality="CT", SOPClassUID=CT_IMAGE, BurnedInAnnotation="YES"))
    assert risk.level == "high"
    assert "burned_in_annotation_yes" in risk.reasons
    assert risk.burned_in_annotation == "YES"


def test_burned_in_annotation_no_is_not_trusted_on_ultrasound():
    # Vendors set "NO" on US frames that plainly carry text; never downgrade.
    risk = classify_pixel_risk(_ds(Modality="US", SOPClassUID=US_IMAGE, BurnedInAnnotation="NO"))
    assert risk.level == "high"


def test_image_type_secondary_is_high():
    risk = classify_pixel_risk(
        _ds(Modality="CT", SOPClassUID=CT_IMAGE, ImageType=["DERIVED", "SECONDARY"])
    )
    assert risk.level == "high"
    assert "image_type_secondary" in risk.reasons


def test_image_type_screen_capture_is_high():
    risk = classify_pixel_risk(
        _ds(Modality="OT", SOPClassUID=CT_IMAGE, ImageType=["DERIVED", "SCREEN SAVE"])
    )
    assert risk.level == "high"
    assert "image_type_screen_capture" in risk.reasons


def test_plain_derived_ct_reformat_is_not_high():
    # DERIVED\PRIMARY (a legitimate reformat/MIP) must not be flagged high or
    # the gate would dump every cross-sectional reformat into review.
    risk = classify_pixel_risk(
        _ds(Modality="CT", SOPClassUID=CT_IMAGE, ImageType=["DERIVED", "PRIMARY"])
    )
    assert risk.level == "none"


def test_ct_chest_primary_is_none():
    risk = classify_pixel_risk(
        _ds(
            Modality="CT",
            SOPClassUID=CT_IMAGE,
            ImageType=["ORIGINAL", "PRIMARY"],
            BodyPartExamined="CHEST",
        )
    )
    assert risk.level == "none"


def test_ct_head_is_low_face_risk():
    risk = classify_pixel_risk(_ds(Modality="CT", SOPClassUID=CT_IMAGE, BodyPartExamined="HEAD"))
    assert risk.level == "low"
    assert any(r.startswith("face_region:") for r in risk.reasons)


def test_overlay_plane_makes_cross_sectional_high():
    # A CT with an overlay plane (group 6000) carries a rendered annotation/PHI
    # carrier → high, even though it would otherwise classify none.
    from pydicom.tag import Tag

    ds = _ds(Modality="CT", SOPClassUID=CT_IMAGE, BodyPartExamined="CHEST")
    ds.add_new(Tag(0x6000, 0x0010), "US", 512)  # OverlayRows — overlay plane present
    risk = classify_pixel_risk(ds)
    assert risk.level == "high"
    assert "overlay_plane_present" in risk.reasons


def test_mr_brain_with_recognizable_features_is_low():
    risk = classify_pixel_risk(
        _ds(Modality="MR", SOPClassUID=MR_IMAGE, RecognizableVisualFeatures="YES")
    )
    assert risk.level == "low"
    assert "recognizable_visual_features_yes" in risk.reasons


def test_presentation_state_is_none():
    risk = classify_pixel_risk(_ds(Modality="PR", SOPClassUID=PRES_STATE))
    assert risk.level == "none"
    assert "no_pixel_data_sop" in risk.reasons


def test_basic_text_sr_is_none_for_pixel_risk():
    # Generic SR text PHI is the header engine's concern (RequiresReview),
    # not the burned-in *pixel* gate.
    risk = classify_pixel_risk(_ds(Modality="SR", SOPClassUID=BASIC_TEXT_SR))
    assert risk.level == "none"


def test_empty_dataset_is_none():
    assert classify_pixel_risk(pydicom.Dataset()).level == "none"


@pytest.mark.parametrize("modality", ["US", "SC", "OT", "XC", "ECG", "IO", "GM", "ES", "OP"])
def test_all_high_risk_modalities(modality):
    assert classify_pixel_risk(_ds(Modality=modality)).level == "high"


# ---- classify_pixel_risk_bytes (the public-egress gate's decision fn) -------


def _dicom_bytes(sop_class: str, **attrs: object) -> bytes:
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


def test_classify_bytes_ultrasound_is_high():
    blob = _dicom_bytes(US_IMAGE, Modality="US")
    risk = classify_pixel_risk_bytes(blob)
    assert risk.is_high


def test_classify_bytes_ct_chest_is_not_high():
    blob = _dicom_bytes(
        CT_IMAGE, Modality="CT", ImageType=["ORIGINAL", "PRIMARY"], BodyPartExamined="CHEST"
    )
    assert classify_pixel_risk_bytes(blob).level == "none"
