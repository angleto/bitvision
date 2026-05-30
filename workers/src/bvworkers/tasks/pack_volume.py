"""Pre-pack a DICOM series into a Float32 volume blob in S3.

Runs as an arq background job so the backend never has to do the
expensive download-decode-stack work synchronously. The result lands
in the derivatives bucket under
``patients/{patient_id}/series/{series_id}/volume.f32`` (built by
``services.derivative_keys.volume_key``) and a ``derivatives`` row
is inserted — the backend's ``volume.raw`` endpoint then serves it
directly.

Memory budget: each pack walks every instance, decodes its pixel
array into a Float32 numpy slot (4 bytes/voxel) and stacks them. A
1500-slice 512×512 CT alone is ~1.5 GiB of Float32 *output*, plus the
DICOM blob set kept alive while sorting them. A 3 GiB worker pod
OOM-killed on real production CTs; the safety net here pre-rejects
volumes that would obviously blow the limit (marked as
``status=too_large`` so the viewer falls through to its slice-by-
slice 2D path) and frees each dataset right after copying its pixel
data, so the in-flight working set stays close to the output size
instead of doubling it.
"""

from __future__ import annotations

import io
import json
import logging
import struct
import uuid

import boto3
import numpy as np
import pydicom
from botocore.client import Config
from bvphoenix.services.derivative_keys import volume_key
from bvphoenix.services.volumes import compute_volume_geometry
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from bvworkers.config import get_settings

log = logging.getLogger(__name__)

DERIVATIVE_KIND = "volume_f32"
DERIVATIVE_FORMAT = "raw"
HEADER_STRUCT = struct.Struct("<3I 3f 2f")

# Hard cap on the volume's output Float32 size. With a 3 GiB worker
# limit we need to leave room for the per-instance pydicom datasets
# (~0.5–1 GiB peak before they're freed), the numpy temporaries
# allocated during ``arr * slope + intercept``, the boto3 upload buffer
# (entire packed bytes), plus interpreter overhead. 1.8 GiB output
# leaves ~1.2 GiB headroom which empirically survives a 1024-slice
# 512×512 PET.
_MAX_OUTPUT_BYTES = 1800 * 1024 * 1024


def _sort_key(ds: pydicom.Dataset) -> tuple[float, int]:
    pos = getattr(ds, "ImagePositionPatient", None)
    z = float(pos[2]) if pos else 0.0
    inst = int(getattr(ds, "InstanceNumber", 0) or 0)
    return (z, inst)


class _VolumeTooLarge(Exception):
    """Raised when the projected packed volume would exceed
    ``_MAX_OUTPUT_BYTES``. Callers translate this into a non-fatal
    ``status=too_large`` outcome — the viewer falls back to its slice-
    by-slice 2D path so the user still sees the series."""


class _Unpackable(Exception):
    """Raised when the series can be loaded but cannot be packed into a
    3-D volume. Three cases today:

    * No instance has ``PixelData`` (SR / Structured Report, KO,
      report-only series, etc).
    * At least one instance is multi-frame (``NumberOfFrames > 1``):
      enhanced MR/CT, US cine, RTImage. ``_pack`` assumes one slice per
      instance, so multi-frame would broadcast-fail or silently mis-
      align the volume. Real multi-frame support would expand each
      frame as its own slice with proper per-frame functional groups —
      out of scope for this guard.
    * At least one instance is colour / RGB (``SamplesPerPixel > 1``):
      US, secondary capture, photo documentation, derived viewer
      screenshots. ``pixel_array`` has shape (rows, cols, channels)
      and would broadcast-fail into the float32 (rows, cols) slot.

    Callers translate this into a non-fatal ``status=unpackable``
    outcome (no Arq retry storm). The viewer's slice-by-slice 2D path
    still works; the user just doesn't get the volume cache."""


