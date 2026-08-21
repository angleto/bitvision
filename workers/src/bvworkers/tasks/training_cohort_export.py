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
import io
import json
import logging
import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pydicom
from bvphoenix.db.engine import make_async_engine
from bvphoenix.db.models import DatasetStudy, LicensedDataset, User
from bvphoenix.db.session import SERVICE_SUBJECT, set_current_subject
from bvphoenix.services import jobs as jobs_service
from bvphoenix.services import k_anonymity, training_cohort
from bvphoenix.services import training_cohort_formats as fmts
from bvphoenix.services.face_deid import get_defacer
from bvphoenix.services.patient_export import _bytes_member, _fetch_blob_bytes
from bvphoenix.services.pixel_deid import classify_pixel_risk_bytes
from bvphoenix.storage import get_s3_storage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
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
            if is_dicom and item.get("clean_key"):
                # Human-approved redaction (contribution accept): ship the
                # verified-clean blob EXACTLY as reviewed. No re-scrub (it was
                # header-scrubbed at staging and CID 7050-stamped at accept)
                # and no re-classify (``classify_pixel_risk`` distrusts
                # ``BurnedInAnnotation=NO`` by design, so it would re-flag it).
                # A missing blob is recorded and skipped — never fall back to
                # the raw high-risk bytes.
                body = _fetch_blob_bytes(
                    storage,
                    item.get("clean_bucket") or item["bucket"],
                    item["clean_key"],
                    deidentify=False,
                )
                if not body:
                    skipped.append(
                        {
                            "study_id": item.get("study_id"),
                            "name": item["name"],
                            "risk": "clean_blob_unavailable",
                        }
                    )
                    progress_q[0] += 1
                    continue
            else:
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


def _stream_cohort_volumes_sync(
    series_plan: list[dict[str, Any]],
    labels_bytes: bytes,
    fmt: str,
    label_index: dict[str, int],
    *,
    bucket: str,
    key: str,
    progress_q: list[int],
    cancel: threading.Event,
) -> tuple[int, dict[uuid.UUID, dict[str, Any]], list[dict[str, Any]]]:
    """Sync core for the volume formats (nnU-Net / MONAI / COCO).

    Per series: fetch + de-id every DICOM instance, run the SAME burned-in-PHI
    gate as the raw bundle — but because a NIfTI/PNG ships the WHOLE series as
    one artifact, a single high-risk (or, with de-facing on, face-risk) slice
    drops the ENTIRE series (it cannot be excluded slice-by-slice without
    leaving a hole in the volume). Surviving series are stacked into an image
    volume + a label volume (masks painted through the dataset-wide label
    index) and serialized: nnU-Net/MONAI emit NIfTI image/label pairs, COCO
    emits per-slice PNG + RLE annotations. ``labels.json`` (the coded manifest)
    and the format manifest (``dataset.json`` / ``annotations/instances.json``)
    trail the artifacts. Per-study size/hash accumulate over the EMITTED bytes
    (the payout weight + integrity for the dataset ledger). Returns
    ``(total_size_bytes, per_study_stats, skipped)``; ``skipped`` records every
    dropped series with the reason so a gap is never silent.

    Memory is bounded per series (one image volume + one label volume at a
    time), not per cohort."""
    storage = get_s3_storage()
    per_study: dict[uuid.UUID, dict[str, Any]] = {}
    skipped: list[dict[str, Any]] = []
    deface_on = get_defacer() is not None
    coco = fmts.CocoBuilder(label_index) if fmt == "coco" else None
    monai_cases: list[tuple[str, str]] = []
    modalities: dict[str, int] = {}
    nnunet_count = [0]

    def _acc(sid: uuid.UUID | None, *blobs: bytes) -> None:
        if sid is None:
            return
        stat = per_study.setdefault(sid, {"size": 0, "hash": hashlib.sha256()})
        for b in blobs:
            stat["size"] += len(b)
            stat["hash"].update(b)

    def _members():  # type: ignore[no-untyped-def]
        for s in series_plan:
            if cancel.is_set():
                raise _ExportCancelledError
            sid = s.get("study_id")
            name = f"{s['study_syn']}/series-{s['series_idx']:02d}"
            # Fetch + de-id every slice; gate on burned-in / face-risk pixels.
            datasets: list[pydicom.Dataset] = []
            risk_hit: str | None = None
            for d in s["dicom"]:
                if d.get("clean_key"):
                    # Human-approved redacted slice: ship exactly what the
                    # reviewer saw (see the raw-bundle core above). A missing
                    # blob drops the series — never substitute raw bytes.
                    body = _fetch_blob_bytes(
                        storage,
                        d.get("clean_bucket") or d["bucket"],
                        d["clean_key"],
                        deidentify=False,
                    )
                    if not body:
                        risk_hit = "clean_blob_unavailable"
                        break
                else:
                    body = _fetch_blob_bytes(storage, d["bucket"], d["key"], deidentify=True)
                    risk = classify_pixel_risk_bytes(body)
                    if risk.is_high or (deface_on and risk.level == "low"):
                        risk_hit = risk.level
                        break
                datasets.append(pydicom.dcmread(io.BytesIO(body)))
            if risk_hit is not None or not datasets:
                skipped.append({"study_id": sid, "name": name, "risk": risk_hit or "no_image"})
                progress_q[0] += 1
                continue
            try:
                img_arr, spacing, ordered = fmts.build_image_volume(datasets)
                mask_inputs = []
                for m in s["masks"]:
                    raw = _fetch_blob_bytes(storage, m["bucket"], m["key"], deidentify=False)
                    mask_inputs.append(
                        {"label": m["label"], "label_map": m.get("label_map") or {}, "raw": raw}
                    )
                label_arr = fmts.build_label_volume(mask_inputs, img_arr.shape, label_index)
            except fmts.CohortFormatError as exc:
                skipped.append({"study_id": sid, "name": name, "risk": f"format:{exc}"})
                progress_q[0] += 1
                continue
            if label_arr is None:
                # No usable mask → nothing supervised to learn from this series.
                skipped.append({"study_id": sid, "name": name, "risk": "no_mask"})
                progress_q[0] += 1
                continue
            modality = str(getattr(ordered[0], "Modality", "") or "image")
            modalities[modality] = modalities.get(modality, 0) + 1
            window = fmts.default_window(ordered[0], img_arr) if fmt == "coco" else (0.0, 0.0)
            datasets = ordered = []  # release per-slice pixel caches early
            case_id = f"{s['study_syn']}_series-{s['series_idx']:02d}"
            if fmt in fmts.NIFTI_FORMATS:
                img_name = fmts.nnunet_image_name(case_id)
                lbl_name = fmts.nnunet_label_name(case_id)
                img_bytes = fmts.write_nifti(img_arr, spacing)
                lbl_bytes = fmts.write_nifti(label_arr, spacing)
                _acc(sid, img_bytes, lbl_bytes)
                monai_cases.append((img_name, lbl_name))
                nnunet_count[0] += 1
                yield _bytes_member(img_name, img_bytes, compress=False)
                yield _bytes_member(lbl_name, lbl_bytes, compress=False)
            else:  # coco
                wc, ww = window
                for z in range(label_arr.shape[0]):
                    if not label_arr[z].any():
                        continue
                    fname = f"images/{case_id}_z{z:04d}.png"
                    if coco is not None and coco.add_slice(fname, label_arr[z]):
                        png = fmts.encode_png(fmts.window_to_uint8(img_arr[z], wc=wc, ww=ww))
                        _acc(sid, png)
                        yield _bytes_member(fname, png, compress=False)
            progress_q[0] += 1

        if cancel.is_set():
            raise _ExportCancelledError
        modality = max(modalities, key=lambda k: modalities[k]) if modalities else "image"
        if fmt == "nnunet":
            man = fmts.nnunet_dataset_json(
                modality=modality, label_index=label_index, num_training=nnunet_count[0]
            )
            yield _bytes_member("dataset.json", _json_bytes(man), compress=True)
        elif fmt == "monai":
            man = fmts.monai_dataset_json(
                modality=modality, label_index=label_index, cases=monai_cases
            )
            yield _bytes_member("dataset.json", _json_bytes(man), compress=True)
        elif fmt == "coco" and coco is not None:
            yield _bytes_member(
                "annotations/instances.json", _json_bytes(coco.build()), compress=True
            )
        progress_q[0] += 1
        if cancel.is_set():
            raise _ExportCancelledError
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


