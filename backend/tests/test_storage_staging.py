"""``storage_staging.stage_upload`` — tie an S3 object to the DB txn.

Regression for Flow task 4c4c6a7a: a blob uploaded *before* its
referencing row is committed must be deleted when the transaction rolls
back (so a mid-ingest failure leaves no orphan key in the bucket), and
kept when it commits. The cleanup is anchored to the session's
transaction outcome, so it serves both the caller that owns its commit
(bulk_ingest) and the one that does not (ingest_document_blob).
"""

from __future__ import annotations

from unittest.mock import MagicMock

from sqlalchemy import text

from bvphoenix.services.storage_staging import stage_upload
from bvphoenix.storage.s3 import S3Storage, UploadResult
from tests.conftest import skip_if_no_db


def _mock_storage() -> MagicMock:
    storage = MagicMock(spec=S3Storage)
    storage.upload_bytes.return_value = UploadResult(bucket="raw", key="k", size_bytes=3)
    return storage


@skip_if_no_db
async def test_staged_upload_deleted_on_rollback(db_session):
    storage = _mock_storage()
    # Open the transaction the upload is anchored to (the real ingest paths
    # always have DB writes alongside the upload).
    await db_session.execute(text("SELECT 1"))

    await stage_upload(
        db_session, bucket="raw", key="patient-docs/x.bin", data=b"abc", storage=storage
    )
    storage.upload_bytes.assert_called_once()
    storage.delete_object.assert_not_called()

    await db_session.rollback()
    # The orphan key is reaped, not left behind.
    storage.delete_object.assert_called_once_with(bucket="raw", key="patient-docs/x.bin")


@skip_if_no_db
async def test_staged_upload_kept_on_commit(db_session):
    storage = _mock_storage()
    await db_session.execute(text("SELECT 1"))

    await stage_upload(
        db_session, bucket="raw", key="patient-docs/y.bin", data=b"abc", storage=storage
    )
    await db_session.commit()
    # Committed → the row is durable → the object must stay.
    storage.delete_object.assert_not_called()

    # And a later rollback must not resurrect cleanup of an already-committed
    # key (the pending set was cleared at commit).
    await db_session.execute(text("SELECT 1"))
    await db_session.rollback()
    storage.delete_object.assert_not_called()


@skip_if_no_db
async def test_multiple_staged_uploads_all_reaped_on_rollback(db_session):
    storage = _mock_storage()
    await db_session.execute(text("SELECT 1"))

    keys = ["patient-docs/a.bin", "patient-docs/b.bin", "patient-docs/c.bin"]
    for k in keys:
        await stage_upload(db_session, bucket="raw", key=k, data=b"x", storage=storage)

    await db_session.rollback()
    assert storage.delete_object.call_count == len(keys)
    deleted = {c.kwargs["key"] for c in storage.delete_object.call_args_list}
    assert deleted == set(keys)
