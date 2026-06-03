"""`bvphoenix-import` — bulk DICOM import CLI.

Walks a folder of DICOM files (CT / MRI / etc.), groups them by
``StudyInstanceUID`` → ``SeriesInstanceUID``, uploads each instance to S3
and inserts the corresponding rows into Postgres. Designed for admin
bootstrap and local dev: it bypasses the presigned-URL upload path that
end users will use via the web client.

One series of 200-500 slices per folder is the expected input shape.
Files that are not valid DICOM are skipped with a warning; the importer
never aborts on a single bad file unless ``--strict`` is passed.
"""

from __future__ import annotations

import hashlib
import sys
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import click
import pydicom
from pydicom.errors import InvalidDicomError
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from bvphoenix.config import get_settings
from bvphoenix.db.models import (
    ClinicalEvent,
    ImagingStudy,
    Instance,
    Patient,
    Series,
    Subject,
    User,
)
from bvphoenix.storage import S3Storage, get_s3_storage

TIERS = ("t1", "t2", "t3", "t4")

# Extensions we try to parse. DICOM files often have no extension at all,
# so we also accept extensionless files and let pydicom decide.
LIKELY_DICOM_SUFFIXES = {".dcm", ".dicom", ".ima", ""}


@dataclass
class InstanceMeta:
    path: Path
    sop_instance_uid: str
    sop_class_uid: str | None
    instance_number: int | None
    size_bytes: int
    sha256: str


@dataclass
class SeriesMeta:
    series_instance_uid: str
    series_number: int | None = None
    modality: str | None = None
    body_part_examined: str | None = None
    series_description: str | None = None
    instances: list[InstanceMeta] = field(default_factory=list)


@dataclass
class StudyMeta:
    study_instance_uid: str
    study_description: str | None = None
    study_date: date | None = None
    series: dict[str, SeriesMeta] = field(default_factory=dict)

    @property
    def modalities(self) -> list[str]:
        return sorted({s.modality for s in self.series.values() if s.modality})


def _sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _parse_dicom_date(value: str | None) -> date | None:
    """DICOM dates are YYYYMMDD strings; return None for missing / malformed."""
    if not value or len(value) != 8:
        return None
    try:
        return datetime.strptime(value, "%Y%m%d").date()
    except ValueError:
        return None


def iter_candidate_files(root: Path, *, recursive: bool) -> Iterable[Path]:
    if root.is_file():
        yield root
        return
    iterator = root.rglob("*") if recursive else root.iterdir()
    for p in iterator:
        if not p.is_file():
            continue
        if p.suffix.lower() in LIKELY_DICOM_SUFFIXES:
            yield p


def scan(root: Path, *, recursive: bool = True, strict: bool = False) -> dict[str, StudyMeta]:
    """Parse DICOM headers across a folder tree into a nested study/series
    structure. Reads only the metadata (``stop_before_pixels=True``) so a
    few thousand files fly through in seconds.
    """
    studies: dict[str, StudyMeta] = {}
    skipped = 0
    for path in iter_candidate_files(root, recursive=recursive):
        try:
            ds = pydicom.dcmread(str(path), stop_before_pixels=True, force=True)
        except (InvalidDicomError, OSError) as exc:
            if strict:
                raise
            click.echo(f"  skip (not DICOM): {path} ({exc})", err=True)
            skipped += 1
            continue

        study_uid = getattr(ds, "StudyInstanceUID", None)
        series_uid = getattr(ds, "SeriesInstanceUID", None)
        sop_uid = getattr(ds, "SOPInstanceUID", None)
        if not (study_uid and series_uid and sop_uid):
            if strict:
                raise click.ClickException(f"missing required UIDs in {path}")
            click.echo(f"  skip (missing UIDs): {path}", err=True)
            skipped += 1
            continue

        study = studies.setdefault(
            study_uid,
            StudyMeta(
                study_instance_uid=study_uid,
                study_description=getattr(ds, "StudyDescription", None),
                study_date=_parse_dicom_date(getattr(ds, "StudyDate", None)),
            ),
        )
        series = study.series.setdefault(
            series_uid,
            SeriesMeta(
                series_instance_uid=series_uid,
                series_number=_as_int(getattr(ds, "SeriesNumber", None)),
                modality=getattr(ds, "Modality", None),
                body_part_examined=getattr(ds, "BodyPartExamined", None),
                series_description=getattr(ds, "SeriesDescription", None),
            ),
        )
        series.instances.append(
            InstanceMeta(
                path=path,
                sop_instance_uid=sop_uid,
                sop_class_uid=getattr(ds, "SOPClassUID", None),
                instance_number=_as_int(getattr(ds, "InstanceNumber", None)),
                size_bytes=path.stat().st_size,
                sha256=_sha256_of(path),
            )
        )

    if skipped:
        click.echo(f"  {skipped} file(s) skipped", err=True)
    return studies


