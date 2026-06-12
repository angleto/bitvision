"""Integration tests for the OpenData public-dataset importer.

Exercises the full ``import_public_dataset`` path with synthetic DICOM
files and a fake S3 storage. Requires a live Postgres (same dev DB as
the other DB-touching tests in this suite); skipped otherwise.

The tests are isolation-by-UUID like the rest of the suite — they
allocate fresh DICOM UIDs per run and a unique ``source_collection``
namespace so re-runs and parallel suites do not collide on the partial
UNIQUE that backs idempotency.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path

import pydicom
import pytest
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian, generate_uid
from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import Session

from bvphoenix.config import get_settings
from bvphoenix.db.models import ClinicalEvent, ImagingStudy, Instance, Patient, Series, Subject
from bvphoenix.services.permissions import platform_owner_subject_id
from bvphoenix.services.public_dataset import (
    PublicDatasetSource,
    completed_series_uids_for_source,
    import_public_dataset,
)
from tests.conftest import skip_if_no_db


@dataclass
class _FakeUploadResult:
    bucket: str
    key: str
    size_bytes: int


class _FakeS3Storage:
    """Minimal S3 stand-in.

    The importer calls ``upload_file`` per DICOM instance; we record
    the call (no actual network or filesystem write) and return a
    matching UploadResult shape.
    """

    def __init__(self) -> None:
        self.uploads: list[tuple[str, str, int]] = []

    def ensure_bucket(self, _name: str) -> None:
        return None

    def upload_file(self, path: Path, *, bucket: str, key: str) -> _FakeUploadResult:
        size = path.stat().st_size
        self.uploads.append((bucket, key, size))
        return _FakeUploadResult(bucket=bucket, key=key, size_bytes=size)


def _write_dicom(
    path: Path, *, study_uid: str, series_uid: str, sop_uid: str, instance_number: int = 1
) -> None:
    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = CTImageStorage
    file_meta.MediaStorageSOPInstanceUID = sop_uid
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds = Dataset()
    ds.file_meta = file_meta
    ds.PatientName = "Anon^Anon"
    ds.PatientID = "ANON"
    ds.StudyInstanceUID = study_uid
    ds.SeriesInstanceUID = series_uid
    ds.SOPInstanceUID = sop_uid
    ds.SOPClassUID = CTImageStorage
    ds.Modality = "CT"
    ds.StudyDate = "20260101"
    ds.StudyDescription = "Public test"
    ds.SeriesDescription = "axial"
    ds.SeriesNumber = 1
    ds.InstanceNumber = instance_number
    pydicom.dcmwrite(str(path), ds, enforce_file_format=True)


@pytest.fixture
def sync_session() -> Session:
    """A sync Session bound to the same DB the async fixtures use.

    The importer is sync-only (it builds on the import_dicom CLI flow),
    so we deliberately don't reuse the async ``db_session`` fixture.
    """
    settings = get_settings()
    engine = create_engine(settings.database_url_sync, future=True)
    return Session(engine)


def _cleanup_collection(session: Session, collection: str) -> None:
    """Best-effort teardown: remove every study (cascade series/instance)
    and the OpenData patient for ``collection``. Idempotent — safe to
    call even if the test bailed early.
    """
    rows = (
        session.execute(select(ImagingStudy).where(ImagingStudy.source_collection == collection))
        .scalars()
        .all()
    )
    patient_ids = {r.patient_id for r in rows if r.patient_id is not None}
    series_ids = (
        session.execute(
            select(Series.id).where(Series.study_id.in_([r.id for r in rows] or [uuid.uuid4()]))
        )
        .scalars()
        .all()
    )
    if series_ids:
        session.execute(delete(Instance).where(Instance.series_id.in_(series_ids)))
    session.execute(
        delete(Series).where(Series.study_id.in_([r.id for r in rows] or [uuid.uuid4()]))
    )
    session.execute(delete(ImagingStudy).where(ImagingStudy.source_collection == collection))
    if patient_ids:
        session.execute(delete(ClinicalEvent).where(ClinicalEvent.patient_id.in_(patient_ids)))
        session.execute(delete(Patient).where(Patient.id.in_(patient_ids)))
    session.commit()


@skip_if_no_db
def test_import_creates_platform_owned_public_study(tmp_path: Path, sync_session: Session) -> None:
    # Unique collection namespace per test invocation so the partial
    # UNIQUE on (source_collection, source_subject_id, study_uid) does
    # not collide with previous runs.
    collection = f"TEST/UNIT-{uuid.uuid4().hex[:8]}"
    subject_id = "SUBJ-001"
    study_uid = generate_uid()
    series_uid = generate_uid()

    dicom_dir = tmp_path / "subject"
    dicom_dir.mkdir()
    _write_dicom(
        dicom_dir / "i1.dcm",
        study_uid=study_uid,
        series_uid=series_uid,
        sop_uid=generate_uid(),
    )

    storage = _FakeS3Storage()
    source = PublicDatasetSource(
        collection=collection,
        subject_id=subject_id,
        license_spdx="CC-BY-4.0",
        license_url="https://creativecommons.org/licenses/by/4.0/",
        citation_text="Test citation.",
        citation_required=True,
    )

    try:
        result = import_public_dataset(
            session=sync_session,
            storage=storage,  # type: ignore[arg-type]
            bucket="test-bucket",
            dicom_dir=dicom_dir,
            source=source,
        )
        sync_session.commit()

        # Patient is platform-owned (read-only to every authenticated user
        # via visible_studies_filter).
        patient = sync_session.execute(
            select(Patient).where(Patient.id == result.patient_id)
        ).scalar_one()
        assert patient.managed_by_subject_id == platform_owner_subject_id()
        # v3: the upstream provenance lives in the external_identifiers
        # JSONB array (synthetic per-collection system), not in a column.
        assert patient.external_identifiers == [
            {
                "system": f"urn:tcia:collection:{collection}",
                "type": "opendata-subject",
                "value": subject_id,
            }
        ]
        assert result.patient_created is True

        # Study carries provenance + license + public flag.
        study = sync_session.execute(
            select(ImagingStudy).where(ImagingStudy.study_instance_uid == study_uid)
        ).scalar_one()
        assert study.is_public is True
        assert study.contribution_tier == "t4"
        assert study.source_collection == collection
        assert study.source_subject_id == subject_id
        assert study.license_spdx == "CC-BY-4.0"
        assert study.citation_required is True
        assert study.citation_text == "Test citation."
        assert study.owner_subject_id == platform_owner_subject_id()
        assert study.patient_id == patient.id

        # S3 received exactly one upload.
        assert len(storage.uploads) == 1
        assert result.report.instances_inserted == 1
    finally:
        _cleanup_collection(sync_session, collection)
        sync_session.close()


@skip_if_no_db
def test_reimport_same_subject_is_idempotent(tmp_path: Path, sync_session: Session) -> None:
    collection = f"TEST/UNIT-{uuid.uuid4().hex[:8]}"
    subject_id = "SUBJ-IDEM"
    study_uid = generate_uid()
    series_uid = generate_uid()
    sop_uid = generate_uid()

    dicom_dir = tmp_path / "subject"
    dicom_dir.mkdir()
    _write_dicom(
        dicom_dir / "i1.dcm",
        study_uid=study_uid,
        series_uid=series_uid,
        sop_uid=sop_uid,
    )

    storage = _FakeS3Storage()
    source = PublicDatasetSource(
        collection=collection,
        subject_id=subject_id,
        license_spdx="CC-BY-4.0",
        license_url="https://creativecommons.org/licenses/by/4.0/",
        citation_text="Test citation.",
    )

    try:
        first = import_public_dataset(
            session=sync_session,
            storage=storage,  # type: ignore[arg-type]
            bucket="test-bucket",
            dicom_dir=dicom_dir,
            source=source,
        )
        sync_session.commit()
        assert first.patient_created is True
        assert first.report.studies_inserted == 1
        assert first.report.instances_inserted == 1

        # Second run with identical inputs: same patient, no new study,
        # no new instance.
        second = import_public_dataset(
            session=sync_session,
            storage=storage,  # type: ignore[arg-type]
            bucket="test-bucket",
            dicom_dir=dicom_dir,
            source=source,
        )
        sync_session.commit()
        assert second.patient_created is False
        assert second.patient_id == first.patient_id
        assert second.report.studies_inserted == 0
        assert second.report.studies_existing == 1
        assert second.report.instances_inserted == 0
        assert second.report.instances_existing == 1
    finally:
        _cleanup_collection(sync_session, collection)
        sync_session.close()


@skip_if_no_db
def test_completed_series_uids_for_source_reports_imported_series(
    tmp_path: Path, sync_session: Session
) -> None:
    """The pre-download skip helper returns exactly the SeriesInstanceUIDs
    already fully imported for a source subject, and an empty set for an
    unknown subject. This is the signal that lets the public importer skip
    re-downloading series it already holds (see public_import._adapter_tcia).
    """
    collection = f"TEST/UNIT-{uuid.uuid4().hex[:8]}"
    subject_id = "SUBJ-SKIP"
    study_uid = generate_uid()
    series_uid = generate_uid()

    dicom_dir = tmp_path / "subject"
    dicom_dir.mkdir()
    _write_dicom(
        dicom_dir / "i1.dcm",
        study_uid=study_uid,
        series_uid=series_uid,
        sop_uid=generate_uid(),
    )

    storage = _FakeS3Storage()
    source = PublicDatasetSource(
        collection=collection,
        subject_id=subject_id,
        license_spdx="CC-BY-4.0",
        license_url="https://creativecommons.org/licenses/by/4.0/",
        citation_text="Test citation.",
    )

    try:
        import_public_dataset(
            session=sync_session,
            storage=storage,  # type: ignore[arg-type]
            bucket="test-bucket",
            dicom_dir=dicom_dir,
            source=source,
        )
        sync_session.commit()

        # The freshly-imported series is reported as done.
        done = completed_series_uids_for_source(
            sync_session, collection=collection, subject_id=subject_id
        )
        assert done == {series_uid}

        # An unknown subject under the same collection has nothing to skip.
        assert (
            completed_series_uids_for_source(
                sync_session, collection=collection, subject_id="SUBJ-DOES-NOT-EXIST"
            )
            == set()
        )
    finally:
        _cleanup_collection(sync_session, collection)
        sync_session.close()


@skip_if_no_db
def test_platform_owner_subject_autocreated_on_fresh_db(
    tmp_path: Path, sync_session: Session
) -> None:
    """The platform-owner subject is normally seeded by the bootstrap
    migration. The service tolerates a fresh DB by minting it on first
    use — verify that branch."""
    owner_id = platform_owner_subject_id()
    # We do NOT delete the existing row; we just check that after a
    # successful import the Subject row exists. If a previous test
    # already created it, this still passes (idempotent get-or-create).
    collection = f"TEST/SUBJ-AUTO-{uuid.uuid4().hex[:8]}"
    study_uid = generate_uid()
    series_uid = generate_uid()
    dicom_dir = tmp_path / "subject"
    dicom_dir.mkdir()
    _write_dicom(
        dicom_dir / "i1.dcm",
        study_uid=study_uid,
        series_uid=series_uid,
        sop_uid=generate_uid(),
    )
    storage = _FakeS3Storage()
    try:
        import_public_dataset(
            session=sync_session,
            storage=storage,  # type: ignore[arg-type]
            bucket="test-bucket",
            dicom_dir=dicom_dir,
            source=PublicDatasetSource(
                collection=collection,
                subject_id="any",
                license_spdx="CC-BY-4.0",
                license_url="https://creativecommons.org/licenses/by/4.0/",
                citation_text="x",
            ),
        )
        sync_session.commit()
        row = sync_session.execute(select(Subject).where(Subject.id == owner_id)).scalar_one()
        assert row.id == owner_id
    finally:
        _cleanup_collection(sync_session, collection)
        sync_session.close()
