"""Arq task: hard-purge soft-deleted patient documents past their
``purge_after`` retention window (Sprint 3, ADR 0006).

Runs nightly. For each due tombstone:

1. Drop every ``patient_document_files`` row's S3 object (best effort).
2. Drop the legacy ``file_s3_key`` object if set.
3. Hard-delete the ``patient_documents`` row (cascades the
   ``patient_document_files`` rows + ``document_study_links``).

Object failures are logged but never block the row deletion: a
permanently broken S3 object would otherwise pin the document
forever.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from bvworkers.config import get_settings

log = logging.getLogger(__name__)

# Bound the per-tick batch so a long downtime backlog does not block
# the worker for 30 minutes. The cron fires nightly so 500 rows/tick
# drains 15k rows/month — well above realistic single-tenant churn.
_BATCH_SIZE = 500


async def purge_expired_documents(
    ctx: dict,  # type: ignore[type-arg]
) -> dict[str, Any]:
    settings = get_settings()
    try:
        from bvphoenix.db.models import Document, DocumentFile
        from bvphoenix.db.session import SERVICE_SUBJECT, set_current_subject
        from bvphoenix.storage import get_s3_storage
    except ImportError as exc:
        log.exception("bvphoenix not importable from worker: %s", exc)
        return {"status": "error", "reason": f"bvphoenix import: {exc}"}

    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    deleted = 0
    files_dropped = 0
    file_failures = 0
    try:
        async with AsyncSession(engine, expire_on_commit=False) as db:
            await set_current_subject(db, SERVICE_SUBJECT)
            now = datetime.now(UTC)
            rows = (
                (
                    await db.execute(
                        select(Document)
                        .where(
                            Document.deleted_at.is_not(None),
                            Document.purge_after.is_not(None),
                            Document.purge_after < now,
                        )
                        .limit(_BATCH_SIZE)
                    )
                )
                .scalars()
                .all()
            )
            if not rows:
                return {
                    "status": "ok",
                    "deleted": 0,
                    "files_dropped": 0,
                    "file_failures": 0,
                }

            storage = get_s3_storage()
            for doc in rows:
                # Drop the multi-file collection first.
                files = (
                    (
                        await db.execute(
                            select(DocumentFile).where(
                                DocumentFile.document_id == doc.id
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                for f in files:
                    if not f.file_s3_key:
                        continue
                    try:
                        storage.delete_object(bucket=settings.s3_bucket_raw, key=f.file_s3_key)
                        files_dropped += 1
                    except Exception as exc:
                        file_failures += 1
                        log.warning(
                            "purge: failed to drop s3 file %s: %s",
                            f.file_s3_key,
                            exc,
                        )
                # Legacy single-file slot.
                if doc.file_s3_key:
                    try:
                        storage.delete_object(bucket=settings.s3_bucket_raw, key=doc.file_s3_key)
                        files_dropped += 1
                    except Exception as exc:
                        file_failures += 1
                        log.warning(
                            "purge: failed to drop legacy s3 file %s: %s",
                            doc.file_s3_key,
                            exc,
                        )
                # Hard-delete the document — cascades to
                # patient_document_files and document_study_links.
                await db.delete(doc)
                deleted += 1
            await db.commit()
    finally:
        await engine.dispose()

    log.info(
        "purge_expired_documents: deleted=%d files_dropped=%d failures=%d",
        deleted,
        files_dropped,
        file_failures,
    )
    return {
        "status": "ok",
        "deleted": deleted,
        "files_dropped": files_dropped,
        "file_failures": file_failures,
    }