def _as_int(v: object) -> int | None:
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


from bvphoenix.cli._common import resolve_owner_by_email as _resolve_owner


def _s3_key(
    *,
    patient_id: object,
    study_id: object,
    series_id: object,
    instance_id: object,
) -> str:
    """Internal-UUID key. Mirrors ``services.dicom_ingest.s3_key_for``
    so CLI imports and HTTP imports share the same S3 layout. DICOM
    UIDs are intentionally absent from the path — they are not
    authoritative across BitVision tenants."""
    prefix = f"patients/{patient_id}" if patient_id is not None else "unassigned"
    return f"{prefix}/studies/{study_id}/series/{series_id}/instances/{instance_id}.dcm"


@dataclass
class ImportReport:
    studies_inserted: int = 0
    studies_existing: int = 0
    series_inserted: int = 0
    instances_inserted: int = 0
    instances_existing: int = 0
    bytes_uploaded: int = 0
    series_ids: list[str] = field(default_factory=list)


def persist_and_upload(
    studies: dict[str, StudyMeta],
    *,
    session: Session,
    storage: S3Storage,
    bucket: str,
    owner: Subject,
    tier: str,
    is_public: bool,
    dry_run: bool,
    patient_id: uuid.UUID | None = None,
    # Provenance & license. NULL on user uploads, populated by the
    # public-dataset importer for T4/is_public=true rows. The DB CHECK
    # in migration 0004 enforces that t4 rows have license_spdx +
    # source_collection set, so callers passing tier='t4' MUST also
    # pass at least those two; the CLI option parser is the gate.
    source_collection: str | None = None,
    source_subject_id: str | None = None,
    license_spdx: str | None = None,
    license_url: str | None = None,
    citation_required: bool = False,
    citation_text: str | None = None,
) -> ImportReport:
    report = ImportReport()
    for smeta in studies.values():
        # Scope by owner — DICOM UIDs are not globally unique in
        # real-world payloads (different sites reuse them), and the DB
        # constraint is now ``UNIQUE(owner_subject_id, study_uid)``.
        study_row = session.execute(
            select(ImagingStudy).where(
                ImagingStudy.study_instance_uid == smeta.study_instance_uid,
                ImagingStudy.owner_subject_id == owner.id,
            )
        ).scalar_one_or_none()
        if study_row is None:
            study_row = ImagingStudy(
                study_instance_uid=smeta.study_instance_uid,
                owner_subject_id=owner.id,
                contribution_tier=tier,
                is_public=is_public,
                study_description=smeta.study_description,
                study_date=smeta.study_date,
                modalities=smeta.modalities,
                patient_id=patient_id,
                source_collection=source_collection,
                source_subject_id=source_subject_id,
                license_spdx=license_spdx,
                license_url=license_url,
                citation_required=citation_required,
                citation_text=citation_text,
            )
            if not dry_run:
                session.add(study_row)
                session.flush()
            report.studies_inserted += 1
        else:
            # Pre-existing study: attach to the requested patient if
            # not already wired. Idempotent re-runs of the importer
            # against the same DVD now stick the studies on the
            # patient's fascicolo instead of leaving them orphaned.
            if patient_id is not None and getattr(study_row, "patient_id", None) is None:
                study_row.patient_id = patient_id
            report.studies_existing += 1

        # v3 invariant: every imaging study has a parent ClinicalEvent.
        # Materialise it here when the study has a patient and no event
        # yet, covering both the freshly-inserted and the pre-existing
        # rows. Skipped on dry-run (no real flush).
        if not dry_run and study_row.patient_id is not None and study_row.clinical_event_id is None:
            event_row = ClinicalEvent(
                patient_id=study_row.patient_id,
                kind="imaging_study",
                event_date=study_row.study_date,
                title=study_row.study_description or "Imaging study",
                source="imaging_ingest",
            )
            session.add(event_row)
            session.flush()
            study_row.clinical_event_id = event_row.id

        for sermeta in smeta.series.values():
            series_row = session.execute(
                select(Series).where(
                    Series.series_instance_uid == sermeta.series_instance_uid,
                    Series.study_id == (study_row.id if not dry_run else None),
                )
            ).scalar_one_or_none()
            if series_row is None:
                series_row = Series(
                    study_id=study_row.id if not dry_run else None,
                    series_instance_uid=sermeta.series_instance_uid,
                    series_number=sermeta.series_number,
                    modality=sermeta.modality,
                    body_part_examined=sermeta.body_part_examined,
                    series_description=sermeta.series_description,
                    expected_instance_count=len(sermeta.instances),
                )
                if not dry_run:
                    session.add(series_row)
                    session.flush()
                report.series_inserted += 1

            for imeta in sermeta.instances:
                existing = session.execute(
                    select(Instance).where(
                        Instance.sop_instance_uid == imeta.sop_instance_uid,
                        Instance.series_id == (series_row.id if not dry_run else None),
                    )
                ).scalar_one_or_none()
                if existing is not None:
                    report.instances_existing += 1
                    continue

                # Pre-allocate Instance.id so the key uses internal
                # UUIDs only; the same id lands on the row.
                instance_id = uuid.uuid4()
                key = _s3_key(
                    patient_id=getattr(study_row, "patient_id", None),
                    study_id=study_row.id if not dry_run else "DRYRUN-STUDY",
                    series_id=series_row.id if not dry_run else "DRYRUN-SERIES",
                    instance_id=instance_id,
                )
                if not dry_run:
                    storage.upload_file(imeta.path, bucket=bucket, key=key)
                report.bytes_uploaded += imeta.size_bytes

                if not dry_run:
                    session.add(
                        Instance(
                            id=instance_id,
                            series_id=series_row.id,
                            sop_instance_uid=imeta.sop_instance_uid,
                            sop_class_uid=imeta.sop_class_uid,
                            instance_number=imeta.instance_number,
                            s3_bucket=bucket,
                            s3_key=key,
                            size_bytes=imeta.size_bytes,
                            content_sha256=imeta.sha256,
                        )
                    )
                report.instances_inserted += 1

            if not dry_run:
                series_row.received_instance_count = len(sermeta.instances)
                series_row.ingestion_complete = True
                report.series_ids.append(str(series_row.id))

        if not dry_run:
            study_row.ingestion_complete = True
            study_row.modalities = smeta.modalities

    if not dry_run:
        session.commit()
    return report


