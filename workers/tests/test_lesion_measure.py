"""Synthetic-phantom validation of the lesion-comparison chain.

A deterministic CI regression that ties Phase 0 (registration in true
space), Phase 2 (warp + measure) and Phase 1 (trajectory) together:

* ``measure_mask`` recovers a sphere's volume and diameter within the
  voxelisation error;
* a lesion grown by a KNOWN factor between two scans (with the follow-up
  also repositioned) is registered, the baseline mask is warped onto the
  follow-up — landing on the grown lesion — and the measured volume ratio
  equals the known growth factor, which the trajectory reports as the same
  percentage change.
"""

from __future__ import annotations

import math
import uuid

import numpy as np
import SimpleITK as sitk  # noqa: N813
from bvphoenix.services.lesion_tracks import TrackTimepoint, compute_trajectory

from bvworkers.lesion_measure import measure_mask, refine_mask_on_image
from bvworkers.registration_core import register_pair, warp_mask

SHAPE = (24, 96, 96)  # (nz, ny, nx), isotropic 1 mm → radius_mm == radius_vox


def _scene(*, offset=(0, 0, 0), sphere_r: float = 10.0) -> tuple[sitk.Image, sitk.Image]:
    """A scene with a lesion sphere (HU 100) plus asymmetric features
    (cube + wedge) so Mattes MI has a unique optimum. ``offset`` shifts the
    whole anatomy (patient repositioning). Returns (image, lesion_mask)."""
    nz, ny, nx = SHAPE
    oz, oy, ox = offset
    arr = np.full(SHAPE, -1000.0, dtype=np.float32)
    zz = np.arange(nz)[:, None, None]
    yy = np.arange(ny)[None, :, None]
    xx = np.arange(nx)[None, None, :]

    cz, cy, cx = nz // 2 + oz, ny // 2 + oy, nx // 2 + ox
    sphere = (zz - cz) ** 2 + (yy - cy) ** 2 + (xx - cx) ** 2 <= sphere_r**2
    arr[sphere] = 100.0
    # Asymmetric features (also shifted) — registration cues, not lesion.
    arr[
        (zz >= 2 + oz)
        & (zz <= 5 + oz)
        & (yy >= 10 + oy)
        & (yy <= 18 + oy)
        & (xx >= 70 + ox)
        & (xx <= 82 + ox)
    ] = 600.0
    arr[(zz >= nz - 5 + oz) & (yy + xx <= 40)] = 400.0

    img = sitk.GetImageFromArray(arr)
    img.SetSpacing((1.0, 1.0, 1.0))
    mask = sitk.GetImageFromArray(sphere.astype(np.uint8))
    mask.CopyInformation(img)
    return img, mask


def _centroid(mask: sitk.Image) -> tuple[float, float, float]:
    stats = sitk.LabelShapeStatisticsImageFilter()
    stats.Execute(sitk.Cast(mask > 0, sitk.sitkUInt8))
    return tuple(stats.GetCentroid(1))  # type: ignore[return-value]


def test_measure_sphere_volume_and_diameter() -> None:
    _img, mask = _scene(sphere_r=10.0)
    m = measure_mask(mask)
    expected_ml = (4.0 / 3.0) * math.pi * 10.0**3 / 1000.0  # ~4.19 ml
    # Voxelised sphere ≈ analytic volume within a few percent.
    assert abs(m["volume_ml"] - expected_ml) / expected_ml < 0.05
    assert abs(m["longest_diameter_mm"] - 20.0) < 2.0
    assert m["n_voxels"] > 0
    assert m["bbox_lps"] is not None


def test_empty_mask_measures_zero() -> None:
    blank = sitk.Image(8, 8, 8, sitk.sitkUInt8)
    m = measure_mask(blank)
    assert m["volume_ml"] == 0.0 and m["n_voxels"] == 0 and m["bbox_lps"] is None


