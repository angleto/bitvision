"""Pre-pack a series' full-res volume AND its low-res preview on ingest.

Runs as an arq background job. Meant to be triggered by the ingestion
completion path — the moment a study finishes uploading, we want the
volume + preview sitting in S3 so the first viewer open is instant
instead of kicking off a 10-second pack.

Today this task calls back into :func:`pack_volume` (which owns the
full-res packing + derivative-row logic) and then generates the
1/8 preview from the same in-memory buffer so we don't re-download the
DICOMs twice.

TODO: wire this into the ingestion completion hook. The likely spot is
``backend/src/bvphoenix/cli/import_dicom.py`` around line ~290 where
``series_row.ingestion_complete = True`` is set — after commit, enqueue
``prefetch_series`` with the series_id. Same pattern for any future
non-CLI ingestion path (HTTP upload, S3 event, DICOMweb C-STORE SCP).
"""

from __future__ import annotations

import asyncio
import json
import struct
import uuid

import boto3
import numpy as np
from botocore.client import Config
from bvphoenix.db.engine import make_async_engine
from bvphoenix.services.derivative_keys import volume_key, volume_preview_key
from bvphoenix.services.volumes import partition_substacks
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from bvworkers.config import get_settings
from bvworkers.tasks.pack_volume import (
    _load_datasets,
    _pack_datasets,
    _Unpackable,
    _VolumeTooLarge,
)

DERIVATIVE_KIND_PREVIEW = "volume_f32_preview"
DERIVATIVE_FORMAT = "raw"
HEADER_STRUCT = struct.Struct("<3I 3f 2f")


def _downsample_packed(packed_bytes: bytes, factor: int = 2) -> bytes:
    """Same math as ``services.volumes.pack_low_res`` but operating on
    raw bytes — we don't want the worker to pull in the backend package."""
    if factor < 1:
        raise ValueError("factor must be >= 1")
    nx, ny, nz, sx, sy, sz, _vmin, _vmax = HEADER_STRUCT.unpack_from(packed_bytes)
    payload = np.frombuffer(packed_bytes, dtype=np.float32, offset=HEADER_STRUCT.size)
    volume = payload.reshape(nz, ny, nx)
    small = np.ascontiguousarray(volume[::factor, ::factor, ::factor])
    new_nz, new_ny, new_nx = small.shape
    new_spacing = (sx * factor, sy * factor, sz * factor)
    vmin = float(small.min())
    vmax = float(small.max())
    header = HEADER_STRUCT.pack(new_nx, new_ny, new_nz, *new_spacing, vmin, vmax)
    return header + small.tobytes(order="C")


