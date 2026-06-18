"""Unit tests for the pure registration core (no DB / S3).

Two guarantees underpinning the longitudinal-comparison feature:

* ``build_sitk_image_from_packed`` places a packed ``volume_f32``
  derivative in TRUE patient space (LPS) — correct size, spacing, origin
  and a correctly-transposed direction matrix. This is the fix for the
  historical index-space bug.
* ``register_pair`` (rigid) recovers a known rigid mis-alignment, so a
  follow-up scan can be brought into the baseline frame before measuring.
"""

from __future__ import annotations

import numpy as np
import SimpleITK as sitk  # noqa: N813
from bvphoenix.services.volumes import HEADER_STRUCT

from bvworkers.registration_core import build_sitk_image_from_packed, register_pair


def _packed(arr: np.ndarray, *, sx: float, sy: float, sz: float) -> bytes:
    """Pack a (nz, ny, nx) float32 array into the volume_f32 wire format."""
    nz, ny, nx = arr.shape
    head = HEADER_STRUCT.pack(nx, ny, nz, sx, sy, sz, float(arr.min()), float(arr.max()))
    return head + np.ascontiguousarray(arr, dtype=np.float32).tobytes()


def test_build_sitk_image_identity_geometry() -> None:
    arr = np.arange(2 * 3 * 4, dtype=np.float32).reshape(2, 3, 4)  # (nz, ny, nx)
    # Spacings chosen exactly representable in float32 (the header packs them
    # as ``f``), and sx != sy to catch an x/y axis swap.
    packed = _packed(arr, sx=0.5, sy=0.25, sz=2.0)
    geom = {
        "origin": [10.0, 20.0, 30.0],
        "direction": [1, 0, 0, 0, 1, 0, 0, 0, 1],  # row=x, col=y, slice=z
        "frame_of_reference_uid": "1.2.3",
    }
    img = build_sitk_image_from_packed(packed, geom)

    assert img.GetSize() == (4, 3, 2)  # (nx, ny, nz)
    assert img.GetSpacing() == (0.5, 0.25, 2.0)
    assert img.GetOrigin() == (10.0, 20.0, 30.0)
    assert img.GetDirection() == (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0)
    # Voxel layout preserved: SimpleITK indexes (x, y, z).
    assert img[0, 0, 0] == 0.0
    assert img[3, 2, 1] == float(arr[1, 2, 3])


def test_build_sitk_image_rotated_direction_is_transposed() -> None:
    # Row cosine = +y, column cosine = -x, slice = +z (a 90° in-plane turn).
    arr = np.zeros((2, 3, 4), dtype=np.float32)
    packed = _packed(arr, sx=1.0, sy=1.0, sz=1.0)
    geom = {
        "origin": [0.0, 0.0, 0.0],
        "direction": [0, 1, 0, -1, 0, 0, 0, 0, 1],  # [row(3), col(3), slice(3)]
        "frame_of_reference_uid": None,
    }
    img = build_sitk_image_from_packed(packed, geom)
    # SimpleITK direction column j = cosine of image axis j, so the stored
    # [row, col, slice] becomes the matrix transpose-by-columns:
    # [[Rx,Cx,Sx],[Ry,Cy,Sy],[Rz,Cz,Sz]] = [0,-1,0, 1,0,0, 0,0,1].
    assert img.GetDirection() == (0.0, -1.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0)


def test_build_sitk_image_missing_geometry_is_identity() -> None:
    arr = np.zeros((2, 3, 4), dtype=np.float32)
    img = build_sitk_image_from_packed(_packed(arr, sx=1.0, sy=1.0, sz=2.0), None)
    assert img.GetOrigin() == (0.0, 0.0, 0.0)
    assert img.GetDirection() == (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0)
    assert img.GetSpacing() == (1.0, 1.0, 2.0)


def _phantom() -> sitk.Image:
    """Asymmetric phantom (sphere + off-centre cube + corner wedge) so
    Mattes MI has a unique optimum. Mirrors test_registration_demons."""
    nz, ny, nx = 16, 64, 64
    arr = np.full((nz, ny, nx), -1000.0, dtype=np.float32)
    zz = np.arange(nz)[:, None, None]
    yy = np.arange(ny)[None, :, None]
    xx = np.arange(nx)[None, None, :]
    cz, cy, cx = nz // 2, ny // 2, nx // 2
    arr[(zz - cz) ** 2 + (yy - cy) ** 2 + (xx - cx) ** 2 <= (16 // 4) ** 2] = 200.0
    arr[(zz >= 2) & (zz <= 5) & (yy >= 8) & (yy <= 16) & (xx >= 40) & (xx <= 50)] = 600.0
    arr[(zz >= nz - 4) & (yy + xx <= 30)] = 400.0
    img = sitk.GetImageFromArray(arr)
    img.SetSpacing((1.0, 1.0, 2.0))
    return img


def _rmse(a: sitk.Image, b: sitk.Image) -> float:
    aa = sitk.GetArrayFromImage(sitk.Cast(a, sitk.sitkFloat32))
    bb = sitk.GetArrayFromImage(sitk.Cast(b, sitk.sitkFloat32))
    return float(np.sqrt(np.mean((aa - bb) ** 2)))


def test_register_pair_rigid_recovers_known_shift() -> None:
    fixed = _phantom()
    shift = sitk.TranslationTransform(3, (4.0, -3.0, 4.0))
    moving = sitk.Resample(fixed, fixed, shift, sitk.sitkLinear, -1000.0, fixed.GetPixelID())

    transform, meta = register_pair(fixed, moving, "rigid")
    assert meta["kind"] == "rigid"
    assert meta["rigid_metric_name"] == "MattesMutualInformation"

    aligned = sitk.Resample(moving, fixed, transform, sitk.sitkLinear, -1000.0, fixed.GetPixelID())
    rmse_before = _rmse(fixed, moving)
    rmse_after = _rmse(fixed, aligned)
    assert rmse_after < rmse_before * 0.5, (
        f"rigid did not align: {rmse_before:.1f} -> {rmse_after:.1f}"
    )


def test_register_pair_rejects_unknown_kind() -> None:
    img = _phantom()
    try:
        register_pair(img, img, "affine")
    except ValueError as exc:
        assert "affine" in str(exc)
    else:
        raise AssertionError("expected ValueError for unknown kind")
