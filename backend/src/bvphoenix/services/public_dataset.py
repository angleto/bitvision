"""Admin-only ingest of curated public DICOM datasets into OpenData.

This is the missing connector ``docs/DESIGN.md:37`` references for the
public demo library. It complements ``services.publish.publish_patient_to_opendata``
(which clones an *existing* private patient with PHI scrub) by handling
the orthogonal case: data that is already de-identified upstream
(TCIA / IDC / OsiriX / MIDRC), already CC-licensed, and has no source
private patient to clone from.

Architectural contract:

* Caller is admin-only — the CLI ``bvphoenix-public-import`` is the
  intended entrypoint. There is no HTTP endpoint that calls this.
* Every patient and study created here is owned by ``platform_owner_subject_id``,
  so ``services.permissions.visible_studies_filter`` will surface them
  read-only to every authenticated user without any per-grant wiring.
* Idempotent on ``(source_collection, source_subject_id)``: re-running
  the importer with the same identifiers updates the in-place rows
  rather than spawning duplicates. The DB partial UNIQUE on those
  columns (migration 0004) is the safety net.
* No PHI scrub. We trust the upstream collection has already
  DICOM-anonymised; the importer instead *records the provenance*
  (collection, license, citation) so the badge UI can attribute
  correctly. If a caller hands us a non-anonymised payload, that is a
  source-selection bug, not something this service can fix at ingest
  time.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from bvphoenix.cli.import_dicom import ImportReport, persist_and_upload, scan
from bvphoenix.config import get_settings
from bvphoenix.db.models import ImagingStudy, Patient, Series, Subject
from bvphoenix.services.permissions import platform_owner_subject_id
from bvphoenix.storage import S3Storage


@dataclass(frozen=True)
class PublicDatasetSource:
    """A single source subject from a curated upstream collection.

    ``collection`` is the human/machine handle ('TCIA/LIDC-IDRI',
    'OsiriX/BRAINIX', 'IDC/midrc-ricord-1c'). ``subject_id`` is the
    upstream subject identifier ('LIDC-IDRI-0001', 'TCGA-LUAD-001').
    Together they form the idempotency key.

    ``display_name`` is the pseudonym shown in the bitvision UI for
    the OpenData Patient row; default is the upstream subject id, which
    is already pseudonymous by upstream contract.
    """

    collection: str
    subject_id: str
    license_spdx: str
    license_url: str
    citation_text: str
    citation_required: bool = True
    display_name: str | None = None


@dataclass
class PublicImportResult:
    patient_id: uuid.UUID
    patient_created: bool
    report: ImportReport


def _get_or_create_platform_owner_subject(session: Session) -> Subject:
    """Return the Subject row for the platform-owner sentinel.

    The row is seeded by the bootstrap migration; if it is missing
    (fresh dev DB), create it. Real prod always has it.
    """
    owner_id = platform_owner_subject_id()
    row = session.execute(select(Subject).where(Subject.id == owner_id)).scalar_one_or_none()
    if row is not None:
        return row
    row = Subject(id=owner_id, kind="system")
    session.add(row)
    session.flush()
    return row


def _find_existing_patient_for_source(
    session: Session, *, collection: str, subject_id: str
) -> Patient | None:
    """Lookup a previously-imported public patient by its source key.

    The idempotency key lives on ImagingStudy (partial UNIQUE on
    source_collection+source_subject_id+study_instance_uid). A patient
    in our DB maps to one source subject, so any existing study with
    matching source identifiers tells us the patient already exists.
    """
    study = session.execute(
        select(ImagingStudy)
        .where(ImagingStudy.source_collection == collection)
        .where(ImagingStudy.source_subject_id == subject_id)
        .limit(1)
    ).scalar_one_or_none()
    if study is None or study.patient_id is None:
        return None
    return session.execute(
        select(Patient).where(Patient.id == study.patient_id)
    ).scalar_one_or_none()


def completed_series_uids_for_source(
    session: Session, *, collection: str, subject_id: str
) -> set[str]:
    """SeriesInstanceUIDs already fully imported for this source subject.

    A series counts only when ``Series.ingestion_complete`` is true — the
    flag is set by ``persist_and_upload`` after every instance of the
    series is uploaded and persisted, so partially-imported series are
    intentionally excluded and will be re-fetched on the next run.

    The public importer uses this to skip re-downloading series it
    already holds: the per-series ``getImage`` ZIP that TCIA assembles is
    the dominant cost of a run (minutes per thin-slice CT series), and a
    full re-run of a manifest would otherwise re-fetch every previously
    imported subject before the DB-level idempotency check discards it.
    """
    rows = (
        session.execute(
            select(Series.series_instance_uid)
            .join(ImagingStudy, Series.study_id == ImagingStudy.id)
            .where(ImagingStudy.source_collection == collection)
            .where(ImagingStudy.source_subject_id == subject_id)
            .where(Series.ingestion_complete.is_(True))
        )
        .scalars()
        .all()
    )
    return set(rows)


def import_public_dataset(
    *,
    session: Session,
    storage: S3Storage,
    bucket: str,
    dicom_dir: Path,
    source: PublicDatasetSource,
    dry_run: bool = False,
) -> PublicImportResult:
    """Import a directory of DICOM files as a public, CC-licensed study set.

    The directory is expected to contain DICOM files belonging to one
    upstream subject. Multi-study, multi-series, multi-modality layouts
    are fine: the scanner groups by ``StudyInstanceUID`` /
    ``SeriesInstanceUID`` and the persistence layer emits one
    ``ImagingStudy`` per study UID.

    A single OpenData ``Patient`` row is created (or reused on re-runs)
    under ``platform_owner_subject_id``; every study from this subject
    attaches to that patient. This matches the upstream data model
    (one TCGA-LUAD-001 == one virtual patient with potentially many
    studies of different modality) and keeps the UI coherent.

    Returns a ``PublicImportResult`` with the patient id, whether it
    was newly created, and the underlying ``ImportReport`` counters.
    """
    owner = _get_or_create_platform_owner_subject(session)

    existing = _find_existing_patient_for_source(
        session, collection=source.collection, subject_id=source.subject_id
    )
    if existing is None:
        patient = Patient(
            managed_by_subject_id=owner.id,
            display_name=source.display_name or source.subject_id,
            # External identifier shape is the FHIR-style array on
            # patients.external_identifiers (JSONB). We record the
            # upstream provenance under a synthetic 'system' so a future
            # de-duplication / re-attribution pass can find these rows
            # back. There is no real-world identity to leak — the
            # subject id is the curated archive's pseudonym.
            external_identifiers=[
                {
                    "system": f"urn:tcia:collection:{source.collection}",
                    "type": "opendata-subject",
                    "value": source.subject_id,
                }
            ],
        )
        session.add(patient)
        session.flush()
        patient_created = True
    else:
        patient = existing
        patient_created = False

    studies = scan(dicom_dir, recursive=True, strict=False)
    if not studies:
        # No DICOMs found — return a zero report rather than failing,
        # so callers can iterate over a manifest and log per-subject.
        return PublicImportResult(
            patient_id=patient.id, patient_created=patient_created, report=ImportReport()
        )

    report = persist_and_upload(
        studies,
        session=session,
        storage=storage,
        bucket=bucket,
        owner=owner,
        tier="t4",
        is_public=True,
        dry_run=dry_run,
        patient_id=patient.id,
        source_collection=source.collection,
        source_subject_id=source.subject_id,
        license_spdx=source.license_spdx,
        license_url=source.license_url,
        citation_required=source.citation_required,
        citation_text=source.citation_text,
    )

    return PublicImportResult(patient_id=patient.id, patient_created=patient_created, report=report)


def storage_target() -> tuple[S3Storage, str]:
    """Resolve the (storage, bucket) pair the importer should write to.

    Centralised here so tests can monkeypatch a single helper instead
    of reaching into config. Production: writes to the raw bucket; the
    rest of the pipeline (volume packing, thumbnails) consumes from
    that bucket as usual.
    """
    from bvphoenix.storage import get_s3_storage

    settings = get_settings()
    storage = get_s3_storage()
    storage.ensure_bucket(settings.s3_bucket_raw)
    return storage, settings.s3_bucket_raw
