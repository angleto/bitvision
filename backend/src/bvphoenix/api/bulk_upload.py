"""Bulk upload API — folders, ZIPs, and heterogeneous file batches.

Accepts a ``multipart/form-data`` payload where the frontend emits one
``files[]`` per uploaded file and a parallel ``relative_paths[]`` array
carrying the browser-side path (from ``webkitdirectory``).

The endpoint is **async** since 2026-04: it stages every (post-ZIP /
post-ISO unpacked) file into the raw bucket under
``_ingest_jobs/<job_id>/`` and enqueues an arq job
(``ingest_bulk_files``) that runs the actual DICOMDIR + classify +
ingest in the background. The HTTP response is ``202 Accepted`` with
a :class:`JobOut` body — the client polls ``GET /api/jobs/{id}`` for
progress + the final result payload, and can ``DELETE /api/jobs/{id}``
to cancel.

ZIP / ISO unpacking still happens in the request thread because the
frontend can stream ISO contents directly (``lib/iso9660.ts``) but
also accepts a raw ``.iso`` upload as a fallback. Server-side
unpacking is bounded by ``MAX_ZIP_DEPTH`` and ``MAX_ISO_BYTES`` to
contain bomb attacks.

The actual ingest pipeline (classify → DICOMDIR-aware DICOM ingest →
non-DICOM document creation) lives in
:mod:`bvphoenix.services.bulk_ingest`; the worker
``bvworkers.tasks.ingest_bulk`` calls it.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import os
import tempfile
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated

from arq import create_pool
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.api._upload_common import (
    resolve_folder as _resolve_folder,
)
from bvphoenix.api._upload_common import (
    resolve_owner_subject as _resolve_owner_subject,
)
from bvphoenix.api._upload_common import (
    resolve_patient as _resolve_patient,
)
from bvphoenix.api.jobs import JobOut, _job_to_out, cap_exceeded_to_http
from bvphoenix.auth import enforce_agent_patient_scope, require_user
from bvphoenix.config import get_settings
from bvphoenix.db.models import (
    Folder,
    Patient,
    Subject,
    User,
)
from bvphoenix.db.session import get_db
from bvphoenix.services import jobs as jobs_service
from bvphoenix.services.archive_guards import (
    MAX_FILE_BYTES,
    MAX_ISO_BYTES,
    MAX_ZIP_DEPTH,
)
from bvphoenix.services.archive_guards import (
    is_safe_archive_member_name as _is_safe_archive_member_name,
)
from bvphoenix.services.arq_redis import redis_settings
from bvphoenix.services.iso_extractor import extract_iso
from bvphoenix.services.permissions import (
    DELETE,
    READ_METADATA,
    WRITE_REPORT,
    can_patient,
    effective_permissions_on_patient,
)
from bvphoenix.services.quota import check_quota_or_raise
from bvphoenix.services.rate_limit import BULK_UPLOAD_LIMIT, limiter
from bvphoenix.storage import get_s3_storage

JOB_KIND_BULK_UPLOAD = "bulk_upload"

router = APIRouter(tags=["bulk-upload"])


# Archive safety caps + zip-slip guard live in ``services/archive_guards``
# (shared with the review-queue staging checks). Symbols are re-imported
# above under their historical names so the unpackers and the existing
# tests keep working unchanged.


# ---- Internal types ----


class _SkippedAtStage(BaseModel):
    filename: str
    reason: str


@dataclass
class _VirtualFile:
    """A file pulled from the request body or an unpacked ZIP / ISO member.

    ``relative_path`` mirrors the browser-side path so DICOMDIR references
    (which are relative to the DICOMDIR file) can be matched back to the
    uploaded blobs by the worker.
    """

    relative_path: str
    filename: str
    data: bytes


@dataclass
class _Staging:
    files: list[_VirtualFile] = field(default_factory=list)
    skipped: list[_SkippedAtStage] = field(default_factory=list)
    zip_archives_found: int = 0


# ---- Helpers ----


# ``_resolve_owner_subject`` / ``_resolve_patient`` / ``_resolve_folder`` were
# lifted to ``api/_upload_common`` (imported above as the same private names)
# so the resumable session endpoints share one permission gate. Behaviour is
# unchanged.


def _unpack_zip(data: bytes, base_path: str, depth: int, staging: _Staging) -> None:
    """Recursively extract a ZIP blob. Files become ``_VirtualFile`` rows
    under ``<base_path>/<member_name>`` so DICOMDIR matching still works
    when an archive carries a full DICOM folder tree.
    """

    if depth > MAX_ZIP_DEPTH:
        staging.skipped.append(
            _SkippedAtStage(
                filename=base_path,
                reason=f"zip nesting exceeds depth {MAX_ZIP_DEPTH}",
            )
        )
        return
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, OSError) as exc:
        staging.skipped.append(_SkippedAtStage(filename=base_path, reason=f"corrupt zip: {exc}"))
        return

    staging.zip_archives_found += 1
    with zf:
        for member in zf.infolist():
            if member.is_dir():
                continue
            if not _is_safe_archive_member_name(member.filename):
                # Zip-slip defence: reject members whose name encodes a
                # traversal (``..``), an absolute path, or a NUL byte
                # before they propagate to ``relative_path`` / S3 keys
                # / DICOMDIR matching.
                staging.skipped.append(
                    _SkippedAtStage(
                        filename=member.filename,
                        reason="zip member name rejected (path traversal or absolute path)",
                    )
                )
                continue
            if member.file_size > MAX_FILE_BYTES:
                staging.skipped.append(
                    _SkippedAtStage(
                        filename=member.filename,
                        reason=f"file too large ({member.file_size} bytes)",
                    )
                )
                continue
            try:
                payload = zf.read(member)
            except (zipfile.BadZipFile, OSError, RuntimeError) as exc:
                staging.skipped.append(
                    _SkippedAtStage(
                        filename=member.filename,
                        reason=f"zip member unreadable: {exc}",
                    )
                )
                continue
            member_rel = f"{base_path}/{member.filename}".lstrip("/")
            inner_name = member.filename.rsplit("/", 1)[-1]
            if inner_name.lower().endswith(".zip"):
                _unpack_zip(payload, member_rel, depth + 1, staging)
                continue
            staging.files.append(
                _VirtualFile(
                    relative_path=member_rel.replace("\\", "/"),
                    filename=inner_name or member.filename,
                    data=payload,
                )
            )


def _unpack_iso_from_path(temp_path: str, base_path: str, staging: _Staging) -> None:
    """Walk an ISO already on disk and append its members to ``staging``.

    The caller is responsible for spooling the multipart upload to
    ``temp_path`` and for unlinking it afterwards — we do not delete
    here so the caller can also do its own ``upload_file`` to the
    object store on the same blob without paying for a second
    in-RAM copy. Each extracted member is appended as a
    ``_VirtualFile`` rooted under ``<base_path>/`` so the worker's
    DICOMDIR-aware ingestion still picks the right files (most
    clinical DVDs ship a ``DICOMDIR`` at the ISO root). Per-member
    size is bounded by ``MAX_FILE_BYTES`` just like the ZIP path.
    """
    stem = base_path
    if stem.lower().endswith(".iso"):
        stem = stem[: -len(".iso")]
    try:
        extracted = 0
        for rel, payload in extract_iso(temp_path, max_file_bytes=MAX_FILE_BYTES):
            if not _is_safe_archive_member_name(rel):
                # Symmetric zip-slip defence on the ISO walker: pycdlib
                # / 7z extractors should already normalise ISO 9660
                # entries to a flat tree, but defensive in depth — a
                # malformed UDF / hybrid filesystem could still emit
                # ``..`` components.
                staging.skipped.append(
                    _SkippedAtStage(
                        filename=rel,
                        reason="iso member name rejected (path traversal or absolute path)",
                    )
                )
                continue
            inner_name = rel.rsplit("/", 1)[-1]
            staging.files.append(
                _VirtualFile(
                    relative_path=f"{stem}/{rel}".lstrip("/"),
                    filename=inner_name or rel,
                    data=payload,
                )
            )
            extracted += 1
        if extracted == 0:
            # The extractor opened the ISO but the walk yielded
            # nothing. Most common cause: a pycdlib-readable
            # ISO with an empty root (vendor packaging error)
            # or a pure UDF that 7z also could not enumerate.
            # Surface the case so the user sees *why* their
            # upload produced no files instead of a silent drop.
            staging.skipped.append(
                _SkippedAtStage(
                    filename=base_path,
                    reason="iso extracted zero files (empty volume or unsupported format)",
                )
            )
    except Exception as exc:
        # failure as a skipped row so the rest of the batch still
        # commits. The ISO image itself is excluded from staging.
        staging.skipped.append(
            _SkippedAtStage(filename=base_path, reason=f"iso parse failed: {exc}")
        )


@dataclass
class _StagingResult:
    """Outcome of streaming the multipart payload directly to S3.

    ``manifest`` lists each blob that landed in the staging prefix
    along with the original filename and browser-side relative path.
    ``total_bytes`` is the running sum of payload sizes pushed to S3,
    used by the quota check downstream. ``skipped`` carries per-file
    reasons (oversize, corrupt zip member, empty file, etc.) the
    worker will fold into its post-ingest summary.

    ``iso_archives`` records every ``.iso`` file we received so the
    worker can persist the original CD/DVD image as a downloadable
    Document. The viewer is not certified for clinical
    reporting, so referring clinicians must always be able to grab
    the bit-identical ISO and read it on a vendor workstation.
    """

    manifest: list[dict] = field(default_factory=list)
    skipped: list[_SkippedAtStage] = field(default_factory=list)
    total_bytes: int = 0
    zip_archives_found: int = 0
    iso_archives: list[dict] = field(default_factory=list)


async def _stage_and_upload(
    files: list[UploadFile],
    relative_paths: list[str],
    *,
    storage,
    bucket: str,
    staging_prefix: str,
    keep_iso_archive: bool = True,
    wrap_iso_in_folder: bool = True,
    extract_iso_contents: bool = True,
) -> _StagingResult:
    """Stream each multipart file straight to S3, never accumulating.

    The previous ``_stage_uploads`` implementation buffered every byte
    of every file into a ``list[_VirtualFile]`` before issuing the S3
    PUTs. A 2.5k-file CD pulled ~1.3 GiB into memory and OOM-killed
    the backend pod (limit 2 Gi). Here we hold at most one file's
    payload at a time: read → PUT → ``del data`` → close the
    SpooledTemporaryFile that FastAPI created so its disk/memory
    footprint is reclaimed before the next iteration.

    ZIP / ISO archives still expand in memory (their unpackers want a
    full blob), but the per-archive caps (``MAX_FILE_BYTES`` /
    ``MAX_ISO_BYTES``) bound the worst case, and we drop the parent
    archive bytes before iterating the members.
    """

    out = _StagingResult()

    async def _put(vf: _VirtualFile, *, subfolder: str | None = None) -> None:
        idx = len(out.manifest)
        s3_key = f"{staging_prefix}{idx:06d}.bin"
        await asyncio.to_thread(
            storage.upload_bytes,
            vf.data,
            bucket=bucket,
            key=s3_key,
        )
        entry: dict = {
            "filename": vf.filename,
            "relative_path": vf.relative_path,
            "s3_key": s3_key,
        }
        # When set, the worker creates (or reuses) a sub-folder with
        # this name under the request's ``target_folder_id`` and
        # routes the file there. Used to keep an unpacked ISO's stray
        # README / TXT / autorun files together with its DICOM tree
        # instead of scattered in the parent folder.
        if subfolder:
            entry["subfolder_name"] = subfolder
        out.manifest.append(entry)
        out.total_bytes += len(vf.data)

    for idx, upload in enumerate(files):
        rel = (
            relative_paths[idx]
            if idx < len(relative_paths) and relative_paths[idx]
            else upload.filename or f"file-{idx}"
        )
        rel = rel.replace("\\", "/")
        name = upload.filename or rel.rsplit("/", 1)[-1] or f"file-{idx}"
        is_iso = name.lower().endswith(".iso")

        if is_iso:
            # ISOs go through a disk-spooled path: a 1.4 GiB DVD blob
            # straight into RAM via ``await upload.read()`` would push
            # the backend pod past its 2 Gi memory limit and surface
            # as 502 Bad Gateway when the kernel OOM-killer fires.
            # ``upload.file`` is the SpooledTemporaryFile FastAPI
            # already created — past its spool threshold it is on
            # disk anyway, so we just copy chunk-by-chunk into our
            # own tempfile (lifecycle controlled here) and then hand
            # that path to ``storage.upload_file`` (boto3 multipart)
            # and ``_unpack_iso_from_path`` (pycdlib reads from path).
            # Memory footprint stays bounded to one chunk size
            # (``8 MiB``) regardless of the ISO size.
            iso_tmp_dir = os.environ.get("BVP_UPLOAD_TMPDIR") or None
            fd, tmp_path_str = tempfile.mkstemp(suffix=".iso", dir=iso_tmp_dir)
            tmp_path = Path(tmp_path_str)

            # Bind ``fd`` and the SpooledTemporaryFile through default
            # args so the closure does not capture loop variables
            # (ruff B023) — we run the closure synchronously inside
            # ``asyncio.to_thread`` for *this* iteration before the
            # for-loop advances, but binding by default keeps the
            # contract explicit and ruff happy.
            def _spool_to_disk(_fd: int = fd, _src=upload.file) -> int:
                # Reset to the start of the upload spool because
                # Starlette has already advanced it during multipart
                # parsing.
                _src.seek(0)
                size = 0
                with os.fdopen(_fd, "wb") as out_f:
                    while True:
                        chunk = _src.read(8 * 1024 * 1024)
                        if not chunk:
                            break
                        out_f.write(chunk)
                        size += len(chunk)
                return size

            try:
                try:
                    iso_size = await asyncio.to_thread(_spool_to_disk)
                except Exception as exc:  # pragma: no cover — defensive
                    with contextlib.suppress(OSError, FileNotFoundError):
                        tmp_path.unlink()
                    out.skipped.append(_SkippedAtStage(filename=name, reason=f"read error: {exc}"))
                    continue
                finally:
                    with contextlib.suppress(Exception):
                        await upload.close()

                if iso_size == 0:
                    out.skipped.append(_SkippedAtStage(filename=name, reason="empty file"))
                    continue
                if iso_size > MAX_ISO_BYTES:
                    out.skipped.append(
                        _SkippedAtStage(
                            filename=name,
                            reason=f"file too large ({iso_size} bytes)",
                        )
                    )
                    continue

                # Optionally preserve the ISO bit-for-bit on object
                # storage. The worker turns each saved archive into a
                # downloadable Document so a referring clinician can
                # always grab the exact CD/DVD image.
                # ``keep_iso_archive=False`` skips the extra storage
                # when the operator only cares about the unpacked
                # content.
                if keep_iso_archive:
                    iso_idx = len(out.iso_archives)
                    iso_key = f"{staging_prefix}_iso/{iso_idx:03d}_{name}"
                    await asyncio.to_thread(
                        storage.upload_file,
                        tmp_path,
                        bucket=bucket,
                        key=iso_key,
                    )
                    out.iso_archives.append(
                        {
                            "filename": name,
                            "relative_path": rel,
                            "s3_key": iso_key,
                            "size_bytes": iso_size,
                        }
                    )
                    out.total_bytes += iso_size

                # ``extract_iso_contents=False`` lets the operator
                # keep the ISO blob *only* — the worker will register
                # the archive as a Document but skip the DICOMDIR /
                # DICOM ingestion step.
                if not extract_iso_contents:
                    continue

                # Group every member of the ISO into a sub-folder
                # named after the ISO stem so stray autorun.inf /
                # README.TXT / vendor viewer payloads don't bleed into
                # the parent folder.
                iso_stem = name
                if iso_stem.lower().endswith(".iso"):
                    iso_stem = iso_stem[: -len(".iso")]
                subfolder = iso_stem if wrap_iso_in_folder else None

                sub = _Staging()
                await asyncio.to_thread(_unpack_iso_from_path, str(tmp_path), rel, sub)
                out.skipped.extend(sub.skipped)
                for vf in sub.files:
                    await _put(vf, subfolder=subfolder)
                    vf.data = b""
            finally:
                with contextlib.suppress(OSError, FileNotFoundError):
                    tmp_path.unlink()
            continue

        # Non-ISO path: bounded by MAX_FILE_BYTES (500 MiB), small
        # enough to read into RAM without OOM risk.
        try:
            data = await upload.read()
        except Exception as exc:  # pragma: no cover — defensive
            out.skipped.append(_SkippedAtStage(filename=name, reason=f"read error: {exc}"))
            continue
        finally:
            # Release the SpooledTemporaryFile FastAPI allocated for
            # this upload — otherwise the in-memory or on-disk buffer
            # lingers until the request ends.
            with contextlib.suppress(Exception):
                await upload.close()

        if not data:
            out.skipped.append(_SkippedAtStage(filename=name, reason="empty file"))
            continue
        if len(data) > MAX_FILE_BYTES:
            out.skipped.append(
                _SkippedAtStage(filename=name, reason=f"file too large ({len(data)} bytes)")
            )
            continue

        if name.lower().endswith(".zip"):
            sub = _Staging()
            _unpack_zip(data, rel, depth=1, staging=sub)
            del data
            out.zip_archives_found += sub.zip_archives_found
            out.skipped.extend(sub.skipped)
            for vf in sub.files:
                await _put(vf)
                vf.data = b""
            continue

        await _put(_VirtualFile(relative_path=rel, filename=name, data=data))
        del data

    return out


# ---- Endpoint ----


@router.post(
    "/upload/bulk",
    response_model=JobOut,
    status_code=status.HTTP_202_ACCEPTED,
)
@limiter.limit(BULK_UPLOAD_LIMIT)
async def bulk_upload(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    files: list[UploadFile] = File(...),
    relative_paths: list[str] | None = Form(None),
    patient_id: uuid.UUID | None = Form(None),
    target_folder_id: uuid.UUID | None = Form(None),
    tier: str = Form("t1"),
    keep_iso_archive: bool = Form(
        True,
        description=(
            "When true (default) the original CD/DVD .iso file is "
            "preserved on storage and surfaced in the fascicolo as a "
            "downloadable Document so a referring clinician "
            "can read it on a certified workstation. Set false to "
            "save storage when the unpacked content is sufficient."
        ),
    ),
    wrap_iso_in_folder: bool = Form(
        True,
        description=(
            "When true (default) every ISO's unpacked content lands "
            "in a sub-folder named after the ISO stem under the "
            "target folder, so vendor README/autorun files don't "
            "scatter across the parent folder."
        ),
    ),
    extract_iso_contents: bool = Form(
        True,
        description=(
            "When true (default) the worker unpacks the ISO and "
            "ingests every member file (DICOMDIR, DICOM, viewer "
            "payloads). Set false to keep the ISO as an archival blob "
            "ONLY and skip extraction — useful when the study has "
            "already been imported via cleaned-up sidecar files in a "
            "previous session and the operator now just wants to "
            "attach the original CD/DVD image. Combined with "
            "``keep_iso_archive=true``, the upload yields exactly one "
            "downloadable Document per ISO and zero new study/"
            "series rows."
        ),
    ),
) -> JobOut:
    """Accept a bulk upload, stage to S3, enqueue a worker.

    Returns ``202 Accepted`` with a :class:`JobOut` carrying the new
    Job's id. The client polls ``GET /api/jobs/{id}`` for progress +
    the final summary (``JobOut.result``), and can cancel mid-flight
    via ``DELETE /api/jobs/{id}``.
    """

    if not files:
        raise HTTPException(status_code=400, detail="no files provided")
    if tier not in ("t1", "t2", "t3", "t4"):
        raise HTTPException(status_code=400, detail="invalid tier")

    owner = await _resolve_owner_subject(db, user)
    patient = await _resolve_patient(db, user, patient_id, request)
    folder = await _resolve_folder(db, user, target_folder_id)

    settings = get_settings()
    storage = get_s3_storage()
    storage.ensure_bucket(settings.s3_bucket_raw)

    # Generate a staging ID up front so manifest entries can reference
    # the keys. We don't yet have the Job UUID (that comes from
    # enqueue_or_get) — staging_id keeps the staging prefix unique
    # across concurrent uploads.
    staging_id = uuid.uuid4()
    staging_prefix = f"_ingest_jobs/{staging_id}/"

    # Stream every uploaded file directly to S3 instead of buffering
    # the whole multipart body in RAM. A 2.5k-file CD ingest would
    # otherwise OOM-kill the backend pod (limit 2 Gi). Per-blob
    # release inside ``_stage_and_upload`` keeps the working set to
    # one file at a time.
    staged = await _stage_and_upload(
        files,
        relative_paths or [],
        storage=storage,
        bucket=settings.s3_bucket_raw,
        staging_prefix=staging_prefix,
        keep_iso_archive=keep_iso_archive,
        wrap_iso_in_folder=wrap_iso_in_folder,
        extract_iso_contents=extract_iso_contents,
    )
    if not staged.manifest and not staged.iso_archives:
        # Every uploaded entry was skipped (empty, oversize, corrupt
        # ZIP, etc). Surface the per-file reasons immediately rather
        # than create an empty Job.
        raise HTTPException(
            status_code=400,
            detail={
                "error": "no valid files",
                "skipped": [{"filename": s.filename, "reason": s.reason} for s in staged.skipped],
            },
        )

    # F11.3: enforce the 10 GiB free-tier cap on T1/T2 against the
    # actual bytes pushed to S3. The check runs *after* staging
    # because the streaming path doesn't know the total upfront —
    # if it trips, sweep the staged keys so we don't leak storage.
    try:
        await check_quota_or_raise(
            db,
            user_subject_id=owner.id,
            tier=tier,
            incoming_bytes=staged.total_bytes,
        )
        # Per-subject hard storage cap (5 GB default, admin-overridable).
        # Run AFTER the OpenData tier check so the user gets the more
        # specific error first when both fire.
        from bvphoenix.services.storage_quota import check_storage_quota

        await check_storage_quota(db, subject_id=owner.id, additional_bytes=staged.total_bytes)
    except Exception:
        for entry in staged.manifest:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(
                    storage.delete_object,
                    bucket=settings.s3_bucket_raw,
                    key=entry["s3_key"],
                )
        raise

    # Surface staging-time skips in the canonical_input so the worker
    # can fold them into the final result. The manifest itself doesn't
    # mention them — they never made it to S3.
    canonical_input: dict = {
        "manifest": staged.manifest,
        "owner_subject_id": str(owner.id),
        "patient_id": str(patient.id) if patient else None,
        "folder_id": str(folder.id) if folder else None,
        "tier": tier,
        "staging_prefix": staging_prefix,
        "stage_skipped": [{"filename": s.filename, "reason": s.reason} for s in staged.skipped],
        "zip_archives_found": staged.zip_archives_found,
        # Worker materialises one Document per entry so the
        # original CD/DVD image stays downloadable from the fascicolo
        # — required because the in-app viewer is not certified for
        # clinical reporting and the referring clinician must always
        # be able to grab the bit-identical archive.
        "iso_archives": staged.iso_archives,
        # Tell the worker whether to land each archive's Document in
        # the same subfolder as its unpacked content (true) or flat in
        # the request's target folder (false). Mirrors the staging
        # decision so the fascicolo shows one self-contained card per
        # CD/DVD instead of the bundle stranded in root next to its
        # extracted files.
        "wrap_iso_in_folder": wrap_iso_in_folder,
    }

    try:
        result = await jobs_service.enqueue_or_get(
            db,
            kind=JOB_KIND_BULK_UPLOAD,
            owner_subject_id=user.subject_id,
            canonical_input=canonical_input,
            scope_ids=(patient.id,) if patient else (),
            is_admin=bool(user.is_admin),
        )
    except jobs_service.JobCapExceededError as exc:
        # Stage already done; the buckets carry leftover staged files.
        # The bucket lifecycle policy on ``_ingest_jobs/`` (cleanup
        # CronJob in workers) will sweep them. Not worth a sync delete
        # here that could compound the failure.
        raise cap_exceeded_to_http(exc) from exc

    if not result.deduped:
        try:
            redis = await create_pool(redis_settings(settings.redis_url))
            arq_handle = await redis.enqueue_job("ingest_bulk_files", str(result.job.id))
            await redis.close()
            if arq_handle is not None:
                await jobs_service.set_arq_job_id(db, result.job.id, arq_handle.job_id)
        except Exception as exc:
            await jobs_service.mark_failed(
                db,
                result.job.id,
                error={"code": "enqueue_failed", "message": str(exc)},
            )
            await db.commit()
            raise HTTPException(
                status_code=503,
                detail={
                    "error": "enqueue_failed",
                    "hint": "worker queue unavailable; try again shortly",
                },
            ) from exc

    await db.commit()
    # Repopulate the ORM instance — the commit above expired its
    # attributes, and ``JobOut.model_validate`` would otherwise
    # trigger async lazy-loads from inside Pydantic (no greenlet
    # context → MissingGreenlet). ``db.get`` would just return the
    # same expired instance from the identity map, so go through
    # ``refresh`` which forces an actual reload.
    await db.refresh(result.job)
    return _job_to_out(result.job)
