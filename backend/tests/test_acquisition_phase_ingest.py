"""Phase 0 of the multiphase contrast-CT viewer: acquisition timing &
contrast-phase persistence.

Two layers:
* Pure unit tests for ``_parse_dicom_time`` (DICOM TM parsing) — no DB.
* DB-backed tests that the ingest path persists AcquisitionTime /
  ContrastBolusAgent / ContrastBolusStartTime onto the series row, and
  that the ``acquisition_phase`` / ``phase_source`` CHECK constraints
  reject out-of-vocabulary values.
"""

from __future__ import annotations

import io
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import time
from typing import BinaryIO

import pydicom
import pytest
import pytest_asyncio
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian, generate_uid
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.db.models import ImagingStudy, Patient, Series, Subject, User
from bvphoenix.services.dicom_ingest import DicomIngestor, _parse_dicom_time
from tests.conftest import skip_if_no_db

# ---- pure: DICOM TM parsing -------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("120000.000", time(12, 0, 0, 0)),
        ("143025.500000", time(14, 30, 25, 500000)),
        ("143025.5", time(14, 30, 25, 500000)),
        ("0930", time(9, 30, 0, 0)),
        ("08", time(8, 0, 0, 0)),
        ("235959", time(23, 59, 59, 0)),
        ("12:00:00", time(12, 0, 0, 0)),  # legacy vendor separator
        ("", None),
        (None, None),
        ("250000", None),  # hour out of range
        ("126500", None),  # minute out of range
        ("1", None),  # too short
        ("abcdef", None),  # non-numeric
    ],
)
def test_parse_dicom_time(raw: str | None, expected: time | None) -> None:
    assert _parse_dicom_time(raw) == expected


def test_parse_dicom_time_accepts_pydicom_tm() -> None:
    """``getattr(ds, 'AcquisitionTime')`` returns a pydicom TM valuerep,
    not a str — ``_parse_dicom_time`` must handle it via ``str()``."""
    ds = pydicom.Dataset()
    ds.AcquisitionTime = "081530.250000"
    assert _parse_dicom_time(ds.AcquisitionTime) == time(8, 15, 30, 250000)


# ---- DB-backed: ingest persistence + constraints ----------------------


def _build_blob(
    *,
    study_uid: str,
    series_uid: str,
    sop_uid: str,
    acquisition_time: str | None = "081500.000",
    contrast_agent: str | None = "Iohexol 350",
    bolus_start_time: str | None = "081420.000",
) -> bytes:
    fm = FileMetaDataset()
    fm.MediaStorageSOPClassUID = CTImageStorage
    fm.MediaStorageSOPInstanceUID = sop_uid
    fm.TransferSyntaxUID = ExplicitVRLittleEndian
    ds = Dataset()
    ds.file_meta = fm
    ds.PatientName = "Test^Patient"
    ds.PatientID = "TEST"
    ds.StudyInstanceUID = study_uid
    ds.SeriesInstanceUID = series_uid
    ds.SOPInstanceUID = sop_uid
    ds.SOPClassUID = CTImageStorage
    ds.Modality = "CT"
    ds.StudyDate = "20260401"
    ds.SeriesDescription = "CT abdomen portal venous"
    ds.SeriesNumber = 3
    ds.InstanceNumber = 1
    ds.BodyPartExamined = "ABDOMEN"
    if acquisition_time is not None:
        ds.AcquisitionTime = acquisition_time
    if contrast_agent is not None:
        ds.ContrastBolusAgent = contrast_agent
    if bolus_start_time is not None:
        ds.ContrastBolusStartTime = bolus_start_time
    buf = io.BytesIO()
    pydicom.dcmwrite(buf, ds, enforce_file_format=True)
    return buf.getvalue()


@dataclass
class _UploadResult:
    bucket: str
    key: str
    size_bytes: int


class _RecordingStorage:
    def __init__(self) -> None:
        self.uploads: list[tuple[str, str, int]] = []

    def upload_bytes(self, data: bytes | BinaryIO, *, bucket: str, key: str) -> _UploadResult:
        body = data.read() if hasattr(data, "read") else data
        assert isinstance(body, bytes)
        self.uploads.append((bucket, key, len(body)))
        return _UploadResult(bucket=bucket, key=key, size_bytes=len(body))


@pytest_asyncio.fixture
async def owner(
    db_session: AsyncSession, make_user: Callable[..., Awaitable[User]]
) -> tuple[User, Subject]:
    user = await make_user(email=f"owner-{uuid.uuid4()}@example.com")
    subject = (
        await db_session.execute(select(Subject).where(Subject.id == user.subject_id))
    ).scalar_one()
    return user, subject


