"""Unit tests for the DICOM import CLI logic.

Generates minimal in-memory DICOM datasets and points the scanner at a
tmp folder. DB / S3 integration is covered in a separate compose-based
test (not in this suite).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pydicom
import pytest
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian, generate_uid

from bvphoenix.cli.import_dicom import (
    _parse_dicom_date,
    iter_candidate_files,
    scan,
)


def _write_minimal_dicom(
    path: Path,
    *,
    study_uid: str,
    series_uid: str,
    sop_uid: str,
    modality: str = "CT",
    instance_number: int = 1,
    study_date: str = "20260401",
    body_part: str = "CHEST",
) -> None:
    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = CTImageStorage
    file_meta.MediaStorageSOPInstanceUID = sop_uid
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian

    ds = Dataset()
    ds.file_meta = file_meta

    ds.PatientName = "Test^Patient"
    ds.PatientID = "TEST123"
    ds.StudyInstanceUID = study_uid
    ds.SeriesInstanceUID = series_uid
    ds.SOPInstanceUID = sop_uid
    ds.SOPClassUID = CTImageStorage
    ds.Modality = modality
    ds.StudyDate = study_date
    ds.StudyDescription = "Thorax"
    ds.SeriesDescription = f"{modality} axial"
    ds.SeriesNumber = 1
    ds.InstanceNumber = instance_number
    ds.BodyPartExamined = body_part

    pydicom.dcmwrite(str(path), ds, enforce_file_format=True)


def test_parse_dicom_date_valid() -> None:
    assert _parse_dicom_date("20260401") == date(2026, 4, 1)


@pytest.mark.parametrize("bad", [None, "", "2026-04-01", "20261301", "not-a-date"])
def test_parse_dicom_date_invalid(bad: str | None) -> None:
    assert _parse_dicom_date(bad) is None


def test_iter_candidate_files_filters_by_suffix(tmp_path: Path) -> None:
    (tmp_path / "a.dcm").write_bytes(b"x")
    (tmp_path / "b.DICOM").write_bytes(b"x")
    (tmp_path / "c.txt").write_bytes(b"x")
    (tmp_path / "d").write_bytes(b"x")  # extensionless — allowed
    found = {p.name for p in iter_candidate_files(tmp_path, recursive=False)}
    assert found == {"a.dcm", "b.DICOM", "d"}


def test_scan_groups_series_under_study(tmp_path: Path) -> None:
    study_uid = generate_uid()
    series_a = generate_uid()
    series_b = generate_uid()

    # Series A — 3 slices
    for i in range(3):
        _write_minimal_dicom(
            tmp_path / f"a{i}.dcm",
            study_uid=study_uid,
            series_uid=series_a,
            sop_uid=generate_uid(),
            modality="CT",
            instance_number=i + 1,
        )
    # Series B — 1 slice, different modality
    _write_minimal_dicom(
        tmp_path / "b0.dcm",
        study_uid=study_uid,
        series_uid=series_b,
        sop_uid=generate_uid(),
        modality="MR",
    )
    # Noise
    (tmp_path / "README.txt").write_bytes(b"not dicom")

    studies = scan(tmp_path, recursive=False)
    assert list(studies.keys()) == [study_uid]
    study = studies[study_uid]
    assert set(study.series.keys()) == {series_a, series_b}
    assert len(study.series[series_a].instances) == 3
    assert len(study.series[series_b].instances) == 1
    assert study.study_date == date(2026, 4, 1)
    assert study.modalities == ["CT", "MR"]


def test_scan_recursive_walks_subfolders(tmp_path: Path) -> None:
    nested = tmp_path / "sub" / "deeper"
    nested.mkdir(parents=True)
    _write_minimal_dicom(
        nested / "x.dcm",
        study_uid=generate_uid(),
        series_uid=generate_uid(),
        sop_uid=generate_uid(),
    )
    assert scan(tmp_path, recursive=True)
    assert not scan(tmp_path, recursive=False)


def test_scan_skips_non_dicom_silently(tmp_path: Path) -> None:
    (tmp_path / "junk.dcm").write_bytes(b"not actually dicom")
    _write_minimal_dicom(
        tmp_path / "real.dcm",
        study_uid=generate_uid(),
        series_uid=generate_uid(),
        sop_uid=generate_uid(),
    )
    studies = scan(tmp_path, recursive=False)
    assert len(studies) == 1


def test_scan_strict_raises_on_bad_file(tmp_path: Path) -> None:
    """A file that parses but lacks UIDs should raise in strict mode;
    a non-DICOM blob may raise either at parse time or at UID check."""
    import click
    from pydicom.errors import InvalidDicomError

    (tmp_path / "junk.dcm").write_bytes(b"nope")
    with pytest.raises((InvalidDicomError, OSError, click.ClickException)):
        scan(tmp_path, recursive=False, strict=True)
