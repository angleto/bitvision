"""The segmentation DELETE endpoint must remove the ORM row, not just the S3
object. A mask-only delete left the ``Segmentation`` row orphaned, pointing at
a now-missing blob, so the viewer kept listing a segmentation whose bytes 404.
"""

import uuid

import pytest
from sqlalchemy import select

from bvphoenix.api.segmentations import delete_segmentation
from bvphoenix.db.models import Segmentation

from .conftest import skip_if_no_db


class _StubStorage:
    """Records delete_object calls; no real S3."""

    def __init__(self) -> None:
        self.deleted: list[tuple[str, str]] = []

    def delete_object(self, *, bucket: str, key: str) -> None:
        self.deleted.append((bucket, key))


@skip_if_no_db
@pytest.mark.asyncio
async def test_delete_removes_orm_row_and_object(
    db_session, make_user, make_study, monkeypatch
) -> None:
    owner = await make_user()
    study, series = await make_study(owner)

    label = "interactive-test"
    seg = Segmentation(
        id=uuid.uuid4(),
        series_id=series.id,
        patient_id=study.patient_id,
        producer="medsam",
        label=label,
        s3_bucket="bvphoenix-derivatives",
        s3_key=f"segmentations/{series.id}/{label}.bin",
        size_bytes=123,
        author_kind="human",
    )
    db_session.add(seg)
    await db_session.commit()

    stub = _StubStorage()
    monkeypatch.setattr("bvphoenix.api.segmentations.get_s3_storage", lambda: stub)

    resp = await delete_segmentation(series_id=series.id, label=label, db=db_session, user=owner)
    assert resp.status_code == 204

    # The ORM row is gone (not just the mask).
    row = (
        await db_session.execute(
            select(Segmentation).where(
                Segmentation.series_id == series.id, Segmentation.label == label
            )
        )
    ).scalar_one_or_none()
    assert row is None
    # The stored key (honoured, not just the derived one) was deleted.
    assert stub.deleted == [("bvphoenix-derivatives", f"segmentations/{series.id}/{label}.bin")]


@skip_if_no_db
@pytest.mark.asyncio
async def test_delete_missing_label_is_idempotent(
    db_session, make_user, make_study, monkeypatch
) -> None:
    owner = await make_user()
    _study, series = await make_study(owner)
    stub = _StubStorage()
    monkeypatch.setattr("bvphoenix.api.segmentations.get_s3_storage", lambda: stub)

    resp = await delete_segmentation(
        series_id=series.id, label="does-not-exist", db=db_session, user=owner
    )
    assert resp.status_code == 204
    # Best-effort object delete under the derived key still fires.
    assert len(stub.deleted) == 1