@skip_if_no_db
@pytest.mark.asyncio
async def test_ingest_persists_acquisition_timing(
    db_session: AsyncSession, owner: tuple[User, Subject]
) -> None:
    _user, subject = owner
    ingestor = DicomIngestor(
        db=db_session,
        storage=_RecordingStorage(),  # type: ignore[arg-type]
        bucket="test-bucket",
        owner=subject,
    )
    series_uid = generate_uid()
    await ingestor.ingest_blob(
        _build_blob(study_uid=generate_uid(), series_uid=series_uid, sop_uid=generate_uid())
    )
    await db_session.flush()

    series = (
        await db_session.execute(select(Series).where(Series.series_instance_uid == series_uid))
    ).scalar_one()
    assert series.acquisition_time_of_day == time(8, 15, 0, 0)
    assert series.contrast_bolus_agent == "Iohexol 350"
    assert series.contrast_bolus_start_time == time(8, 14, 20, 0)
    # Classification is a later phase — ingest leaves the phase unset.
    assert series.acquisition_phase is None
    assert series.phase_source is None

    study = next(iter(ingestor.touched_studies.values()))
    await db_session.delete(series)
    await db_session.delete(study)
    await db_session.commit()


@skip_if_no_db
@pytest.mark.asyncio
async def test_ingest_unenhanced_has_no_contrast_agent(
    db_session: AsyncSession, owner: tuple[User, Subject]
) -> None:
    _user, subject = owner
    ingestor = DicomIngestor(
        db=db_session,
        storage=_RecordingStorage(),  # type: ignore[arg-type]
        bucket="test-bucket",
        owner=subject,
    )
    series_uid = generate_uid()
    await ingestor.ingest_blob(
        _build_blob(
            study_uid=generate_uid(),
            series_uid=series_uid,
            sop_uid=generate_uid(),
            contrast_agent=None,
            bolus_start_time=None,
        )
    )
    await db_session.flush()
    series = (
        await db_session.execute(select(Series).where(Series.series_instance_uid == series_uid))
    ).scalar_one()
    assert series.contrast_bolus_agent is None
    assert series.contrast_bolus_start_time is None
    assert series.acquisition_time_of_day == time(8, 15, 0, 0)

    study = next(iter(ingestor.touched_studies.values()))
    await db_session.delete(series)
    await db_session.delete(study)
    await db_session.commit()


async def _make_series(db_session: AsyncSession, owner: tuple[User, Subject]) -> Series:
    user, _subject = owner
    patient = Patient(
        id=uuid.uuid4(),
        managed_by_subject_id=user.subject_id,
        display_name="Constraint Patient",
    )
    db_session.add(patient)
    await db_session.flush()
    study = ImagingStudy(
        id=uuid.uuid4(),
        patient_id=patient.id,
        study_instance_uid=f"1.2.840.{uuid.uuid4().int}"[:64],
        owner_subject_id=user.subject_id,
        modalities=["CT"],
        study_description="CT abdomen",
    )
    db_session.add(study)
    await db_session.flush()
    series = Series(
        id=uuid.uuid4(),
        study_id=study.id,
        series_instance_uid=generate_uid(),
        modality="CT",
    )
    db_session.add(series)
    await db_session.flush()
    return series


@skip_if_no_db
@pytest.mark.asyncio
async def test_acquisition_phase_accepts_vocabulary(
    db_session: AsyncSession, owner: tuple[User, Subject]
) -> None:
    series = await _make_series(db_session, owner)
    series.acquisition_phase = "portal_venous"
    series.phase_source = "auto"
    series.phase_confidence = 0.82
    await db_session.flush()
    await db_session.refresh(series)
    assert series.acquisition_phase == "portal_venous"
    assert series.phase_confidence == pytest.approx(0.82)
    # Discard the patient/study/series so the make_user teardown can drop
    # the owning subject without tripping an FK (these rows reference it).
    await db_session.rollback()


@skip_if_no_db
@pytest.mark.asyncio
async def test_acquisition_phase_rejects_unknown_value(
    db_session: AsyncSession, owner: tuple[User, Subject]
) -> None:
    series = await _make_series(db_session, owner)
    series.acquisition_phase = "venous"  # not in ACQUISITION_PHASES
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


@skip_if_no_db
@pytest.mark.asyncio
async def test_phase_confidence_range_enforced(
    db_session: AsyncSession, owner: tuple[User, Subject]
) -> None:
    series = await _make_series(db_session, owner)
    series.acquisition_phase = "arterial"
    series.phase_source = "auto"
    series.phase_confidence = 1.5  # out of [0,1]
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()
