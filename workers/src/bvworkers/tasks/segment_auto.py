"""Automatic anatomical segmentation via TotalSegmentator.

Runs on CPU by default (the engine supports it; expect ~10-30 min per
abdominal CT depending on the host). The task pulls the packed F32
volume from the derivatives bucket, materialises a NIfTI on a tmp
path, runs the TotalSegmentator pipeline restricted to the requested
``roi_subset``, and uploads each output mask back to the derivatives
bucket as a raw ``uint8`` buffer at
``segmentations/{series_id}/{label}.bin`` — the same layout the
``segmentations`` API serves to the viewer, so the produced masks
become immediately consumable without an extra round-trip through
the upload endpoint.

Designed to be idempotent: existing labels are left in place by
default and only overwritten when ``overwrite=True`` is requested.
"""

from __future__ import annotations

import asyncio
import logging
import struct
import tempfile
import uuid
from pathlib import Path
from typing import Any

import boto3
import numpy as np
from botocore.client import Config

from bvworkers.config import get_settings

logger = logging.getLogger(__name__)


# Mapping from TotalSegmentator output filenames (without extension)
# to the labels the viewer expects. TotalSegmentator follows the
# ``totalsegmentator/resources/labels.py`` naming, which is itself
# stable across versions. We keep an explicit allowlist so a future
# upstream rename surfaces as a missing-label error instead of a
# silent drop.
DEFAULT_ROI_SUBSET: tuple[str, ...] = (
    "liver",
    "kidney_right",
    "kidney_left",
    "urinary_bladder",
    "spleen",
    "pancreas",
    "stomach",
    "gallbladder",
    "aorta",
    "inferior_vena_cava",
    "portal_vein_and_splenic_vein",
    "lung_upper_lobe_left",
    "lung_lower_lobe_left",
    "lung_upper_lobe_right",
    "lung_middle_lobe_right",
    "lung_lower_lobe_right",
    "heart",
)


# 32-byte packed-volume header. Mirrors ``HEADER_STRUCT`` in
# ``backend/src/bvphoenix/services/volumes.py`` — duplicated rather
# than imported so the worker can run without the backend on its
# PYTHONPATH. The format guarantee is part of the wire contract.
HEADER_STRUCT = struct.Struct("<3I 3f 2f")


def _s3_client(settings: Any) -> Any:
    return boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint_url,
        region_name=settings.s3_region,
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key,
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )


def _fetch_packed_volume(
    s3: Any,
    bucket: str,
    key: str,
) -> tuple[np.ndarray, tuple[float, float, float], tuple[int, int, int]]:
    """Download the packed volume and return (scalar array, spacing,
    dimensions). Array shape is ``(nz, ny, nx)`` — Z fastest along the
    outer axis is what nibabel's affine math expects."""
    obj = s3.get_object(Bucket=bucket, Key=key)
    body: bytes = obj["Body"].read()
    nx, ny, nz, sx, sy, sz, _vmin, _vmax = HEADER_STRUCT.unpack_from(body, 0)
    n = nx * ny * nz
    scalars = np.frombuffer(body, dtype=np.float32, count=n, offset=HEADER_STRUCT.size)
    # Wire format is x-fastest (i + j*nx + k*nx*ny). Reshape to
    # ``(nz, ny, nx)`` so axis 0 is Z, matching nibabel's "image space"
    # convention where the data array is ``(X, Y, Z)`` AFTER a final
    # transpose. We keep ``(nz, ny, nx)`` here and transpose just-in-time
    # before nibabel sees it.
    arr = scalars.reshape(nz, ny, nx)
    return arr, (float(sx), float(sy), float(sz)), (int(nx), int(ny), int(nz))


