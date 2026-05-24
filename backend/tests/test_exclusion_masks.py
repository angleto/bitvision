"""Tests for ``services.exclusion_masks.build_exclusion_mask``.

The unit portion mocks the S3 storage adapter so we can verify the
mask-load + OR + reshape logic without hitting Postgres or a bucket.
The integration portion (skip_if_no_db) exercises the marker-id branch
end-to-end against the dev Postgres so a regression in the SQL select
or the geometry-shape validation surfaces immediately.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, patch

import numpy as np
import pytest

from bvphoenix.db.models import Series
from bvphoenix.services.exclusion_masks import build_exclusion_mask
from tests.conftest import skip_if_no_db


def _fake_series(series_id: uuid.UUID | None = None) -> Series:
    """Build a duck-typed Series stand-in: build_exclusion_mask only
    reads ``series.id``."""
    return cast(Series, SimpleNamespace(id=series_id or uuid.uuid4()))


@pytest.mark.asyncio
async def test_returns_none_when_no_exclusion_requested() -> None:
    db = AsyncMock()
    result = await build_exclusion_mask(
        db=db,
        series=_fake_series(),
        shape_zyx=(4, 8, 8),
        exclude_segmentation_labels=None,
        exclude_marker_ids=None,
    )
    assert result is None


@pytest.mark.asyncio
async def test_returns_none_when_empty_lists() -> None:
    db = AsyncMock()
    result = await build_exclusion_mask(
        db=db,
        series=_fake_series(),
        shape_zyx=(4, 8, 8),
        exclude_segmentation_labels=[],
        exclude_marker_ids=[],
    )
    assert result is None


@pytest.mark.asyncio
async def test_segmentation_label_loaded_and_reshaped() -> None:
    nz, ny, nx = 4, 8, 8
    # Synthetic mask: lower-Z half True (kidneys-ish region), upper half False.
    raw = np.zeros((nz, ny, nx), dtype=np.uint8)
    raw[: nz // 2] = 1

    fake_storage = SimpleNamespace(get_object_bytes=lambda bucket, key: raw.tobytes())
    with patch(
        "bvphoenix.services.exclusion_masks.get_s3_storage",
        return_value=fake_storage,
    ):
        db = AsyncMock()
        result = await build_exclusion_mask(
            db=db,
            series=_fake_series(),
            shape_zyx=(nz, ny, nx),
            exclude_segmentation_labels=["kidney_left"],
            exclude_marker_ids=None,
        )
    assert result is not None
    assert result.shape == (nz, ny, nx)
    assert result.dtype == bool
    # Lower-Z half excluded, upper half not.
    assert result[: nz // 2].all()
    assert not result[nz // 2 :].any()


@pytest.mark.asyncio
async def test_invalid_label_dropped_silently() -> None:
    """Defense in depth: the label regex matches the ``segmentations``
    API. Path traversal, spaces, and slashes never reach the storage
    adapter — the helper drops them silently so a stray label from a
    misbehaving client doesn't 422 the whole find operation."""

    def storage_get(bucket: str, key: str) -> bytes:
        pytest.fail(f"storage should not be hit for an invalid label (key={key!r})")

    fake_storage = SimpleNamespace(get_object_bytes=storage_get)
    with patch(
        "bvphoenix.services.exclusion_masks.get_s3_storage",
        return_value=fake_storage,
    ):
        db = AsyncMock()
        result = await build_exclusion_mask(
            db=db,
            series=_fake_series(),
            shape_zyx=(4, 8, 8),
            exclude_segmentation_labels=["../kidney_left", "with spaces", "kidney/left"],
            exclude_marker_ids=None,
        )
    # Every label flunked the regex → no storage call → no mask.
    assert result is None


@pytest.mark.asyncio
async def test_missing_segmentation_returns_none() -> None:
    """When a label exists in the catalog but the blob is missing /
    in-flight, the helper drops silently so the find can still run."""

    def raise_not_found(bucket: str, key: str) -> bytes:
        raise Exception("NoSuchKey")

    fake_storage = SimpleNamespace(get_object_bytes=raise_not_found)
    with patch(
        "bvphoenix.services.exclusion_masks.get_s3_storage",
        return_value=fake_storage,
    ):
        db = AsyncMock()
        result = await build_exclusion_mask(
            db=db,
            series=_fake_series(),
            shape_zyx=(4, 8, 8),
            exclude_segmentation_labels=["kidney_left"],
            exclude_marker_ids=None,
        )
    assert result is None