async def prefetch_series(ctx: dict, series_id: str) -> dict:  # type: ignore[type-arg]
    """Pre-pack full-res volume + 1/8 preview for one series.

    Idempotent: if either derivative already exists we skip re-uploading.
    """
    settings = get_settings()
    engine = make_async_engine(settings.database_url, pool_pre_ping=True)

    s3 = boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint_url,
        region_name=settings.s3_region,
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key,
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )

    sid = uuid.UUID(series_id)
    result = {"series_id": series_id, "full": "skipped", "preview": "skipped"}

    async with AsyncSession(engine) as db:
        # Resolve patient context up-front so the cache key can be
        # patient-scoped (collision-impossible across tenants). The
        # DICOM ``series_instance_uid`` is intentionally not in the
        # path — that field is not authoritative across tenants.
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
            return {**result, "status": "not_found"}
        patient_id = row[1]

        # Do we already have the full-res blob? If so, download it so we
        # can build the preview from the cached copy without re-packing.
        # Only the PRIMARY sub-stack (index 0) is warmed here for a fast
        # first viewer open; the extra mDIXON / multi-echo contrasts pack
        # on demand (volume.raw?stack=N) or via pack_volume.
        existing_full = await db.execute(
            text(
                "SELECT s3_bucket, s3_key FROM derivatives "
                "WHERE series_id = :sid AND kind = 'volume_f32' "
                "AND format = 'raw' AND stack_index = 0"
            ),
            {"sid": sid},
        )
        full_row = existing_full.first()

        if full_row:
            resp = s3.get_object(Bucket=full_row[0], Key=full_row[1])
            packed_bytes = resp["Body"].read()
        else:
            instances_res = await db.execute(
                text(
                    "SELECT s3_bucket, s3_key FROM instances WHERE series_id = :sid "
                    "ORDER BY instance_number ASC NULLS LAST"
                ),
                {"sid": sid},
            )
            instances = [{"s3_bucket": r[0], "s3_key": r[1]} for r in instances_res.all()]
            if not instances:
                await engine.dispose()
                return {**result, "status": "no_instances"}
            bucket = instances[0]["s3_bucket"]
            # De-interleave co-located sub-stacks and warm only the primary
            # (stack 0), so a multi-stack mDIXON series never caches the
            # broken interleaved blob. Skip gracefully on unpackable /
            # oversize series — the 2D viewer path still works.
            try:
                datasets = await asyncio.to_thread(_load_datasets, s3, bucket, instances)
                stacks = partition_substacks(datasets)
                del datasets
                packed_bytes, geometry = await asyncio.to_thread(_pack_datasets, stacks[0].datasets)
            except (_Unpackable, _VolumeTooLarge, MemoryError) as exc:
                await engine.dispose()
                return {**result, "status": "skipped", "reason": str(exc)}

            cache_key = volume_key(patient_id=patient_id, series_id=sid)
            s3.put_object(
                Bucket=settings.s3_bucket_derivatives,
                Key=cache_key,
                Body=packed_bytes,
            )
            await db.execute(
                text(
                    "INSERT INTO derivatives "
                    "(series_id, kind, format, stack_index, s3_bucket, s3_key, "
                    "size_bytes, generator_version, geometry) "
                    "VALUES (:sid, 'volume_f32', 'raw', 0, :bucket, :key, :size, :ver, "
                    "CAST(:geom AS jsonb)) "
                    "ON CONFLICT (series_id, kind, format, stack_index) DO NOTHING"
                ),
                {
                    "sid": sid,
                    "bucket": settings.s3_bucket_derivatives,
                    "key": cache_key,
                    "size": len(packed_bytes),
                    "ver": "prefetch-v1",
                    "geom": json.dumps(geometry) if geometry else None,
                },
            )
            await db.commit()
            result["full"] = "packed"

        # Preview — skip if already present.
        existing_prev = await db.execute(
            text(
                "SELECT id FROM derivatives "
                "WHERE series_id = :sid AND kind = :kind AND format = :fmt"
            ),
            {"sid": sid, "kind": DERIVATIVE_KIND_PREVIEW, "fmt": DERIVATIVE_FORMAT},
        )
        if not existing_prev.first():
            preview_bytes = await asyncio.to_thread(_downsample_packed, packed_bytes, 2)
            prev_key = volume_preview_key(patient_id=patient_id, series_id=sid)
            s3.put_object(
                Bucket=settings.s3_bucket_derivatives,
                Key=prev_key,
                Body=preview_bytes,
            )
            await db.execute(
                text(
                    "INSERT INTO derivatives (series_id, kind, format, s3_bucket, s3_key, size_bytes, generator_version) "
                    "VALUES (:sid, :kind, :fmt, :bucket, :key, :size, :ver)"
                ),
                {
                    "sid": sid,
                    "kind": DERIVATIVE_KIND_PREVIEW,
                    "fmt": DERIVATIVE_FORMAT,
                    "bucket": settings.s3_bucket_derivatives,
                    "key": prev_key,
                    "size": len(preview_bytes),
                    "ver": "prefetch-v1",
                },
            )
            await db.commit()
            result["preview"] = "packed"

    await engine.dispose()
    result["status"] = "ok"
    return result
