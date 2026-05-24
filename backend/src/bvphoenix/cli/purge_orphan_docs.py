"""``bvphoenix-purge-orphan-docs`` — delete documents wedged in the
wrong S3 bucket so the user can re-upload them cleanly.

Background — pre-fix bug
========================
Before this CLI was needed, ``services/bulk_ingest.py`` and
``api/documents.py`` were writing patient-document binaries to
``s3_bucket_derivatives`` while every read endpoint
(``/content``, ``/thumbnail``, etc.) reads from ``s3_bucket_raw``. The
canonical bucket per ``api/patients.py:1894`` is ``raw``. The
mismatch produced 404s on preview and grid thumbnails for every doc
ingested via the bulk pipeline.

The code is now fixed (writes go to ``raw``). But existing
``Document`` rows still point at keys that physically live in
``derivatives`` and are not visible to the readers — so the user
can't open them. Per the operator's instruction, those orphans are
to be **deleted** (not migrated) so a fresh re-upload via the
fixed pipeline produces a clean state.

What this script does
=====================
For every ``Document`` whose ``file_s3_key`` starts with
``patient-docs/``:

* If the object is present in ``s3_bucket_raw``, leave it alone.
* If it is missing from ``raw`` AND present in ``derivatives``, the
  row is an orphan from the buggy pipeline:
  - delete the S3 object in ``derivatives``;
  - delete the ``Document`` row (so re-uploading the same blob
    doesn't get blocked by the dedup hash check).
* If it is missing from both buckets, log a warning — that's an
  even older issue this script doesn't try to repair.

The script is **dry-run by default**: it prints what it would do and
exits. Pass ``--apply`` to actually mutate state. The dry-run output
is the full list, so the operator can spot-check before committing.

Idempotent: re-running ``--apply`` is a no-op once the orphans are
gone.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field

import click
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from bvphoenix.config import get_settings
from bvphoenix.db.models import Document
from bvphoenix.storage import get_s3_storage


@dataclass
class _Counts:
    scanned: int = 0
    ok_in_raw: int = 0
    orphans_in_derivatives: int = 0
    missing_from_both: int = 0
    deleted_objects: int = 0
    deleted_rows: int = 0
    delete_object_errors: list[str] = field(default_factory=list)


@click.command()
@click.option(
    "--patient-id",
    default=None,
    help="Restrict the scan to a single patient. Default: all patients.",
)
@click.option(
    "--apply",
    is_flag=True,
    default=False,
    help="Actually delete the orphan S3 objects + DB rows. Default is dry-run.",
)
def main(patient_id: str | None, apply: bool) -> None:
    """Find and (optionally) delete documents stuck in the derivatives bucket."""
    settings = get_settings()
    storage = get_s3_storage()
    raw = settings.s3_bucket_raw
    derivatives = settings.s3_bucket_derivatives

    if raw == derivatives:
        click.echo(
            f"raw bucket and derivatives bucket are identical ({raw!r}); nothing to migrate.",
            err=True,
        )
        sys.exit(0)

    engine = create_engine(settings.database_url_sync, future=True)
    counts = _Counts()

    with Session(engine, future=True) as db:
        stmt = select(Document).where(Document.file_s3_key.like("patient-docs/%"))
        if patient_id:
            stmt = stmt.where(Document.patient_id == patient_id)
        rows = db.execute(stmt).scalars().all()

        click.echo(
            f"scanning {len(rows)} document rows under patient-docs/ "
            f"(raw={raw!r}, derivatives={derivatives!r}, mode={'APPLY' if apply else 'DRY-RUN'})"
        )

        for doc in rows:
            counts.scanned += 1
            key = doc.file_s3_key
            assert key is not None  # filtered by SQL above
            in_raw = storage.object_exists(bucket=raw, key=key)
            if in_raw:
                counts.ok_in_raw += 1
                continue
            in_deriv = storage.object_exists(bucket=derivatives, key=key)
            if not in_deriv:
                counts.missing_from_both += 1
                click.echo(
                    f"  WARN doc={doc.id} title={doc.title!r} key={key!r} "
                    "missing in BOTH buckets — leaving row untouched"
                )
                continue

            counts.orphans_in_derivatives += 1
            click.echo(
                f"  ORPHAN doc={doc.id} title={doc.title!r} key={key!r} "
                "(in derivatives, not in raw)"
            )

            if not apply:
                continue

            try:
                storage.delete_object(bucket=derivatives, key=key)
                counts.deleted_objects += 1
            except Exception as exc:
                counts.delete_object_errors.append(f"{key}: {exc}")
                click.echo(
                    f"    delete s3 object failed: {exc} — "
                    "leaving DB row in place to avoid orphaning the blob",
                    err=True,
                )
                continue

            db.delete(doc)
            counts.deleted_rows += 1

        if apply:
            db.commit()

    click.echo("")
    click.echo("Summary")
    click.echo(f"  scanned                  : {counts.scanned}")
    click.echo(f"  already in raw           : {counts.ok_in_raw}")
    click.echo(f"  orphan in derivatives    : {counts.orphans_in_derivatives}")
    click.echo(f"  missing from both        : {counts.missing_from_both}")
    if apply:
        click.echo(f"  deleted s3 objects       : {counts.deleted_objects}")
        click.echo(f"  deleted db rows          : {counts.deleted_rows}")
        if counts.delete_object_errors:
            click.echo(f"  s3 delete errors         : {len(counts.delete_object_errors)}")
            for err in counts.delete_object_errors:
                click.echo(f"      {err}")
    else:
        click.echo("  (dry-run: nothing was deleted. Re-run with --apply to commit.)")


if __name__ == "__main__":
    main()
