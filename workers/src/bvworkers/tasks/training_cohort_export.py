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

On success it also MATERIALIZES the cohort as a standalone, reusable
Dataset (Flow task a5c3f73e, Option 3): a ``licensed_datasets`` row
(``status='open'``) plus one ``dataset_studies`` row per study, carrying the
contributor (the study's active training consent) and the per-study
de-identified byte count + content hash accumulated while streaming. A
license can later bind this dataset (``training_licenses.dataset_id``) and
``services.contributor_payouts.assemble_payouts`` splits revenue by those
per-study bytes. The standalone manifest object backs
``licensed_datasets.manifest_s3_key``.

Progress mirrors the study export (a shared counter polled by an async
task → Job.progress_done). The same poller also honours a mid-stream
cancel: ``request_cancellation`` flips the Job to ``cancelled`` and the
streaming thread bails before its next member (``upload_iter`` aborts the
multipart, so no orphan artifact); a cancelled run never materializes a
dataset. The Job is recoverable cross-session via its scope + arq_job_id.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import threading
import uuid
from datetime import UTC, datetime
from typing import Any

from bvphoenix.db.models import DatasetStudy, LicensedDataset, User
from bvphoenix.db.session import SERVICE_SUBJECT, set_current_subject
from bvphoenix.services import jobs as jobs_service
from bvphoenix.services import k_anonymity, training_cohort
from bvphoenix.services.face_deid import get_defacer
from bvphoenix.services.patient_export import _bytes_member, _fetch_blob_bytes
from bvphoenix.services.pixel_deid import classify_pixel_risk_bytes
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
) -> tuple[int, dict[uuid.UUID, dict[str, Any]], list[dict[str, Any]]]:
    """Sync core: fetch each blob (DICOM de-id'd, masks raw), zip-stream
    into the S3 multipart sink. O(part_size) memory.

    Accumulates per-study de-identified byte count + a running SHA-256 as it
    streams (keyed on the work item's real ``study_id``) so the dataset
    producer can write one ``DatasetStudy`` per study with an accurate
    ``size_bytes`` (the payout weight) and ``content_sha256``. Returns
    ``(total_size_bytes, {study_id: {size_bytes, content_sha256}}, skipped)``
    where ``skipped`` lists the high-risk-pixel instances excluded by the
    burned-in-PHI gate (see below).

    ``cancel`` is checked before each member (and before the trailing
    labels.json): once the poller trips it, the generator raises
    ``_ExportCancelledError`` so ``upload_iter`` aborts the multipart cleanly."""
    storage = get_s3_storage()
    per_study: dict[uuid.UUID, dict[str, Any]] = {}
    skipped: list[dict[str, Any]] = []
    # Resolve the de-facer once: when enabled (``face_deid_enabled``), face-risk
    # (``low``) instances are gated too — see the per-item comment below. Off by
    # default, so the face branch is inert unless explicitly enabled.
    deface_on = get_defacer() is not None

    def _members():  # type: ignore[no-untyped-def]
        for item in work:
            if cancel.is_set():
                raise _ExportCancelledError
            is_dicom = item["kind"] == "dicom"
            body = _fetch_blob_bytes(storage, item["bucket"], item["key"], deidentify=is_dicom)
            # Burned-in-pixel PHI gate: ``deidentify`` above scrubs the DICOM
            # *header* but leaves pixels untouched, so an instance with PHI
            # burned into the image (ultrasound banners, secondary capture,
            # dose-report screenshots) would otherwise ship to the public /
            # licensed cohort. ``high``-risk instances are ALWAYS EXCLUDED.
            #
            # ``low``-risk (recognizable-visual-feature: head/face CT/MR/PT) is
            # excluded too WHEN de-facing is enabled — this is an automated
            # egress with no human-review step, and a face can be surface-
            # rendered from the volume, so it must not ship un-defaced. With
            # de-facing off (default) face-risk ships as today (no regression).
            # Excluded instances are recorded so the drop is never silent.
            if is_dicom:
                risk = classify_pixel_risk_bytes(body)
                if risk.is_high or (deface_on and risk.level == "low"):
                    skipped.append(
                        {
                            "study_id": item.get("study_id"),
                            "name": item["name"],
                            "risk": risk.level,
                        }
                    )
                    progress_q[0] += 1
                    continue
            sid = item.get("study_id")
            if sid is not None:
                stat = per_study.setdefault(sid, {"size": 0, "hash": hashlib.sha256()})
                stat["size"] += len(body)
                stat["hash"].update(body)
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
    stats = {
        sid: {"size_bytes": s["size"], "content_sha256": s["hash"].hexdigest()}
        for sid, s in per_study.items()
    }
    return result.size_bytes, stats, skipped


