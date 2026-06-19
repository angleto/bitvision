"""Per-voxel wash-out / subtraction heat map for one ROI slice.

Radiological highlighting of WHERE a lesion washes out: the API layer reads
the ROI's slice slab in two phases (reusing the ranged-slab read), hands the
two central slices here, and we return a colour-mapped RGBA PNG. Pure numpy +
Pillow; no DB / S3 here so it is unit-testable on synthetic slices.

Colour convention (matches the curve legend in WashoutPanel):
    GREEN  = a > b  → wash-out (the voxel clears contrast between the phases)
    RED    = a < b  → uptake / persistent enhancement
    alpha  ∝ |a-b|  → near-zero difference is transparent (overlay-friendly)
"""

from __future__ import annotations

import io

import numpy as np
from PIL import Image


def diff_map_rgba(
    slice_a: np.ndarray, slice_b: np.ndarray, vabs: float | None = None
) -> tuple[np.ndarray, float]:
    """RGBA heat map of ``a - b`` per voxel. Returns ``(rgba HxWx4 uint8, vabs)``
    where ``vabs`` is the symmetric colour scale (the 99th percentile of |a-b|
    unless supplied)."""
    a = np.asarray(slice_a, dtype=np.float32)
    b = np.asarray(slice_b, dtype=np.float32)
    d = a - b
    if vabs is None:
        vabs = float(np.percentile(np.abs(d), 99)) if d.size else 1.0
    if not vabs or vabs <= 0:
        vabs = 1.0
    t = np.clip(d / vabs, -1.0, 1.0)
    pos = np.clip(t, 0.0, 1.0)  # wash-out
    neg = np.clip(-t, 0.0, 1.0)  # uptake
    h, w = d.shape
    rgba = np.zeros((h, w, 4), dtype=np.uint8)
    rgba[..., 0] = (neg * 230.0).astype(np.uint8)  # R = uptake
    rgba[..., 1] = (pos * 230.0).astype(np.uint8)  # G = wash-out
    rgba[..., 3] = (np.abs(t) * 255.0).astype(np.uint8)
    return rgba, vabs


def encode_png(rgba: np.ndarray, scale: int = 1) -> bytes:
    """PNG-encode an RGBA array, optionally nearest-neighbour upscaled so a
    small lesion crop is legible in the panel."""
    img = Image.fromarray(rgba, mode="RGBA")
    if scale > 1:
        img = img.resize(
            (rgba.shape[1] * scale, rgba.shape[0] * scale), resample=Image.Resampling.NEAREST
        )
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