@click.command(
    name="bvphoenix-import",
    help="Bulk-import a folder of DICOM files (CT, MRI, ...) into bitvision phoenix.",
)
@click.option("--input", "input_path", type=click.Path(exists=True, path_type=Path), required=True)
@click.option("--owner-email", required=True, help="Existing user's email — will own the studies.")
@click.option(
    "--patient-id",
    type=click.UUID,
    default=None,
    help=(
        "Optional: attach every imported study to this Patient (existence"
        " is verified up-front). Without it studies stay 'unassigned' and "
        "must be linked through the UI later."
    ),
)
@click.option("--tier", type=click.Choice(TIERS), default="t1", show_default=True)
@click.option("--public/--private", default=False, show_default=True)
@click.option(
    "--recursive/--no-recursive", default=True, show_default=True, help="Walk subfolders."
)
@click.option("--strict", is_flag=True, help="Abort on the first unreadable file.")
@click.option("--dry-run", is_flag=True, help="Parse and report, no uploads or DB writes.")
def main(
    input_path: Path,
    owner_email: str,
    patient_id: uuid.UUID | None,
    tier: str,
    public: bool,
    recursive: bool,
    strict: bool,
    dry_run: bool,
) -> None:
    click.echo(f"scanning {input_path} ...")
    studies = scan(input_path, recursive=recursive, strict=strict)
    if not studies:
        click.echo("no DICOM files found.", err=True)
        sys.exit(1)

    total_instances = sum(
        len(series.instances) for study in studies.values() for series in study.series.values()
    )
    click.echo(
        f"found {len(studies)} stud{'y' if len(studies) == 1 else 'ies'}, "
        f"{sum(len(s.series) for s in studies.values())} series, "
        f"{total_instances} instances"
    )

    settings = get_settings()
    storage = get_s3_storage()
    storage.ensure_bucket(settings.s3_bucket_raw)

    engine = create_engine(settings.database_url_sync, future=True)
    with Session(engine) as session:
        owner = _resolve_owner(session, owner_email)
        if patient_id is not None:
            patient_row = session.execute(
                select(Patient).where(Patient.id == patient_id)
            ).scalar_one_or_none()
            if patient_row is None:
                raise click.ClickException(f"no patient found with id {patient_id}")
            click.echo(f"attaching studies to patient {patient_id}")
        report = persist_and_upload(
            studies,
            session=session,
            storage=storage,
            bucket=settings.s3_bucket_raw,
            owner=owner,
            tier=tier,
            is_public=public,
            dry_run=dry_run,
            patient_id=patient_id,
        )

    click.echo("---")
    click.echo(f"studies:    +{report.studies_inserted} new, {report.studies_existing} existing")
    click.echo(f"series:     +{report.series_inserted} new")
    click.echo(
        f"instances:  +{report.instances_inserted} new, {report.instances_existing} existing"
    )
    click.echo(f"uploaded:   {report.bytes_uploaded / 1_048_576:.1f} MiB")
    if dry_run:
        click.echo("(dry-run: no changes written)")
    elif report.series_ids:
        _enqueue_pack_jobs(settings, report.series_ids)


