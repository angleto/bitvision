"""``bvphoenix-public-import-pathology`` — bootstrap the OpenData
pathology (WSI) library from curated license-clean archives.

The pathology counterpart of ``bvphoenix-public-import``. Same manifest
shape, same platform-owner ownership wiring, same idempotency contract —
but a different fetch loop. A single whole-slide image is 0.5-3 GB, so
this CLI downloads **one slide at a time**, imports it, then deletes the
local file before fetching the next. That keeps scratch bounded to ~1
slide no matter how large the collection (the radiology CLI can hold a
whole subject of DICOM ZIPs because each file is small; here it cannot).

Adapters (manifest ``adapter:`` field):

* ``http`` — direct URL download. Covers the OpenSlide freely-distributable
  CC0 test data (CMU-1, …) and any TCIA-hosted SVS whose per-slide HTTPS
  URL is listed in the manifest (CPTAC / Post-NAT-BRCA, CC-BY).
* ``aws_open_data`` — anonymous (unsigned) S3 listing + download from a
  public AWS Open Data bucket. Covers CAMELYON16/17 (CC0). Bucket / region
  / prefix come from the manifest so the exact bucket name is not baked in.
* ``gdc`` — DEFERRED. TCGA diagnostic SVS via the NCI GDC are open-access
  but carry no SPDX redistribution grant; redistributing them needs written
  NCI sign-off. The adapter refuses to run with a clear message rather than
  silently ingesting unlicensed data.

Each downloaded slide is handed to
:func:`bvphoenix.services.public_pathology.import_public_pathology_slide`,
which mints/reuses the platform-owned patient and delegates to
``import_pathology_slide``. This CLI owns the fetch + the per-slide
transaction; the service owns the DB+S3 work.

Manifest schema is documented in ``infra/public_datasets/README.md``.
"""

from __future__ import annotations

import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import click
import httpx
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from bvphoenix.cli._public_http import HTTP_TIMEOUT, _http_get_with_retry
from bvphoenix.config import get_settings
from bvphoenix.services.pathology_import import storage_target
from bvphoenix.services.pathology_jobs import enqueue_tile_jobs_sync
from bvphoenix.services.public_pathology import (
    PublicPathologySource,
    completed_slide_keys_for_source,
    import_public_pathology_slide,
)

# WSI suffixes OpenSlide can open as a single file. ``.mrxs`` is excluded
# on purpose: it is a multi-file format (a .mrxs pointer + a sidecar dir),
# which the single-URL http / single-object aws adapters cannot fetch.
_SINGLE_FILE_WSI_SUFFIXES: frozenset[str] = frozenset(
    {".svs", ".ndpi", ".tif", ".tiff", ".scn", ".dcm"}
)


@dataclass
class PathologyItem:
    """One concrete slide to fetch, with its download descriptor.

    ``upstream_file_id`` is the stable per-slide key (the URL basename for
    http, the object key for aws). It is persisted in ``slide_label`` so
    the pre-download skip is exact per slide.
    """

    subject_id: str
    upstream_file_id: str
    ext: str
    stain: str | None = None
    display_name: str | None = None
    # http descriptor
    url: str | None = None
    sha256: str | None = None
    # aws_open_data descriptor
    s3_bucket: str | None = None
    s3_key: str | None = None
    s3_region: str | None = None


@dataclass
class ManifestPathologySource:
    collection: str
    adapter: str
    license_spdx: str
    license_url: str
    citation_text: str
    citation_required: bool
    stain: str | None = None
    # http: explicit per-slide entries
    slides: list[dict] = field(default_factory=list)
    # aws_open_data: bucket listing config
    s3_bucket: str | None = None
    s3_region: str = "us-east-1"
    s3_prefixes: list[str] = field(default_factory=list)
    # gdc (deferred): subject ids, recorded so the manifest documents intent
    subjects: list[str] = field(default_factory=list)


def _parse_manifest(path: Path) -> list[ManifestPathologySource]:
    import yaml

    raw = yaml.safe_load(path.read_text())
    if not isinstance(raw, dict) or "sources" not in raw:
        raise click.ClickException(f"manifest {path}: missing top-level 'sources' list")
    out: list[ManifestPathologySource] = []
    for entry in raw["sources"]:
        out.append(
            ManifestPathologySource(
                collection=entry["collection"],
                adapter=entry["adapter"],
                license_spdx=entry["license_spdx"],
                license_url=entry["license_url"],
                citation_text=entry["citation_text"],
                citation_required=bool(entry.get("citation_required", True)),
                stain=entry.get("stain"),
                slides=list(entry.get("slides") or []),
                s3_bucket=entry.get("s3_bucket"),
                s3_region=entry.get("s3_region", "us-east-1"),
                s3_prefixes=list(entry.get("s3_prefixes") or []),
                subjects=list(entry.get("subjects") or []),
            )
        )
    return out


def _ext_of(name: str) -> str:
    suffix = Path(name).suffix.lower()
    return suffix


# --------------------------------------------------------------------------
# Adapters: list the slides a source offers (cheap, no large download yet).
# --------------------------------------------------------------------------


