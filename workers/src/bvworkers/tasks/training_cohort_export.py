"""Training cohort export worker (annotation overhaul P5-rest).

Streams a de-identified training-set ZIP to S3 behind a Job: per-study
DICOM images (scrubbed via the DICOM de-identifier) + segmentation masks
(headerless raw uint8 .bin — no PHI) + ``labels.json`` (the coded labels
manifest), all under SYNTHETIC study ids so no patient / study / series /
finding / author identifier reaches the artifact.

The cohort is re-selected here (not at enqueue), so a consent revoked
between enqueue and run is honored. Reuses the proven streaming machinery
from ``services.patient_export`` (stream-zip member tuples +
``S3Storage.upload_iter`` + ``_fetch_blob_bytes``); only the plan is
ours (multi-patient, synthetic-keyed) because ``_build_export_plan`` is
single-patient + identifying.

Progress mirrors the study export (a shared counter polled by an async
task → Job.progress_done). The same poller also honours a mid-stream
cancel: ``request_cancellation`` flips the Job to ``cancelled`` and the
streaming thread bails before its next member (``upload_iter`` aborts the
multipart, so no orphan artifact). The Job is recoverable cross-session
via its scope + arq_job_id.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import threading
import uuid
from datetime import UTC, datetime
from typing import Any

from bvphoenix.db.models import User
from bvphoenix.db.session import SERVICE_SUBJECT, set_current_subject
from bvphoenix.services import jobs as jobs_service
from bvphoenix.services import k_anonymity, training_cohort
from bvphoenix.services.patient_export import _bytes_member, _fetch_blob_bytes
from bvphoenix.storage import get_s3_storage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from stream_zip import stream_zip

from bvworkers.config import get_settings

logger = logging.getLogger(__name__)


class _ExportCancelledError(Exception):
    """Raised by the streaming core when the Job was cancelled mid-stream.

    Cooperative cancel: ``request_cancellation`` flips the Job row to
    ``cancelled`` immediately; the progress poller below reads that at its
    next tick and trips the shared flag, and the blob generator bails before
    the next member. The half-written S3 multipart is aborted by
    ``upload_iter`` (it aborts on any generator exception, so no orphan part
    is left), and the worker returns ``cancelled`` without touching the
    already-terminal Job status.
    """


def _filters(q: dict[str, Any]) -> dict[str, Any]:
    """Map the canonical query input to select_cohort kwargs."""
    return {
        "type": q.get("type"),
        "anatomy": q.get("anatomy"),
        "laterality": q.get("laterality"),
        "morphology": q.get("morphology"),
        "status_filter": q.get("status"),
        "min_diameter_mm": q.get("min_diameter_mm"),
        "max_diameter_mm": q.get("max_diameter_mm"),
        "min_volume_ml": q.get("min_volume_ml"),
        "min_suv_max": q.get("min_suv_max"),
        "scope": q.get("scope", "all"),
        "k_min": q.get("k_min", k_anonymity.DEFAULT_K_MIN),
    }


def _stream_cohort_sync(
    work: list[dict[str, Any]],
    labels_bytes: bytes,
    *,
    bucket: str,
    key: str,
    progress_q: list[int],
    cancel: threading.Event,
) -> int:
    """Sync core: fetch each blob (DICOM de-id'd, masks raw), zip-stream
    into the S3 multipart sink. O(part_size) memory.

    ``cancel`` is checked before each member (and before the trailing
    labels.json): once the poller trips it, the generator raises
    ``_ExportCancelledError`` so ``upload_iter`` aborts the multipart cleanly."""
    storage = get_s3_storage()

    def _members():  # type: ignore[no-untyped-def]
        for item in work:
            if cancel.is_set():
                raise _ExportCancelledError
            body = _fetch_blob_bytes(
                storage, item["bucket"], item["key"], deidentify=(item["kind"] == "dicom")
            )
            # DICOM + mask blobs are already-incompressible binary → STORE.
            yield _bytes_member(item["name"], body, compress=False)
            progress_q[0] += 1
        if cancel.is_set():
            raise _ExportCancelledError
        # Coded labels manifest → DEFLATE (text).
        yield _bytes_member("labels.json", labels_bytes, compress=True)
        progress_q[0] += 1

    result = storage.upload_iter(
        stream_zip(_members()), bucket=bucket, key=key, content_type="application/zip"
    )
    return result.size_bytes


async def _stream_cohort(
    engine: Any,
    job_id: uuid.UUID,
    work: list[dict[str, Any]],
    labels_bytes: bytes,
    *,
    bucket: str,
    key: str,
    total: int,
) -> int:
    """Run the sync stream in a thread while an async poller publishes the
    shared progress counter to the Job."""
    progress_q = [0]
    stop = asyncio.Event()
    cancel = threading.Event()

    async def _poll() -> None:
        while not stop.is_set():
            await asyncio.sleep(1.0)
            try:
                async with AsyncSession(engine, expire_on_commit=False) as ps:
                    await set_current_subject(ps, SERVICE_SUBJECT)
                    # Cooperative cancel: a concurrent request_cancellation
                    # flips the Job to ``cancelled``; trip the shared flag so
                    # the streaming thread bails before its next member.
                    job = await jobs_service.get_job(ps, job_id)
                    if job.status == "cancelled":
                        cancel.set()
                        stop.set()
                        break
                    await jobs_service.update_progress(
                        ps,
                        job_id,
                        progress_done=progress_q[0],
                        progress_total=total,
                        stage="bundling",
                    )
                    await ps.commit()
            except Exception:  # pragma: no cover — progress is best-effort
                pass

    poller = asyncio.create_task(_poll())
    try:
        return await asyncio.to_thread(
            _stream_cohort_sync,
            work,
            labels_bytes,
            bucket=bucket,
            key=key,
            progress_q=progress_q,
            cancel=cancel,
        )
    finally:
        stop.set()
        poller.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await poller


async def training_cohort_export_zip(
    ctx: dict[str, Any],
    job_id: str,
    owner_subject_id: str,
    canonical_input_json: str,
) -> dict[str, Any]:
    """Arq task: assemble + stream the de-identified training cohort ZIP."""
    jid = uuid.UUID(job_id)
    query = json.loads(canonical_input_json)
    settings = get_settings()
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as db:
            await set_current_subject(db, SERVICE_SUBJECT)
            await jobs_service.mark_running(db, jid)
            await db.commit()

            owner = (
                await db.execute(select(User).where(User.subject_id == uuid.UUID(owner_subject_id)))
            ).scalar_one_or_none()
            if owner is None:
                await jobs_service.mark_failed(
                    db, jid, error={"code": "owner_not_found", "message": "export owner not found"}
                )
                await db.commit()
                return {"status": "failed", "error": "owner_not_found"}

            try:
                rows, kanon = await training_cohort.select_cohort(db, owner, **_filters(query))
            except k_anonymity.KAnonymityError as exc:
                await jobs_service.mark_failed(
                    db, jid, error={"code": "k_anonymity_not_met", "message": str(exc)}
                )
                await db.commit()
                return {"status": "failed", "error": "k_anonymity_not_met"}

            study_syn = training_cohort.synthetic_study_map(rows)
            manifest = training_cohort.build_labels_manifest(
                rows,
                dataset_id=job_id,
                generated_at=datetime.now(UTC).isoformat(),
                kanon=kanon,
                study_syn=study_syn,
            )
            work = await training_cohort.cohort_blob_plan(db, study_syn)

        labels_bytes = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
        total = len(work) + 1  # + labels.json
        bucket = settings.s3_bucket_derivatives
        key = f"exports/training/{job_id}/cohort.zip"
        size = await _stream_cohort(
            engine, jid, work, labels_bytes, bucket=bucket, key=key, total=total
        )

        async with AsyncSession(engine, expire_on_commit=False) as db:
            await set_current_subject(db, SERVICE_SUBJECT)
            await jobs_service.mark_succeeded(db, jid, result_uri=f"s3://{bucket}/{key}")
            await db.commit()
        return {
            "status": "ok",
            "studies": len(study_syn),
            "findings": len(rows),
            "size_bytes": size,
        }
    except _ExportCancelledError:
        # The Job is already terminal ('cancelled', set by request_cancellation);
        # upload_iter aborted the multipart so there is no orphan artifact. Do
        # NOT mark succeeded/failed — that would clobber the cancelled status.
        logger.info("training cohort export %s cancelled mid-stream", job_id)
        return {"status": "cancelled", "job_id": job_id}
    except Exception as exc:
        logger.exception("training cohort export failed for job %s", job_id)
        try:
            async with AsyncSession(engine, expire_on_commit=False) as db:
                await set_current_subject(db, SERVICE_SUBJECT)
                await jobs_service.mark_failed(
                    db,
                    jid,
                    error={"code": "export_error", "message": f"{type(exc).__name__}: {exc}"},
                )
                await db.commit()
        except Exception:  # pragma: no cover
            logger.exception("failed to mark job %s failed", job_id)
        return {"status": "failed", "error": str(exc)}
    finally:
        await engine.dispose()


__all__ = ["training_cohort_export_zip"]