def test_growth_phantom_register_warp_measure_trajectory() -> None:
    growth = 1.2  # +20% volume
    r_follow = 10.0 * growth ** (1.0 / 3.0)
    base_img, base_mask = _scene(offset=(0, 0, 0), sphere_r=10.0)
    follow_img, follow_mask = _scene(offset=(2, 3, -1), sphere_r=r_follow)

    # Register follow-up (fixed) to baseline (moving): transform maps the
    # follow-up grid back to baseline space.
    transform, _meta = register_pair(follow_img, base_img, "rigid")

    # Warp the baseline lesion mask into the follow-up frame; it must land
    # on the follow-up lesion (so it is a usable seed for re-measurement).
    warped = warp_mask(base_mask, transform, follow_img)
    wc = _centroid(warped)
    fc = _centroid(follow_mask)
    dist = math.dist(wc, fc)
    assert dist < 3.0, f"warped seed missed the follow-up lesion by {dist:.2f} mm"

    # Measure each lesion on its own real voxels; the ratio is the growth.
    vol_base = measure_mask(base_mask)["volume_ml"]
    vol_follow = measure_mask(follow_mask)["volume_ml"]
    assert abs((vol_follow / vol_base) - growth) < 0.05

    # The trajectory reports the same percentage change.
    traj = compute_trajectory(
        [
            TrackTimepoint(
                point_id=uuid.uuid4(),
                finding_id=uuid.uuid4(),
                measured_on=None,
                is_baseline=True,
                volume_ml=vol_base,
            ),
            TrackTimepoint(
                point_id=uuid.uuid4(),
                finding_id=uuid.uuid4(),
                measured_on=None,
                is_baseline=False,
                volume_ml=vol_follow,
            ),
        ]
    )
    assert traj["summary"]["overall_direction"] == "increase"
    assert abs(traj["summary"]["volume_pct_change_total"] - 20.0) < 5.0


def test_refine_recovers_grown_volume_from_baseline_seed() -> None:
    """The 'semi-automatic re-measure' claim: warping the baseline mask onto
    the follow-up and refining on the follow-up's real voxels recovers the
    GROWN lesion volume, not the baseline seed size."""
    growth = 1.4  # +40%, clearly above the seed
    r_follow = 10.0 * growth ** (1.0 / 3.0)
    base_img, base_mask = _scene(sphere_r=10.0)
    follow_img, follow_mask = _scene(offset=(2, 3, -1), sphere_r=r_follow)

    transform, _meta = register_pair(follow_img, base_img, "rigid")
    warped_seed = warp_mask(base_mask, transform, follow_img)
    refined = refine_mask_on_image(warped_seed, follow_img)

    vol_refined = measure_mask(refined)["volume_ml"]
    vol_follow_true = measure_mask(follow_mask)["volume_ml"]
    vol_base = measure_mask(base_mask)["volume_ml"]
    # Recovers the TRUE follow-up volume (within 10%), well above the seed.
    assert abs(vol_refined - vol_follow_true) / vol_follow_true < 0.1
    assert vol_refined > vol_base * 1.2


def test_refine_leak_guard_keeps_prior_for_soft_tissue() -> None:
    """Soft-tissue leak guard: a lesion embedded in same-HU tissue (a mass
    abutting chest wall / mediastinum) has no intensity-defined boundary, so
    the threshold region-grow would leak across the connected tissue. The
    guard detects the non-discriminative growth shell and returns the
    registered prior instead. Disabling it (``max_shell_above=1.0``)
    reproduces the leak — the bug a real RIDER mass surfaced."""
    shape = (40, 80, 80)
    arr = np.full(shape, -1000.0, dtype=np.float32)
    # A large soft-tissue block (HU 40); the lesion sits fully inside it so
    # the whole growth shell is same-HU tissue (no boundary contrast).
    arr[2:38, 10:70, 10:70] = 40.0
    img = sitk.GetImageFromArray(arr)
    img.SetSpacing((1.0, 1.0, 1.0))

    zz = np.arange(40)[:, None, None]
    yy = np.arange(80)[None, :, None]
    xx = np.arange(80)[None, None, :]
    sph = (zz - 20) ** 2 + (yy - 40) ** 2 + (xx - 40) ** 2 <= 8**2
    seed = sitk.GetImageFromArray(sph.astype(np.uint8))
    seed.CopyInformation(img)

    v_seed = measure_mask(seed)["volume_ml"]
    # Default gate: non-discriminative shell -> fall back to the prior.
    v_guarded = measure_mask(refine_mask_on_image(seed, img))["volume_ml"]
    assert abs(v_guarded - v_seed) < 1e-6
    # Gate disabled: the region-grow leaks across the connected tissue.
    v_leaked = measure_mask(refine_mask_on_image(seed, img, max_shell_above=1.0))["volume_ml"]
    assert v_leaked > v_seed * 2.0
