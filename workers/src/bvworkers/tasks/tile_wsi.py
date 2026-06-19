"""Render a pathology slide into a DeepZoom (DZI) tile pyramid.

Step 2 of ``docs/pathology_wsi_spike.md``. The OpenSeadragon viewer
serves tiles from a pre-generated pyramid in the derivatives bucket; this
arq task builds it once per slide (idempotent on ``dzi_ready``) so the
serving path is a stateless S3 proxy with zero per-request compute.

Handles all three ``slide_class`` values uniformly through pyvips
``dzsave``; only the reader differs — WSI formats (SVS/NDPI/DICOM-WSI)
open via ``openslideload`` (the base pyramid level ONLY, so the PHI
label/macro associated images are excluded by construction), ordinary
gross/micrograph images via ``new_from_file``.

Disk discipline (the dominant risk on a 60 GiB node): the source is
downloaded to a bounded scratch dir, deleted BEFORE the tile upload
begins, and tiles are uploaded then unlinked level-by-level so peak
local disk stays ~max(source, tile_tree) rather than their sum. A
per-pod Redis lock bounds concurrency to one tiling job per worker pod.

Job-tracked (``@with_safety_net``): the row flips to ``failed`` even if
the pod is OOM/evicted mid-render, and progress feeds the viewer's
"tiling…" state.
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import socket
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import boto3
from botocore.client import Config

from bvworkers.config import get_settings
from bvworkers.job_safety import with_safety_net

log = logging.getLogger(__name__)

GENERATOR_VERSION = "tile-v1"
TILE_SIZE = 512
OVERLAP = 0
TILE_FORMAT = "jpeg"  # stored as .jpg; dzi_format column value
JPEG_QUALITY = 85
_UPLOAD_WORKERS = 16

# Source formats that must be read through libvips' openslide loader.
_WSI_READER_FORMATS = {"svs", "ndpi", "ome-tiff", "dicom-wsi", "mrxs", "scn"}


class _PhiDimensionError(Exception):
    """Loaded image dimensions disagree with the de-identified metadata
    captured at ingest — fail closed rather than tile a possibly
    label-bearing image."""


class _EngineUnavailableError(Exception):
    """libvips lacks the openslide loader needed for this WSI source."""


def _has_openslide_loader() -> bool:
    try:
        import pyvips

        return bool(pyvips.type_find("VipsOperation", "openslideload"))
    except Exception:  # pragma: no cover - import/FFI failure
        return False


def _open_and_tile(
    *,
    source_path: str,
    out_base: str,
    slide_class: str,
    source_format: str,
    expected_dims: tuple[int, int] | None,
) -> dict[str, Any]:
    """Open the source with pyvips, ICC-correct, and dzsave the pyramid.

    Runs in a worker thread (libvips is blocking C). Returns descriptor
    metadata. Raises :class:`_PhiDimensionError` /
    :class:`_EngineUnavailableError` for the fail-closed paths.
    """
    import pyvips

    # Keep libvips from caching decoded tiles in RAM — the worker pod
    # shares its memory budget with other tasks.
    pyvips.cache_set_max(0)
    pyvips.cache_set_max_mem(0)

    use_openslide = slide_class == "wsi" or source_format in _WSI_READER_FORMATS
    image: Any  # pyvips dispatches methods dynamically; Pyright can't see them
    if use_openslide:
        if not _has_openslide_loader():
            raise _EngineUnavailableError(source_format)
        # level=0 reads ONLY the base pyramid; associated_images (label,
        # macro) live in a separate namespace and are never touched.
        image = pyvips.Image.openslideload(source_path, level=0)
    else:
        image = pyvips.Image.new_from_file(source_path, access="sequential")

    # PHI dimension guard: the tiler must read the same image the
    # de-identifying ingest measured. A mismatch means we are looking at
    # a different image/level than expected — fail closed.
    if expected_dims is not None:
        ew, eh = expected_dims
        if int(image.width) != int(ew) or int(image.height) != int(eh):
            raise _PhiDimensionError(f"loaded {image.width}x{image.height} != expected {ew}x{eh}")

    # ICC -> sRGB so every tile is colorimetrically consistent
    # regardless of scanner (regulatory-critical for brightfield). Only
    # when an embedded profile is present; a blind transform on an
    # untagged image corrupts color.
    icc_applied = False
    if image.get_typeof("icc-profile-data") != 0:
        try:
            image = image.icc_transform("srgb", embedded=True, intent="relative")
            icc_applied = True
        except Exception as exc:  # malformed profile: passthrough
            log.warning("tile_wsi: ICC transform failed, passthrough — %s", exc)

    width, height = int(image.width), int(image.height)

    # depth="onepixel" (the dzsave default) emits the FULL DeepZoom
    # pyramid down to a 1px top level. OpenSeadragon's tile source
    # iterates levels 0..ceil(log2(maxDim)); a shallower pyramid
    # (depth="onetile") would 404 the low-zoom levels. The extra objects
    # are the handful of 1-tile top levels — negligible.
    image.dzsave(
        out_base,
        tile_size=TILE_SIZE,
        overlap=OVERLAP,
        suffix=f".jpg[Q={JPEG_QUALITY}]",
        layout="dz",
        depth="onepixel",
    )

    files_dir = f"{out_base}_files"
    levels = (
        len([d for d in os.listdir(files_dir) if d.isdigit()]) if os.path.isdir(files_dir) else 0
    )
    return {
        "width": width,
        "height": height,
        "levels": levels,
        "tile_size": TILE_SIZE,
        "overlap": OVERLAP,
        "format": TILE_FORMAT,
        "icc_applied": icc_applied,
        "files_dir": files_dir,
        "descriptor_path": f"{out_base}.dzi",
    }


def _upload_pyramid(
    *,
    s3: Any,
    bucket: str,
    slide_id: str,
    files_dir: str,
    descriptor_path: str,
    put_extra: dict[str, Any],
) -> int:
    """Upload the DZI tree + descriptor, unlinking each tile after its
    PUT so peak local disk does not hold the whole tree. Runs in a
    worker thread. Returns the number of objects uploaded."""
    from bvphoenix.services.pathology_keys import dzi_descriptor_key, dzi_tile_key

    uploaded = 0
    if not os.path.isdir(files_dir):
        return 0

    # Highest level first (largest, most tiles) so disk frees fastest.
    levels = sorted((d for d in os.listdir(files_dir) if d.isdigit()), key=int, reverse=True)
    for level in levels:
        level_dir = os.path.join(files_dir, level)
        tile_files = [f for f in os.listdir(level_dir) if f.endswith(".jpg")]

        def _put_one(fname: str, _level: str = level, _dir: str = level_dir) -> None:
            col_row = fname[:-4]  # strip .jpg
            col, row = col_row.split("_", 1)
            key = dzi_tile_key(slide_id, int(_level), int(col), int(row), fmt="jpg")
            path = os.path.join(_dir, fname)
            with open(path, "rb") as fh:
                s3.put_object(
                    Bucket=bucket,
                    Key=key,
                    Body=fh.read(),
                    ContentType="image/jpeg",
                    **put_extra,
                )
            os.unlink(path)

        with ThreadPoolExecutor(max_workers=_UPLOAD_WORKERS) as pool:
            list(pool.map(_put_one, tile_files))
        uploaded += len(tile_files)
        # Drop the drained level dir.
        with contextlib.suppress(OSError):
            os.rmdir(level_dir)

    # Descriptor last: it is the readiness marker.
    with open(descriptor_path, "rb") as fh:
        s3.put_object(
            Bucket=bucket,
            Key=dzi_descriptor_key(slide_id),
            Body=fh.read(),
            ContentType="application/xml",
            **put_extra,
        )
    uploaded += 1
    return uploaded


@with_safety_net("tile_wsi")
async def tile_wsi(ctx: dict, job_id: str, slide_id: str) -> dict[str, Any]:  # type: ignore[type-arg]
    """Arq entry point: build the DZI pyramid for one pathology slide."""
    import asyncio

    try:
        jid = uuid.UUID(job_id)
        sid = uuid.UUID(slide_id)
    except (TypeError, ValueError) as exc:
        log.error("tile_wsi: invalid uuid: %s", exc)
        return {"status": "error", "reason": f"invalid uuid: {exc}"}

    settings = get_settings()

    try:
        from bvphoenix.db.models import PathologySlide
        from bvphoenix.db.session import SERVICE_SUBJECT, set_current_subject
        from bvphoenix.services import jobs as jobs_service
        from bvphoenix.services.pathology_keys import dzi_descriptor_key
        from sqlalchemy import select, update
        from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    except ImportError as exc:  # pragma: no cover
        log.exception("tile_wsi: bvphoenix not importable: %s", exc)
        return {"status": "error", "reason": f"bvphoenix import: {exc}"}

    s3 = boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint_url,
        region_name=settings.s3_region,
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key,
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )
    derivatives_bucket = settings.s3_bucket_derivatives
    put_extra = settings.put_extra_args()

    engine = create_async_engine(settings.database_url, pool_pre_ping=True)

    async def _set_error(code: str, detail: str) -> None:
        async with AsyncSession(engine, expire_on_commit=False) as db:
            await set_current_subject(db, SERVICE_SUBJECT)
            await db.execute(
                update(PathologySlide)
                .where(PathologySlide.id == sid)
                .values(dzi_error={"code": code, "detail": detail})
            )
            await jobs_service.mark_failed(db, jid, error={"code": code, "message": detail})
            await db.commit()

    redis = ctx.get("redis")
    lock_key = f"wsi_tile_inflight:{socket.gethostname()}"
    lock_held = False
    scratch_root: str | None = None

    try:
        # --- 1. cheap gates: idempotency, capability, size -------------
        async with AsyncSession(engine, expire_on_commit=False) as db:
            await set_current_subject(db, SERVICE_SUBJECT)
            slide = (
                await db.execute(select(PathologySlide).where(PathologySlide.id == sid))
            ).scalar_one_or_none()
            if slide is None:
                await jobs_service.mark_failed(
                    db, jid, error={"code": "not_found", "message": f"slide {sid} not found"}
                )
                await db.commit()
                return {"status": "not_found", "slide_id": slide_id}
            if slide.dzi_ready:
                await jobs_service.mark_succeeded(db, jid, result_uri=f"dzi://{slide_id}")
                await db.commit()
                return {"status": "already_tiled", "slide_id": slide_id}

            src_bucket = slide.s3_bucket
            src_key = slide.s3_source_key
            slide_class = slide.slide_class
            source_format = slide.source_format
            size_bytes = int(slide.size_bytes or 0)
            base_dims = (
                (int(slide.base_width), int(slide.base_height))
                if slide.base_width and slide.base_height
                else None
            )

        use_openslide = slide_class == "wsi" or source_format in _WSI_READER_FORMATS
        if use_openslide and not _has_openslide_loader():
            await _set_error("engine_unavailable", f"no openslide loader for {source_format}")
            return {"status": "engine_unavailable", "slide_id": slide_id}

        if size_bytes > settings.wsi_tile_max_bytes:
            await _set_error(
                "too_large_for_tiling",
                f"{size_bytes} > cap {settings.wsi_tile_max_bytes}",
            )
            return {"status": "too_large_for_tiling", "slide_id": slide_id}

        # --- 2. per-pod concurrency lock (bound disk pressure) --------
        if redis is not None:
            try:
                lock_held = bool(await redis.set(lock_key, slide_id, nx=True, ex=3600))
            except Exception:  # redis hiccup: don't block tiling entirely
                lock_held = False
            if not lock_held:
                # Another tiling job owns this pod. Re-enqueue with a
                # delay; the Job row stays queued (mark_running not yet
                # called), so nothing flips to failed.
                with contextlib.suppress(Exception):  # pragma: no cover
                    await redis.enqueue_job("tile_wsi", job_id, slide_id, _defer_by=60)
                return {"status": "deferred", "slide_id": slide_id}

        # --- 3. run ----------------------------------------------------
        async with AsyncSession(engine, expire_on_commit=False) as db:
            await set_current_subject(db, SERVICE_SUBJECT)
            await jobs_service.mark_running(db, jid)
            await jobs_service.update_progress(db, jid, stage="downloading")
            await db.commit()

        scratch_base = settings.wsi_scratch_dir or None
        scratch_root = os.path.join(
            scratch_base or __import__("tempfile").gettempdir(), f"wsi-{slide_id}"
        )
        os.makedirs(scratch_root, exist_ok=True)
        ext = os.path.splitext(src_key)[1] or ".dat"
        source_path = os.path.join(scratch_root, f"source{ext}")
        dzi_dir = os.path.join(scratch_root, "dzi")
        os.makedirs(dzi_dir, exist_ok=True)
        out_base = os.path.join(dzi_dir, "image")  # matches DZI_BASENAME

        await asyncio.to_thread(s3.download_file, src_bucket, src_key, source_path)

        async with AsyncSession(engine, expire_on_commit=False) as db:
            await set_current_subject(db, SERVICE_SUBJECT)
            await jobs_service.update_progress(db, jid, stage="tiling")
            await db.commit()

        try:
            meta = await asyncio.to_thread(
                _open_and_tile,
                source_path=source_path,
                out_base=out_base,
                slide_class=slide_class,
                source_format=source_format,
                expected_dims=base_dims,
            )
        except _PhiDimensionError as exc:
            await _set_error("phi_dimension_mismatch", str(exc))
            return {"status": "phi_dimension_mismatch", "slide_id": slide_id}
        except _EngineUnavailableError as exc:
            await _set_error("engine_unavailable", str(exc))
            return {"status": "engine_unavailable", "slide_id": slide_id}

        # Free the source BEFORE uploading tiles so peak disk stays
        # ~max(source, tile_tree) rather than their sum.
        with contextlib.suppress(OSError):
            os.unlink(source_path)

        async with AsyncSession(engine, expire_on_commit=False) as db:
            await set_current_subject(db, SERVICE_SUBJECT)
            await jobs_service.update_progress(db, jid, stage="uploading")
            await db.commit()

        await asyncio.to_thread(
            _upload_pyramid,
            s3=s3,
            bucket=derivatives_bucket,
            slide_id=slide_id,
            files_dir=meta["files_dir"],
            descriptor_path=meta["descriptor_path"],
            put_extra=put_extra,
        )

        # --- 4. commit success ----------------------------------------
        gen_version = GENERATOR_VERSION + ("-icc" if meta["icc_applied"] else "-noicc")
        async with AsyncSession(engine, expire_on_commit=False) as db:
            await set_current_subject(db, SERVICE_SUBJECT)
            await db.execute(
                update(PathologySlide)
                .where(PathologySlide.id == sid)
                .values(
                    s3_dzi_key=dzi_descriptor_key(slide_id),
                    dzi_ready=True,
                    dzi_levels=meta["levels"],
                    dzi_tile_size=meta["tile_size"],
                    dzi_overlap=meta["overlap"],
                    dzi_format=meta["format"],
                    dzi_generator_version=gen_version,
                    dzi_error=None,
                    # Backfill base dims if ingest left them null (ordinary
                    # images), so the PHI guard works on a re-tile.
                    base_width=meta["width"],
                    base_height=meta["height"],
                )
            )
            await jobs_service.mark_succeeded(db, jid, result_uri=f"dzi://{slide_id}")
            await db.commit()

        return {
            "status": "tiled",
            "slide_id": slide_id,
            "levels": meta["levels"],
            "icc_applied": meta["icc_applied"],
        }
    finally:
        if scratch_root:
            shutil.rmtree(scratch_root, ignore_errors=True)
        if lock_held and redis is not None:
            with contextlib.suppress(Exception):  # pragma: no cover
                await redis.delete(lock_key)
        await engine.dispose()
