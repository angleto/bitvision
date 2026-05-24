"""Pathology / WSI ingest service (Step 1 of the spike doc).

Counterpart of ``cli.import_dicom.persist_and_upload`` and
``services.public_dataset.import_public_dataset`` but for histology
slides (SVS / NDPI / OME-TIFF / DICOM-WSI). End-to-end shape:

1. Open the source file with OpenSlide.
2. Hash the bytes (SHA-256, streaming) — basis of the synthetic
   slide UID and the idempotency check.
3. Generate a 512 px JPEG thumbnail + (when present) a macro
   overview JPEG via :mod:`wsi_deid`. The slide label is
   intentionally **not** extracted (PHI surface — see spike §6).
4. Upload the source + derived JPEGs to S3 under
   ``patients/{patient_id}/pathology/{slide_id}/...``.
5. Insert one ``PathologySlide`` + one ``ClinicalEvent`` (kind
   ``pathology_slide``) wired by ``clinical_event_id``.

Idempotency: re-ingesting the same physical file (same SHA-256)
under the same owner is a no-op; the existing row is returned.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import openslide
from sqlalchemy import select
from sqlalchemy.orm import Session

from bvphoenix.config import get_settings
from bvphoenix.db.models import ClinicalEvent, PathologySlide
from bvphoenix.services.wsi_deid import (
    extract_macro_jpeg,
    generate_thumbnail_jpeg,
    safe_properties,
)
from bvphoenix.storage import S3Storage

# UUID5 namespace anchoring the synthetic ``slide_instance_uid`` we
# mint for non-DICOM sources. Same value lives in tests so the test
# vectors are reproducible.
_SLIDE_UID_NAMESPACE = uuid.UUID("a3f1c2b4-9d8e-4f6a-9c1d-1e2f3a4b5c6d")

# Lowercase suffix → source_format string stored in DB. Anything not
# listed falls back to ``other``; the importer still uploads but the
# viewer (Step 2) will need a matching reader to render it.
_FORMAT_BY_SUFFIX: dict[str, str] = {
    ".svs": "svs",
    ".ndpi": "ndpi",
    ".tif": "ome-tiff",  # safest default for a multi-page tiled TIFF
    ".tiff": "ome-tiff",
    ".mrxs": "mrxs",
    ".scn": "scn",
    ".dcm": "dicom-wsi",
}


@dataclass(frozen=True)
class PathologyImportSource:
    """Inputs collected by the CLI / public-pathology connector and
    handed to :func:`import_pathology_slide`.

    Private uploads leave ``source_collection``/``source_subject_id``
    /``license_*`` as None and ``is_public=False``. OpenData
    pathology imports populate all of them and ``is_public=True``,
    matching the imaging_studies / public_dataset contract.
    """

    path: Path
    owner_subject_id: uuid.UUID
    patient_id: uuid.UUID
    tier: str = "t1"
    is_public: bool = False
    stain: str | None = None
    block_label: str | None = None
    slide_label: str | None = None
    # Provenance / license — required when ``tier='t4'`` per the
    # ``ck_pathology_slides_t4_license`` CHECK constraint.
    source_collection: str | None = None
    source_subject_id: str | None = None
    license_spdx: str | None = None
    license_url: str | None = None
    citation_required: bool = False
    citation_text: str | None = None


@dataclass
class PathologyImportResult:
    slide_id: uuid.UUID
    clinical_event_id: uuid.UUID | None
    created: bool
    bytes_uploaded: int


def _sha256_of(path: Path) -> tuple[str, int]:
    """Return (hex digest, byte count). Streams to keep RSS bounded
    even on multi-GB slides."""
    h = hashlib.sha256()
    n = 0
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
            n += len(chunk)
    return h.hexdigest(), n


def _slide_uid(*, source_format: str, sha256: str, openslide_obj: Any) -> str:
    """Mint a stable, deterministic slide identifier.

    For DICOM-WSI we keep the source's SOPInstanceUID; for everything
    else we synthesise UUID5(namespace, sha256) so the same physical
    file always maps to the same UID across re-uploads.
    """
    if source_format == "dicom-wsi":
        # OpenSlide stores DICOM tags under "dicom.SOPInstanceUID" etc.
        # Fallback to UUID5 if the tag is not exposed.
        sop = openslide_obj.properties.get("dicom.SOPInstanceUID")
        if sop:
            return str(sop)
    return str(uuid.uuid5(_SLIDE_UID_NAMESPACE, sha256))


def _s3_keys(*, patient_id: uuid.UUID, slide_id: uuid.UUID, source_ext: str) -> dict[str, str]:
    prefix = f"patients/{patient_id}/pathology/{slide_id}"
    return {
        "source": f"{prefix}/source{source_ext}",
        "thumbnail": f"{prefix}/thumbnail.jpg",
        "macro": f"{prefix}/macro.jpg",
    }


def _existing_slide(
    session: Session, *, owner_subject_id: uuid.UUID, slide_uid: str
) -> PathologySlide | None:
    return session.execute(
        select(PathologySlide).where(
            PathologySlide.owner_subject_id == owner_subject_id,
            PathologySlide.slide_instance_uid == slide_uid,
        )
    ).scalar_one_or_none()


def import_pathology_slide(
    *,
    session: Session,
    storage: S3Storage,
    bucket: str,
    source: PathologyImportSource,
    dry_run: bool = False,
) -> PathologyImportResult:
    """Ingest one WSI file. Returns a result describing what changed.

    The DB session is *not* committed — callers wrap in a transaction
    and decide when to commit. S3 uploads happen before the DB
    insert; on a failed insert the source remains as an orphan blob
    which the periodic GC sweep collects (same shape as the DICOM
    ingest path).
    """
    if not source.path.exists():
        raise FileNotFoundError(source.path)

    # File-size cap. OpenSlide.open() memory-maps the file; a malicious
    # or accidentally enormous WSI can exhaust the worker's memory
    # before any of the downstream logic gets a chance to validate
    # the input. Reject anything past ``BVP_WSI_MAX_BYTES`` (default
    # 30 GiB — comfortably above clinical scanner output, e.g. a
    # 100k × 100k 40x SVS is ~5-15 GiB).
    from bvphoenix.config import get_settings as _get_settings

    max_bytes = _get_settings().wsi_max_bytes
    actual_bytes = source.path.stat().st_size
    if actual_bytes > max_bytes:
        raise ValueError(
            f"WSI file too large: {actual_bytes:,} bytes > BVP_WSI_MAX_BYTES={max_bytes:,}"
        )

    suffix = source.path.suffix.lower()
    source_format = _FORMAT_BY_SUFFIX.get(suffix, "other")

    slide_obj = openslide.OpenSlide(str(source.path))
    try:
        sha256, size_bytes = _sha256_of(source.path)
        slide_uid = _slide_uid(source_format=source_format, sha256=sha256, openslide_obj=slide_obj)

        existing = _existing_slide(
            session, owner_subject_id=source.owner_subject_id, slide_uid=slide_uid
        )
        if existing is not None:
            return PathologyImportResult(
                slide_id=existing.id,
                clinical_event_id=existing.clinical_event_id,
                created=False,
                bytes_uploaded=0,
            )

        # Pre-allocate so all S3 keys can be computed before any insert.
        slide_id = uuid.uuid4()
        keys = _s3_keys(
            patient_id=source.patient_id, slide_id=slide_id, source_ext=suffix or ".bin"
        )

        props = safe_properties(slide_obj.properties)
        magnification = _maybe_float(props.get("openslide.objective-power")) or _maybe_float(
            props.get("aperio.AppMag")
        )
        mpp_x = _maybe_float(props.get("openslide.mpp-x"))
        mpp_y = _maybe_float(props.get("openslide.mpp-y"))
        scanner_make = props.get("openslide.vendor")
        # ``openslide.dimensions`` is (width, height) on the base level.
        base_w, base_h = slide_obj.dimensions
        levels = slide_obj.level_count

        # Derived images. Thumbnail is always produced; macro only when
        # the scanner embedded one. Label is intentionally skipped (PHI).
        thumbnail_bytes = generate_thumbnail_jpeg(slide_obj)
        macro_bytes = extract_macro_jpeg(slide_obj)

        bytes_uploaded = 0
        if not dry_run:
            # Source: stream the original from disk so a 10 GB slide
            # never lands in RAM.
            storage.upload_file(source.path, bucket=bucket, key=keys["source"])
            bytes_uploaded += size_bytes
            storage.upload_bytes(thumbnail_bytes, bucket=bucket, key=keys["thumbnail"])
            bytes_uploaded += len(thumbnail_bytes)
            if macro_bytes is not None:
                storage.upload_bytes(macro_bytes, bucket=bucket, key=keys["macro"])
                bytes_uploaded += len(macro_bytes)

        # Clinical event first so we can wire the FK back on the slide.
        event = ClinicalEvent(
            patient_id=source.patient_id,
            kind="pathology_slide",
            event_date=None,
            title=f"Vetrino {source.stain or 'istologico'}",
            source="pathology_ingest",
        )
        if not dry_run:
            session.add(event)
            session.flush()

        slide = PathologySlide(
            id=slide_id,
            patient_id=source.patient_id,
            clinical_event_id=event.id if not dry_run else None,
            owner_subject_id=source.owner_subject_id,
            slide_instance_uid=slide_uid,
            block_label=source.block_label,
            slide_label=source.slide_label,
            stain=source.stain,
            scanner_make=scanner_make,
            magnification=magnification,
            mpp_x=mpp_x,
            mpp_y=mpp_y,
            base_width=int(base_w),
            base_height=int(base_h),
            pyramid_levels=int(levels),
            source_format=source_format,
            s3_bucket=bucket,
            s3_source_key=keys["source"],
            size_bytes=size_bytes,
            content_sha256=sha256,
            s3_thumbnail_key=keys["thumbnail"] if not dry_run else None,
            s3_macro_key=keys["macro"] if (macro_bytes is not None and not dry_run) else None,
            s3_label_key=None,  # never written — see wsi_deid module docstring
            label_redacted=True,
            contribution_tier=source.tier,
            is_public=source.is_public,
            ingestion_complete=not dry_run,
            source_collection=source.source_collection,
            source_subject_id=source.source_subject_id,
            license_spdx=source.license_spdx,
            license_url=source.license_url,
            citation_required=source.citation_required,
            citation_text=source.citation_text,
        )
        if not dry_run:
            session.add(slide)
            session.flush()

        return PathologyImportResult(
            slide_id=slide.id,
            clinical_event_id=event.id if not dry_run else None,
            created=not dry_run,
            bytes_uploaded=bytes_uploaded,
        )
    finally:
        slide_obj.close()


def _maybe_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def storage_target() -> tuple[S3Storage, str]:
    """Resolve the (storage, bucket) pair the importer writes to.

    Mirrors :func:`services.public_dataset.storage_target` so the CLI
    layer can switch between DICOM and pathology ingest with the same
    plumbing.
    """
    from bvphoenix.storage import get_s3_storage

    settings = get_settings()
    storage = get_s3_storage()
    storage.ensure_bucket(settings.s3_bucket_raw)
    return storage, settings.s3_bucket_raw


_ = datetime  # keep the import in case future code stamps event_date
_ = Any  # silence "unused" if Any usage migrates inline in the future
