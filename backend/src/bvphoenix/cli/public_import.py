"""``bvphoenix-public-import`` — bootstrap the OpenData demo library
from curated public DICOM archives (TCIA, OsiriX).

Two adapters cover the supported sources:

* ``tcia`` — REST against ``services.cancerimagingarchive.net``. Used
  for TCIA-hosted collections (TCGA-*, LIDC-IDRI, QIN-*, MIDRC-RICORD,
  COVID-19-AR, etc.). One ZIP per series, expanded to a flat dir.
* ``osirix_zip`` — direct HTTP ZIP download. Used for the Pixmeo
  educational samples (BRAINIX, MANIX, PHENIX, MAGIX) and any other
  vendor that ships a single ZIP per subject.

Both adapters write to a temp dir, then the per-subject directory is
handed to :func:`bvphoenix.services.public_dataset.import_public_dataset`,
which is responsible for the DB+S3 work and the platform-owner
ownership wiring. This CLI never touches the DB directly.

Invocation:

    bvphoenix-public-import --manifest infra/public_datasets/manifest.yaml
    bvphoenix-public-import --manifest infra/public_datasets/manifest.yaml \\
        --only TCIA/LIDC-IDRI/LIDC-IDRI-0001  # pilot, single subject

Manifest schema is documented in ``infra/public_datasets/README.md``.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode

import click
import httpx
import yaml
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from bvphoenix.cli._public_http import (
    HTTP_TIMEOUT,
    _http_get_json_with_retry,
    _http_get_with_retry,
)
from bvphoenix.cli.import_dicom import _enqueue_pack_jobs
from bvphoenix.config import get_settings
from bvphoenix.services.public_dataset import (
    PublicDatasetSource,
    completed_series_uids_for_source,
    import_public_dataset,
    storage_target,
)

# TCIA REST: use the NBIA v1 API. The legacy ``services/v4/TCIA/query``
# endpoint was decommissioned (~2024-2025) and now answers with TCP
# resets / 30s+ silence on getSeries and "Server disconnected without
# sending a response" on getImage — confirmed by direct curl probe
# from outside the cluster, so it is the API that is gone, not our
# egress. The NBIA v1 endpoint is the supported public successor and
# returns the same parameter shape (Collection + PatientID for
# getSeries, SeriesInstanceUID for getImage) plus JSON-by-default
# without needing ``format=json``. See nbia.cancerimagingarchive.net.
TCIA_REST_BASE = "https://services.cancerimagingarchive.net/nbia-api/services/v1"

# HTTP retry policy + streaming download helpers live in ``_public_http``
# so the pathology public-import CLI shares the exact same bounded-retry
# contract. The functions are imported at module top so the existing
# ``monkeypatch.setattr(public_import, "_http_get_json_with_retry", ...)``
# in the adapter tests keeps patching the name the adapters resolve.


@dataclass
class ManifestSubject:
    """One subject entry from a manifest source block.

    ``identifier`` is the upstream subject id used in the adapter call
    (TCIA PatientID, or a free-form id for osirix_zip). ``url`` is
    adapter-specific: required for osirix_zip, ignored by tcia.
    ``display_name`` overrides the default pseudonym shown in UI.
    """

    identifier: str
    url: str | None = None
    display_name: str | None = None


@dataclass
class ManifestSource:
    collection: str
    adapter: str
    license_spdx: str
    license_url: str
    citation_text: str
    citation_required: bool
    subjects: list[ManifestSubject]
    # ``subjects: all`` in the manifest sets this; the main loop then
    # enumerates every PatientID in the collection via NBIA getPatient
    # before iterating (whole-collection ingest for the maximal wave).
    all_subjects: bool = False
    # Series whose BodyPartExamined (upper-cased) is in this set are
    # dropped before download. Used to exclude the NIH-Controlled
    # head/face series bundled in otherwise-CC-BY collections (CMB-LCA /
    # CMB-CRC), which are not redistributable.
    exclude_body_parts: frozenset[str] = frozenset()


def _parse_manifest(path: Path) -> list[ManifestSource]:
    raw = yaml.safe_load(path.read_text())
    if not isinstance(raw, dict) or "sources" not in raw:
        raise click.ClickException(f"manifest {path}: missing top-level 'sources' list")
    sources: list[ManifestSource] = []
    for entry in raw["sources"]:
        subjects_raw = entry.get("subjects") or []
        all_subjects = False
        subjects: list[ManifestSubject] = []
        # ``subjects: all`` (scalar string) requests whole-collection
        # enumeration; the explicit-list form stays a list as before.
        if isinstance(subjects_raw, str):
            if subjects_raw.strip().lower() != "all":
                raise click.ClickException(
                    f"manifest: scalar 'subjects' must be 'all', got {subjects_raw!r}"
                )
            all_subjects = True
        else:
            for s in subjects_raw:
                if isinstance(s, str):
                    subjects.append(ManifestSubject(identifier=s))
                elif isinstance(s, dict):
                    subjects.append(
                        ManifestSubject(
                            identifier=s["id"],
                            url=s.get("url"),
                            display_name=s.get("display_name"),
                        )
                    )
                else:
                    raise click.ClickException(f"manifest: bad subject entry {s!r}")
        exclude_body_parts = frozenset(
            str(b).strip().upper() for b in (entry.get("exclude_body_parts") or [])
        )
        sources.append(
            ManifestSource(
                collection=entry["collection"],
                adapter=entry["adapter"],
                license_spdx=entry["license_spdx"],
                license_url=entry["license_url"],
                citation_text=entry["citation_text"],
                citation_required=bool(entry.get("citation_required", True)),
                subjects=subjects,
                all_subjects=all_subjects,
                exclude_body_parts=exclude_body_parts,
            )
        )
    return sources


def _adapter_tcia_list_patients(client: httpx.Client, *, collection: str) -> list[str]:
    """Return every PatientID NBIA offers for ``collection``.

    Backs ``subjects: all`` so the maximal wave does not require
    hand-listing every subject. NBIA's getPatient is a cheap JSON call;
    the field is ``PatientID`` on v1 but some mirrors emit ``PatientId``,
    so we accept either. Sorted + de-duplicated for stable run order.
    """
    tcia_collection = collection.split("/", 1)[1] if "/" in collection else collection
    url = f"{TCIA_REST_BASE}/getPatient?" + urlencode(
        {"Collection": tcia_collection, "format": "json"}
    )
    data = _http_get_json_with_retry(client, url, what=f"TCIA getPatient {tcia_collection}")
    if not isinstance(data, list):
        raise click.ClickException(f"TCIA getPatient: unexpected payload for {tcia_collection!r}")
    ids = {
        str(e.get("PatientID") or e.get("PatientId"))
        for e in data
        if isinstance(e, dict) and (e.get("PatientID") or e.get("PatientId"))
    }
    return sorted(ids)


@contextmanager
def _temp_workdir() -> Iterator[Path]:
    d = Path(tempfile.mkdtemp(prefix="bvphoenix-public-import-"))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _unzip(zip_path: Path, out_dir: Path) -> int:
    """Extract a ZIP flat into ``out_dir`` and return file count.

    Both TCIA and OsiriX serve ZIPs of DICOM files; we accept either
    a flat layout or one level of subfolders.
    """
    count = 0
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(out_dir)
        for name in zf.namelist():
            if not name.endswith("/"):
                count += 1
    return count


def _adapter_tcia(
    client: httpx.Client,
    *,
    collection: str,
    subject_id: str,
    workdir: Path,
    skip_series: set[str] | None = None,
    keep_series: set[str] | None = None,
    exclude_body_parts: frozenset[str] = frozenset(),
) -> Path | None:
    """Fetch one TCIA subject's worth of DICOMs.

    1. List series for (Collection, PatientID) — a cheap JSON call.
    2. For each SeriesInstanceUID not already in ``skip_series``,
       download the per-series ZIP and expand into
       ``workdir/<subject>/<series_uid>/``.

    ``skip_series`` holds SeriesInstanceUIDs already fully imported (see
    :func:`completed_series_uids_for_source`); they are filtered out
    *before* the expensive ``getImage`` ZIP download, which is the whole
    point of the optimisation. Returns the subject's root directory, or
    ``None`` when every series the subject offers is already imported (no
    download performed, nothing to scan).

    ``keep_series``, when non-empty, restricts the fetch to exactly those
    SeriesInstanceUIDs (an allow-list). A subject like a longitudinal TCIA
    case can carry 100+ series across many reconstructions/SEG/RTSTRUCT;
    targeted ingest (one CT pair + its ground-truth SEG) would otherwise
    pull and pack the whole subject. Requested UIDs the subject does not
    offer are reported and skipped, not fatal.
    """
    skip_series = skip_series or set()
    keep_series = keep_series or set()
    # Strip the leading "TCIA/" namespace the manifest uses so it's
    # explicit which collection lives where.
    tcia_collection = collection.split("/", 1)[1] if "/" in collection else collection

    list_url = f"{TCIA_REST_BASE}/getSeries?" + urlencode(
        {"Collection": tcia_collection, "PatientID": subject_id, "format": "json"}
    )
    series_list = _http_get_json_with_retry(
        client, list_url, what=f"TCIA getSeries {tcia_collection}/{subject_id}"
    )
    if not isinstance(series_list, list) or not series_list:
        raise click.ClickException(
            f"TCIA: no series for collection={tcia_collection!r} subject={subject_id!r}"
        )

    if keep_series:
        offered = {e.get("SeriesInstanceUID") for e in series_list}
        missing = keep_series - offered
        if missing:
            click.echo(
                f"    --only-series: {len(missing)} requested UID(s) not offered by "
                f"{subject_id}, skipped: {', '.join(sorted(missing))}",
                err=True,
            )

    subject_dir = workdir / subject_id
    subject_dir.mkdir(parents=True, exist_ok=True)
    fetched = 0
    skipped = 0
    excluded_body = 0
    for entry in series_list:
        series_uid = entry.get("SeriesInstanceUID")
        if not series_uid:
            continue
        if keep_series and series_uid not in keep_series:
            continue
        if exclude_body_parts:
            body = str(entry.get("BodyPartExamined") or "").strip().upper()
            if body in exclude_body_parts:
                excluded_body += 1
                continue
        if series_uid in skip_series:
            skipped += 1
            continue
        zip_path = subject_dir / f"{series_uid}.zip"
        series_url = f"{TCIA_REST_BASE}/getImage?" + urlencode({"SeriesInstanceUID": series_uid})
        click.echo(f"    fetching series {series_uid[:24]}…")
        _http_get_with_retry(client, series_url, zip_path, what=f"TCIA series {series_uid}")
        series_dir = subject_dir / series_uid
        series_dir.mkdir(exist_ok=True)
        files = _unzip(zip_path, series_dir)
        click.echo(f"      → {files} file(s)")
        zip_path.unlink(missing_ok=True)
        fetched += 1
    if skipped:
        click.echo(f"    {skipped} series already imported, {fetched} fetched")
    if excluded_body:
        click.echo(
            f"    {excluded_body} series excluded by body-part filter "
            f"({', '.join(sorted(exclude_body_parts))})"
        )
    if fetched == 0:
        # Subject is fully present already — no ZIP downloaded, nothing
        # to scan. Drop the empty dir and signal the caller to skip.
        shutil.rmtree(subject_dir, ignore_errors=True)
        return None
    return subject_dir


def _adapter_osirix_zip(
    client: httpx.Client,
    *,
    subject: ManifestSubject,
    workdir: Path,
) -> Path:
    """Fetch a single ZIP from a manifest-supplied URL."""
    if not subject.url:
        raise click.ClickException(
            f"osirix_zip: subject {subject.identifier} missing 'url' in manifest"
        )
    subject_dir = workdir / subject.identifier
    subject_dir.mkdir(parents=True, exist_ok=True)
    zip_path = subject_dir / "payload.zip"
    click.echo(f"    fetching {subject.url}")
    _http_get_with_retry(client, subject.url, zip_path, what=subject.url)
    files = _unzip(zip_path, subject_dir)
    click.echo(f"    → {files} file(s) extracted")
    zip_path.unlink(missing_ok=True)
    return subject_dir


# ---- IDC (NCI Imaging Data Commons) adapter -------------------------------
# Some TCIA collections are NOT reachable through the NBIA v1 REST API
# (``getPatient`` returns an empty body) because their DICOM lives only in the
# Imaging Data Commons public buckets — NLST (National Lung Screening Trial,
# ~26k subjects) is the prime example. IDC publishes a per-series index
# (collection_id → PatientID → SeriesInstanceUID → object keys) plus anonymous
# public S3 buckets (idc-open-data / -two / -cr). We use the maintained
# ``idc-index`` package for the collection→series map and anonymous ``boto3``
# for the bytes, ONE SERIES AT A TIME, so a disk-constrained pod never holds
# more than a single series of a multi-TB collection. ``idc-index`` is imported
# lazily so the API image pays nothing unless an ``idc`` manifest entry runs.

_IDC_CLIENT = None


def _idc_client():
    """Lazily build the shared IDCClient (first call downloads the index)."""
    global _IDC_CLIENT
    if _IDC_CLIENT is None:
        try:
            from idc_index import IDCClient
        except ImportError as exc:  # pragma: no cover - import-time guard
            raise click.ClickException(
                "the 'idc' adapter requires the idc-index package (pip install idc-index)"
            ) from exc
        _IDC_CLIENT = IDCClient.client()
    return _IDC_CLIENT


def _idc_s3():
    """Anonymous (unsigned) S3 client for the public IDC buckets."""
    import boto3
    from botocore import UNSIGNED
    from botocore.config import Config

    return boto3.client("s3", config=Config(signature_version=UNSIGNED))


def _idc_collection_id(collection: str) -> str:
    """Strip the manifest namespace ("IDC/nlst" -> "nlst")."""
    return collection.split("/", 1)[1] if "/" in collection else collection


def _adapter_idc_list_patients(*, collection: str) -> list[str]:
    """Return every PatientID IDC holds for ``collection`` (backs subjects: all)."""
    client = _idc_client()
    cid = _idc_collection_id(collection)
    if cid not in set(client.get_collections()):
        raise click.ClickException(f"IDC: collection {cid!r} is not in the IDC index")
    sel = client.index[client.index["collection_id"] == cid]
    return sorted({str(p) for p in sel["PatientID"]})


def _adapter_idc(
    *,
    collection: str,
    subject_id: str,
    workdir: Path,
    skip_series: set[str] | None = None,
    keep_series: set[str] | None = None,
    allow_noncommercial: bool = False,
) -> Path | None:
    """Fetch one IDC subject's DICOM via anonymous public S3, one series at a time.

    Mirrors :func:`_adapter_tcia`'s contract: skips ``skip_series`` *before* the
    download, restricts to ``keep_series`` when given, writes
    ``workdir/<subject>/<series_uid>/<instance>.dcm``, and returns ``None`` when
    every series the subject offers is already imported.

    License safety: IDC keys commercially-restricted (CC-BY-NC) series into the
    ``idc-open-data-cr`` bucket. A ``*-cr`` series is refused unless the manifest
    entry is itself CC-BY-NC (``allow_noncommercial``), so non-commercial data is
    never silently relabelled as CC-BY.
    """
    client = _idc_client()
    s3 = _idc_s3()
    skip_series = skip_series or set()
    keep_series = keep_series or set()
    cid = _idc_collection_id(collection)
    idx = client.index
    sel = idx[(idx["collection_id"] == cid) & (idx["PatientID"].astype(str) == subject_id)]
    if sel.empty:
        raise click.ClickException(f"IDC: no series for {cid}/{subject_id}")

    subject_dir = workdir / subject_id
    subject_dir.mkdir(parents=True, exist_ok=True)
    fetched = 0
    skipped = 0
    refused_nc = 0
    for series_uid in (str(u) for u in sel["SeriesInstanceUID"]):
        if keep_series and series_uid not in keep_series:
            continue
        if series_uid in skip_series:
            skipped += 1
            continue
        urls = client.get_series_file_URLs(series_uid)
        if not urls:
            continue
        first_bucket = urls[0][len("s3://") :].split("/", 1)[0]
        if first_bucket.endswith("-cr") and not allow_noncommercial:
            # commercially-restricted (CC-BY-NC) data under a CC-BY manifest entry
            refused_nc += 1
            continue
        series_dir = subject_dir / series_uid
        series_dir.mkdir(exist_ok=True)
        click.echo(f"    fetching idc series {series_uid[:24]}… ({len(urls)} inst)")
        for url in urls:
            if not url.startswith("s3://"):
                raise click.ClickException(f"IDC: unexpected non-s3 URL {url!r}")
            bucket, key = url[len("s3://") :].split("/", 1)
            s3.download_file(bucket, key, str(series_dir / Path(key).name))
        fetched += 1
    if skipped:
        click.echo(f"    {skipped} series already imported, {fetched} fetched")
    if refused_nc:
        click.echo(
            f"    {refused_nc} CC-BY-NC series refused (idc-open-data-cr) under a "
            f"commercial-licensed entry",
            err=True,
        )
    if fetched == 0:
        shutil.rmtree(subject_dir, ignore_errors=True)
        return None
    return subject_dir


@click.command(
    name="bvphoenix-public-import",
    help="Bootstrap OpenData public dataset from a curated manifest (TCIA, OsiriX).",
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
    help=(
        "Restrict to specific subjects, format 'COLLECTION/SUBJECT_ID'. "
        "Repeatable. Useful for pilot runs."
    ),
)
@click.option(
    "--only-series",
    "only_series",
    multiple=True,
    help=(
        "Restrict the tcia adapter to specific SeriesInstanceUIDs (allow-list, "
        "repeatable). Applies within every selected subject. Lets you ingest a "
        "targeted slice of a large subject (e.g. one CT pair + its ground-truth "
        "SEG) instead of all 100+ series. Ignored by the osirix_zip adapter."
    ),
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Download + scan but do not write to S3 or DB.",
)
@click.option(
    "--continue-on-error/--abort-on-error",
    default=True,
    show_default=True,
    help="Per-subject failures are logged and skipped (default) vs aborting the run.",
)
@click.option(
    "--reimport-existing",
    is_flag=True,
    help=(
        "Re-download and re-process series that are already fully imported. "
        "By default the importer skips them before download (idempotency "
        "optimisation); pass this to force a full re-fetch, e.g. to repair "
        "a corrupted upload."
    ),
)
def main(
    manifest_path: Path,
    only: tuple[str, ...],
    only_series: tuple[str, ...],
    dry_run: bool,
    continue_on_error: bool,
    reimport_existing: bool,
) -> None:
    sources = _parse_manifest(manifest_path)
    settings = get_settings()
    storage, bucket = storage_target()

    only_set = set(only)
    keep_series = set(only_series)
    engine = create_engine(settings.database_url_sync, future=True)

    processed = 0
    succeeded = 0
    failed: list[tuple[str, str, str]] = []
    # Series completed this run, to enqueue volume-pack + image-embed jobs
    # for. Without this the public importer leaves studies un-indexed for
    # visual search (the regular bvphoenix-import CLI already does this).
    enqueue_series_ids: list[str] = []

    with _temp_workdir() as workdir, httpx.Client(timeout=HTTP_TIMEOUT) as client:
        # Resolve ``subjects: all`` sources into explicit PatientID lists
        # before iterating, so the progress counter and --only matching
        # work the same as for hand-listed manifests. Only tcia supports
        # whole-collection enumeration.
        for src in sources:
            if not src.all_subjects:
                continue
            if src.adapter not in ("tcia", "idc"):
                raise click.ClickException(
                    f"'subjects: all' only supported by the tcia/idc adapters "
                    f"(collection {src.collection!r} uses {src.adapter!r})"
                )
            try:
                if src.adapter == "idc":
                    patient_ids = _adapter_idc_list_patients(collection=src.collection)
                else:
                    patient_ids = _adapter_tcia_list_patients(client, collection=src.collection)
            except Exception as exc:
                click.echo(f"# {src.collection}: enumeration FAILED: {exc}", err=True)
                failed.append((src.collection, "*", f"getPatient enumeration: {exc}"))
                src.subjects = []
                if not continue_on_error:
                    raise
                continue
            src.subjects = [ManifestSubject(identifier=p) for p in patient_ids]
            click.echo(
                f"# {src.collection}: enumerated {len(patient_ids)} subjects (subjects: all)"
            )

        total_subjects = sum(len(s.subjects) for s in sources)
        for src in sources:
            for subj in src.subjects:
                key = f"{src.collection}/{subj.identifier}"
                processed += 1
                if only_set and key not in only_set:
                    continue
                click.echo(f"[{processed}/{total_subjects}] {key} (adapter={src.adapter})")
                try:
                    # Pre-download idempotency probe: which series do we
                    # already hold complete for this source subject? The
                    # adapter skips those before the costly per-series ZIP
                    # download. Pure optimisation — ingestion_complete=True
                    # guarantees the instances are present — so it is safe
                    # unless --reimport-existing forces a full re-fetch.
                    skip_series: set[str] = set()
                    if not reimport_existing:
                        with Session(engine) as probe:
                            skip_series = completed_series_uids_for_source(
                                probe,
                                collection=src.collection,
                                subject_id=subj.identifier,
                            )

                    if src.adapter == "tcia":
                        subject_dir = _adapter_tcia(
                            client,
                            collection=src.collection,
                            subject_id=subj.identifier,
                            workdir=workdir,
                            skip_series=skip_series,
                            keep_series=keep_series,
                            exclude_body_parts=src.exclude_body_parts,
                        )
                    elif src.adapter == "osirix_zip":
                        # OsiriX ships one ZIP per subject; there is no
                        # per-series fetch to trim. If anything is already
                        # imported for this subject, treat it as done.
                        if skip_series:
                            click.echo(f"  ✓ already imported ({len(skip_series)} series), skipped")
                            succeeded += 1
                            continue
                        subject_dir = _adapter_osirix_zip(client, subject=subj, workdir=workdir)
                    elif src.adapter == "idc":
                        subject_dir = _adapter_idc(
                            collection=src.collection,
                            subject_id=subj.identifier,
                            workdir=workdir,
                            skip_series=skip_series,
                            keep_series=keep_series,
                            allow_noncommercial="-NC-" in src.license_spdx.upper(),
                        )
                    else:
                        raise click.ClickException(f"unknown adapter: {src.adapter!r}")

                    if subject_dir is None:
                        click.echo(
                            f"  ✓ already fully imported ({len(skip_series)} series), skipped"
                        )
                        succeeded += 1
                        continue

                    source = PublicDatasetSource(
                        collection=src.collection,
                        subject_id=subj.identifier,
                        license_spdx=src.license_spdx,
                        license_url=src.license_url,
                        citation_text=src.citation_text,
                        citation_required=src.citation_required,
                        display_name=subj.display_name,
                    )
                    with Session(engine) as session:
                        result = import_public_dataset(
                            session=session,
                            storage=storage,
                            bucket=bucket,
                            dicom_dir=subject_dir,
                            source=source,
                            dry_run=dry_run,
                        )
                        if not dry_run:
                            session.commit()
                    click.echo(
                        f"  ✓ patient_id={result.patient_id} created={result.patient_created} "
                        f"studies=+{result.report.studies_inserted}/{result.report.studies_existing} "
                        f"instances=+{result.report.instances_inserted}/{result.report.instances_existing}"
                    )
                    enqueue_series_ids.extend(result.report.series_ids)
                    # Free the per-subject temp dir before the next one
                    # so a 100-subject run does not balloon to 50 GB
                    # of resident scratch space.
                    shutil.rmtree(subject_dir, ignore_errors=True)
                    succeeded += 1
                except Exception as exc:
                    click.echo(f"  ✗ FAILED: {exc}", err=True)
                    failed.append((src.collection, subj.identifier, str(exc)))
                    if not continue_on_error:
                        raise

    # Index the freshly-imported studies for visual search: enqueue
    # volume-pack (all series) + image-embed (embeddable series) jobs.
    # Idempotent — embed_series skips series that already have a vector —
    # so it is safe even when --reimport-existing re-ran some subjects.
    if not dry_run and enqueue_series_ids:
        _enqueue_pack_jobs(settings, enqueue_series_ids)

    click.echo("---")
    click.echo(f"processed:  {processed}")
    click.echo(f"succeeded:  {succeeded}")
    click.echo(f"failed:     {len(failed)}")
    if failed:
        click.echo("failures:")
        for coll, sid, msg in failed:
            click.echo(f"  - {coll}/{sid}: {msg}")
        sys.exit(2)


if __name__ == "__main__":
    main()