def _write_nifti(
    arr_zyx: np.ndarray,
    spacing: tuple[float, float, float],
    out_path: Path,
) -> None:
    """Write a NIfTI file TotalSegmentator can read. The affine encodes
    voxel spacing on the diagonal; orientation is RAS-equivalent for
    the worker's purposes (TotalSegmentator runs voxel-space
    inference, the affine is preserved on the output mask)."""
    import nibabel as nib

    sx, sy, sz = spacing
    affine = np.diag([sx, sy, sz, 1.0]).astype(np.float64)
    # nibabel expects ``(X, Y, Z)``; we have ``(nz, ny, nx)`` so
    # transpose to ``(nx, ny, nz)``.
    data_xyz = np.transpose(arr_zyx, (2, 1, 0)).astype(np.float32, copy=False)
    img = nib.Nifti1Image(data_xyz, affine)
    nib.save(img, str(out_path))


def _read_mask_to_bytes(path: Path, target_shape_zyx: tuple[int, int, int]) -> bytes:
    """Read a TotalSegmentator output NIfTI mask, resample to the
    target volume's shape if needed, and return the raw uint8 buffer
    in x-fastest order matching the segmentations API contract."""
    import nibabel as nib

    img = nib.load(str(path))
    data = np.asarray(img.dataobj)
    # nibabel data is ``(X, Y, Z)`` — transpose to ``(Z, Y, X)``.
    arr_zyx = np.transpose(data, (2, 1, 0))
    # Binarise — TotalSegmentator outputs 0/1 already, but a safety
    # cast in case future versions emit multi-class.
    mask_zyx = (arr_zyx != 0).astype(np.uint8)
    if mask_zyx.shape != target_shape_zyx:
        # Nearest-neighbour resample to target shape. Same pattern as
        # ``services.segmentation_import._resample_nearest`` (mirrored
        # here so the worker can produce masks that match the viewer's
        # packed volume even if TotalSegmentator's internal resampling
        # diverges).
        src_z, src_y, src_x = mask_zyx.shape
        tgt_z, tgt_y, tgt_x = target_shape_zyx
        z_idx = np.clip((np.arange(tgt_z) * src_z / tgt_z).astype(np.int64), 0, src_z - 1)
        y_idx = np.clip((np.arange(tgt_y) * src_y / tgt_y).astype(np.int64), 0, src_y - 1)
        x_idx = np.clip((np.arange(tgt_x) * src_x / tgt_x).astype(np.int64), 0, src_x - 1)
        mask_zyx = mask_zyx[np.ix_(z_idx, y_idx, x_idx)]
    # Flatten in (Z, Y, X) → x-fastest by reversing the iteration:
    # numpy's default C order with shape ``(nz, ny, nx)`` is
    # k-major, so contiguous bytes are already x-fastest.
    return np.ascontiguousarray(mask_zyx).tobytes()


def _run_totalsegmentator(
    nifti_in: Path,
    out_dir: Path,
    roi_subset: list[str],
    fast: bool,
) -> None:
    """Invoke the TotalSegmentator engine. Lazy import so workers
    without the ``seg`` extra don't pay the import cost."""
    try:
        from totalsegmentator.python_api import totalsegmentator
    except ImportError as exc:  # pragma: no cover — extra not installed
        raise RuntimeError(
            "totalsegmentator not installed; run ``uv sync --extra seg`` on the worker host"
        ) from exc

    out_dir.mkdir(parents=True, exist_ok=True)
    totalsegmentator(
        input=str(nifti_in),
        output=str(out_dir),
        task="total",
        fast=fast,
        roi_subset=roi_subset,
        device="cpu",
        output_type="nifti",
        ml=False,  # one file per organ instead of a single multilabel volume
        quiet=True,
        verbose=False,
    )


def _totalseg_version() -> str | None:
    """Best-effort TotalSegmentator package version for provenance."""
    try:
        import importlib.metadata

        return importlib.metadata.version("totalsegmentator")
    except Exception:
        return None


