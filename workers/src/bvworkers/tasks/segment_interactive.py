"""Interactive 2D segmentation via MedSAM-2 (Segment Anything 2,
medical-domain weights).

Given a single click (or a list of clicks) on one slice of the volume,
returns a binary mask for that slice. The viewer uses this for the
"magic-wand" tool: click on the liver, get a 2D contour back, optionally
extend through neighbouring slices.

The model is loaded lazily on first call and kept resident in the
worker process — Arq workers run as long-lived asyncio loops, so the
amortised cost is one model load per worker lifetime.
"""

from __future__ import annotations

import asyncio
import logging
import struct
import uuid
from typing import Any

import boto3
import numpy as np
from botocore.client import Config

from bvworkers.config import get_settings

logger = logging.getLogger(__name__)


HEADER_STRUCT = struct.Struct("<3I 3f 2f")


# Lazily-loaded model handle. ``None`` until the first prediction
# call. Subsequent calls reuse the same instance.
_MODEL: Any = None


def _ensure_model() -> Any:
    """Load MedSAM-2 weights on first use. Falls back to a runtime
    error with a clear hint when the optional dependency isn't
    installed (the platform extra is ``seg``)."""
    global _MODEL
    if _MODEL is not None:
        return _MODEL
    try:
        # ``segment_anything_2`` ships predictor classes plus a model
        # registry. MedSAM-2 weights are released as a checkpoint
        # compatible with SAM-2's ``build_sam2`` factory; users either
        # provide a path via ``BVP_MEDSAM_CKPT`` or rely on the
        # default which falls back to vanilla SAM-2 (still usable on
        # contrast-enhanced CT, just less precise on muscle / fat).
        import os

        from sam2.build_sam import build_sam2  # type: ignore[import-not-found]
        from sam2.sam2_image_predictor import (  # type: ignore[import-not-found]
            SAM2ImagePredictor,
        )

        ckpt = os.environ.get("BVP_MEDSAM_CKPT", "facebook/sam2-hiera-tiny")
        cfg = os.environ.get("BVP_MEDSAM_CFG", "sam2_hiera_t.yaml")
        sam2 = build_sam2(cfg, ckpt, device="cpu")
        _MODEL = SAM2ImagePredictor(sam2)
    except ImportError as exc:  # pragma: no cover — extra not installed
        raise RuntimeError(
            "segment-anything-2 not installed; run ``uv sync --extra seg`` on the worker host"
        ) from exc
    return _MODEL


def _s3_client(settings: Any) -> Any:
    return boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint_url,
        region_name=settings.s3_region,
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key,
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )


def _fetch_volume_header_and_slice(
    s3: Any,
    bucket: str,
    key: str,
    axis: int,
    slice_idx: int,
) -> tuple[np.ndarray, tuple[int, int, int]]:
    """Stream a single 2D slice out of the packed volume without
    materialising the full array. We pre-fetch the 32-byte header,
    derive offsets, then issue a Range request for just the slice's
    voxels. Saves bandwidth on large CTs (~10× faster than pulling
    the whole 500 MiB volume for a 256 KiB slice)."""
    head = s3.get_object(Bucket=bucket, Key=key, Range=f"bytes=0-{HEADER_STRUCT.size - 1}")[
        "Body"
    ].read()
    nx, ny, nz, _sx, _sy, _sz, _vmin, _vmax = HEADER_STRUCT.unpack_from(head, 0)
    if axis == 2:
        # Axial: slice spans nx*ny voxels at fixed Z. F32 = 4 bytes.
        per_slice = nx * ny * 4
        offset = HEADER_STRUCT.size + slice_idx * per_slice
    elif axis == 1:
        # Sagittal-ish: stride'd reads — fall back to fetching the
        # whole volume since random access through Range gets
        # expensive (one Range per row). Same logic as full-volume
        # fetch in segment_auto, kept here so we don't import
        # cross-task.
        body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        n = nx * ny * nz
        full = np.frombuffer(body, dtype=np.float32, count=n, offset=HEADER_STRUCT.size).reshape(
            nz, ny, nx
        )
        return full[:, slice_idx, :].astype(np.float32, copy=True), (nx, ny, nz)
    else:
        body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        n = nx * ny * nz
        full = np.frombuffer(body, dtype=np.float32, count=n, offset=HEADER_STRUCT.size).reshape(
            nz, ny, nx
        )
        return full[:, :, slice_idx].astype(np.float32, copy=True), (nx, ny, nz)
    end = offset + per_slice - 1
    body = s3.get_object(Bucket=bucket, Key=key, Range=f"bytes={offset}-{end}")["Body"].read()
    arr = np.frombuffer(body, dtype=np.float32, count=nx * ny).reshape(ny, nx)
    return arr, (int(nx), int(ny), int(nz))