def _enqueue_pack_jobs(settings, series_ids: list[str]) -> None:
    """Enqueue volume pre-packing AND image-embedding jobs per series.

    ``pack_volume`` builds the cached volume for every series; ``embed_series``
    builds the BiomedCLIP image vector so the study is reachable by similarity
    search (``/api/similar-to``) right after import. Without the embed enqueue,
    imported studies have no image vectors and similarity search returns
    nothing. ``embed_series`` is idempotent (skips when the vector already
    exists), so re-imports stay cheap.

    Embedding is enqueued only for diagnostic-image modalities. Non-image
    series (SR / PR / SEG) cannot be embedded and would only churn the
    worker + pollute ``embedding_errors``; their modality is looked up here
    and skipped. Single source of truth: bvphoenix.services.embeddable.
    """
    try:
        import asyncio

        from arq import create_pool

        from bvphoenix.services.arq_redis import redis_settings
        from bvphoenix.services.embeddable import is_embeddable_modality

        engine = create_engine(settings.database_url_sync, future=True)
        with Session(engine) as session:
            rows = session.execute(
                text("SELECT id::text, modality FROM series WHERE id::text = ANY(:ids)"),
                {"ids": list(series_ids)},
            ).all()
        embeddable_ids = [rid for (rid, mod) in rows if is_embeddable_modality(mod)]

        async def _enqueue() -> None:
            redis = await create_pool(redis_settings(settings.redis_url))
            for sid in series_ids:
                await redis.enqueue_job("pack_volume", sid)
            for sid in embeddable_ids:
                await redis.enqueue_job("embed_series", sid)
            await redis.close()

        asyncio.run(_enqueue())
        click.echo(
            f"enqueued {len(series_ids)} volume pack + {len(embeddable_ids)} embedding job(s)"
        )
    except Exception as exc:
        click.echo(f"warning: could not enqueue pack/embed jobs: {exc}", err=True)


if __name__ == "__main__":
    main()
