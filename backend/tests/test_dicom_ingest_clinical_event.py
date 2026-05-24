"""DicomIngestor materialises the parent ClinicalEvent on finalize.

v3 invariant: every imaging study is the imaging projection of a
``ClinicalEvent`` (kind='imaging_study', 1:1 via
``imaging_studies.clinical_event_id``). Pre-fix the ingestor created
the ImagingStudy without the parent event; this test pins the new
behaviour so a future regression cannot silently re-introduce orphan
imaging rows (which is what hid every patient bulk-imported before
2026-05-03 from the timeline).
"""

from __future__ import annotations

import io
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import BinaryIO

import pydicom
import pytest
import pytest_asyncio
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian, generate_uid
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.db.models import ClinicalEvent, ImagingStudy, Patient, Subject, User
from bvphoenix.services.dicom_ingest import DicomIngestor, ensure_imaging_event
from tests.conftest import skip_if_no_db

pytestmark = [pytest.mark.asyncio, skip_if_no_db]


@dataclass
class _UploadResult:
    bucket: str
    key: str
    size_bytes: int


class _RecordingStorage:
    """In-memory stub that satisfies the ingestor's storage contract.

    Captures every (bucket, key, body) tuple so a test can assert on
    keys when needed. Mirrors :class:`bvphoenix.storage.S3Storage`'s
    ``upload_bytes`` signature; nothing else from the ingestor is
    invoked in this test.
    """

    def __init__(self) -> None:
        self.uploads: list[tuple[str, str, int]] = []

    def upload_bytes(self, data: bytes | BinaryIO, *, bucket: str, key: str) -> _UploadResult:
        body = data.read() if hasattr(data, "read") else data
        assert isinstance(body, bytes)
        self.uploads.append((bucket, key, len(body)))
        return _UploadResult(bucket=bucket, key=key, size_bytes=len(body))


def _build_dicom_blob(
    *,
    study_uid: str,
    series_uid: str,
    sop_uid: str,
    study_description: str = "Thorax",
    study_date: str = "20260401",
) -> bytes:
    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = CTImageStorage
    file_meta.MediaStorageSOPInstanceUID = sop_uid
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds = Dataset()
    ds.file_meta = file_meta
    ds.PatientName = "Test^Patient"
    ds.PatientID = "TEST"
    ds.StudyInstanceUID = study_uid
    ds.SeriesInstanceUID = series_uid
    ds.SOPInstanceUID = sop_uid
    ds.SOPClassUID = CTImageStorage
    ds.Modality = "CT"
    ds.StudyDate = study_date
    ds.StudyDescription = study_description
    ds.SeriesDescription = "CT axial"
    ds.SeriesNumber = 1
    ds.InstanceNumber = 1
    ds.BodyPartExamined = "CHEST"
    buf = io.BytesIO()
    pydicom.dcmwrite(buf, ds, enforce_file_format=True)
    return buf.getvalue()


@pytest_asyncio.fixture
async def owner(
    db_session: AsyncSession, make_user: Callable[..., Awaitable[User]]
) -> tuple[User, Subject]:
    """Returns (User, Subject) — DicomIngestor takes the Subject as
    ``owner``, the rest of the test acts via the User."""
    user = await make_user(email=f"owner-{uuid.uuid4()}@example.com")
    subject = (
        await db_session.execute(select(Subject).where(Subject.id == user.subject_id))
    ).scalar_one()
    return user, subject


