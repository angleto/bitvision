"""Build the boolean exclusion mask used by ROI stats and hot-spot search.

Two input shapes are merged into a single ``(nz, ny, nx)`` boolean array
where ``True`` voxels are dropped from the analysis:

* ``exclude_segmentation_labels`` resolves to ``.bin`` masks under the
  ``segmentations/{series_id}/{label}.bin`` derivative-bucket layout
  written by the TotalSegmentator worker (see
  ``workers/src/bvworkers/tasks/segment_auto.py``).
* ``exclude_marker_ids`` resolves to ``Marker`` rows of kind
  ``bbox.exclusion`` whose ``geometry.min_ijk / max_ijk`` rectangle
  is set ``True`` in the mask. This is the day-1 fallback for series
  without an automatic segmentation (e.g. PET on production ARM64
  cluster before the TotalSegmentator wheel is unblocked).

Missing labels are dropped silently: a PET series whose CT companion
hasn't finished segmenting yet should still return *some* answer rather
than 422 the operator out of the workflow.

Storage isolation: bucket / key never leave this module. Callers pass
labels and marker ids; bytes are fetched server-side via the shared
``get_s3_storage()`` adapter.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import Sequence

import numpy as np
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.config import get_settings
from bvphoenix.db.models import Marker, Series
from bvphoenix.storage import get_s3_storage

_LABEL_RE = re.compile(r"^[a-zA-Z0-9._-]{1,64}$")


def _seg_key(series_id: uuid.UUID, label: str) -> str:
    return f"segmentations/{series_id}/{label}.bin"


async def _load_segmentation_mask(
    bucket: str,
    series_id: uuid.UUID,
    label: str,
    shape_zyx: tuple[int, int, int],
) -> np.ndarray | None:
    """Fetch one mask blob and return a boolean ``(nz, ny, nx)`` array,
    or ``None`` when the object is missing / has the wrong byte count.

    The ``.bin`` wire format is ``uint8``, x-fastest (i + j*nx + k*nx*ny),
    same layout the TotalSegmentator worker writes. We reshape into
    ``(nz, ny, nx)`` to match the packed F32 volume convention used by
    ``find_series_hot_spots`` and ``compute_series_roi_stats``.
    """
    storage = get_s3_storage()
    try:
        raw = await asyncio.to_thread(
            storage.get_object_bytes,
            bucket=bucket,
            key=_seg_key(series_id, label),
        )
    except Exception:
        return None

    nz, ny, nx = shape_zyx
    expected = nx * ny * nz
    if len(raw) != expected:
        # Wrong-size mask: surface as silent drop rather than a 500. The
        # auto-segmentation may still be in progress and have written a
        # partial blob.
        return None

    arr = np.frombuffer(raw, dtype=np.uint8).reshape(nz, ny, nx)
    return arr > 0


async def _load_marker_bbox(
    db: AsyncSession,
    marker_id: uuid.UUID,
    series_id: uuid.UUID,
    shape_zyx: tuple[int, int, int],
) -> tuple[int, int, int, int, int, int] | None:
    """Return ``(i0, j0, k0, i1, j1, k1)`` clamped to volume bounds for
    a ``bbox.exclusion`` marker attached to ``series_id``, or ``None``
    when the marker is absent / wrong kind / cross-series."""
    marker = (
        await db.execute(
            select(Marker).where(Marker.id == marker_id, Marker.kind == "bbox.exclusion")
        )
    ).scalar_one_or_none()
    if marker is None:
        return None

    # bbox.exclusion can be attached either directly to the series or to
    # the parent study; both are valid because the geometry is stored in
    # voxel indices of that specific series volume.
    if marker.target_kind == "series" and marker.target_id != series_id:
        return None

    geo = marker.geometry or {}
    min_ijk = geo.get("min_ijk")
    max_ijk = geo.get("max_ijk")
    if (
        not isinstance(min_ijk, list)
        or not isinstance(max_ijk, list)
        or len(min_ijk) != 3
        or len(max_ijk) != 3
    ):
        return None

    nz, ny, nx = shape_zyx
    try:
        i0 = max(0, int(min_ijk[0]))
        j0 = max(0, int(min_ijk[1]))
        k0 = max(0, int(min_ijk[2]))
        i1 = min(int(nx) - 1, int(max_ijk[0]))
        j1 = min(int(ny) - 1, int(max_ijk[1]))
        k1 = min(int(nz) - 1, int(max_ijk[2]))
    except (TypeError, ValueError):
        return None

    if i0 > i1 or j0 > j1 or k0 > k1:
        return None
    return (i0, j0, k0, i1, j1, k1)


async def build_exclusion_mask(
    db: AsyncSession,
    series: Series,
    shape_zyx: tuple[int, int, int],
    exclude_segmentation_labels: Sequence[str] | None,
    exclude_marker_ids: Sequence[uuid.UUID] | None,
) -> np.ndarray | None:
    """Compose a boolean ``(nz, ny, nx)`` exclusion mask. ``True`` voxels
    are excluded. Returns ``None`` when no exclusion was requested or
    when every requested input resolved to nothing — caller can keep the
    fast path with no mask AND.

    The function silently drops:
    * labels that don't exist in the bucket (mid-flight seg job, or label
      not produced for this FOV);
    * labels that fail the safety regex (defense in depth for callers
      forwarding user input);
    * marker ids that don't exist, are wrong kind, or point to a
      different series.

    A 422 here would be hostile to the operator: the right UX is to show
    the operator which labels were excluded vs not in the audit trail
    rather than to abort the whole find.
    """
    labels = list(exclude_segmentation_labels or ())
    marker_ids = list(exclude_marker_ids or ())
    if not labels and not marker_ids:
        return None

    nz, ny, nx = shape_zyx
    settings = get_settings()
    derivatives_bucket = settings.s3_bucket_derivatives

    exclusion: np.ndarray | None = None

    for label in labels:
        if not _LABEL_RE.match(label):
            continue
        mask = await _load_segmentation_mask(
            bucket=derivatives_bucket,
            series_id=series.id,
            label=label,
            shape_zyx=shape_zyx,
        )
        if mask is None:
            continue
        if exclusion is None:
            exclusion = mask.copy()
        else:
            exclusion |= mask

    for marker_id in marker_ids:
        bbox = await _load_marker_bbox(
            db=db,
            marker_id=marker_id,
            series_id=series.id,
            shape_zyx=shape_zyx,
        )
        if bbox is None:
            continue
        i0, j0, k0, i1, j1, k1 = bbox
        if exclusion is None:
            exclusion = np.zeros((nz, ny, nx), dtype=bool)
        exclusion[k0 : k1 + 1, j0 : j1 + 1, i0 : i1 + 1] = True

    return exclusion
