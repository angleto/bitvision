"""DICOM de-identification provenance (M0b): PS3.15 / CID 7050.

Round-trips a minimal DICOM through ``deidentify_dicom_bytes`` and asserts the
machine-readable de-identification provenance commercial systems emit.
"""

from __future__ import annotations

from io import BytesIO

import pydicom
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian, generate_uid

from bvphoenix.services.deidentify import deidentify_dicom_bytes


def _dicom_bytes(**attrs: object) -> bytes:
    ds = Dataset()
    ds.PatientName = "Rossi^Mario"
    ds.PatientID = "MRN-12345"
    ds.ReferringPhysicianName = "Bianchi^Luca"
    ds.StudyInstanceUID = generate_uid()
    ds.SeriesInstanceUID = generate_uid()
    ds.SOPInstanceUID = generate_uid()
    ds.SOPClassUID = CTImageStorage
    ds.Modality = "CT"
    for k, v in attrs.items():
        setattr(ds, k, v)
    fm = FileMetaDataset()
    fm.MediaStorageSOPClassUID = CTImageStorage
    fm.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
    fm.TransferSyntaxUID = ExplicitVRLittleEndian
    fm.ImplementationClassUID = generate_uid()
    ds.file_meta = fm
    buf = BytesIO()
    ds.save_as(buf, write_like_original=False)
    return buf.getvalue()


def test_deid_sets_patient_identity_removed():
    ds = pydicom.dcmread(BytesIO(deidentify_dicom_bytes(_dicom_bytes())))
    assert ds.PatientIdentityRemoved == "YES"


def test_deid_writes_basic_profile_code_sequence():
    ds = pydicom.dcmread(BytesIO(deidentify_dicom_bytes(_dicom_bytes())))
    seq = ds.DeidentificationMethodCodeSequence
    codes = {(item.CodeValue, item.CodingSchemeDesignator) for item in seq}
    # The Basic Profile code is always present; default options add their own
    # coded items (clean descriptors, modified dates, retain characteristics).
    assert ("113100", "DCM") in codes
    basic = next(i for i in seq if i.CodeValue == "113100")
    assert "Basic Application Confidentiality Profile" in basic.CodeMeaning


def test_deid_scrubs_patient_name():
    ds = pydicom.dcmread(BytesIO(deidentify_dicom_bytes(_dicom_bytes())))
    assert "Rossi" not in str(ds.PatientName)
    assert str(ds.PatientName).startswith("ANON")


def test_provenance_code_sequence_not_in_nested_items():
    # Provenance markers belong on the top-level dataset only; nested sequence
    # items get scrubbed but must not carry the method code sequence.
    nested = Dataset()
    nested.PatientName = "Verdi^Anna"
    out = deidentify_dicom_bytes(_dicom_bytes(RequestAttributesSequence=[nested]))
    ds = pydicom.dcmread(BytesIO(out))
    item = ds.RequestAttributesSequence[0]
    assert "DeidentificationMethodCodeSequence" not in item
