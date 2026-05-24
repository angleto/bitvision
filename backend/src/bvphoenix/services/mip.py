"""Rotating Maximum-Intensity-Projection cine generation.

Standard PET reading workflow: a single 2D image is generated for each
angle θ in [0, 360) by:

  1. Rotating the volume around the patient's vertical (z) axis by θ
  2. Taking the per-pixel maximum along the y axis (anterior-posterior)
  3. Flipping z so the head is on top (PET MIP convention)
  4. Inverting the grayscale (high SUV → dark, the radiologist reading
     convention)

The output is a single PNG sprite-sheet with the N frames stacked
vertically. The frontend animates by translating the visible window.
A sprite-sheet is preferred to N individual frames because it gives a
single network round-trip and the browser decodes once.

Computation is heavy (scipy.ndimage.rotate at order=1 on a 256³ volume,
36 angles, runs ~10 s). Callers should cache the result in the
derivatives bucket and look it up on subsequent reads.
"""

from __future__ import annotations

import io
from dataclasses import dataclass

import numpy as np
from PIL import Image

from bvphoenix.services.pet_voi import VolumeBlob


@dataclass(frozen=True, slots=True)
class MipSprite:
    """Output of a rotating-MIP run.

    Attributes:
        png_bytes: vertically-stacked PNG (height = frame_height * frame_count)
        frame_count: number of angles
        frame_width / frame_height: per-frame size in pixels
        suv_window: (low, high) clip window applied before normalisation
        units: ``"SUV"`` if SUV-normalised, ``"raw"`` otherwise
    """

    png_bytes: bytes
    frame_count: int
    frame_width: int
    frame_height: int
    suv_window: tuple[float, float]
    units: str


def compute_rotating_mip_sprite(
    blob: VolumeBlob,
    *,
    num_frames: int = 36,
    suv_factor_bw: float | None = None,
    invert: bool = True,
    target_height: int = 384,
) -> MipSprite:
    """Generate a rotating-MIP sprite-sheet for ``blob``.

    Frames are sized so the projected height equals ``target_height``;
    aspect ratio is preserved by upscaling (or downscaling for very
    tall volumes) with bilinear resampling.
    """
    try:
        from scipy import ndimage as ndi
    except ImportError as exc:
        raise RuntimeError("rotating MIP requires scipy.ndimage; install scipy to enable") from exc

    voxels = blob.voxels  # shape (nz, ny, nx)
    nz, _ny, nx = voxels.shape

    if suv_factor_bw and suv_factor_bw > 0:
        scaled = voxels.astype(np.float32) * suv_factor_bw
        units = "SUV"
    else:
        scaled = voxels.astype(np.float32)
        units = "raw"

    # Empty-space skipping (Addendum A §8.2). PET volumes are
    # dominated by background — rotating the full cube N times for
    # the cine wastes work on voxels that never contribute to the
    # MIP. We crop to the bounding box of "non-trivial" voxels (>
    # 5 % of the global p99 threshold the cine windowing uses)
    # before the rotation/projection loop. Padding 4 voxels on each
    # axis to leave a margin around the patient silhouette.
    threshold_for_bbox = 0.05 * float(np.percentile(scaled, 99.5))
    if threshold_for_bbox > 0:
        active = scaled > threshold_for_bbox
        if active.any():
            zs = np.any(active, axis=(1, 2))
            ys = np.any(active, axis=(0, 2))
            xs = np.any(active, axis=(0, 1))
            z_idx = np.flatnonzero(zs)
            y_idx = np.flatnonzero(ys)
            x_idx = np.flatnonzero(xs)
            z0, z1 = max(0, int(z_idx[0]) - 4), min(nz, int(z_idx[-1]) + 5)
            y0, y1 = max(0, int(y_idx[0]) - 4), min(_ny, int(y_idx[-1]) + 5)
            x0, x1 = max(0, int(x_idx[0]) - 4), min(nx, int(x_idx[-1]) + 5)
            scaled = scaled[z0:z1, y0:y1, x0:x1]
            nz, _ny, nx = scaled.shape

    # Window: clip 0..p99.5 of the volume so liver/heart hot spots are
    # visible without saturating. PET reading convention.
    p99 = float(np.percentile(scaled, 99.5))
    if p99 <= 0:
        p99 = 1.0
    suv_window = (0.0, p99)

    sx, _sy, sz = blob.spacing
    # Aspect: the rotated MIP frames are (nz, nx) before resizing.
    # Height = nz * sz, Width = nx * sx (both in mm). Resize so height
    # == target_height keeping aspect.
    aspect = (nx * sx) / max(1.0, nz * sz)
    target_h = target_height
    target_w = max(64, round(target_h * aspect))

    frames: list[np.ndarray] = []
    for i in range(num_frames):
        angle = 360.0 * i / num_frames
        # Rotate in the xy plane (axes 2 = x, 1 = y); reshape=False keeps
        # the array shape so the projection is consistent across frames.
        rotated = ndi.rotate(
            scaled, angle, axes=(2, 1), reshape=False, order=1, mode="constant", cval=0
        )
        mip = rotated.max(axis=1)  # shape (nz, nx)

        norm = np.clip(mip / p99, 0.0, 1.0)
        if invert:
            norm = 1.0 - norm

        # Flip z so head is up (DICOM conventions vary; this matches the
        # PET MIP cine you see on workstations).
        norm = np.flipud(norm)

        img8 = (norm * 255).astype(np.uint8)
        # Resize to target frame size with bilinear; PIL handles non-uniform
        # source aspect cleanly.
        pil = Image.fromarray(img8, mode="L").resize((target_w, target_h), resample=Image.BILINEAR)
        frames.append(np.asarray(pil, dtype=np.uint8))

    sprite = np.concatenate(frames, axis=0)  # vertical stack
    out = io.BytesIO()
    Image.fromarray(sprite, mode="L").save(out, format="PNG", optimize=True)

    return MipSprite(
        png_bytes=out.getvalue(),
        frame_count=num_frames,
        frame_width=target_w,
        frame_height=target_h,
        suv_window=suv_window,
        units=units,
    )