async def _persist_segmentation_rows(
    settings: Any,
    series_id: uuid.UUID,
    *,
    metas: list[tuple[str, str, int, int]],
    patient_id: str | None,
    author_kind: str,
    agent_token_id: str | None,
    created_by: str | None,
) -> None:
    """Upsert one Segmentation ORM row per produced mask, with provenance.
    Idempotent on (series_id, producer, label) so re-runs replace cleanly.
    Best-effort: a DB failure here never loses the masks already in S3."""
    from bvphoenix.db.models import Segmentation
    from sqlalchemy import func
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    def _uid(v: str | None) -> uuid.UUID | None:
        try:
            return uuid.UUID(v) if v else None
        except (ValueError, TypeError):
            return None

    pid, token, actor = _uid(patient_id), _uid(agent_token_id), _uid(created_by)
    version = _totalseg_version()
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    try:
        async with AsyncSession(engine) as db:
            for label, key, size_bytes, nonzero in metas:
                stmt = (
                    pg_insert(Segmentation)
                    .values(
                        series_id=series_id,
                        patient_id=pid,
                        producer="totalsegmentator",
                        producer_version=version,
                        label=label,
                        s3_bucket=settings.s3_bucket_derivatives,
                        s3_key=key,
                        size_bytes=size_bytes,
                        nonzero_voxels=nonzero,
                        label_map={},
                        author_kind=author_kind,
                        agent_token_id=token,
                        created_by_subject_id=actor,
                    )
                    .on_conflict_do_update(
                        constraint="uq_segmentations_series_producer_label",
                        set_={
                            "patient_id": pid,
                            "s3_key": key,
                            "size_bytes": size_bytes,
                            "nonzero_voxels": nonzero,
                            "producer_version": version,
                            "author_kind": author_kind,
                            "agent_token_id": token,
                            "created_by_subject_id": actor,
                            "created_at": func.now(),
                        },
                    )
                )
                await db.execute(stmt)
            await db.commit()
    except Exception:
        logger.exception("segmentation ORM persist failed for series %s", series_id)
    finally:
        await engine.dispose()