def _pack(s3_client, bucket: str, instance_rows: list[dict]) -> tuple[bytes, dict | None]:
    """Download DICOMs from S3, decode, stack, return ``(packed_bytes,
    geometry)``.

    ``geometry`` is the real patient-space frame computed from the same
    sorted datasets (origin / direction cosines / FrameOfReferenceUID;
    see ``compute_volume_geometry``), served back to the viewer so it
    builds the Cornerstone volume in true LPS space. ``None`` for legacy
    series without orientation tags.

    Frees each pydicom Dataset immediately after copying its pixel
    data into the output volume so peak RAM stays close to the
    output size, not 2x. Raises :class:`_VolumeTooLarge` *before*
    allocating the volume if the projected size would exceed the
    safety cap, so a 5 GiB CT total-body scan doesn't OOM-kill the
    worker on every retry.
    """
    datasets: list[pydicom.Dataset] = []
    for row in instance_rows:
        resp = s3_client.get_object(Bucket=bucket, Key=row["s3_key"])
        ds = pydicom.dcmread(io.BytesIO(resp["Body"].read()))
        if "PixelData" not in ds:
            continue
        datasets.append(ds)

    if not datasets:
        raise _Unpackable("no instances with pixel data")

    # Multi-frame DICOM: a single instance carries N frames, so
    # ``ds.pixel_array`` is shape (frames, rows, cols) instead of
    # (rows, cols). The slot-fill loop below assumes one slice per
    # instance and would broadcast-fail. Skip the whole series with a
    # non-fatal status instead of crashing the Arq job and triggering
    # a retry storm. The 2D viewer path still serves the frames.
    multi_frame = [
        ds for ds in datasets
        if int(getattr(ds, "NumberOfFrames", 1) or 1) > 1
    ]
    if multi_frame:
        n_frames = int(getattr(multi_frame[0], "NumberOfFrames", 0) or 0)
        raise _Unpackable(
            f"multi-frame DICOM not supported by pack_volume "
            f"(NumberOfFrames={n_frames}, instances={len(multi_frame)})"
        )

    # Colour / RGB DICOM: SamplesPerPixel > 1 produces a
    # (rows, cols, channels) pixel_array that won't broadcast into
    # the float32 (rows, cols) slot. Common for US, photo
    # documentation, secondary capture viewer screenshots.
    rgb = [
        ds for ds in datasets
        if int(getattr(ds, "SamplesPerPixel", 1) or 1) > 1
    ]
    if rgb:
        spp = int(getattr(rgb[0], "SamplesPerPixel", 0) or 0)
        raise _Unpackable(
            f"colour DICOM not supported by pack_volume "
            f"(SamplesPerPixel={spp}, instances={len(rgb)})"
        )

    datasets.sort(key=_sort_key)
    # Geometry MUST be computed here, before the fill loop frees each
    # dataset (datasets[i] = None below), and from the sorted order so it
    # matches the on-wire scalar layout.
    geometry = compute_volume_geometry(datasets)
    first = datasets[0]
    rows = int(first.Rows)
    cols = int(first.Columns)
    nz = len(datasets)
    # Safety cap: header is fixed size, payload is nz*rows*cols*4 bytes.
    # boto3 PUTs the whole packed buffer in one go so the in-process
    # working set effectively doubles at upload time. Reject upfront.
    projected = nz * rows * cols * 4 + HEADER_STRUCT.size
    if projected > _MAX_OUTPUT_BYTES:
        raise _VolumeTooLarge(
            f"projected volume {projected} bytes "
            f"({nz}×{rows}×{cols}×4) exceeds cap {_MAX_OUTPUT_BYTES}"
        )

    pixel_spacing = getattr(first, "PixelSpacing", [1.0, 1.0])
    sx, sy = float(pixel_spacing[1]), float(pixel_spacing[0])
    slice_thickness = float(getattr(first, "SliceThickness", 1.0) or 1.0)
    if len(datasets) >= 2:
        p0 = getattr(datasets[0], "ImagePositionPatient", None)
        p1 = getattr(datasets[1], "ImagePositionPatient", None)
        if p0 and p1:
            slice_thickness = abs(float(p1[2]) - float(p0[2])) or slice_thickness

    volume = np.empty((nz, rows, cols), dtype=np.float32)
    for i in range(nz):
        ds = datasets[i]
        slope = float(getattr(ds, "RescaleSlope", 1.0) or 1.0)
        intercept = float(getattr(ds, "RescaleIntercept", 0.0) or 0.0)
        if slope != 1.0 or intercept != 0.0:
            volume[i] = ds.pixel_array.astype(np.float32) * slope + intercept
        else:
            volume[i] = ds.pixel_array.astype(np.float32)
        # Drop the reference so pydicom can release the encoded pixel
        # bytes + the Dataset's other tag arrays. Without this every
        # slice stays alive until the whole loop finishes — peak RAM
        # ends up ~2x the output volume size.
        datasets[i] = None  # type: ignore[call-overload]

    vmin, vmax = float(volume.min()), float(volume.max())
    header = HEADER_STRUCT.pack(cols, rows, nz, sx, sy, slice_thickness, vmin, vmax)
    return header + volume.tobytes(order="C"), geometry


