"""Tests for the DICOMDIR parser."""

from __future__ import annotations

import io

import pydicom
import pytest
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.sequence import Sequence
from pydicom.uid import ExplicitVRLittleEndian

from bvphoenix.services.dicomdir_parser import (
    DicomdirParseError,
    parse_dicomdir,
)

# Media Storage Directory Storage SOP Class UID — this is the fixed UID
# identifying a DICOMDIR file in a DICOM File-Set (PS3.4 Annex F).
_MEDIA_STORAGE_DIRECTORY_STORAGE = "1.2.840.10008.1.3.10"


def _make_record(record_type: str, **kwargs: object) -> Dataset:
    rec = Dataset()
    rec.DirectoryRecordType = record_type
    rec.OffsetOfTheNextDirectoryRecord = 0
    rec.RecordInUseFlag = 0xFFFF
    rec.OffsetOfReferencedLowerLevelDirectoryEntity = 0
    for k, v in kwargs.items():
        setattr(rec, k, v)
    return rec


def _build_dicomdir_bytes() -> bytes:
    """Build a valid DICOMDIR in memory with 2 patients, each with 1 study,
    1 series, 1 image. Offsets are left at 0 (pydicom's low-level writer
    doesn't patch them — only ``FileSet`` does), which exercises the
    parser's sequential-walk fallback.
    """
    records: list[Dataset] = []

    # Patient 1
    p1 = _make_record("PATIENT", PatientID="P001", PatientName="DOE^JOHN")
    s1 = _make_record(
        "STUDY",
        StudyInstanceUID="1.2.3.4.1",
        StudyDescription="Chest CT",
        StudyDate="20240105",
    )
    se1 = _make_record(
        "SERIES",
        SeriesInstanceUID="1.2.3.4.1.1",
        Modality="CT",
        SeriesNumber=1,
        SeriesDescription="Axial",
    )
    i1 = _make_record(
        "IMAGE",
        ReferencedFileID=["DICOM", "IMG0001", "IM000001.DCM"],
        ReferencedSOPInstanceUIDInFile="1.2.3.4.1.1.1",
        ReferencedSOPClassUIDInFile="1.2.840.10008.5.1.4.1.1.2",
        InstanceNumber=1,
    )

    # Patient 2
    p2 = _make_record("PATIENT", PatientID="P002", PatientName="SMITH^JANE")
    s2 = _make_record(
        "STUDY",
        StudyInstanceUID="1.2.3.4.2",
        StudyDescription="Brain MR",
        StudyDate="20240210",
    )
    se2 = _make_record(
        "SERIES",
        SeriesInstanceUID="1.2.3.4.2.1",
        Modality="MR",
        SeriesNumber=1,
        SeriesDescription="T1",
    )
    i2 = _make_record(
        "IMAGE",
        ReferencedFileID="DICOM\\IMG0002\\IM000001.DCM",  # backslash form
        ReferencedSOPInstanceUIDInFile="1.2.3.4.2.1.1",
        ReferencedSOPClassUIDInFile="1.2.840.10008.5.1.4.1.1.4",
        InstanceNumber=1,
    )

    records = [p1, s1, se1, i1, p2, s2, se2, i2]

    # File meta required for a Media Storage Directory file.
    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = _MEDIA_STORAGE_DIRECTORY_STORAGE
    file_meta.MediaStorageSOPInstanceUID = "1.2.3.4.9999"
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian

    ds = Dataset()
    ds.file_meta = file_meta
    ds.is_little_endian = True
    ds.is_implicit_VR = False
    ds.OffsetOfTheFirstDirectoryRecordOfTheRootDirectoryEntity = 0
    ds.OffsetOfTheLastDirectoryRecordOfTheRootDirectoryEntity = 0
    ds.FileSetConsistencyFlag = 0
    ds.DirectoryRecordSequence = Sequence(records)

    buf = io.BytesIO()
    pydicom.dcmwrite(buf, ds, enforce_file_format=True)
    return buf.getvalue()


@pytest.mark.asyncio
async def test_parse_dicomdir_two_patients() -> None:
    blob = _build_dicomdir_bytes()
    tree = await parse_dicomdir(blob)

    assert tree.source_file_size == len(blob)
    assert len(tree.patients) == 2

    # Patient order may depend on pydicom's offset writer, sort by id.
    patients = sorted(tree.patients, key=lambda p: p.patient_id or "")
    p1, p2 = patients
    assert p1.patient_id == "P001"
    assert p1.patient_name == "DOE^JOHN"
    assert p2.patient_id == "P002"

    assert len(p1.studies) == 1
    study = p1.studies[0]
    assert study.study_instance_uid == "1.2.3.4.1"
    assert study.study_description == "Chest CT"
    assert study.study_date is not None and study.study_date.isoformat() == "2024-01-05"

    assert len(study.series) == 1
    series = study.series[0]
    assert series.series_instance_uid == "1.2.3.4.1.1"
    assert series.modality == "CT"
    assert series.series_number == 1

    assert len(series.images) == 1
    img = series.images[0]
    assert img.relative_path == "DICOM/IMG0001/IM000001.DCM"
    assert img.sop_instance_uid == "1.2.3.4.1.1.1"
    assert img.instance_number == 1

    # Patient 2 used backslash path — should be normalized.
    img2 = p2.studies[0].series[0].images[0]
    assert img2.relative_path == "DICOM/IMG0002/IM000001.DCM"


@pytest.mark.asyncio
async def test_parse_dicomdir_empty_bytes() -> None:
    with pytest.raises(DicomdirParseError):
        await parse_dicomdir(b"")


@pytest.mark.asyncio
async def test_parse_dicomdir_garbage() -> None:
    with pytest.raises(DicomdirParseError):
        await parse_dicomdir(b"not a dicom file at all")


@pytest.mark.asyncio
async def test_parse_dicomdir_missing_directory_record_sequence() -> None:
    # Build a minimal DICOM file without DirectoryRecordSequence.
    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.2"
    file_meta.MediaStorageSOPInstanceUID = "1.2.3.4.5"
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian

    ds = Dataset()
    ds.file_meta = file_meta
    ds.is_little_endian = True
    ds.is_implicit_VR = False
    ds.PatientID = "X"

    buf = io.BytesIO()
    pydicom.dcmwrite(buf, ds, enforce_file_format=True)

    with pytest.raises(DicomdirParseError):
        await parse_dicomdir(buf.getvalue())
