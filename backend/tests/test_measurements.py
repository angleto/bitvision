"""Pure unit tests for the geometric measurements service."""

from __future__ import annotations

import math

import pytest

from bvphoenix.services.measurements import (
    MissingSpacingError,
    Spacing3D,
    compute_distance,
    compute_volume,
    spacing_from_meta,
)


def test_spacing_from_meta_with_spacing_between_slices() -> None:
    sp = spacing_from_meta({"PixelSpacing": [0.7, 0.5], "SpacingBetweenSlices": 1.5})
    # PixelSpacing in DICOM is [row, column].
    assert sp == Spacing3D(column_mm=0.5, row_mm=0.7, slice_mm=1.5)


def test_spacing_from_meta_falls_back_to_slice_thickness() -> None:
    sp = spacing_from_meta({"PixelSpacing": [1.0, 1.0], "SliceThickness": 2.0})
    assert sp.slice_mm == 2.0


def test_spacing_from_meta_raises_when_missing_pixel_spacing() -> None:
    with pytest.raises(MissingSpacingError) as exc:
        spacing_from_meta({"SliceThickness": 2.0})
    assert "PixelSpacing" in exc.value.missing


def test_spacing_from_meta_raises_when_missing_slice() -> None:
    with pytest.raises(MissingSpacingError) as exc:
        spacing_from_meta({"PixelSpacing": [0.7, 0.7]})
    assert "SliceThickness" in exc.value.missing


def test_compute_distance_simple() -> None:
    sp = Spacing3D(column_mm=1.0, row_mm=1.0, slice_mm=1.0)
    out = compute_distance((0, 0, 0), (3, 4, 0), sp)
    assert math.isclose(out["distance_mm"], 5.0)


def test_compute_distance_anisotropic() -> None:
    sp = Spacing3D(column_mm=0.7, row_mm=0.7, slice_mm=2.0)
    out = compute_distance((0, 0, 0), (10, 0, 5), sp)
    expected = math.sqrt((10 * 0.7) ** 2 + (5 * 2.0) ** 2)
    assert math.isclose(out["distance_mm"], expected)


def test_compute_volume_normalises_corners() -> None:
    sp = Spacing3D(column_mm=1.0, row_mm=1.0, slice_mm=1.0)
    a = compute_volume((10, 10, 10), (5, 5, 5), sp)
    b = compute_volume((5, 5, 5), (10, 10, 10), sp)
    assert a["volume_mm3"] == b["volume_mm3"] == 125.0
    assert a["volume_ml"] == 0.125


def test_compute_volume_anisotropic() -> None:
    sp = Spacing3D(column_mm=0.5, row_mm=0.5, slice_mm=2.0)
    out = compute_volume((0, 0, 0), (4, 4, 4), sp)
    # extent = (4*0.5, 4*0.5, 4*2.0) = (2, 2, 8) mm; volume = 32 mm^3
    assert math.isclose(out["volume_mm3"], 32.0)
    assert out["extent_mm"] == [2.0, 2.0, 8.0]
