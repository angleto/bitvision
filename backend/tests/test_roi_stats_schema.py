"""Schema-only checks for ``/series/{id}/roi-stats`` request validation.

These tests stay above the DB / S3 layer: they only exercise the
Pydantic model that maps the request body. The integration coverage
(real volume, sphere mask) lives elsewhere and requires Postgres +
the packed-volume fixture, so it's kept separate.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from bvphoenix.api.studies import ROIStatsIn


def test_rectangle_kind_accepts_bbox() -> None:
    body = ROIStatsIn.model_validate(
        {
            "kind": "rectangle",
            "min_ijk": [10, 20, 5],
            "max_ijk": [40, 60, 12],
        }
    )
    assert body.kind == "rectangle"
    assert body.min_ijk == [10, 20, 5]
    assert body.max_ijk == [40, 60, 12]
    assert body.center_ijk is None
    assert body.radius_mm is None


def test_sphere_kind_accepts_center_and_radius() -> None:
    body = ROIStatsIn.model_validate(
        {
            "kind": "sphere",
            "center_ijk": [50, 60, 30],
            "radius_mm": 15.0,
            "suv_variant": "bw",
        }
    )
    assert body.kind == "sphere"
    assert body.center_ijk == [50, 60, 30]
    assert body.radius_mm == 15.0
    assert body.suv_variant == "bw"


def test_sphere_rejects_zero_or_negative_radius() -> None:
    # radius_mm must be > 0
    with pytest.raises(ValidationError):
        ROIStatsIn.model_validate({"kind": "sphere", "center_ijk": [1, 2, 3], "radius_mm": 0.0})
    with pytest.raises(ValidationError):
        ROIStatsIn.model_validate({"kind": "sphere", "center_ijk": [1, 2, 3], "radius_mm": -1.0})


def test_sphere_rejects_radius_above_cap() -> None:
    # 200 mm cap guards against accidental "whole-body sphere" requests.
    with pytest.raises(ValidationError):
        ROIStatsIn.model_validate({"kind": "sphere", "center_ijk": [1, 2, 3], "radius_mm": 250.0})


def test_unknown_kind_rejected() -> None:
    with pytest.raises(ValidationError):
        ROIStatsIn.model_validate({"kind": "free-hand", "min_ijk": [0, 0, 0], "max_ijk": [1, 1, 1]})


def test_center_ijk_requires_three_components() -> None:
    with pytest.raises(ValidationError):
        ROIStatsIn.model_validate({"kind": "sphere", "center_ijk": [1, 2], "radius_mm": 10.0})
    with pytest.raises(ValidationError):
        ROIStatsIn.model_validate({"kind": "sphere", "center_ijk": [1, 2, 3, 4], "radius_mm": 10.0})
