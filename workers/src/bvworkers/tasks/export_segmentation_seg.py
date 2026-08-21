"""Async DICOM SEG export worker.

Builds a conformant, geo-referenced DICOM SEG for one stored ``Segmentation``
and uploads it to S3 so it can be downloaded off-platform via the standard
job-result download token (``GET /api/jobs/{id}/result_download?dt=``). This is
the asynchronous, MCP-reachable twin of the synchronous
``GET /series/{id}/segmentations/{label}/dicom-seg`` route — same bytes, but a
Job + S3 artifact so a many-slice series never blocks a request and an agent can
fetch it through the storage-isolated token flow.

All the geometry-preserving serialization lives in
``bvphoenix.services.dicom_seg_export.export_segmentation_seg``; this task is
only the Job lifecycle + S3 upload wrapper (mirrors ``export_study_zip`` /
``training_cohort_export_zip``). The owner's read permission is re-checked at run
time, so a grant revoked between enqueue and run is honoured.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from bvphoenix.db.engine import make_async_engine
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bvworkers.config import get_settings

log = logging.getLogger(__name__)


async def export_segmentation_seg_dicom(
    ctx: dict,  # type: ignore[type-arg]
    job_id: str,
    segmentation_id: str,
    owner_subject_id: str,
    canonical_input_json: str,
) -> dict[str, Any]:
    """Arq entry point: build + upload the SEG, then mark the Job succeeded with
    ``result_uri = s3://<derivatives>/exports/segmentation-seg/<job>/...``."""
    try:
        jid = uuid.UUID(job_id)
        seg_uuid = uuid.UUID(segmentation_id)
        sub = uuid.UUID(owner_subject_id)
    except (TypeError, ValueError) as exc:
        log.error("invalid uuid argument: %s", exc)
        return {"status": "error", "reason": f"invalid uuid: {exc}"}

    settings = get_settings()
    try:
        from bvphoenix.db.models import ImagingStudy, Segmentation, Series, User
        from bvphoenix.db.session import SERVICE_SUBJECT, set_current_subject
        from bvphoenix.services import jobs as jobs_service
        from bvphoenix.services.dicom_seg_export import SegExportError, export_segmentation_seg
        from bvphoenix.services.permissions import READ_PIXELS, can
        from bvphoenix.storage import get_s3_storage
    except ImportError as exc:
        log.exception("bvphoenix not importable from worker: %s", exc)
        return {"status": "error", "reason": f"bvphoenix import: {exc}"}

    engine = make_async_engine(settings.database_url, pool_pre_ping=True)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as db:
            await set_current_subject(db, SERVICE_SUBJECT)
            await jobs_service.mark_running(db, jid)
            await db.commit()

            seg = (
                await db.execute(select(Segmentation).where(Segmentation.id == seg_uuid))
            ).scalar_one_or_none()
            if seg is None:
                await jobs_service.mark_failed(
                    db,
                    jid,
                    error={"code": "segmentation_not_found", "message": "segmentation not found"},
                )
                await db.commit()
                return {"status": "error", "reason": "segmentation not found"}

            user = (
                await db.execute(select(User).where(User.subject_id == sub))
            ).scalar_one_or_none()
            if user is None:
                await jobs_service.mark_failed(
                    db, jid, error={"code": "owner_not_found", "message": "export owner not found"}
                )
                await db.commit()
                return {"status": "error", "reason": "owner not found"}

            # Re-check the owner's read permission at run time (a grant may have
            # been revoked between enqueue and execution).
            study = (
                await db.execute(
                    select(ImagingStudy)
                    .join(Series, Series.study_id == ImagingStudy.id)
                    .where(Series.id == seg.series_id)
                )
            ).scalar_one_or_none()
            if study is None or not await can(db, user=user, action=READ_PIXELS, study=study):
                await jobs_service.mark_failed(
                    db,
                    jid,
                    error={
                        "code": "forbidden",
                        "message": "not authorized to export this segmentation",
                    },
                )
                await db.commit()
                return {"status": "error", "reason": "forbidden"}

            try:
                seg_bytes = await export_segmentation_seg(db, seg)
            except SegExportError as exc:
                await jobs_service.mark_failed(
                    db, jid, error={"code": "seg_export_error", "message": str(exc)}
                )
                await db.commit()
                return {"status": "error", "reason": str(exc)}

        bucket = settings.s3_bucket_derivatives
        key = f"exports/segmentation-seg/{job_id}/seg-{segmentation_id}.dcm"
        await asyncio.to_thread(
            lambda: get_s3_storage().upload_bytes(seg_bytes, bucket=bucket, key=key)
        )

        async with AsyncSession(engine, expire_on_commit=False) as db:
            await set_current_subject(db, SERVICE_SUBJECT)
            await jobs_service.mark_succeeded(db, jid, result_uri=f"s3://{bucket}/{key}")
            await db.commit()
        return {
            "status": "ok",
            "job_id": job_id,
            "size_bytes": len(seg_bytes),
            "result_uri": f"s3://{bucket}/{key}",
        }
    except Exception as exc:
        log.exception("segmentation SEG export failed for job %s", job_id)
        try:
            async with AsyncSession(engine, expire_on_commit=False) as db:
                from bvphoenix.db.session import SERVICE_SUBJECT, set_current_subject
                from bvphoenix.services import jobs as jobs_service

                await set_current_subject(db, SERVICE_SUBJECT)
                await jobs_service.mark_failed(
                    db,
                    jid,
                    error={"code": "export_error", "message": f"{type(exc).__name__}: {exc}"},
                )
                await db.commit()
        except Exception:  # pragma: no cover
            log.exception("failed to mark job %s failed", job_id)
        return {"status": "failed", "error": str(exc)}
    finally:
        await engine.dispose()


__all__ = ["export_segmentation_seg_dicom"]
