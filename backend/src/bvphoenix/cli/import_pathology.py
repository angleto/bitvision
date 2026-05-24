"""``bvphoenix-import-pathology`` — bulk ingest of WSI files.

Mirrors :mod:`bvphoenix.cli.import_dicom` but targets pathology /
histology data (SVS / NDPI / OME-TIFF / DICOM-WSI).

Walks a folder (recursively by default), feeds every supported slide
to :func:`services.pathology_import.import_pathology_slide`. Each
slide becomes one ``pathology_slides`` row + one ``clinical_events``
row with ``kind='pathology_slide'``. Idempotent on the source file
SHA-256, so re-runs against the same folder do not duplicate.
"""

from __future__ import annotations

import sys
import uuid
from collections.abc import Iterable
from pathlib import Path

import click
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from bvphoenix.config import get_settings
from bvphoenix.db.models import Patient, Subject, User
from bvphoenix.services.pathology_import import (
    PathologyImportSource,
    import_pathology_slide,
    storage_target,
)

# Extensions OpenSlide can open. Anything else is skipped with a
# warning (or aborts on --strict). We deliberately *do not* try to
# auto-detect via header sniffing: the cost is real I/O, the win is
# marginal, and a misnamed file is a user error worth surfacing.
_WSI_SUFFIXES = {".svs", ".ndpi", ".tif", ".tiff", ".mrxs", ".scn", ".dcm"}

_TIERS = ("t1", "t2", "t3", "t4")


def _iter_candidate_files(root: Path, *, recursive: bool) -> Iterable[Path]:
    if root.is_file():
        yield root
        return
    iterator = root.rglob("*") if recursive else root.iterdir()
    for p in iterator:
        if p.is_file() and p.suffix.lower() in _WSI_SUFFIXES:
            yield p


from bvphoenix.cli._common import resolve_owner_by_email as _resolve_owner


@click.command(
    name="bvphoenix-import-pathology",
    help="Bulk-import whole-slide images (SVS, NDPI, OME-TIFF, DICOM-WSI) into bitvision phoenix.",
)
@click.option("--input", "input_path", type=click.Path(exists=True, path_type=Path), required=True)
@click.option("--owner-email", required=True, help="Existing user's email — will own the slides.")
@click.option(
    "--patient-id",
    type=click.UUID,
    required=True,
    help=(
        "Patient UUID to attach the slides to. Required because every "
        "pathology_slides row is patient-scoped (CASCADE FK)."
    ),
)
@click.option("--tier", type=click.Choice(_TIERS), default="t1", show_default=True)
@click.option("--public/--private", default=False, show_default=True)
@click.option(
    "--stain",
    default=None,
    help="Stain name, e.g. 'H&E', 'Ki-67', 'ER'. Stored verbatim on every imported slide.",
)
@click.option(
    "--block-label", default=None, help="Block identifier on the gross specimen (e.g. 'A2')."
)
@click.option("--slide-label", default=None, help="Slide identifier within the block (e.g. '3').")
# OpenData / public-dataset attribution. Required by the DB CHECK
# whenever --tier=t4 is requested. The CLI validates eagerly so the
# operator sees the gap before any S3 byte is uploaded.
@click.option("--source-collection", default=None, help="OpenData collection slug.")
@click.option("--source-subject-id", default=None, help="Upstream subject identifier.")
@click.option("--license-spdx", default=None, help="SPDX license code (e.g. 'CC-BY-4.0').")
@click.option("--license-url", default=None, help="Canonical URL of the license.")
@click.option("--citation-required/--no-citation-required", default=False, show_default=True)
@click.option("--citation-text", default=None, help="Suggested citation string.")
@click.option(
    "--recursive/--no-recursive", default=True, show_default=True, help="Walk subfolders."
)
@click.option("--strict", is_flag=True, help="Abort on the first unsupported / unreadable file.")
@click.option("--dry-run", is_flag=True, help="Scan + report, no uploads or DB writes.")
def main(
    input_path: Path,
    owner_email: str,
    patient_id: uuid.UUID,
    tier: str,
    public: bool,
    stain: str | None,
    block_label: str | None,
    slide_label: str | None,
    source_collection: str | None,
    source_subject_id: str | None,
    license_spdx: str | None,
    license_url: str | None,
    citation_required: bool,
    citation_text: str | None,
    recursive: bool,
    strict: bool,
    dry_run: bool,
) -> None:
    # Eager license validation: the DB CHECK ck_pathology_slides_t4_license
    # would catch this later, but we want a clean message before paying
    # the upload cost.
    if tier == "t4" and not (license_spdx and source_collection):
        raise click.ClickException(
            "--tier=t4 requires --license-spdx and --source-collection (DB CHECK enforced)."
        )

    candidates = list(_iter_candidate_files(input_path, recursive=recursive))
    if not candidates:
        click.echo("no WSI files found.", err=True)
        sys.exit(1)
    click.echo(f"found {len(candidates)} WSI file(s) to ingest")

    settings = get_settings()
    storage, bucket = storage_target()

    engine = create_engine(settings.database_url_sync, future=True)
    inserted = 0
    skipped_existing = 0
    failed: list[tuple[str, str]] = []

    with Session(engine) as session:
        owner = _resolve_owner(session, owner_email)
        patient = session.execute(
            select(Patient).where(Patient.id == patient_id)
        ).scalar_one_or_none()
        if patient is None:
            raise click.ClickException(f"no patient found with id {patient_id}")
        click.echo(
            f"owner={owner.id}  patient={patient.id}  tier={tier}  public={public}  "
            f"stain={stain or '-'}"
        )

        for idx, path in enumerate(candidates, start=1):
            click.echo(f"[{idx}/{len(candidates)}] {path}")
            source = PathologyImportSource(
                path=path,
                owner_subject_id=owner.id,
                patient_id=patient.id,
                tier=tier,
                is_public=public,
                stain=stain,
                block_label=block_label,
                slide_label=slide_label,
                source_collection=source_collection,
                source_subject_id=source_subject_id,
                license_spdx=license_spdx,
                license_url=license_url,
                citation_required=citation_required,
                citation_text=citation_text,
            )
            try:
                result = import_pathology_slide(
                    session=session,
                    storage=storage,
                    bucket=bucket,
                    source=source,
                    dry_run=dry_run,
                )
                if result.created:
                    inserted += 1
                    click.echo(
                        f"  ✓ slide_id={result.slide_id} "
                        f"event_id={result.clinical_event_id} "
                        f"uploaded={result.bytes_uploaded / 1_048_576:.1f} MiB"
                    )
                else:
                    skipped_existing += 1
                    click.echo(f"  · already imported (slide_id={result.slide_id})")
            except Exception as exc:
                click.echo(f"  ✗ FAILED: {exc}", err=True)
                failed.append((str(path), str(exc)))
                if strict:
                    raise

        if not dry_run:
            session.commit()

    click.echo("---")
    click.echo(f"inserted:  {inserted}")
    click.echo(f"existing:  {skipped_existing}")
    click.echo(f"failed:    {len(failed)}")
    if failed:
        for p, e in failed:
            click.echo(f"  - {p}: {e}", err=True)
        sys.exit(2)


if __name__ == "__main__":
    main()
