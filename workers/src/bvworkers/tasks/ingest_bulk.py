"""Arq task: run a bulk-upload Job that the API has staged into S3.

The API endpoint ``POST /api/upload/bulk`` only stages multipart
bytes into ``_ingest_jobs/<job_id>/`` and creates a Job row pointing
at them. This task pulls the manifest, runs the actual ingest via
:func:`bvphoenix.services.bulk_ingest.process_bulk_ingest`, reports
progress via ``services.jobs.update_progress``, and on completion
deletes the staging objects.

Lifecycle:

1. Mark Job ``running``.
2. Read the manifest + parameters from ``Job.input``.
3. Call ``process_bulk_ingest`` with a progress callback that
   forwards stage + counts to the Job row (one DB roundtrip per
   batch of 5 files; service batches internally).
4. Stash the JSON-serialised summary in ``Job.input.result`` so the
   frontend can render it on completion (the Job model has no
   dedicated ``result`` column; ``input`` is the closest fit).
5. Mark Job ``succeeded``.
6. Best-effort delete of every staged S3 key.

Any unhandled exception flips the row to ``failed`` with a
structured error payload.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from bvworkers.config import get_settings
from bvworkers.job_safety import mark_job_failed_raw, with_safety_net

log = logging.getLogger(__name__)


@with_safety_net("ingest_bulk_files")
async def ingest_bulk_files(
    ctx: dict,  # type: ignore[type-arg]
    job_id: str,
) -> dict[str, Any]:
    try:
        jid = uuid.UUID(job_id)
    except (TypeError, ValueError) as exc:
        log.error("invalid job_id: %s", exc)
        return {"status": "error", "reason": f"invalid uuid: {exc}"}

    try:
        from bvphoenix.db.models.jobs import Job
        from bvphoenix.db.session import SERVICE_SUBJECT, set_current_subject
        from bvphoenix.services import jobs as jobs_service
        from bvphoenix.services.bulk_ingest import (
            IngestSummary,
            StagedFile,
            process_bulk_ingest,
            summary_to_dict,
        )
        from bvphoenix.storage import get_s3_storage
    except ImportError as exc:
        log.exception("bvphoenix import failed: %s", exc)
        # Outer ``with_safety_net`` would catch this on exception, but
        # we intentionally return rather than raise (keeps arq from
        # retrying a hopeless job). Manually trigger the same DB
        # update so the Job row doesn't sit in ``queued`` forever.
        await mark_job_failed_raw(
            job_id,
            code="bvphoenix_import_failed",
            message=str(exc),
        )
        return {"status": "error", "reason": f"import: {exc}"}

    settings = get_settings()
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as db:
            await set_current_subject(db, SERVICE_SUBJECT)
            await jobs_service.mark_running(db, jid)
            await db.commit()

            job = await jobs_service.get_job(db, jid)
            payload: dict[str, Any] = job.input or {}
            # Group staged files by ``subfolder_name`` so each ISO's
            # unpacked content can be ingested into its own sub-folder
            # under the request's target folder. ``None`` means
            # "directly in the target folder" (loose files / non-ISO
            # uploads).
            grouped: dict[str | None, list[StagedFile]] = {}
            staged_all: list[StagedFile] = []
            for entry in payload.get("manifest", []) or []:
                sf = StagedFile(
                    relative_path=entry["relative_path"],
                    filename=entry["filename"],
                    s3_key=entry["s3_key"],
                )
                staged_all.append(sf)
                grouped.setdefault(entry.get("subfolder_name"), []).append(sf)
            try:
                owner_subject_id = uuid.UUID(payload["owner_subject_id"])
            except (KeyError, ValueError) as exc:
                await jobs_service.mark_failed(
                    db,
                    jid,
                    error={
                        "code": "bad_input",
                        "message": f"owner_subject_id missing/invalid: {exc}",
                    },
                )
                await db.commit()
                return {"status": "error", "reason": "bad input"}
            patient_id = (
                uuid.UUID(payload["patient_id"]) if payload.get("patient_id") else None
            )
            folder_id = (
                uuid.UUID(payload["folder_id"]) if payload.get("folder_id") else None
            )
            tier = payload.get("tier", "t1")

            async def progress(done: int, total: int, stage: str) -> None:
                await jobs_service.update_progress(
                    db,
                    jid,
                    progress_done=done,
                    progress_total=total or None,
                    stage=stage,
                )
                await db.commit()

            # Resolve / create one sub-folder per ``subfolder_name``
            # present in the manifest. The bare ``None`` group keeps
            # the request's ``folder_id`` verbatim. We do this once
            # up-front so the per-group ingests can reuse the IDs
            # without racing on the create.
            group_folder_ids: dict[str | None, uuid.UUID | None] = {}
            for group_name in grouped.keys():
                if group_name is None:
                    group_folder_ids[None] = folder_id
                    continue
                group_folder_ids[group_name] = await _ensure_subfolder(
                    db,
                    parent_folder_id=folder_id,
                    patient_id=patient_id,
                    owner_subject_id=owner_subject_id,
                    name=group_name,
                )

            iso_archives_payload = payload.get("iso_archives") or []
            try:
                summary: IngestSummary | None = None
                for group_name, group_files in grouped.items():
                    group_summary = await process_bulk_ingest(
                        db,
                        staged_files=group_files,
                        owner_subject_id=owner_subject_id,
                        patient_id=patient_id,
                        folder_id=group_folder_ids[group_name],
                        tier=tier,
                        progress_cb=progress,
                    )
                    summary = (
                        group_summary
                        if summary is None
                        else _merge_summaries(summary, group_summary)
                    )
                if summary is None:
                    # Manifest is empty. Two cases:
                    #   1) ISO-only upload where every ISO failed to
                    #      extract (pycdlib can't read UDF-only or
                    #      proprietary images). ``iso_archives`` is
                    #      populated; we still persist them as
                    #      Documents because the original archive is
                    #      the legal source of truth (the viewer is
                    #      not certified for clinical reporting).
                    #   2) No manifest, no ISOs — the API should have
                    #      rejected this upstream; treat as a hard
                    #      error.
                    if not iso_archives_payload:
                        raise RuntimeError("empty manifest reached the worker")
                    summary = IngestSummary()
            except Exception as exc:
                log.exception("bulk ingest failed for job %s", jid)
                await jobs_service.mark_failed(
                    db,
                    jid,
                    error={"code": "ingest_failed", "message": str(exc)},
                )
                await db.commit()
                return {"status": "error", "reason": str(exc)}

            # Stash the summary in Job.input.result so the frontend can
            # render counts + skipped reasons on completion. The Job
            # model has no dedicated result JSONB column; reuse input
            # rather than introduce a migration.
            #
            # Merge the staging-time skips (ZIP/ISO unpack failures the
            # endpoint surfaced before enqueue) into the worker's
            # post-ingest skips so the UI shows one combined list.
            result_dict = summary_to_dict(summary)
            stage_skipped = payload.get("stage_skipped") or []
            if stage_skipped:
                result_dict["skipped"] = list(stage_skipped) + result_dict["skipped"]
            result_dict["zip_archives_found"] = payload.get(
                "zip_archives_found", 0
            )
            # Persist every uploaded ISO archive as a downloadable
            # Document so a referring clinician can always grab
            # the bit-identical original CD/DVD image and read it on
            # a certified workstation. The in-app viewer is not
            # certified for clinical reporting; the original archive
            # is the legal source of truth.
            iso_archives = iso_archives_payload
            if iso_archives and patient_id is not None:
                await _persist_iso_archives(
                    db,
                    iso_archives=iso_archives,
                    patient_id=patient_id,
                    folder_id=folder_id,
                    owner_subject_id=owner_subject_id,
                    src_bucket=settings.s3_bucket_raw,
                    wrap_in_folder=bool(payload.get("wrap_iso_in_folder", True)),
                )

            new_input = dict(payload)
            new_input["result"] = result_dict
            await db.execute(
                update(Job).where(Job.id == jid).values(input=new_input)
            )
            await jobs_service.mark_succeeded(db, jid, result_uri=None)
            await db.commit()

            # Best-effort cleanup of staging keys. A leak doesn't break
            # anything; lifecycle policies on the bucket can sweep
            # ``_ingest_jobs/`` after N days as backup. ISO archives
            # were copied to the patient's stable prefix above, so the
            # staging copy is safe to drop here.
            storage = get_s3_storage()
            for sf in staged_all:
                try:
                    await asyncio.to_thread(
                        storage.delete_object,
                        bucket=settings.s3_bucket_raw,
                        key=sf.s3_key,
                    )
                except Exception:
                    log.warning("staging cleanup failed for %s", sf.s3_key)
            for iso in iso_archives:
                try:
                    await asyncio.to_thread(
                        storage.delete_object,
                        bucket=settings.s3_bucket_raw,
                        key=iso["s3_key"],
                    )
                except Exception:
                    log.warning(
                        "iso staging cleanup failed for %s", iso["s3_key"]
                    )

            # Enqueue pack_volume per touched series so the viewer
            # doesn't have to pack on first open. Mirrors what the
            # legacy ``api/dicom_upload`` endpoint does after a
            # /studies POST. Best-effort: a Redis blip here just means
            # the viewer pays the pack cost on cold open.
            if summary.series_ids:
                try:
                    from arq import create_pool
                    from bvphoenix.config import get_settings as _get_bvp_settings
                    from bvphoenix.services.arq_redis import redis_settings

                    bvp_settings = _get_bvp_settings()
                    pool = await create_pool(redis_settings(bvp_settings.redis_url))
                    try:
                        for sid in summary.series_ids:
                            await pool.enqueue_job("pack_volume", sid)
                    finally:
                        await pool.close()
                    log.info(
                        "bulk ingest job %s: enqueued pack_volume for %d series",
                        jid,
                        len(summary.series_ids),
                    )
                except Exception:
                    log.exception(
                        "bulk ingest job %s: pack_volume enqueue failed", jid
                    )

            log.info(
                "bulk ingest job %s done: %d studies, %d documents, %d skipped",
                jid,
                len(summary.studies_created),
                len(summary.documents_created),
                len(summary.skipped),
            )
            return {"status": "ok", **result_dict}
    finally:
        await engine.dispose()


async def _ensure_subfolder(
    db: AsyncSession,
    *,
    parent_folder_id: uuid.UUID | None,
    patient_id: uuid.UUID | None,
    owner_subject_id: uuid.UUID,
    name: str,
) -> uuid.UUID | None:
    """Find or create a sub-folder named ``name`` under ``parent_folder_id``.

    Used to land an ISO's unpacked content in a self-contained folder
    so vendor README/autorun files don't pollute the parent folder.
    Match is by ``(parent_folder_id, name, patient_id)`` so the same
    operator re-uploading the same ISO into the same target merges
    into the existing folder instead of cloning it.

    Returns the folder UUID, or ``None`` if neither the parent nor a
    patient_id was provided (root-of-workspace ingest, can't anchor a
    subfolder safely — fall back to flat ingest in the caller).
    """
    from sqlalchemy import select

    from bvphoenix.db.models import Folder

    # An ISO without a patient or parent has nowhere stable to live.
    # Returning None makes the caller skip the wrap.
    if patient_id is None and parent_folder_id is None:
        return None

    existing = (
        await db.execute(
            select(Folder).where(
                Folder.name == name,
                Folder.parent_folder_id == parent_folder_id,
                Folder.patient_id == patient_id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing.id

    folder = Folder(
        name=name,
        parent_folder_id=parent_folder_id,
        patient_id=patient_id,
        owner_subject_id=owner_subject_id,
    )
    db.add(folder)
    await db.flush()
    await db.commit()
    return folder.id


def _merge_summaries(a, b):
    """Combine two ``IngestSummary`` instances in place.

    The bulk ingest now runs once per ``subfolder_name`` group so each
    ISO lands in its own folder. The worker still wants a single
    summary to write back to ``Job.input.result`` and the frontend
    expects one combined list of created studies / documents / skips.
    """
    a.studies_created.extend(b.studies_created)
    a.documents_created.extend(b.documents_created)
    a.skipped.extend(b.skipped)
    a.series_ids.extend(b.series_ids)
    return a


async def _persist_iso_archives(
    db: AsyncSession,
    *,
    iso_archives: list[dict],
    patient_id: uuid.UUID,
    folder_id: uuid.UUID | None,
    owner_subject_id: uuid.UUID,
    src_bucket: str,
    wrap_in_folder: bool = True,
) -> None:
    """S3-copy each staged ISO into the patient's stable prefix and
    record one Document per archive.

    Path scheme: ``patients/<patient_id>/iso/<doc_uuid>_<filename>``.
    The leading UUID prevents collisions when the same DVD is
    re-uploaded under the same filename.

    v3: the Document is tagged with the canonical 3-axis values for
    a diagnostic ISO archive:
      kind_id        = 'imaging_study_bundle'   (LOINC 18748-4)
      provenance_id  = 'dicom_dvd_iso'           (bit-perfect CD/DVD)
      authority_id   = 'original'                (primary copy)
    plus ``content_sha256`` + ``original_blob_hash`` populated from
    the bytes so the dedup pass can collapse re-uploads of the same
    DVD under one canonical artefact.

    When ``wrap_in_folder`` is True (mirrors the staging-side flag),
    the Document lands in the same per-ISO subfolder as the unpacked
    files instead of in the request's root folder. Without this, the
    .iso bundle would sit stranded in the parent folder while its
    extracted DICOM tree lives one level down, and the operator would
    see a confusing "stuff scattered in root" layout. Falls back to
    ``folder_id`` (root) if the subfolder cannot be created.

    Errors are logged and tolerated — the studies/documents already
    exist on the patient at this point, so a failed ISO persistence
    leaves the fascicolo functional, just without the original CD
    image. Operators can re-upload to retry.
    """
    import hashlib
    from datetime import UTC, datetime

    from bvphoenix.db.models import Document
    from bvphoenix.storage import get_s3_storage

    storage = get_s3_storage()
    settings = get_settings()
    dest_bucket = settings.s3_bucket_raw

    for iso in iso_archives:
        try:
            doc_id = uuid.uuid4()
            safe_name = (iso.get("filename") or "archive.iso").replace(
                "/", "_"
            )
            # Resolve where the bundle Document should land. Mirror the
            # staging-side subfolder logic so the bundle ends up in the
            # same place as the unpacked DICOMDIR/IMAGES tree. If no
            # wrap, or the subfolder creation fails, fall back to the
            # request's root folder.
            target_folder_id = folder_id
            if wrap_in_folder:
                iso_stem = safe_name
                if iso_stem.lower().endswith(".iso"):
                    iso_stem = iso_stem[: -len(".iso")]
                if iso_stem:
                    try:
                        subfolder_id = await _ensure_subfolder(
                            db,
                            parent_folder_id=folder_id,
                            patient_id=patient_id,
                            owner_subject_id=owner_subject_id,
                            name=iso_stem,
                        )
                    except Exception:
                        log.exception(
                            "failed to ensure subfolder for iso %s — "
                            "Document will land in the root folder",
                            safe_name,
                        )
                        await db.rollback()
                        subfolder_id = None
                    if subfolder_id is not None:
                        target_folder_id = subfolder_id
            dst_key = f"patients/{patient_id}/iso/{doc_id}_{safe_name}"
            await asyncio.to_thread(
                storage.copy_object,
                src_bucket=src_bucket,
                src_key=iso["s3_key"],
                dst_bucket=dest_bucket,
                dst_key=dst_key,
            )
            # Hash the ISO bytes for dedup. ``copy_object`` does not
            # return the bytes, so we read them back from the dest
            # bucket — single round-trip, the storage layer streams
            # so the in-memory footprint is the chunk size.
            try:
                payload, _, _ = await asyncio.to_thread(
                    storage.iter_object,
                    bucket=dest_bucket,
                    key=dst_key,
                )
                hasher = hashlib.sha256()
                for chunk in payload:
                    hasher.update(chunk)
                sha256 = hasher.hexdigest()
            except Exception:
                # If the hash cannot be computed (older storage shim,
                # very large file timing out), fall back to None — the
                # FK-less columns are nullable so the Document still
                # inserts; dedup is best-effort here.
                sha256 = None

            doc = Document(
                id=doc_id,
                patient_id=patient_id,
                uploaded_by_subject_id=owner_subject_id,
                kind_id="imaging_study_bundle",
                provenance_id="dicom_dvd_iso",
                authority_id="original",
                title=f"DVD originale — {safe_name}",
                file_s3_key=dst_key,
                file_content_type="application/x-iso9660-image",
                document_date=datetime.now(UTC).date(),
                content_sha256=sha256,
                original_blob_hash=sha256,
            )
            db.add(doc)
            await db.flush()
            # ``Document`` has no direct ``folder_id`` column — the
            # placement is recorded via the polymorphic ``folder_items``
            # M:N table. Without an entry the Document shows up in the
            # fascicolo root; we add one when ``target_folder_id``
            # resolved to a subfolder so the bundle lands next to its
            # unpacked DICOM tree.
            if target_folder_id is not None:
                from bvphoenix.db.models import FolderItem

                db.add(
                    FolderItem(
                        folder_id=target_folder_id,
                        resource_kind="document",
                        resource_id=doc_id,
                    )
                )
            await db.commit()
            log.info(
                "persisted iso archive %s as Document %s",
                safe_name,
                doc_id,
            )
        except Exception:
            log.exception(
                "failed to persist iso archive %s — viewer ingest is still ok",
                iso.get("filename"),
            )
            await db.rollback()
