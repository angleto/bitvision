"""Pure unit tests for the wash-out computation + world-space ROI sampling.
No DB, no S3 — synthetic HU values and a phantom volume with known geometry.
"""

from __future__ import annotations

import numpy as np

from bvphoenix.services.roi_sampling import (
    sample_bbox_hu,
    sample_sphere_hu,
    world_to_ijk,
)
from bvphoenix.services.washout import PhaseHu, compute_washout

# ---- washout math -----------------------------------------------------


def test_apw_rpw_adenoma_like() -> None:
    # U=10, E(portal)=100, D(delayed)=40 -> APW=66.7%, RPW=60%.
    r = compute_washout(
        [
            PhaseHu("unenhanced", 10.0),
            PhaseHu("portal_venous", 100.0),
            PhaseHu("delayed", 40.0),
        ]
    )
    assert r.enhanced_phase == "portal_venous"
    assert r.delayed_phase == "delayed"
    assert r.absolute_enhancement_hu == 90.0
    assert r.apw is not None and abs(r.apw - (100 * 60 / 90)) < 1e-6
    assert r.rpw is not None and abs(r.rpw - 60.0) < 1e-6
    assert r.apw_ge_60 is True
    assert r.rpw_ge_40 is True
    assert r.unenhanced_below_10hu is False
    assert [p.acquisition_phase for p in r.curve] == ["unenhanced", "portal_venous", "delayed"]


def test_apw_rpw_nonadenoma_like() -> None:
    # U=20, E=80, D=70 -> APW=16.7%, RPW=12.5% (both below threshold).
    r = compute_washout(
        [PhaseHu("unenhanced", 20.0), PhaseHu("portal_venous", 80.0), PhaseHu("delayed", 70.0)]
    )
    assert r.apw is not None and r.apw < 60
    assert r.rpw is not None and r.rpw < 40
    assert r.apw_ge_60 is False
    assert r.rpw_ge_40 is False


def test_rpw_without_unenhanced() -> None:
    # No unenhanced phase -> APW not computable, RPW still is.
    r = compute_washout([PhaseHu("portal_venous", 100.0), PhaseHu("delayed", 40.0)])
    assert r.apw is None
    assert r.apw_ge_60 is None
    assert r.rpw is not None and abs(r.rpw - 60.0) < 1e-6


def test_unenhanced_below_10_flag() -> None:
    r = compute_washout([PhaseHu("unenhanced", 4.0), PhaseHu("portal_venous", 90.0)])
    assert r.unenhanced_below_10hu is True
    # No delayed phase -> no wash-out indices.
    assert r.apw is None and r.rpw is None


def test_delayed_must_follow_enhanced() -> None:
    # Only arterial + a (wrongly) earlier "unenhanced" as delayed candidate:
    # arterial enhanced, no valid later delayed -> no indices.
    r = compute_washout([PhaseHu("unenhanced", 10.0), PhaseHu("arterial", 120.0)])
    assert r.enhanced_phase == "arterial"
    assert r.delayed_phase is None
    assert r.rpw is None


def test_arterial_fallback_enhanced_with_delayed() -> None:
    # No portal phase: arterial is the enhanced fallback, delayed is later.
    r = compute_washout([PhaseHu("arterial", 100.0), PhaseHu("delayed", 50.0)])
    assert r.enhanced_phase == "arterial"
    assert r.delayed_phase == "delayed"
    assert r.rpw is not None and abs(r.rpw - 50.0) < 1e-6


# ---- world-space ROI sampling -----------------------------------------

# Axis-aligned phantom: origin at (0,0,0), identity direction (row=x, col=y,
# slice=z), 1mm isotropic, 20^3 voxels. arr[k,j,i] = i+j+k makes the value
# at a voxel a known function of its index.
_GEOM = {
    "origin": [0.0, 0.0, 0.0],
    "direction": [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0],
    "frame_of_reference_uid": "1.2.3",
}
_SPACING = (1.0, 1.0, 1.0)


def _phantom() -> np.ndarray:
    n = 20
    arr = np.zeros((n, n, n), dtype=np.float32)
    for k in range(n):
        for j in range(n):
            for i in range(n):
                arr[k, j, i] = i + j + k
    return arr


def test_world_to_ijk_identity_geometry() -> None:
    assert world_to_ijk((0.0, 0.0, 0.0), _GEOM, _SPACING) == (0.0, 0.0, 0.0)
    assert world_to_ijk((5.0, 6.0, 7.0), _GEOM, _SPACING) == (5.0, 6.0, 7.0)


def test_world_to_ijk_translated_origin() -> None:
    geom = {**_GEOM, "origin": [10.0, 20.0, 30.0]}
    # world (12,23,34) - origin (10,20,30) = (2,3,4).
    assert world_to_ijk((12.0, 23.0, 34.0), geom, _SPACING) == (2.0, 3.0, 4.0)


def test_world_to_ijk_none_without_geometry() -> None:
    assert world_to_ijk((0, 0, 0), None, _SPACING) is None
    assert world_to_ijk((0, 0, 0), {"origin": None, "direction": None}, _SPACING) is None


def test_sample_sphere_hu_phantom() -> None:
    arr = _phantom()
    # Sphere centered at voxel (10,10,10) where value = 30; small radius so
    # the mean is ~30 by symmetry (value is linear in i+j+k).
    s = sample_sphere_hu(arr, _SPACING, (10.0, 10.0, 10.0), 2.0)
    assert s.voxel_count > 0
    assert abs(s.mean - 30.0) < 1e-6  # linear field, symmetric mask -> exact center value


def test_sample_bbox_hu_phantom() -> None:
    arr = _phantom()
    # bbox [0,0,0]..[1,1,1] -> 8 voxels, values i+j+k in {0,1,1,1,2,2,2,3}.
    s = sample_bbox_hu(arr, (0, 0, 0), (1, 1, 1))
    assert s.voxel_count == 8
    assert abs(s.mean - 1.5) < 1e-6
    assert s.min == 0.0
    assert s.max == 3.0
