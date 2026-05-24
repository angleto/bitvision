"""Volume-of-Interest quantification on PET series.

Two ROI shapes are first-class:

  - **Spherical**: center (mm in patient coords) + radius (mm). The
    classic "place a sphere around the lesion" used in clinical
    refertazione.
  - **Threshold**: a seed point + a SUV (or absolute pixel) cutoff.
    The connected-component starting at the seed, of voxels above the
    threshold, becomes the VOI. Useful for isocontouring at SUVmax×0.4
    or absolute SUV 4.0 — the staple metabolic-tumor-volume rule.

Returned metrics, all SUV-normalised when the series carries decay-
corrected dose (factor_bw available):

  - ``SUVmax``  — peak voxel
  - ``SUVpeak`` — mean of the 1 cm³ sphere centred at SUVmax
  - ``SUVmean`` — mean of the whole VOI
  - ``MTV``     — Metabolic Tumor Volume (mL of voxels in VOI)
  - ``TLG``     — Total Lesion Glycolysis (SUVmean × MTV)
  - ``voxel_count`` for sanity check

The pipeline reads the packed .raw volume from S3 (already used by the
3D viewer), so we don't re-rasterise the DICOM series. Computation
is pure numpy / scipy.ndimage and runs in well under 1s for typical
PET WB volumes (~256 × 256 × 200).
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass
from typing import Literal

import numpy as np

# scipy is a heavy dep (~80MB). Only the threshold-VOI path needs
# connected-component labeling; spherical VOI works with numpy alone.
# Lazy-import inside ``compute_voi_threshold`` so the spherical path
# stays usable on installs without scipy.
try:
    from scipy import ndimage as _ndi  # type: ignore

    _SCIPY_AVAILABLE = True
except ImportError:
    _ndi = None  # type: ignore
    _SCIPY_AVAILABLE = False


@dataclass(frozen=True, slots=True)
class VolumeBlob:
    """Raw volume payload as served by ``/api/series/{id}/volume.raw``.

    The header layout is fixed: 12 bytes nx/ny/nz uint32 LE, 12 bytes
    sx/sy/sz float32 LE (mm), 8 bytes range float32 LE, then nx*ny*nz
    float32 LE scalars. ``read_volume_blob`` parses it into typed
    numpy arrays so VOI math reads cleanly.
    """

    dims: tuple[int, int, int]  # x, y, z (column, row, slice)
    spacing: tuple[float, float, float]  # mm
    voxels: np.ndarray  # shape (nz, ny, nx) float32 — z is slow axis

    @property
    def voxel_volume_ml(self) -> float:
        """Single voxel volume in mL (1 mm³ = 0.001 mL)."""
        sx, sy, sz = self.spacing
        return (sx * sy * sz) / 1000.0


def parse_volume_blob(payload: bytes) -> VolumeBlob:
    """Decode the 32-byte header + float32 scalars used by /volume.raw."""
    if len(payload) < 32:
        raise ValueError("volume blob shorter than header")
    nx, ny, nz = struct.unpack_from("<III", payload, 0)
    sx, sy, sz = struct.unpack_from("<fff", payload, 12)
    expected = nx * ny * nz * 4
    if len(payload) < 32 + expected:
        raise ValueError(
            f"volume payload truncated: header says {nx}x{ny}x{nz} "
            f"({expected} bytes scalars), got {len(payload) - 32}"
        )
    scalars = np.frombuffer(payload, dtype="<f4", count=nx * ny * nz, offset=32)
    voxels = scalars.reshape((nz, ny, nx))
    return VolumeBlob(dims=(nx, ny, nz), spacing=(sx, sy, sz), voxels=voxels)


@dataclass(frozen=True, slots=True)
class VoiMetrics:
    suv_max: float
    suv_peak: float | None
    suv_mean: float
    mtv_ml: float
    tlg: float
    voxel_count: int
    units: Literal["SUV", "raw"]
    voi_kind: str
    notes: list[str]


def _peak_1cc(values: np.ndarray, voxel_volume_ml: float) -> float | None:
    """SUV peak: mean of voxels in the 1 mL sphere around the brightest.

    1 mL = 1000 mm³, so for typical PET voxels (~5 mm³) we average ~200
    voxels. We approximate the sphere by taking the top-N brightest
    voxels of the VOI where N = ceil(1 / voxel_volume_ml). Returns None
    if the VOI is too small.
    """
    if values.size == 0 or voxel_volume_ml <= 0:
        return None
    n = max(1, math.ceil(1.0 / voxel_volume_ml))
    if n > values.size:
        return None
    # argpartition is O(n) and gives us the top-n without a full sort.
    idx = np.argpartition(values, -n)[-n:]
    return float(values[idx].mean())


def compute_voi_spherical(
    blob: VolumeBlob,
    *,
    center_mm: tuple[float, float, float],
    radius_mm: float,
    suv_factor_bw: float | None,
) -> VoiMetrics:
    """Quantify a spherical VOI of ``radius_mm`` around ``center_mm``.

    ``center_mm`` is in the same patient coordinate frame the volume
    blob's spacing implies (origin at voxel 0,0,0 — the volume packer
    bakes the origin out, so coordinates are already centered for us).
    """
    nx, ny, nz = blob.dims
    sx, sy, sz = blob.spacing
    cx, cy, cz = center_mm

    # Bounding box in voxel space, clipped to the volume.
    x0 = max(0, math.floor((cx - radius_mm) / sx))
    x1 = min(nx, math.ceil((cx + radius_mm) / sx) + 1)
    y0 = max(0, math.floor((cy - radius_mm) / sy))
    y1 = min(ny, math.ceil((cy + radius_mm) / sy) + 1)
    z0 = max(0, math.floor((cz - radius_mm) / sz))
    z1 = min(nz, math.ceil((cz + radius_mm) / sz) + 1)

    if x0 >= x1 or y0 >= y1 or z0 >= z1:
        return VoiMetrics(
            suv_max=0.0,
            suv_peak=None,
            suv_mean=0.0,
            mtv_ml=0.0,
            tlg=0.0,
            voxel_count=0,
            units="SUV" if suv_factor_bw else "raw",
            voi_kind="spherical",
            notes=["VOI outside volume bounds"],
        )

    sub = blob.voxels[z0:z1, y0:y1, x0:x1]
    zz = (np.arange(z0, z1) * sz)[:, None, None]
    yy = (np.arange(y0, y1) * sy)[None, :, None]
    xx = (np.arange(x0, x1) * sx)[None, None, :]
    dist2 = (xx - cx) ** 2 + (yy - cy) ** 2 + (zz - cz) ** 2
    mask = dist2 <= radius_mm * radius_mm

    return _metrics_from_mask(
        sub,
        mask,
        voxel_volume_ml=blob.voxel_volume_ml,
        suv_factor_bw=suv_factor_bw,
        kind="spherical",
    )


def compute_voi_threshold(
    blob: VolumeBlob,
    *,
    seed_mm: tuple[float, float, float],
    threshold_value: float,
    threshold_units: Literal["SUV", "raw"],
    suv_factor_bw: float | None,
    max_voxels: int = 5_000_000,
) -> VoiMetrics:
    """Connected-component VOI: from ``seed_mm``, grow above ``threshold``.

    Threshold is interpreted in SUV when ``threshold_units='SUV'`` (and
    ``suv_factor_bw`` is available), otherwise as raw pixel value.
    Uses 26-connectivity for typical metabolic tumor isocontouring.
    """
    notes: list[str] = []
    raw_threshold = threshold_value
    if threshold_units == "SUV":
        if suv_factor_bw is None or suv_factor_bw <= 0:
            notes.append(
                "SUV threshold requested but factor_bw missing — treating as raw pixel value"
            )
        else:
            raw_threshold = threshold_value / suv_factor_bw

    sx, sy, sz = blob.spacing
    nx, ny, nz = blob.dims
    sx_idx = max(0, min(nx - 1, round(seed_mm[0] / sx)))
    sy_idx = max(0, min(ny - 1, round(seed_mm[1] / sy)))
    sz_idx = max(0, min(nz - 1, round(seed_mm[2] / sz)))

    if blob.voxels[sz_idx, sy_idx, sx_idx] < raw_threshold:
        return VoiMetrics(
            suv_max=0.0,
            suv_peak=None,
            suv_mean=0.0,
            mtv_ml=0.0,
            tlg=0.0,
            voxel_count=0,
            units="SUV" if suv_factor_bw else "raw",
            voi_kind="threshold",
            notes=[*notes, "seed voxel below threshold"],
        )

    if not _SCIPY_AVAILABLE:
        raise RuntimeError(
            "threshold-VOI requires scipy.ndimage; install scipy to enable "
            "connected-component labeling. Spherical VOI works without scipy."
        )
    above = blob.voxels >= raw_threshold
    structure = np.ones((3, 3, 3), dtype=bool)  # 26-connectivity
    labels, _n = _ndi.label(above, structure=structure)
    seed_label = int(labels[sz_idx, sy_idx, sx_idx])
    if seed_label == 0:
        return VoiMetrics(
            suv_max=0.0,
            suv_peak=None,
            suv_mean=0.0,
            mtv_ml=0.0,
            tlg=0.0,
            voxel_count=0,
            units="SUV" if suv_factor_bw else "raw",
            voi_kind="threshold",
            notes=[*notes, "seed isolated"],
        )
    mask = labels == seed_label
    count = int(mask.sum())
    if count > max_voxels:
        notes.append(f"VOI clipped: {count} voxels exceeds safety cap {max_voxels}")
        # Take only the top max_voxels brightest ones connected to seed.
        # (Defensive: catastrophic seeds in nearly-uniform volumes.)
        flat = np.where(mask.flatten())[0]
        vals = blob.voxels.flatten()[flat]
        keep = flat[np.argpartition(vals, -max_voxels)[-max_voxels:]]
        mask = np.zeros_like(mask).flatten()
        mask[keep] = True
        mask = mask.reshape(blob.voxels.shape)

    return _metrics_from_mask(
        blob.voxels,
        mask,
        voxel_volume_ml=blob.voxel_volume_ml,
        suv_factor_bw=suv_factor_bw,
        kind="threshold",
        extra_notes=notes,
    )


def _metrics_from_mask(
    voxels: np.ndarray,
    mask: np.ndarray,
    *,
    voxel_volume_ml: float,
    suv_factor_bw: float | None,
    kind: str,
    extra_notes: list[str] | None = None,
) -> VoiMetrics:
    notes = list(extra_notes or [])
    selected = voxels[mask]
    if selected.size == 0:
        return VoiMetrics(
            suv_max=0.0,
            suv_peak=None,
            suv_mean=0.0,
            mtv_ml=0.0,
            tlg=0.0,
            voxel_count=0,
            units="SUV" if suv_factor_bw else "raw",
            voi_kind=kind,
            notes=[*notes, "empty VOI"],
        )

    factor = suv_factor_bw if suv_factor_bw and suv_factor_bw > 0 else None
    raw_max = float(selected.max())
    raw_mean = float(selected.mean())
    peak_raw = _peak_1cc(selected.astype(np.float32), voxel_volume_ml)

    if factor is not None:
        suv_max = raw_max * factor
        suv_mean = raw_mean * factor
        suv_peak = peak_raw * factor if peak_raw is not None else None
    else:
        suv_max = raw_max
        suv_mean = raw_mean
        suv_peak = peak_raw

    mtv_ml = float(selected.size) * voxel_volume_ml
    tlg = suv_mean * mtv_ml

    return VoiMetrics(
        suv_max=suv_max,
        suv_peak=suv_peak,
        suv_mean=suv_mean,
        mtv_ml=mtv_ml,
        tlg=tlg,
        voxel_count=int(selected.size),
        units="SUV" if factor is not None else "raw",
        voi_kind=kind,
        notes=notes,
    )
