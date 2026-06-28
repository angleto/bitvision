"""Tie an S3 object's lifecycle to the DB transaction that anchors it.

A blob written to S3 *before* the row that references it is committed
becomes an orphan key the moment the transaction rolls back: S3 has no
transactions, the database does, so a mid-ingest failure (a later DB
error, a failing commit) leaves bytes in the bucket that nothing in the
database points at.

``stage_upload`` closes that gap. It uploads the bytes AND registers the
``(bucket, key)`` on the SQLAlchemy session; a one-shot ``after_rollback``
listener best-effort deletes every still-pending key, while
``after_commit`` simply forgets them (they are now referenced by durable
rows). Because the cleanup is anchored to the *session's* transaction
outcome, it serves both ingest ownership models with one mechanism:

* ``bulk_ingest`` owns its own ``commit()`` — a failing commit triggers
  the rollback that reaps the document keys it just uploaded;
* ``ingest_document_blob`` does NOT commit (its caller does) — the
  caller's rollback reaps the key with no per-caller cleanup code.

Storage isolation holds: keys never leave the backend; only the
server-side ops log records a failed best-effort delete so an operator
can reconcile.

Residual (bounded, out of scope here): a hard process crash between the
S3 PUT and the rollback handler still orphans a key. That window is what
the staging-prefix / bucket-lifecycle GC sweeps are for; this module
closes the rollback path, which is the common, in-process failure.
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from bvphoenix.storage import S3Storage, get_s3_storage
from bvphoenix.storage.s3 import UploadResult

logger = logging.getLogger(__name__)

# Keys into ``Session.info`` (per-session scratch space, not shared across
# the pool): the list of objects to reap on rollback, and a flag so the
# event listeners are wired exactly once per session.
_PENDING_KEY = "_bvp_pending_s3_objects"
_WIRED_KEY = "_bvp_s3_staging_wired"


def _pending(sync_session: Session) -> list[tuple[S3Storage, str, str]]:
    return sync_session.info.setdefault(_PENDING_KEY, [])


def _on_commit(sync_session: Session) -> None:
    # Durable now: the rows referencing these keys are committed, so the
    # objects must stay. Forget them.
    sync_session.info.pop(_PENDING_KEY, None)


def _on_rollback(sync_session: Session) -> None:
    pending = sync_session.info.pop(_PENDING_KEY, None)
    if not pending:
        return
    for storage, bucket, key in pending:
        try:
            storage.delete_object(bucket=bucket, key=key)
        except Exception:
            # Best-effort: a failed cleanup must never mask the original
            # error that caused the rollback. Log for operator reconcile;
            # the GC sweep is the backstop.
            logger.warning(
                "storage_staging: best-effort delete of orphan key failed (bucket=%s key=%s)",
                bucket,
                key,
                exc_info=True,
            )


def _ensure_wired(db: AsyncSession) -> None:
    sync_session = db.sync_session
    if sync_session.info.get(_WIRED_KEY):
        return
    event.listen(sync_session, "after_commit", _on_commit)
    event.listen(sync_session, "after_rollback", _on_rollback)
    sync_session.info[_WIRED_KEY] = True


async def stage_upload(
    db: AsyncSession,
    *,
    bucket: str,
    key: str,
    data: bytes,
    storage: S3Storage | None = None,
) -> UploadResult:
    """Upload ``data`` to ``bucket/key`` and tie its lifetime to ``db``.

    Drop-in for ``storage.upload_bytes(...)`` on any ingest path that
    uploads a blob and commits the referencing row later: if that
    transaction rolls back, the object is deleted instead of orphaned.
    """
    storage = storage or get_s3_storage()
    result = await asyncio.to_thread(storage.upload_bytes, data, bucket=bucket, key=key)
    _ensure_wired(db)
    _pending(db.sync_session).append((storage, bucket, key))
    return result


__all__ = ["stage_upload"]