async def _stream_cohort(
    engine: Any,
    job_id: uuid.UUID,
    work: list[dict[str, Any]],
    labels_bytes: bytes,
    *,
    bucket: str,
    key: str,
    total: int,
) -> tuple[int, dict[uuid.UUID, dict[str, Any]], list[dict[str, Any]]]:
    """Run the sync stream in a thread while an async poller publishes the
    shared progress counter to the Job. Returns the streamed size, the
    per-study byte/hash stats the dataset producer needs, and the list of
    high-risk-pixel instances the burned-in-PHI gate excluded."""
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
        size, per_study_stats, pixel_skipped = await _stream_cohort(
            engine, jid, work, labels_bytes, bucket=bucket, key=key, total=total
        )
        if pixel_skipped:
            by_risk: dict[str, int] = {}
            for s in pixel_skipped:
                lvl = str(s.get("risk") or "high")
                by_risk[lvl] = by_risk.get(lvl, 0) + 1
            logger.warning(
                "training cohort export %s: burned-in-PHI / face-risk gate EXCLUDED %d "
                "instance(s) from the public artifact (by risk: %s); affected studies=%s",
                job_id,
                len(pixel_skipped),
                by_risk,
                sorted({str(s["study_id"]) for s in pixel_skipped if s.get("study_id")}),
            )

        # Materialize the standalone Dataset (Option 3): the manifest gets its
        # own S3 object (the ZIP is the bundle, but licensed_datasets.manifest_s3_*
        # must point at a real object), then the ledger rows so a license can
        # later bind this cohort and assemble_payouts can split by per-study
        # bytes. Empty cohorts never reach here (k-anonymity would have failed).
        manifest_key = f"exports/training/{job_id}/labels.json"
        await asyncio.to_thread(
            lambda: get_s3_storage().upload_bytes(labels_bytes, bucket=bucket, key=manifest_key)
        )
        manifest_hash = hashlib.sha256(labels_bytes).hexdigest()
        empty_sha = hashlib.sha256(b"").hexdigest()

        async with AsyncSession(engine, expire_on_commit=False) as db:
            await set_current_subject(db, SERVICE_SUBJECT)
            study_ids = list(study_syn.keys())
            contributors = await training_cohort.resolve_cohort_contributors(db, study_ids)
            dataset = LicensedDataset(
                status="open",
                manifest_hash=manifest_hash,
                study_count=len(study_ids),
                contributor_count=len({c for c in contributors.values() if c is not None}),
                k_anon=min(kanon.values()) if kanon else 1,
                manifest_s3_bucket=bucket,
                manifest_s3_key=manifest_key,
            )
            db.add(dataset)
            await db.flush()
            for sid in study_ids:
                stat = per_study_stats.get(sid)
                db.add(
                    DatasetStudy(
                        dataset_id=dataset.id,
                        study_id=sid,
                        contributor_subject_id=contributors.get(sid),
                        # The study's anonymized artifact lives inside the cohort
                        # ZIP; size/hash are per-study (payout weight + integrity),
                        # accumulated while streaming.
                        anonymized_s3_bucket=bucket,
                        anonymized_s3_key=key,
                        content_sha256=stat["content_sha256"] if stat else empty_sha,
                        size_bytes=stat["size_bytes"] if stat else 0,
                    )
                )
            await jobs_service.mark_succeeded(db, jid, result_uri=f"s3://{bucket}/{key}")
            await db.commit()
            dataset_id = str(dataset.id)
        return {
            "status": "ok",
            "studies": len(study_syn),
            "findings": len(rows),
            "size_bytes": size,
            "dataset_id": dataset_id,
            "pixel_phi_skipped": len(pixel_skipped),
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
