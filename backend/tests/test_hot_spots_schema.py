"""Schema test for ``HotSpotsIn`` exclusion knobs.

The integration behaviour of the exclusion mask is covered in
``test_exclusion_masks.py``; this file enforces the request contract
so a malformed agent payload is rejected before touching the cached
packed volume.
"""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from bvphoenix.api.studies import HotSpotsIn


def test_default_request_is_valid() -> None:
    """No exclusion + sensible defaults — current callers must keep
    working without code changes."""
    m = HotSpotsIn.model_validate({})
    assert m.threshold_mode == "percent_of_max"
    assert m.exclude_segmentation_labels is None
    assert m.exclude_marker_ids is None


def test_accepts_exclusion_labels() -> None:
    m = HotSpotsIn.model_validate(
        {"exclude_segmentation_labels": ["kidney_left", "kidney_right", "urinary_bladder"]}
    )
    assert m.exclude_segmentation_labels == [
        "kidney_left",
        "kidney_right",
        "urinary_bladder",
    ]


def test_accepts_exclusion_marker_ids() -> None:
    ids = [uuid.uuid4() for _ in range(3)]
    m = HotSpotsIn.model_validate({"exclude_marker_ids": [str(i) for i in ids]})
    assert m.exclude_marker_ids == ids


def test_rejects_too_many_labels() -> None:
    with pytest.raises(ValidationError):
        HotSpotsIn.model_validate(
            {"exclude_segmentation_labels": [f"label_{i}" for i in range(33)]}
        )


def test_rejects_too_many_marker_ids() -> None:
    with pytest.raises(ValidationError):
        HotSpotsIn.model_validate({"exclude_marker_ids": [str(uuid.uuid4()) for _ in range(17)]})
