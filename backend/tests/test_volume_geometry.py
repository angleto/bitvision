"""Unit tests for ``compute_volume_geometry``.

This is the keystone of the viewer's patient-space correctness: the
packed ``volume.raw`` blob carries no orientation/position tags (its
32-byte header is frozen), so the viewer used to fabricate an identity
frame. ``compute_volume_geometry`` recovers the real DICOM geometry from
the sorted datasets so on-image L/R orientation markers, world-space
measurements, and the FrameOfReference safety check are driven by data,
not assumptions.

Contract under test:
  * ``direction`` = [rowCosines(3), columnCosines(3), sliceCosines(3)]
    in Cornerstone3D order.
  * the slice axis is taken from the actual first->last
    ImagePositionPatient vector (sign-correct), falling back to the
    right-handed cross product for a single slice.
  * legacy series with no IOP/IPP return None (or a FoR-only partial).
"""

from __future__ import annotations

import math

from pydicom.dataset import Dataset

from bvphoenix.services.volumes import compute_volume_geometry

_FOR = "1.2.840.113619.2.55.3.604688119.1.20260529.100000"


def _slice(
    *,
    iop: list[float] | None = None,
    ipp: list[float] | None = None,
    for_uid: str | None = None,
) -> Dataset:
    ds = Dataset()
    if iop is not None:
        ds.ImageOrientationPatient = iop
    if ipp is not None:
        ds.ImagePositionPatient = ipp
    if for_uid is not None:
        ds.FrameOfReferenceUID = for_uid
    return ds


def _approx(a: list[float], b: list[float], tol: float = 1e-6) -> bool:
    return len(a) == len(b) and all(
        math.isclose(x, y, abs_tol=tol) for x, y in zip(a, b, strict=False)
    )


AXIAL = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0]


class TestAxial:
    def test_head_first_ascending_z(self) -> None:
        datasets = [
            _slice(iop=AXIAL, ipp=[10.0, 20.0, 0.0], for_uid=_FOR),
            _slice(iop=AXIAL, ipp=[10.0, 20.0, 2.0], for_uid=_FOR),
            _slice(iop=AXIAL, ipp=[10.0, 20.0, 4.0], for_uid=_FOR),
        ]
        geom = compute_volume_geometry(datasets)
        assert geom is not None
        assert _approx(geom["origin"], [10.0, 20.0, 0.0])
        assert _approx(geom["direction"], [1, 0, 0, 0, 1, 0, 0, 0, 1])
        assert geom["frame_of_reference_uid"] == _FOR

    def test_descending_z_flips_slice_axis_sign(self) -> None:
        # Datasets handed in descending-Z order: the slice axis must
        # point in -Z (first->last), not the right-handed +Z default.
        # This is the feet-first / reversed-stack safety case.
        datasets = [
            _slice(iop=AXIAL, ipp=[0.0, 0.0, 8.0]),
            _slice(iop=AXIAL, ipp=[0.0, 0.0, 4.0]),
            _slice(iop=AXIAL, ipp=[0.0, 0.0, 0.0]),
        ]
        geom = compute_volume_geometry(datasets)
        assert geom is not None
        assert _approx(geom["origin"], [0.0, 0.0, 8.0])
        assert _approx(geom["direction"][6:9], [0.0, 0.0, -1.0])


class TestSagittal:
    def test_sagittal_slice_axis_from_positions(self) -> None:
        # Sagittal acquisition: rows run A->P (y), columns run S->I (-z),
        # slices step along +x (L). Slice axis must come out [1,0,0].
        sag = [0.0, 1.0, 0.0, 0.0, 0.0, -1.0]
        datasets = [
            _slice(iop=sag, ipp=[0.0, 0.0, 0.0]),
            _slice(iop=sag, ipp=[5.0, 0.0, 0.0]),
        ]
        geom = compute_volume_geometry(datasets)
        assert geom is not None
        assert _approx(geom["direction"][0:3], [0, 1, 0])
        assert _approx(geom["direction"][3:6], [0, 0, -1])
        assert _approx(geom["direction"][6:9], [1, 0, 0])


class TestSingleSliceAndFallbacks:
    def test_single_slice_uses_cross_product(self) -> None:
        geom = compute_volume_geometry([_slice(iop=AXIAL, ipp=[1.0, 2.0, 3.0])])
        assert geom is not None
        assert _approx(geom["direction"][6:9], [0.0, 0.0, 1.0])
        assert geom["frame_of_reference_uid"] is None

    def test_missing_iop_and_ipp_returns_none(self) -> None:
        assert compute_volume_geometry([_slice()]) is None

    def test_missing_geometry_but_for_present_returns_partial(self) -> None:
        geom = compute_volume_geometry([_slice(for_uid=_FOR)])
        assert geom == {
            "origin": None,
            "direction": None,
            "frame_of_reference_uid": _FOR,
        }

    def test_empty_returns_none(self) -> None:
        assert compute_volume_geometry([]) is None

    def test_direction_vectors_are_unit_length(self) -> None:
        # Non-orthonormal / unnormalized IOP must come out normalized.
        datasets = [
            _slice(iop=AXIAL, ipp=[0.0, 0.0, 0.0]),
            _slice(iop=AXIAL, ipp=[0.0, 0.0, 3.3]),
        ]
        geom = compute_volume_geometry(datasets)
        assert geom is not None
        d = geom["direction"]
        for vec in (d[0:3], d[3:6], d[6:9]):
            assert math.isclose(math.sqrt(sum(c * c for c in vec)), 1.0, abs_tol=1e-6)
