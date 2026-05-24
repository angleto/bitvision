"""Schema test for the lens-probe pin path.

The viewer's Lens probe pins a circular sample as a sphere on the
backend (``compute_roi_stats(kind='sphere', radius_mm=<small>)``). Small
radii (down to 1 mm) must pass schema validation; below the 1 mm floor
the field constraint ``gt=0`` rejects the request before it touches S3.
"""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from bvphoenix.api.studies import ROIStatsIn


def test_sphere_accepts_lens_radii() -> None:
    """The lens probe uses radii in [1, 50] mm (UX clamps in the
    frontend). Confirm the schema accepts each step the wheel would
    produce without flagging it as out-of-range."""
    for r in (1.0, 2.0, 5.0, 15.0, 50.0):
        m = ROIStatsIn.model_validate(
            {"kind": "sphere", "center_ijk": [64, 64, 40], "radius_mm": r}
        )
        assert m.radius_mm == r


def test_sphere_rejects_zero_radius_for_lens() -> None:
    """A wheel-down past the floor must not produce a request that
    reaches the server: zero radius is degenerate."""
    with pytest.raises(ValidationError):
        ROIStatsIn.model_validate({"kind": "sphere", "center_ijk": [64, 64, 40], "radius_mm": 0.0})


def test_sphere_accepts_suv_variant_with_radius() -> None:
    """PET pin: the lens sends ``suv_variant`` alongside the small
    radius so the response carries SUV-mean/-max/-peak in one round
    trip without a second SUV-factors fetch."""
    m = ROIStatsIn.model_validate(
        {
            "kind": "sphere",
            "center_ijk": [64, 64, 40],
            "radius_mm": 5.0,
            "suv_variant": "bw",
        }
    )
    assert m.suv_variant == "bw"
    assert m.radius_mm == 5.0


def test_sphere_accepts_exclusion_inputs() -> None:
    """The lens pin can carry exclusion inputs (e.g. when the operator
    is sampling around an organ but wants to exclude the organ itself
    from the mean). Schema must accept both shapes."""
    marker_id = uuid.uuid4()
    m = ROIStatsIn.model_validate(
        {
            "kind": "sphere",
            "center_ijk": [64, 64, 40],
            "radius_mm": 5.0,
            "exclude_segmentation_labels": ["kidney_left", "kidney_right"],
            "exclude_marker_ids": [str(marker_id)],
        }
    )
    assert m.exclude_segmentation_labels == ["kidney_left", "kidney_right"]
    assert m.exclude_marker_ids == [marker_id]


def test_sphere_rejects_more_than_32_labels() -> None:
    """Schema caps the label list to avoid an operator passing every
    TotalSegmentator label by accident."""
    with pytest.raises(ValidationError):
        ROIStatsIn.model_validate(
            {
                "kind": "sphere",
                "center_ijk": [64, 64, 40],
                "radius_mm": 5.0,
                "exclude_segmentation_labels": [f"label_{i}" for i in range(33)],
            }
        )


def test_sphere_rejects_more_than_16_marker_ids() -> None:
    with pytest.raises(ValidationError):
        ROIStatsIn.model_validate(
            {
                "kind": "sphere",
                "center_ijk": [64, 64, 40],
                "radius_mm": 5.0,
                "exclude_marker_ids": [str(uuid.uuid4()) for _ in range(17)],
            }
        )
