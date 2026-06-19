"""Admin-only ingest of curated public pathology WSI into OpenData.

The pathology counterpart of :mod:`services.public_dataset`. It closes
the one gap that keeps :func:`services.pathology_import.import_pathology_slide`
from serving public data directly: that function needs a concrete
``patient_id``, but a public slide has no private patient to attach to.
This module mints (or reuses) a *platform-owned* virtual patient keyed by
``(source_collection, source_subject_id)`` and then delegates the actual
ingest to ``import_pathology_slide`` with ``tier='t4'`` / ``is_public=True``.

Architectural contract (mirrors ``services.public_dataset``):

* Caller is admin-only — the CLI ``bvphoenix-public-import-pathology`` is
  the intended entrypoint. There is no HTTP endpoint that calls this.
* Every patient and slide created here is owned by
  ``platform_owner_subject_id``, so the pathology API's visibility OR
  surfaces them read-only to every authenticated user (and anonymous
  visitors via ``is_public``) without per-grant wiring.
* Idempotent on ``(source_collection, source_subject_id, slide_uid)``:
  the partial UNIQUE on ``pathology_slides`` (migration 0005) plus the
  ``(owner_subject_id, slide_instance_uid)`` UNIQUE make a re-run a
  no-op for already-ingested slides. ``completed_slide_keys_for_source``
  lets the connector skip the multi-GB download *before* it happens.
* No PHI scrub at ingest. We trust the upstream collection is already
  de-identified (CAMELYON CC0, OpenSlide test data, CPTAC CC-BY); the
  importer never writes the slide label and records the provenance so
  the badge UI can attribute correctly. A non-anonymised payload is a
  source-selection bug, not something this service can fix at ingest.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from bvphoenix.db.models import PathologySlide, Patient
from bvphoenix.services.pathology_import import (
    PathologyImportResult,
    PathologyImportSource,
    import_pathology_slide,
)
from bvphoenix.services.permissions import get_or_create_platform_owner_subject
from bvphoenix.storage import S3Storage


@dataclass(frozen=True)
class PublicPathologySource:
    """One public slide's worth of provenance handed to the importer.

    ``collection`` is the human/machine handle ('GDC/TCGA-BRCA',
    'CAMELYON/CAMELYON16', 'OpenSlide/test-data'); ``subject_id`` is the
    upstream case/subject identifier. Together they form the patient
    idempotency key. ``upstream_file_id`` (when set, e.g. a GDC file id)
    is stored in ``slide_label`` so the pre-download skip can be precise
    per slide rather than per subject.
    """

    collection: str
    subject_id: str
    license_spdx: str
    license_url: str
    citation_text: str
    citation_required: bool = True
    display_name: str | None = None
    stain: str | None = None
    block_label: str | None = None
    upstream_file_id: str | None = None


@dataclass
class PublicPathologyResult:
    patient_id: uuid.UUID
    patient_created: bool
    slide_result: PathologyImportResult


def _find_existing_public_pathology_patient(
    session: Session, *, collection: str, subject_id: str
) -> Patient | None:
    """Look up a previously-imported public pathology patient by source key.

    The idempotency key lives on ``pathology_slides`` (partial UNIQUE on
    source_collection+source_subject_id+slide_instance_uid). A patient in
    our DB maps to one source subject, so any existing slide with matching
    source identifiers tells us the patient already exists.
    """
    slide = session.execute(
        select(PathologySlide)
        .where(PathologySlide.source_collection == collection)
        .where(PathologySlide.source_subject_id == subject_id)
        .limit(1)
    ).scalar_one_or_none()
    if slide is None or slide.patient_id is None:
        return None
    return session.execute(
        select(Patient).where(Patient.id == slide.patient_id)
    ).scalar_one_or_none()


def get_or_create_public_pathology_patient(
    session: Session,
    *,
    collection: str,
    subject_id: str,
    display_name: str | None = None,
) -> tuple[Patient, bool]:
    """Return ``(patient, created)`` for a platform-owned public pathology subject.

    Mirrors the radiology pattern but locates the existing patient through
    ``pathology_slides.source_*`` rather than ``imaging_studies.source_*``.
    The provenance is recorded in ``external_identifiers`` under a neutral
    ``urn:opendata:pathology:`` system so GDC, CAMELYON and direct-HTTP
    sources all share one shape.
    """
    owner = get_or_create_platform_owner_subject(session)
    existing = _find_existing_public_pathology_patient(
        session, collection=collection, subject_id=subject_id
    )
    if existing is not None:
        return existing, False
    patient = Patient(
        managed_by_subject_id=owner.id,
        display_name=display_name or subject_id,
        external_identifiers=[
            {
                "system": f"urn:opendata:pathology:{collection}",
                "type": "opendata-subject",
                "value": subject_id,
            }
        ],
    )
    session.add(patient)
    session.flush()
    return patient, True


def completed_slide_keys_for_source(
    session: Session, *, collection: str, subject_id: str
) -> set[str]:
    """Upstream keys already fully ingested for this source subject.

    Returns the union of ``slide_label`` (the upstream file id, e.g. a GDC
    file_id, set by the connector) and ``slide_instance_uid`` for every
    slide marked ``ingestion_complete`` under ``(collection, subject_id)``.
    The connector filters its listing against this set *before* the
    expensive multi-GB WSI download — a single SVS is 0.5-3 GB, so skipping
    pre-download is the whole point. Partially-ingested slides are excluded
    (their ``ingestion_complete`` is false) and will be re-fetched.
    """
    rows = session.execute(
        select(PathologySlide.slide_label, PathologySlide.slide_instance_uid)
        .where(PathologySlide.source_collection == collection)
        .where(PathologySlide.source_subject_id == subject_id)
        .where(PathologySlide.ingestion_complete.is_(True))
    ).all()
    keys: set[str] = set()
    for slide_label, slide_uid in rows:
        if slide_label:
            keys.add(slide_label)
        keys.add(slide_uid)
    return keys


def import_public_pathology_slide(
    *,
    session: Session,
    storage: S3Storage,
    bucket: str,
    path: Path,
    source: PublicPathologySource,
    dry_run: bool = False,
) -> PublicPathologyResult:
    """Ingest one public WSI as a CC-licensed, platform-owned slide.

    Binds (or reuses) the platform-owned patient for the source subject,
    then delegates to :func:`import_pathology_slide` with the OpenData
    flags set. The DB session is *not* committed — the CLI commits per
    slide so a long run resumes cleanly.
    """
    owner = get_or_create_platform_owner_subject(session)
    patient, patient_created = get_or_create_public_pathology_patient(
        session,
        collection=source.collection,
        subject_id=source.subject_id,
        display_name=source.display_name,
    )

    pio_source = PathologyImportSource(
        path=path,
        owner_subject_id=owner.id,
        patient_id=patient.id,
        tier="t4",
        is_public=True,
        stain=source.stain,
        block_label=source.block_label,
        # Carry the upstream file id so the per-slide pre-download skip is
        # exact. Documented convention: for public imports slide_label holds
        # the connector's upstream identifier, not a curator-supplied label.
        slide_label=source.upstream_file_id,
        source_collection=source.collection,
        source_subject_id=source.subject_id,
        license_spdx=source.license_spdx,
        license_url=source.license_url,
        citation_required=source.citation_required,
        citation_text=source.citation_text,
    )
    slide_result = import_pathology_slide(
        session=session,
        storage=storage,
        bucket=bucket,
        source=pio_source,
        dry_run=dry_run,
    )
    return PublicPathologyResult(
        patient_id=patient.id,
        patient_created=patient_created,
        slide_result=slide_result,
    )