async def segment_auto(
    ctx: dict[str, Any],
    series_id: str,
    roi_subset: list[str] | None = None,
    overwrite: bool = False,
    fast: bool = True,
    *,
    author_kind: str = "system",
    agent_token_id: str | None = None,
    created_by: str | None = None,
    patient_id: str | None = None,
) -> dict[str, Any]:
    """Arq task: run TotalSegmentator on the series's packed volume
    and upload one binary mask per ROI to the derivatives bucket.

    ``fast=True`` (default) uses TotalSegmentator's 3 mm model, which
    is the only sane choice on CPU — the standard 1.5 mm model takes
    >1 hour for a typical CT abdomen on a desktop CPU. The 3 mm
    output is more than enough for volume-rendering masking; it loses
    fine vessel detail but on a CPU host that's the trade-off.
    """
    settings = get_settings()
    s3 = _s3_client(settings)
    sid = uuid.UUID(series_id)
    rois = list(roi_subset) if roi_subset else list(DEFAULT_ROI_SUBSET)
    if not rois:
        return {"status": "no_rois", "series_id": series_id}

    # Skip ROIs that already have a mask unless ``overwrite`` is set.
    # Avoids re-running the model on labels already produced by an
    # earlier auto-segment run or a manual upload.
    if not overwrite:
        prefix = f"segmentations/{sid}/"
        existing: set[str] = set()
        try:
            paginator = s3.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=settings.s3_bucket_derivatives, Prefix=prefix):
                for obj in page.get("Contents", []):
                    key = obj["Key"]
                    label = key[len(prefix) :].removesuffix(".bin")
                    existing.add(label)
        except Exception:
            existing = set()
        rois = [r for r in rois if r not in existing]
        if not rois:
            return {
                "status": "all_present",
                "series_id": series_id,
                "produced": [],
            }

    # Look up the packed volume key in the derivatives bucket. We
    # build it the same way ``services.volumes`` does: keyed on
    # series_instance_uid, not the row id, since the file lives in
    # an external S3 namespace.
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    try:
        async with AsyncSession(engine) as db:
            row = (
                await db.execute(
                    text(
                        "SELECT s.series_instance_uid, d.s3_bucket, d.s3_key, st.patient_id "
                        "FROM series s "
                        "LEFT JOIN derivatives d "
                        "  ON d.series_id = s.id AND d.kind = 'volume_f32' "
                        "  AND d.stack_index = 0 "
                        "LEFT JOIN imaging_studies st ON st.id = s.study_id "
                        "WHERE s.id = :sid"
                    ),
                    {"sid": sid},
                )
            ).first()
            if row is None:
                return {"status": "series_not_found", "series_id": series_id}
            _series_uid, vol_bucket, vol_key, db_patient_id = row
            if not vol_bucket or not vol_key:
                return {
                    "status": "volume_not_packed",
                    "series_id": series_id,
                    "hint": "trigger pack_volume first",
                }
    finally:
        await engine.dispose()

    arr_zyx, spacing, dims = await asyncio.to_thread(_fetch_packed_volume, s3, vol_bucket, vol_key)
    nx, ny, nz = dims

    produced: list[str] = []
    failures: list[dict[str, str]] = []
    # (label, s3_key, size_bytes, nonzero_voxels) for the ORM promotion.
    produced_meta: list[tuple[str, str, int, int]] = []

    with tempfile.TemporaryDirectory(prefix="bvp-totalseg-") as td:
        tmp = Path(td)
        nifti_in = tmp / "input.nii.gz"
        out_dir = tmp / "out"
        await asyncio.to_thread(_write_nifti, arr_zyx, spacing, nifti_in)
        try:
            await asyncio.to_thread(_run_totalsegmentator, nifti_in, out_dir, rois, fast)
        except Exception as exc:
            logger.exception("totalsegmentator failed for series %s", series_id)
            return {
                "status": "engine_error",
                "series_id": series_id,
                "error": str(exc),
            }

        for roi in rois:
            mask_path = out_dir / f"{roi}.nii.gz"
            if not mask_path.exists():
                # Some TotalSegmentator versions emit ``.nii`` (not
                # gzipped) when the host's nibabel is configured that
                # way. Try the unzipped path as a fallback.
                alt = out_dir / f"{roi}.nii"
                if alt.exists():
                    mask_path = alt
                else:
                    failures.append({"label": roi, "error": "missing output file"})
                    continue
            try:
                mask_bytes = await asyncio.to_thread(_read_mask_to_bytes, mask_path, (nz, ny, nx))
            except Exception as exc:
                failures.append({"label": roi, "error": str(exc)})
                continue
            key = f"segmentations/{sid}/{roi}.bin"
            await asyncio.to_thread(
                lambda b=mask_bytes, k=key: s3.put_object(
                    Bucket=settings.s3_bucket_derivatives,
                    Key=k,
                    Body=b,
                    **settings.put_extra_args(),
                )
            )
            nonzero = int((np.frombuffer(mask_bytes, dtype=np.uint8) != 0).sum())
            produced.append(roi)
            produced_meta.append((roi, key, len(mask_bytes), nonzero))

    # Promote the produced masks to Segmentation ORM rows with provenance.
    # The worker has no request context — author_kind / agent_token /
    # created_by were threaded through from the enqueue site; patient_id
    # falls back to the study's patient resolved above. Best-effort: a
    # persist failure must not lose the masks already in S3.
    if produced_meta:
        await _persist_segmentation_rows(
            settings,
            sid,
            metas=produced_meta,
            patient_id=patient_id or (str(db_patient_id) if db_patient_id else None),
            author_kind=author_kind,
            agent_token_id=agent_token_id,
            created_by=created_by,
        )

    return {
        "status": "ok" if produced else "no_output",
        "series_id": series_id,
        "produced": produced,
        "failures": failures,
        "fast_mode": fast,
        "cpu_only": True,
    }


# Re-exported only for clarity at task-registration time.
__all__ = ["DEFAULT_ROI_SUBSET", "segment_auto"]