async def test_finalize_creates_clinical_event_for_assigned_patient(
    db_session: AsyncSession, owner: tuple[User, Subject]
) -> None:
    """Happy path: ingest one DICOM, assign patient, finalize.

    Expectation: a single ``clinical_events`` row is created with
    ``kind='imaging_study'`` and ``imaging_studies.clinical_event_id``
    points at it. The event picks up the study's date and description.
    """
    user, subject = owner
    patient = Patient(
        id=uuid.uuid4(),
        managed_by_subject_id=user.subject_id,
        display_name="Test Patient",
    )
    db_session.add(patient)
    await db_session.flush()

    storage = _RecordingStorage()
    ingestor = DicomIngestor(
        db=db_session,
        storage=storage,  # type: ignore[arg-type]
        bucket="test-bucket",
        owner=subject,
    )

    blob = _build_dicom_blob(
        study_uid=generate_uid(),
        series_uid=generate_uid(),
        sop_uid=generate_uid(),
        study_description="CT thorax with contrast",
    )
    await ingestor.ingest_blob(blob)

    # Bulk-ingest assigns patient_id post-hoc; we replay that here.
    for study in ingestor.touched_studies.values():
        study.patient_id = patient.id

    await ingestor.finalize()
    await db_session.commit()

    studies = (
        (
            await db_session.execute(
                select(ImagingStudy).where(ImagingStudy.patient_id == patient.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(studies) == 1, "exactly one imaging study should land for this patient"
    study = studies[0]
    assert study.clinical_event_id is not None, (
        "finalize() must materialise the parent clinical_event"
    )

    events = (
        (
            await db_session.execute(
                select(ClinicalEvent).where(ClinicalEvent.patient_id == patient.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(events) == 1, "exactly one clinical_event should be created"
    event = events[0]
    assert event.id == study.clinical_event_id
    assert event.kind == "imaging_study"
    assert event.title == "CT thorax with contrast"

    # Cleanup — composite cascade runs imaging→event when the study is
    # deleted; we drop the patient last so the FK chain unwinds cleanly.
    await db_session.delete(study)
    await db_session.delete(event)
    await db_session.delete(patient)
    await db_session.commit()


async def test_finalize_skips_orphan_studies(
    db_session: AsyncSession, owner: tuple[User, Subject]
) -> None:
    """Orphan studies (no patient) cannot get a ClinicalEvent because
    ``clinical_events.patient_id`` is NOT NULL. ``finalize()`` must
    leave them as-is and not raise."""
    _user, subject = owner
    storage = _RecordingStorage()
    ingestor = DicomIngestor(
        db=db_session,
        storage=storage,  # type: ignore[arg-type]
        bucket="test-bucket",
        owner=subject,
    )
    blob = _build_dicom_blob(
        study_uid=generate_uid(),
        series_uid=generate_uid(),
        sop_uid=generate_uid(),
    )
    await ingestor.ingest_blob(blob)
    # Skip the patient assignment — leave the study orphan.

    await ingestor.finalize()
    await db_session.commit()

    study = next(iter(ingestor.touched_studies.values()))
    assert study.patient_id is None
    assert study.clinical_event_id is None, (
        "orphan studies must not get an event (would violate the NOT NULL on patient_id)"
    )

    await db_session.delete(study)
    await db_session.commit()


async def test_ensure_imaging_event_idempotent(
    db_session: AsyncSession, owner: tuple[User, Subject]
) -> None:
    """``ensure_imaging_event`` is the helper the orphan-assignment
    path calls when an existing study gets linked to a patient.
    Calling it twice on a study that already has an event must return
    the existing event, not duplicate it."""
    user, _subject = owner
    patient = Patient(
        id=uuid.uuid4(),
        managed_by_subject_id=user.subject_id,
        display_name="Test Patient",
    )
    db_session.add(patient)
    await db_session.flush()

    study = ImagingStudy(
        id=uuid.uuid4(),
        patient_id=patient.id,
        study_instance_uid=f"1.2.840.{uuid.uuid4().int}"[:64],
        owner_subject_id=user.subject_id,
        modalities=["CT"],
        study_description="CT thorax",
    )
    db_session.add(study)
    await db_session.flush()

    first = await ensure_imaging_event(db_session, study)
    assert first is not None
    second = await ensure_imaging_event(db_session, study)
    assert second is not None
    assert first.id == second.id, "idempotent: second call returns the existing event"

    rows = (
        (
            await db_session.execute(
                select(ClinicalEvent).where(ClinicalEvent.patient_id == patient.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1, "no duplicate event from the second call"

    await db_session.delete(study)
    await db_session.delete(first)
    await db_session.delete(patient)
    await db_session.commit()