def _normalise_for_sam(slice_arr: np.ndarray) -> np.ndarray:
    """Window the float scalar slice into [0, 255] uint8 RGB so SAM-2
    can ingest it. Uses a robust 1-99 percentile so contrast-enhanced
    CTs render with usable dynamic range without the user having to
    pick a window. Output shape ``(H, W, 3)``.
    """
    flat = slice_arr.flatten()
    finite = flat[np.isfinite(flat)]
    if finite.size == 0:
        return np.zeros((*slice_arr.shape, 3), dtype=np.uint8)
    lo, hi = np.percentile(finite, (1, 99))
    if hi - lo < 1e-3:
        lo, hi = finite.min(), finite.max() or 1.0
    win = np.clip((slice_arr - lo) / max(1e-6, hi - lo), 0, 1)
    g = (win * 255).astype(np.uint8)
    return np.stack([g, g, g], axis=-1)


def _run_predictor(
    image_rgb: np.ndarray,
    point_coords: list[tuple[float, float]],
    point_labels: list[int],
) -> np.ndarray:
    """Single inference. Returns a uint8 mask shaped ``(H, W)`` with
    1 = foreground, 0 = background. Picks the highest-scoring of the
    three candidate masks SAM-2 returns."""
    predictor = _ensure_model()
    predictor.set_image(image_rgb)
    coords = np.array(point_coords, dtype=np.float32)
    labels = np.array(point_labels, dtype=np.int32)
    masks, scores, _ = predictor.predict(
        point_coords=coords,
        point_labels=labels,
        multimask_output=True,
    )
    best = int(np.argmax(scores))
    return (masks[best] > 0).astype(np.uint8)


async def medsam_predict_2d(
    ctx: dict[str, Any],
    series_id: str,
    axis: int,
    slice_idx: int,
    point_coords: list[tuple[float, float]],
    point_labels: list[int] | None = None,
) -> dict[str, Any]:
    """Arq task: run a single MedSAM-2 inference on one slice.

    ``axis``: 0 = sagittal (X-fixed), 1 = coronal (Y-fixed),
              2 = axial (Z-fixed). The viewer picks the axis matching
              the active viewport; clicks are passed in voxel coords
              of that 2D slice.
    ``point_coords``: list of (x, y) pairs in slice coordinates.
    ``point_labels``: optional 0/1 label per point (1 = include,
                      0 = exclude). Defaults to all-1 when omitted.

    Returns ``{ status, mask_b64, shape }``. The mask is base64-encoded
    raw uint8 bytes so the API layer can hand it back to the frontend
    without forcing JSON-unfriendly binary into the response envelope.
    """
    settings = get_settings()
    sid = uuid.UUID(series_id)
    s3 = _s3_client(settings)
    if point_labels is None:
        point_labels = [1] * len(point_coords)
    if len(point_labels) != len(point_coords):
        return {
            "status": "bad_request",
            "error": "point_labels length must match point_coords",
        }

    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    try:
        async with AsyncSession(engine) as db:
            row = (
                await db.execute(
                    text(
                        "SELECT d.s3_bucket, d.s3_key "
                        "FROM derivatives d "
                        "WHERE d.series_id = :sid AND d.kind = 'volume_f32' "
                        "AND d.stack_index = 0"
                    ),
                    {"sid": sid},
                )
            ).first()
            if row is None:
                return {
                    "status": "volume_not_packed",
                    "series_id": series_id,
                }
            vol_bucket, vol_key = row
    finally:
        await engine.dispose()

    slice_arr, dims = await asyncio.to_thread(
        _fetch_volume_header_and_slice,
        s3,
        vol_bucket,
        vol_key,
        axis,
        slice_idx,
    )
    rgb = _normalise_for_sam(slice_arr)
    try:
        mask = await asyncio.to_thread(_run_predictor, rgb, point_coords, point_labels)
    except Exception as exc:
        logger.exception("MedSAM prediction failed for series %s", series_id)
        return {
            "status": "engine_error",
            "series_id": series_id,
            "error": str(exc),
        }

    import base64

    mask_b64 = base64.b64encode(mask.tobytes()).decode("ascii")
    return {
        "status": "ok",
        "shape": list(mask.shape),
        "mask_b64": mask_b64,
        "axis": axis,
        "slice_idx": slice_idx,
        "volume_dims": list(dims),
    }


__all__ = ["medsam_predict_2d"]
