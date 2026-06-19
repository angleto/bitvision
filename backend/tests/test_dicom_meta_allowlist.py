"""Unit tests for the DICOM metadata allowlist (Sprint 5)."""

from __future__ import annotations

import pydicom
from pydicom.dataset import Dataset, FileDataset
from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian, generate_uid

from bvphoenix.services.dicom_meta_allowlist import (
    _PHI_TAG_NAMES,
    DICOM_META_ALLOWLIST_V1,
    extract_allowlisted,
)


def _stub_dataset() -> pydicom.Dataset:
    fm = Dataset()
    fm.MediaStorageSOPClassUID = CTImageStorage
    fm.MediaStorageSOPInstanceUID = generate_uid()
    fm.TransferSyntaxUID = ExplicitVRLittleEndian
    ds = FileDataset("phantom", {}, file_meta=fm, preamble=b"\0" * 128)
    return ds


def test_multivalue_ds_is_returned_as_list() -> None:
    ds = _stub_dataset()
    ds.PixelSpacing = [1.5, 0.75]
    ds.ImageOrientationPatient = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0]
    ds.ImagePositionPatient = [10.0, 20.0, 30.0]
    out = extract_allowlisted(ds)
    assert out["PixelSpacing"] == [1.5, 0.75]
    assert out["ImageOrientationPatient"] == [1.0, 0.0, 0.0, 0.0, 1.0, 0.0]
    assert out["ImagePositionPatient"] == [10.0, 20.0, 30.0]


def test_phi_tags_dropped() -> None:
    ds = _stub_dataset()
    ds.PatientName = "Test^Patient"
    ds.PatientID = "PID-12345"
    ds.PatientBirthDate = "19700101"
    ds.Modality = "CT"
    out = extract_allowlisted(ds)
    assert "PatientName" not in out
    assert "PatientID" not in out
    assert "PatientBirthDate" not in out
    assert out["Modality"] == "CT"


def test_unknown_version_raises() -> None:
    ds = _stub_dataset()
    ds.Modality = "CT"
    try:
        extract_allowlisted(ds, version="v999")
    except ValueError as e:
        assert "v999" in str(e)
    else:
        raise AssertionError("expected ValueError on unknown version")


def test_allowlist_is_stable() -> None:
    """The shape of the allowlist is part of the API contract; an
    accidental rename or removal should fail this test."""
    expected_subset = {
        "Modality",
        "SliceThickness",
        "PixelSpacing",
        "ImageOrientationPatient",
        "ImagePositionPatient",
        "Rows",
        "Columns",
        "RescaleSlope",
        "RescaleIntercept",
    }
    assert expected_subset.issubset(DICOM_META_ALLOWLIST_V1.keys())


def test_contrast_bolus_start_time_allowlisted() -> None:
    """ContrastBolusStartTime (0018,1042) drives contrast-phase
    classification (seconds-post-injection). It is acquisition timing,
    not patient-identifying data, so it must be allowlisted and surfaced
    — and must NOT be in the PHI denylist."""
    info = DICOM_META_ALLOWLIST_V1.get("ContrastBolusStartTime")
    assert info is not None, "ContrastBolusStartTime must be allowlisted"
    assert info["tag"] == (0x0018, 0x1042)
    assert info["vr"] == "TM"
    assert "ContrastBolusStartTime" not in _PHI_TAG_NAMES

    ds = _stub_dataset()
    ds.ContrastBolusStartTime = "081420.000"
    out = extract_allowlisted(ds)
    assert out["ContrastBolusStartTime"] == "081420.000"