def _list_items_http(src: ManifestPathologySource) -> list[PathologyItem]:
    items: list[PathologyItem] = []
    for s in src.slides:
        if not isinstance(s, dict) or "url" not in s or "subject_id" not in s:
            raise click.ClickException(
                f"{src.collection}: http slide entry needs 'subject_id' + 'url', got {s!r}"
            )
        url = str(s["url"])
        ext = _ext_of(url)
        if ext not in _SINGLE_FILE_WSI_SUFFIXES:
            raise click.ClickException(
                f"{src.collection}: unsupported WSI suffix {ext!r} for {url} "
                f"(allowed: {', '.join(sorted(_SINGLE_FILE_WSI_SUFFIXES))})"
            )
        items.append(
            PathologyItem(
                subject_id=str(s["subject_id"]),
                upstream_file_id=str(s.get("upstream_file_id") or Path(url).name),
                ext=ext,
                stain=s.get("stain") or src.stain,
                display_name=s.get("display_name"),
                url=url,
                sha256=s.get("sha256"),
            )
        )
    return items


def _list_items_aws(src: ManifestPathologySource) -> list[PathologyItem]:
    """List WSI objects under the configured prefixes of a public AWS bucket.

    Anonymous (unsigned) access — these are AWS Open Data buckets (e.g.
    CAMELYON). The subject id is derived from the object basename so each
    slide maps to its own platform-owned virtual patient.
    """
    import boto3
    from botocore import UNSIGNED
    from botocore.client import Config

    if not src.s3_bucket:
        raise click.ClickException(f"{src.collection}: aws_open_data needs 's3_bucket'")
    client = boto3.client(
        "s3", region_name=src.s3_region, config=Config(signature_version=UNSIGNED)
    )
    prefixes = src.s3_prefixes or [""]
    items: list[PathologyItem] = []
    paginator = client.get_paginator("list_objects_v2")
    for prefix in prefixes:
        for page in paginator.paginate(Bucket=src.s3_bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                ext = _ext_of(key)
                if ext not in _SINGLE_FILE_WSI_SUFFIXES:
                    continue
                stem = Path(key).stem
                items.append(
                    PathologyItem(
                        subject_id=stem,
                        upstream_file_id=key,
                        ext=ext,
                        stain=src.stain,
                        s3_bucket=src.s3_bucket,
                        s3_key=key,
                        s3_region=src.s3_region,
                    )
                )
    return items


def _list_items(src: ManifestPathologySource) -> list[PathologyItem]:
    if src.adapter == "http":
        return _list_items_http(src)
    if src.adapter == "aws_open_data":
        return _list_items_aws(src)
    if src.adapter == "gdc":
        raise click.ClickException(
            f"{src.collection}: the 'gdc' adapter is deferred. TCGA slides via the NCI "
            "GDC are open-access but carry no redistribution license; ingesting them to a "
            "public, commercially-intended tier needs written NCI sign-off. Remove this "
            "source or move it to the 'http' adapter once a licensed per-slide URL exists."
        )
    raise click.ClickException(f"{src.collection}: unknown adapter {src.adapter!r}")


def _download_item(item: PathologyItem, client: httpx.Client, dest: Path) -> None:
    """Fetch one slide to ``dest`` (already including the right suffix)."""
    if item.url:
        _http_get_with_retry(client, item.url, dest, what=f"{item.subject_id} {item.url}")
        if item.sha256:
            import hashlib

            h = hashlib.sha256()
            with dest.open("rb") as fh:
                for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                    h.update(chunk)
            got = h.hexdigest()
            if got != item.sha256.lower():
                raise click.ClickException(
                    f"{item.subject_id}: sha256 mismatch (manifest {item.sha256}, got {got})"
                )
    elif item.s3_bucket and item.s3_key:
        import boto3
        from botocore import UNSIGNED
        from botocore.client import Config

        s3 = boto3.client(
            "s3", region_name=item.s3_region, config=Config(signature_version=UNSIGNED)
        )
        s3.download_file(item.s3_bucket, item.s3_key, str(dest))
    else:
        raise click.ClickException(f"{item.subject_id}: item has no download descriptor")


@click.command(
    name="bvphoenix-public-import-pathology",
    help="Bootstrap the OpenData pathology (WSI) library from a curated manifest.",
)
@click.option(
    "--manifest",
    "manifest_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
)
@click.option(
    "--only",
    multiple=True,
    help="Restrict to subjects, format 'COLLECTION/SUBJECT_ID'. Repeatable. Pilot runs.",
)
@click.option(
    "--scratch-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Directory for one-slide-at-a-time downloads (default: $TMPDIR). In the K8s "
    "Job this points at the bounded emptyDir mount.",
)
@click.option(
    "--max-slides-per-subject",
    type=int,
    default=0,
    help="Safety cap on slides ingested per subject (0 = unlimited). Useful for a first pilot.",
)
@click.option("--dry-run", is_flag=True, help="Download + open but do not write to S3 or DB.")
@click.option(
    "--continue-on-error/--abort-on-error",
    default=True,
    show_default=True,
    help="Per-slide failures are logged and skipped (default) vs aborting the run.",
)
@click.option(
    "--reimport-existing",
    is_flag=True,
    help="Re-download slides already fully imported (bypasses the pre-download skip).",
)
def main(
    manifest_path: Path,
    only: tuple[str, ...],
    scratch_dir: Path | None,
    max_slides_per_subject: int,
    dry_run: bool,
    continue_on_error: bool,
    reimport_existing: bool,
) -> None:
    sources = _parse_manifest(manifest_path)
    settings = get_settings()
    storage, bucket = storage_target()
    only_set = set(only)
    engine = create_engine(settings.database_url_sync, future=True)

    scratch = Path(scratch_dir) if scratch_dir else Path(tempfile.gettempdir())
    scratch.mkdir(parents=True, exist_ok=True)

    processed = 0
    succeeded = 0
    skipped = 0
    failed: list[tuple[str, str, str]] = []
    per_subject_count: dict[str, int] = {}

    with httpx.Client(timeout=HTTP_TIMEOUT) as client:
        for src in sources:
            click.echo(f"# {src.collection} (adapter={src.adapter})")
            try:
                items = _list_items(src)
            except click.ClickException as exc:
                click.echo(f"  ✗ {exc.message}", err=True)
                failed.append((src.collection, "*", exc.message))
                if not continue_on_error:
                    raise
                continue

            # Per-(collection, subject) cache of already-ingested upstream
            # keys, so the multi-GB download is skipped before it starts.
            done_cache: dict[str, set[str]] = {}

            for item in items:
                key = f"{src.collection}/{item.subject_id}"
                if only_set and key not in only_set:
                    continue
                processed += 1

                if max_slides_per_subject and (
                    per_subject_count.get(item.subject_id, 0) >= max_slides_per_subject
                ):
                    continue

                if not reimport_existing:
                    if item.subject_id not in done_cache:
                        with Session(engine) as probe:
                            done_cache[item.subject_id] = completed_slide_keys_for_source(
                                probe, collection=src.collection, subject_id=item.subject_id
                            )
                    if item.upstream_file_id in done_cache[item.subject_id]:
                        skipped += 1
                        click.echo(f"  ✓ {key} :: {item.upstream_file_id} already imported")
                        continue

                dest = scratch / f"{item.subject_id}-{Path(item.upstream_file_id).stem}{item.ext}"
                try:
                    click.echo(f"  ↓ {key} :: {item.upstream_file_id}")
                    _download_item(item, client, dest)
                    source = PublicPathologySource(
                        collection=src.collection,
                        subject_id=item.subject_id,
                        license_spdx=src.license_spdx,
                        license_url=src.license_url,
                        citation_text=src.citation_text,
                        citation_required=src.citation_required,
                        display_name=item.display_name,
                        stain=item.stain,
                        upstream_file_id=item.upstream_file_id,
                    )
                    with Session(engine) as session:
                        result = import_public_pathology_slide(
                            session=session,
                            storage=storage,
                            bucket=bucket,
                            path=dest,
                            source=source,
                            dry_run=dry_run,
                        )
                        if not dry_run:
                            session.commit()
                    sr = result.slide_result
                    click.echo(
                        f"  ✓ patient={result.patient_id} created={result.patient_created} "
                        f"slide={sr.slide_id} new={sr.created} bytes=+{sr.bytes_uploaded}"
                    )
                    per_subject_count[item.subject_id] = (
                        per_subject_count.get(item.subject_id, 0) + 1
                    )
                    succeeded += 1
                    # Enqueue DZI tiling so the deep-zoom viewer's pyramid is
                    # built as slides land. The viewer serves pre-generated
                    # tiles and returns 409 until ``dzi_ready``; enqueuing per
                    # slide (not at end-of-run) makes a multi-day bulk import
                    # viewable progressively. Best-effort: a redis outage must
                    # not fail an otherwise-successful ingest — the backfill CLI
                    # (bvphoenix-tile-pathology --all) re-queues any slide left
                    # with dzi_ready=false.
                    if not dry_run and sr.created:
                        try:
                            enqueue_tile_jobs_sync(settings.redis_url, [str(sr.slide_id)])
                        except Exception as exc:
                            click.echo(
                                f"  warning: could not enqueue tiling for {sr.slide_id}: {exc}",
                                err=True,
                            )
                except Exception as exc:
                    click.echo(f"  ✗ FAILED {key}: {exc}", err=True)
                    failed.append((src.collection, item.subject_id, str(exc)))
                    if not continue_on_error:
                        raise
                finally:
                    # Bound scratch to ~1 slide: always drop the local file
                    # before fetching the next, even on failure.
                    dest.unlink(missing_ok=True)

    click.echo("---")
    click.echo(f"processed:  {processed}")
    click.echo(f"succeeded:  {succeeded}")
    click.echo(f"skipped:    {skipped} (already imported)")
    click.echo(f"failed:     {len(failed)}")
    if failed:
        for coll, sid, msg in failed:
            click.echo(f"  - {coll}/{sid}: {msg}")
        sys.exit(2)


if __name__ == "__main__":
    main()
