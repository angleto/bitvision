"""Cron: sweep abandoned resumable upload sessions (DESIGN.md §11.6 GC).

A non-committed ``upload_sessions`` row that hasn't progressed in
``services.upload_sessions.STALE_WINDOW`` (or is past its ``expires_at``) is
abandoned — the user closed the tab mid-upload and never resumed. We abort each
file's open S3 multipart upload + delete any already-staged objects
(``abort_session``) then drop the rows (ON DELETE CASCADE removes the file
rows), so incomplete parts are released within minutes instead of waiting for
the bucket's ``AbortIncompleteMultipartUpload`` lifecycle rule. Best-effort on
the S3 side: a single failed abort never strands the sweep.

Mirrors ``cleanup_jobs.cleanup_expired_jobs``: runs under the service subject
(RLS), lazily imports ``bvphoenix`` so a misconfigured worker fails soft.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from bvworkers.config import get_settings

log = logging.getLogger(__name__)

# Bound per tick so a backlog of abandoned sessions can't run the cron past
# its job timeout; the next tick picks up the rest.
_BATCH_SIZE = 200


async def cleanup_upload_sessions(ctx: dict) -> dict[str, Any]:  # type: ignore[type-arg]
    """Cron entry point. Returns a small summary dict for logging."""
    settings = get_settings()

    try:
        from bvphoenix.db.session import SERVICE_SUBJECT, set_current_subject
        from bvphoenix.services import upload_sessions as svc
        from bvphoenix.storage import get_s3_storage
    except ImportError as exc:
        log.exception("bvphoenix not importable from worker: %s", exc)
        return {"status": "error", "reason": f"bvphoenix import: {exc}"}

    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    aborted = 0
    abort_failures = 0
    deleted = 0
    try:
        async with AsyncSession(engine, expire_on_commit=False) as db:
            await set_current_subject(db, SERVICE_SUBJECT)

            rows = await svc.stale_sessions(db, batch_size=_BATCH_SIZE)
            if not rows:
                return {"status": "ok", "deleted": 0, "aborted": 0}

            storage = get_s3_storage()
            bucket = settings.s3_bucket_raw
            session_ids = []
            for s in rows:
                session_ids.append(s.id)
                try:
                    # Releases S3 multipart parts + staged objects and commits.
                    await svc.abort_session(db, storage, bucket=bucket, session=s)
                    aborted += 1
                except Exception:
                    abort_failures += 1
                    log.warning(
                        "abort_session failed for stale upload session %s", s.id, exc_info=True
                    )

            deleted = await svc.delete_sessions(db, session_ids)
            await db.commit()

        return {
            "status": "ok",
            "deleted": deleted,
            "aborted": aborted,
            "abort_failures": abort_failures,
        }
    finally:
        await engine.dispose()
