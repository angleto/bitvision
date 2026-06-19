"""World-space ROI sampling on packed Float32 volumes.

The single-series ``/series/{id}/roi-stats`` endpoint samples an ROI given
in that series' voxel (ijk) frame. Cross-phase washout needs the opposite:
ONE region of anatomy, defined in patient space (LPS world coordinates),
sampled in EVERY phase's own grid. This module provides:

* ``world_to_ijk`` — invert a packed volume's geometry (the exact inverse
  of ``services.volumes.compute_volume_geometry``: direction rows are the
  row / column / slice cosines, origin is the IPP of voxel (0,0,0)) so a
  world point maps to fractional voxel indices in any phase's grid;
* ``sample_sphere_hu`` / ``sample_bbox_hu`` — mean / std / min / max over a
  spherical or axis-aligned ROI on the HU volume (the packed scalars are
  already rescale-applied, so the values are true HU).

Kept deliberately narrower than ``api.studies.roi_stats`` (no SUV scaling,
no PERCIST 1 cm³ peak, no exclusion masks): washout only needs the regional
mean HU per phase. The sphere-mask geometry mirrors that endpoint; a future
consolidation could share a single core if the richer variant is factored
apart from its SUV/exclusion concerns.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class RoiHuStats:
    voxel_count: int
    mean: float
    std: float
    min: float
    max: float


def world_to_ijk(
    world: tuple[float, float, float],
    geometry: dict | None,
    spacing: tuple[float, float, float],
) -> tuple[float, float, float] | None:
    """Map a patient-space (LPS) point to fractional voxel indices (i, j, k)
    of a packed volume, or ``None`` when the volume has no real geometry.

    ``geometry`` is ``derivatives.geometry`` (origin + 9-element direction).
    ``spacing`` is ``(sx, sy, sz)`` from the packed header. i indexes the
    fastest axis (row cosine), j the column cosine, k the slice axis —
    matching ``arr.reshape(nz, ny, nx)[k, j, i]``.
    """
    if not geometry:
        return None
    origin = geometry.get("origin")
    direction = geometry.get("direction")
    if not origin or not direction or len(origin) != 3 or len(direction) != 9:
        return None
    sx, sy, sz = spacing
    if sx <= 0 or sy <= 0 or sz <= 0:
        return None
    o = np.asarray(origin, dtype=np.float64)
    d = np.asarray(direction, dtype=np.float64).reshape(3, 3)  # rows: row, col, slice
    rel = np.asarray(world, dtype=np.float64) - o
    i = float(np.dot(d[0], rel) / sx)
    j = float(np.dot(d[1], rel) / sy)
    k = float(np.dot(d[2], rel) / sz)
    return (i, j, k)


# ---- ranged-read slab helpers -----------------------------------------------
#
# A packed volume is ``[header][float32 z,y,x]`` and slice k is one contiguous
# ``ny*nx*4`` block. An ROI only touches a few slices, so the wash-out endpoint
# can ranged-GET just those slices instead of downloading the whole 100-500 MB
# volume (the dominant wash-out latency over the bandwidth-limited egress).


def slab_k_range_sphere(ck: float, radius_mm: float, sz: float, nz: int) -> tuple[int, int]:
    """Inclusive slice range [k0, k1] that fully contains a sphere of
    ``radius_mm`` centred at fractional slice ``ck`` — so a ranged read of just
    these slices suffices to sample the ROI."""
    if sz <= 0 or nz <= 0:
        return (0, max(0, nz - 1))
    half_k = max(1, int(np.ceil(radius_mm / sz)))
    k0 = max(0, int(np.floor(ck)) - half_k)
    k1 = min(nz - 1, int(np.ceil(ck)) + half_k)
    return (k0, k1)


def slab_k_range_bbox(k_a: float, k_b: float, nz: int) -> tuple[int, int]:
    """Inclusive slice range covering a bbox spanning fractional slices."""
    if nz <= 0:
        return (0, 0)
    k0 = max(0, int(np.floor(min(k_a, k_b))))
    k1 = min(nz - 1, int(np.ceil(max(k_a, k_b))))
    return (k0, k1)


def slab_byte_range(k0: int, k1: int, ny: int, nx: int, header_size: int) -> tuple[int, int]:
    """Byte ``(start, length)`` of inclusive slices ``k0..k1`` in a packed
    Float32 volume (slice k is contiguous after the header)."""
    per_slice = ny * nx * 4
    return (header_size + k0 * per_slice, (k1 - k0 + 1) * per_slice)


def sample_sphere_hu(
    arr_zyx: np.ndarray,
    spacing: tuple[float, float, float],
    center_ijk: tuple[float, float, float],
    radius_mm: float,
) -> RoiHuStats:
    """Sample HU over a physical-space sphere centred at ``center_ijk``.

    Raises ``ValueError`` when the sphere encloses no in-bounds voxel.
    """
    nz, ny, nx = arr_zyx.shape
    sx, sy, sz = spacing
    ci, cj, ck = center_ijk
    ci_i = round(ci)
    cj_i = round(cj)
    ck_i = round(ck)
    half_i = max(1, int(np.ceil(radius_mm / sx)))
    half_j = max(1, int(np.ceil(radius_mm / sy)))
    half_k = max(1, int(np.ceil(radius_mm / sz)))
    i0 = max(0, ci_i - half_i)
    i1 = min(nx - 1, ci_i + half_i)
    j0 = max(0, cj_i - half_j)
    j1 = min(ny - 1, cj_i + half_j)
    k0 = max(0, ck_i - half_k)
    k1 = min(nz - 1, ck_i + half_k)
    if i0 > i1 or j0 > j1 or k0 > k1:
        raise ValueError("sphere center is outside the volume")
    sub = arr_zyx[k0 : k1 + 1, j0 : j1 + 1, i0 : i1 + 1]
    # Physical distance (mm) of each voxel centre from the fractional sphere
    # centre, per axis, then the spherical mask in true physical space.
    kk = (np.arange(k0, k1 + 1) - ck) * sz
    jj = (np.arange(j0, j1 + 1) - cj) * sy
    ii = (np.arange(i0, i1 + 1) - ci) * sx
    dist2 = kk[:, None, None] ** 2 + jj[None, :, None] ** 2 + ii[None, None, :] ** 2
    mask = dist2 <= (radius_mm * radius_mm)
    vals = sub[mask]
    if vals.size == 0:
        raise ValueError("sphere encloses zero voxels (radius too small for spacing)")
    return _stats(vals)


def sample_bbox_hu(
    arr_zyx: np.ndarray,
    min_ijk: tuple[float, float, float],
    max_ijk: tuple[float, float, float],
) -> RoiHuStats:
    """Sample HU over an axis-aligned bbox (inclusive), clamped to bounds.

    Index-space (no spacing needed): ``min_ijk`` / ``max_ijk`` are voxel
    indices (the caller maps a world bbox to ijk via :func:`world_to_ijk`)."""
    nz, ny, nx = arr_zyx.shape
    i0 = max(0, round(min(min_ijk[0], max_ijk[0])))
    j0 = max(0, round(min(min_ijk[1], max_ijk[1])))
    k0 = max(0, round(min(min_ijk[2], max_ijk[2])))
    i1 = min(nx - 1, round(max(min_ijk[0], max_ijk[0])))
    j1 = min(ny - 1, round(max(min_ijk[1], max_ijk[1])))
    k1 = min(nz - 1, round(max(min_ijk[2], max_ijk[2])))
    if i0 > i1 or j0 > j1 or k0 > k1:
        raise ValueError("bbox is empty after clamping to volume bounds")
    sub = arr_zyx[k0 : k1 + 1, j0 : j1 + 1, i0 : i1 + 1]
    if sub.size == 0:
        raise ValueError("bbox encloses zero voxels")
    return _stats(sub.reshape(-1))


def _stats(vals: np.ndarray) -> RoiHuStats:
    return RoiHuStats(
        voxel_count=int(vals.size),
        mean=float(vals.mean()),
        std=float(vals.std()),
        min=float(vals.min()),
        max=float(vals.max()),
    )
