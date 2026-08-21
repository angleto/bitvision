"""Arq task: build and upload a GDPR Art. 20 data-portability ZIP.

Mirrors :mod:`bvworkers.tasks.export_patient` for a single-user
bundle. Delegates the actual SQL + zip work to
:func:`bvphoenix.services.gdpr_export.build_gdpr_zip`; the task body
is just lifecycle plumbing (mark running, upload, mark succeeded /
failed, write audit row).

Permission model: the API has already authenticated the caller as
themself. The worker re-resolves the User row, but does not re-check
authorization — the GDPR endpoint is by definition a user exporting
their own data, and the input is just ``user_subject_id`` so there
is no other subject to authorize against.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from bvphoenix.db.engine import make_async_engine
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bvworkers.config import get_settings

log = logging.getLogger(__name__)


async def export_gdpr_zip(
    ctx: dict,  # type: ignore[type-arg]
    job_id: str,
    user_subject_id: str,
) -> dict[str, Any]:
    """Arq entry point.

    Args:
        ctx: arq context.
        job_id: Job row UUID (string).
        user_subject_id: Subject UUID of the user whose bundle to
            export. Equals ``Job.owner_subject_id`` — passed
            explicitly so the task signature stays parallel to the
            patient-export task.
    """
    try:
        jid = uuid.UUID(job_id)
        sid = uuid.UUID(user_subject_id)
    except (TypeError, ValueError) as exc:
        log.error("invalid uuid argument: %s", exc)
        return {"status": "error", "reason": f"invalid uuid: {exc}"}

    settings = get_settings()

    try:
        from bvphoenix.db.models import User
        from bvphoenix.db.session import SERVICE_SUBJECT, set_current_subject
        from bvphoenix.services import jobs as jobs_service
        from bvphoenix.services.audit import log_action
        from bvphoenix.services.gdpr_export import (
            build_gdpr_zip,
            upload_gdpr_zip,
        )
    except ImportError as exc:
        log.exception("bvphoenix not importable from worker: %s", exc)
        return {"status": "error", "reason": f"bvphoenix import: {exc}"}

    engine = make_async_engine(settings.database_url, pool_pre_ping=True)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as db:
            await set_current_subject(db, SERVICE_SUBJECT)
            await jobs_service.mark_running(db, jid)
            await db.commit()

            user = (
                await db.execute(select(User).where(User.subject_id == sid))
            ).scalar_one_or_none()
            if user is None:
                await jobs_service.mark_failed(
                    db,
                    jid,
                    error={
                        "code": "user_not_found",
                        "message": f"user {sid} not found",
                    },
                )
                await db.commit()
                return {"status": "error", "reason": "user not found"}

            await jobs_service.update_progress(
                db, jid, stage="building_zip"
            )
            await db.commit()

            try:
                zip_bytes, bundle = await build_gdpr_zip(db, user)
            except Exception as exc:
                log.exception("gdpr zip build failed for job %s", jid)
                await jobs_service.mark_failed(
                    db,
                    jid,
                    error={"code": "build_failed", "message": str(exc)},
                )
                await db.commit()
                return {"status": "error", "reason": "build failed"}

            await jobs_service.update_progress(db, jid, stage="uploading")
            await db.commit()

            try:
                bucket, key = upload_gdpr_zip(
                    zip_bytes, job_id=jid, user=user
                )
            except Exception as exc:
                log.exception("gdpr S3 upload failed for job %s", jid)
                await jobs_service.mark_failed(
                    db,
                    jid,
                    error={"code": "upload_failed", "message": str(exc)},
                )
                await db.commit()
                return {"status": "error", "reason": "upload failed"}

            result_uri = f"s3://{bucket}/{key}"
            await jobs_service.mark_succeeded(
                db, jid, result_uri=result_uri
            )
            await db.commit()

        await log_action(
            actor_subject_id=sid,
            action="gdpr.export",
            resource_kind="user",
            resource_id=sid,
            metadata={
                "job_id": str(jid),
                "bytes": len(zip_bytes),
                "schema_version": bundle.get("schema_version"),
                "s3_key": key,
            },
        )
        return {
            "status": "ok",
            "job_id": str(jid),
            "result_uri": result_uri,
            "size_bytes": len(zip_bytes),
        }
    finally:
        await engine.dispose()