async def pack_volume(ctx: dict, series_id: str) -> dict:  # type: ignore[type-arg]
    """Arq task: pre-pack a series volume and cache in S3."""
    settings = get_settings()
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)

    s3 = boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint_url,
        region_name=settings.s3_region,
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key,
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )

    sid = uuid.UUID(series_id)

    async with AsyncSession(engine) as db:
        # Check if derivative already exists
        existing = await db.execute(
            text(
                "SELECT id FROM derivatives WHERE series_id = :sid AND kind = :kind AND format = :fmt"
            ),
            {"sid": sid, "kind": DERIVATIVE_KIND, "fmt": DERIVATIVE_FORMAT},
        )
        if existing.first():
            await engine.dispose()
            return {"status": "already_packed", "series_id": series_id}

        # Resolve the patient context up-front so the cache key can
        # be patient-scoped (collision-impossible across tenants).
        # ``series_instance_uid`` is no longer used for keying — it
        # could collide between unrelated tenants — but we still
        # surface a "not found" if the row vanished.
        series_row = await db.execute(
            text(
                "SELECT s.id, st.patient_id "
                "FROM series s "
                "JOIN imaging_studies st ON st.id = s.study_id "
                "WHERE s.id = :sid"
            ),
            {"sid": sid},
        )
        row = series_row.first()
        if not row:
            await engine.dispose()
            return {"status": "not_found", "series_id": series_id}
        patient_id = row[1]

        # Get instances
        result = await db.execute(
            text(
                "SELECT s3_bucket, s3_key FROM instances WHERE series_id = :sid "
                "ORDER BY instance_number ASC NULLS LAST"
            ),
            {"sid": sid},
        )
        instances = [{"s3_bucket": r[0], "s3_key": r[1]} for r in result.all()]
        if not instances:
            await engine.dispose()
            return {"status": "no_instances", "series_id": series_id}

        bucket = instances[0]["s3_bucket"]

        # Pack (sync, CPU-bound). ``_VolumeTooLarge`` and ``MemoryError``
        # are caught explicitly so a single oversize series can't
        # OOM-kill the worker. The derivative is just *not* created
        # — the viewer's slice-by-slice 2D path keeps working.
        # Other exceptions still propagate so arq retries them and
        # ``with_safety_net`` flips the row to failed.
        import asyncio

        try:
            packed_bytes, geometry = await asyncio.to_thread(_pack, s3, bucket, instances)
        except _VolumeTooLarge as exc:
            log.warning("pack_volume: skipping series %s — %s", series_id, exc)
            await engine.dispose()
            return {
                "status": "too_large",
                "series_id": series_id,
                "reason": str(exc),
            }
        except _Unpackable as exc:
            # Series we can load but can't pack as a 3-D volume —
            # SR/structured reports, multi-frame, etc. Surface as a
            # benign skipped status; the 2D viewer path still works.
            # Stops the Arq retry storm previously caused by the
            # bare ``ValueError`` propagating out of ``_pack``.
            log.info("pack_volume: skipping series %s — %s", series_id, exc)
            await engine.dispose()
            return {
                "status": "unpackable",
                "series_id": series_id,
                "reason": str(exc),
            }
        except MemoryError as exc:
            # Reached even after the pre-check (e.g. a multi-frame
            # PET expanded mid-decode). Mark and bail; do not retry.
            log.exception("pack_volume: MemoryError on series %s — %s", series_id, exc)
            await engine.dispose()
            return {
                "status": "out_of_memory",
                "series_id": series_id,
                "reason": str(exc),
            }

        # Upload to derivatives bucket. Key uses the BitVision Series
        # UUID (``sid``) inside a per-patient prefix — DICOM
        # ``series_instance_uid`` is intentionally NOT in the path
        # because that field is not authoritative across tenants.
        cache_key = volume_key(patient_id=patient_id, series_id=sid)
        s3.put_object(
            Bucket=settings.s3_bucket_derivatives,
            Key=cache_key,
            Body=packed_bytes,
            **settings.put_extra_args(),
        )

        # Record derivative. ``geometry`` carries the real patient-space
        # frame so the viewer's volume.raw fetch (which may be a cache
        # hit on this row) gets X-Volume-* headers instead of falling
        # back to an identity frame. Cast to jsonb explicitly: asyncpg
        # binds the dumped string, Postgres parses it.
        await db.execute(
            text(
                "INSERT INTO derivatives "
                "(series_id, kind, format, s3_bucket, s3_key, size_bytes, generator_version, geometry) "
                "VALUES (:sid, :kind, :fmt, :bucket, :key, :size, :ver, CAST(:geom AS jsonb))"
            ),
            {
                "sid": sid,
                "kind": DERIVATIVE_KIND,
                "fmt": DERIVATIVE_FORMAT,
                "bucket": settings.s3_bucket_derivatives,
                "key": cache_key,
                "size": len(packed_bytes),
                "ver": "worker-pack-v1",
                "geom": json.dumps(geometry) if geometry else None,
            },
        )
        await db.commit()

    await engine.dispose()
    return {
        "status": "packed",
        "series_id": series_id,
        "size_bytes": len(packed_bytes),
        "cache_key": cache_key,
    }