def _json_bytes(obj: Any) -> bytes:
    return json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")


async def _run_streamer(
    engine: Any,
    job_id: uuid.UUID,
    total: int,
    runner: Callable[
        [list[int], threading.Event],
        tuple[int, dict[uuid.UUID, dict[str, Any]], list[dict[str, Any]]],
    ],
) -> tuple[int, dict[uuid.UUID, dict[str, Any]], list[dict[str, Any]]]:
    """Run a sync streaming ``runner`` in a thread while an async poller
    publishes the shared progress counter to the Job and honours a mid-stream
    cancel. Format-agnostic: the bvphoenix bundle and the volume formats share
    this exact progress + cancellation machinery. ``runner(progress_q, cancel)``
    returns ``(size_bytes, per_study_stats, skipped)``."""
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
        return await asyncio.to_thread(runner, progress_q, cancel)
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
    engine = make_async_engine(settings.database_url, pool_pre_ping=True)
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
            fmt = str(query.get("format") or "bvphoenix")
            if fmt not in fmts.COHORT_FORMATS:
                fmt = "bvphoenix"
            if fmt == "bvphoenix":
                work = await training_cohort.cohort_blob_plan(db, study_syn)
                series_plan: list[dict[str, Any]] = []
                label_index: dict[str, int] = {}
            else:
                series_plan = await training_cohort.cohort_series_plan(db, study_syn)
                label_index = fmts.build_label_index([s["masks"] for s in series_plan])
                work = []

        labels_bytes = _json_bytes(manifest)
        bucket = settings.s3_bucket_derivatives
        key = f"exports/training/{job_id}/cohort.zip"
        if fmt == "bvphoenix":
            total = len(work) + 1  # + labels.json

            def runner(pq: list[int], cx: threading.Event) -> Any:
                return _stream_cohort_sync(
                    work, labels_bytes, bucket=bucket, key=key, progress_q=pq, cancel=cx
                )
        else:
            total = len(series_plan) + 2  # + format manifest + labels.json

            def runner(pq: list[int], cx: threading.Event) -> Any:
                return _stream_cohort_volumes_sync(
                    series_plan,
                    labels_bytes,
                    fmt,
                    label_index,
                    bucket=bucket,
                    key=key,
                    progress_q=pq,
                    cancel=cx,
                )

        size, per_study_stats, pixel_skipped = await _run_streamer(engine, jid, total, runner)
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