@pytest.mark.asyncio
async def test_wrong_size_mask_dropped() -> None:
    """A blob whose byte count doesn't match ``nx*ny*nz`` is rejected —
    most often a half-written file from a worker still in flight."""
    nz, ny, nx = 4, 8, 8
    truncated = np.zeros((nz * ny * nx) // 2, dtype=np.uint8).tobytes()
    fake_storage = SimpleNamespace(get_object_bytes=lambda bucket, key: truncated)
    with patch(
        "bvphoenix.services.exclusion_masks.get_s3_storage",
        return_value=fake_storage,
    ):
        db = AsyncMock()
        result = await build_exclusion_mask(
            db=db,
            series=_fake_series(),
            shape_zyx=(nz, ny, nx),
            exclude_segmentation_labels=["kidney_left"],
            exclude_marker_ids=None,
        )
    assert result is None


@pytest.mark.asyncio
async def test_two_labels_or_together() -> None:
    nz, ny, nx = 4, 8, 8
    left = np.zeros((nz, ny, nx), dtype=np.uint8)
    left[:, :, : nx // 2] = 1  # left half
    right = np.zeros((nz, ny, nx), dtype=np.uint8)
    right[:, :, nx // 2 :] = 1  # right half

    def storage_get(bucket: str, key: str) -> bytes:
        if key.endswith("kidney_left.bin"):
            return left.tobytes()
        if key.endswith("kidney_right.bin"):
            return right.tobytes()
        raise Exception("unexpected key")

    fake_storage = SimpleNamespace(get_object_bytes=storage_get)
    with patch(
        "bvphoenix.services.exclusion_masks.get_s3_storage",
        return_value=fake_storage,
    ):
        db = AsyncMock()
        result = await build_exclusion_mask(
            db=db,
            series=_fake_series(),
            shape_zyx=(nz, ny, nx),
            exclude_segmentation_labels=["kidney_left", "kidney_right"],
            exclude_marker_ids=None,
        )
    assert result is not None
    # Union covers the full volume.
    assert result.all()


@skip_if_no_db
@pytest.mark.asyncio
async def test_marker_bbox_exclusion_sets_true_region(db_session, make_user, make_study) -> None:
    """Integration: insert a ``bbox.exclusion`` marker, build the mask,
    confirm the ijk-bbox region is set to True and everything else is
    False."""
    from bvphoenix.db.models import Marker

    owner = await make_user()
    _study, series = await make_study(owner)

    marker_id = uuid.uuid4()
    marker = Marker(
        id=marker_id,
        patient_id=_study.patient_id,
        target_kind="series",
        target_id=series.id,
        kind="bbox.exclusion",
        geometry={"min_ijk": [2, 3, 1], "max_ijk": [4, 5, 2]},
        author_kind="human",
    )
    db_session.add(marker)
    await db_session.flush()
    await db_session.commit()
    try:
        result = await build_exclusion_mask(
            db=db_session,
            series=series,
            shape_zyx=(6, 8, 8),
            exclude_segmentation_labels=None,
            exclude_marker_ids=[marker_id],
        )
        assert result is not None
        # Region [k 1..2, j 3..5, i 2..4] inclusive set True.
        assert result[1:3, 3:6, 2:5].all()
        # Voxel just outside should be False.
        assert not result[0, 3, 2]
        assert not result[1, 2, 2]
        assert not result[1, 3, 1]
        assert not result[3, 3, 2]
    finally:
        from sqlalchemy import delete

        await db_session.execute(delete(Marker).where(Marker.id == marker_id))
        await db_session.commit()


@skip_if_no_db
@pytest.mark.asyncio
async def test_marker_wrong_kind_dropped(db_session, make_user, make_study) -> None:
    """A marker that exists but is the wrong kind (e.g. ``bbox.lesion``)
    must NOT contribute to the exclusion mask — defense against an
    operator clicking the wrong marker id."""
    from bvphoenix.db.models import Marker

    owner = await make_user()
    _study, series = await make_study(owner)

    marker_id = uuid.uuid4()
    marker = Marker(
        id=marker_id,
        patient_id=_study.patient_id,
        target_kind="series",
        target_id=series.id,
        kind="bbox.lesion",  # NOT bbox.exclusion
        geometry={"min_ijk": [0, 0, 0], "max_ijk": [4, 5, 2]},
        author_kind="human",
    )
    db_session.add(marker)
    await db_session.flush()
    await db_session.commit()
    try:
        result = await build_exclusion_mask(
            db=db_session,
            series=series,
            shape_zyx=(6, 8, 8),
            exclude_segmentation_labels=None,
            exclude_marker_ids=[marker_id],
        )
        assert result is None
    finally:
        from sqlalchemy import delete

        await db_session.execute(delete(Marker).where(Marker.id == marker_id))
        await db_session.commit()
