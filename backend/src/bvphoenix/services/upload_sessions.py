"""Resumable upload session service (DESIGN.md §11.6).

The durable, resumable backend behind ``/api/upload/sessions``. A bulk upload
becomes a three-phase session whose handle exists from byte zero:

1. ``create_session`` — persist an ``upload_sessions`` row (status
   ``awaiting_bytes``) + one ``upload_session_files`` row per declared file,
   BEFORE any bytes. The handle now survives every disconnect.
2. ``append_chunk`` — per file, each call uploads one fixed-size chunk as one
   S3 multipart upload-part. Idempotent on the byte offset: a re-sent chunk at
   an already-acked offset is a no-op returning the current offset; a gap is a
   409 so the client re-syncs from the authoritative ``received_offset``. When
   a file's offset reaches its declared size the multipart upload is completed
   and the file flips to ``staged``.
3. ``commit_session`` — once every file is staged, build the SAME
   ``canonical_input`` manifest the legacy ``/api/upload/bulk`` builds (pointing
   at the staged keys) and hand off to the UNCHANGED
   ``jobs_service.enqueue_or_get`` + ``ingest_bulk_files`` worker. Idempotent:
   a re-commit returns the already-linked job.

``abort_session`` (and the cleanup sweeper) abort every open multipart upload
and delete staged objects so an abandoned session leaks nothing.

All bytes flow THROUGH the backend — no presigned PUT (storage isolation). The
client only ever sees ``session_id``, ``file_index`` and offsets; the S3 key /
upload-id stay server-side. Phase 1 stages each uploaded file as-is; ISOs are
expanded client-side before upload and ZIP server-side unpack-at-commit is a
tracked follow-up (the legacy endpoint stays for ZIP-heavy uploads meanwhile).
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import HTTPException
from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.db.models import Folder, Job, Patient, Subject, UploadSession, UploadSessionFile
from bvphoenix.db.models.upload_sessions import UPLOAD_SESSION_ACTIVE_STATUSES
from bvphoenix.services import jobs as jobs_service
from bvphoenix.storage.s3 import S3Storage

# Fixed client chunk = one S3 part. ≥ 5 MiB (S3 minimum, last part exempt) and
# a power-of-two so PartNumber = offset // CHUNK_SIZE + 1 is exact.
CHUNK_SIZE: int = 8 * 1024 * 1024
# Mirrors the legacy per-file cap (bulk_upload.MAX_FILE_BYTES).
MAX_UPLOAD_FILE_BYTES: int = 500 * 1024 * 1024
MAX_SESSION_FILES: int = 50_000
# Hard sweep deadline; a non-committed session past this is reaped even if it
# never went stale. The sweeper also reaps on shorter updated_at staleness.
SESSION_TTL_HOURS: int = 24
# Parallel-session cap per owner (cheap guard against a client opening
# thousands of awaiting_bytes sessions to exhaust rows / S3 multipart slots).
MAX_ACTIVE_SESSIONS_PER_OWNER: int = 25

JOB_KIND_BULK_UPLOAD = "bulk_upload"


def _staging_prefix(session_id: uuid.UUID) -> str:
    return f"_ingest_jobs/{session_id}/"


def _file_key(session_id: uuid.UUID, file_index: int) -> str:
    return f"{_staging_prefix(session_id)}{file_index}.bin"


class FileDecl:
    """One declared file in a create-session manifest (validated by the API
    schema before it reaches here)."""

    __slots__ = ("filename", "relative_path", "sha256", "size")

    def __init__(
        self, *, filename: str, relative_path: str | None, size: int, sha256: str | None
    ) -> None:
        self.filename = filename
        self.relative_path = relative_path
        self.size = size
        self.sha256 = sha256


async def _active_session_count(db: AsyncSession, owner_subject_id: uuid.UUID) -> int:
    return int(
        (
            await db.execute(
                select(func.count())
                .select_from(UploadSession)
                .where(
                    UploadSession.owner_subject_id == owner_subject_id,
                    UploadSession.status.in_(tuple(UPLOAD_SESSION_ACTIVE_STATUSES)),
                )
            )
        ).scalar_one()
    )


async def create_session(
    db: AsyncSession,
    *,
    owner: Subject,
    patient: Patient | None,
    folder: Folder | None,
    tier: str,
    files: list[FileDecl],
    keep_iso_archive: bool = True,
    wrap_iso_in_folder: bool = True,
    extract_iso_contents: bool = True,
) -> UploadSession:
    """Create a durable upload session + its per-file rows (no bytes yet)."""
    if not files:
        raise HTTPException(status_code=400, detail="no files declared")
    if len(files) > MAX_SESSION_FILES:
        raise HTTPException(status_code=400, detail=f"too many files (max {MAX_SESSION_FILES})")
    if tier not in ("t1", "t2", "t3", "t4"):
        raise HTTPException(status_code=400, detail="invalid tier")
    for f in files:
        if f.size < 0:
            raise HTTPException(status_code=400, detail=f"negative size for {f.filename!r}")
        if f.size > MAX_UPLOAD_FILE_BYTES:
            raise HTTPException(
                status_code=413,
                detail={
                    "code": "file_too_large",
                    "filename": f.filename,
                    "size": f.size,
                    "max": MAX_UPLOAD_FILE_BYTES,
                },
            )

    if await _active_session_count(db, owner.id) >= MAX_ACTIVE_SESSIONS_PER_OWNER:
        raise HTTPException(
            status_code=429,
            detail={
                "code": "too_many_upload_sessions",
                "max": MAX_ACTIVE_SESSIONS_PER_OWNER,
                "hint": "finish or cancel an in-progress upload first",
            },
        )

    declared_total = sum(f.size for f in files)
    session = UploadSession(
        owner_subject_id=owner.id,
        patient_id=patient.id if patient else None,
        folder_id=folder.id if folder else None,
        tier=tier,
        keep_iso_archive=keep_iso_archive,
        wrap_iso_in_folder=wrap_iso_in_folder,
        extract_iso_contents=extract_iso_contents,
        status="awaiting_bytes",
        declared_total_bytes=declared_total,
        received_total_bytes=0,
        scope_ids=[patient.id] if patient else None,
        expires_at=datetime.now(UTC) + timedelta(hours=SESSION_TTL_HOURS),
    )
    db.add(session)
    await db.flush()  # assign session.id for the file keys

    for idx, f in enumerate(files):
        db.add(
            UploadSessionFile(
                session_id=session.id,
                file_index=idx,
                filename=f.filename,
                relative_path=f.relative_path,
                declared_size=f.size,
                declared_sha256=f.sha256,
                s3_key=_file_key(session.id, idx),
                # A zero-byte file needs no bytes — mark it staged up front so
                # it never blocks commit; it is excluded from the manifest (the
                # legacy path skips empty files too).
                status="staged" if f.size == 0 else "pending",
                received_offset=0,
                parts=[],
            )
        )
    await db.commit()
    await db.refresh(session)
    return session


async def get_session_for_owner(
    db: AsyncSession, *, session_id: uuid.UUID, owner_subject_id: uuid.UUID
) -> UploadSession:
    session = await db.get(UploadSession, session_id)
    if session is None or session.owner_subject_id != owner_subject_id:
        raise HTTPException(status_code=404, detail="upload session not found")
    return session


async def list_session_files(db: AsyncSession, *, session_id: uuid.UUID) -> list[UploadSessionFile]:
    rows = (
        await db.execute(
            select(UploadSessionFile)
            .where(UploadSessionFile.session_id == session_id)
            .order_by(UploadSessionFile.file_index)
        )
    ).scalars()
    return list(rows)


async def _get_file(
    db: AsyncSession, *, session_id: uuid.UUID, file_index: int
) -> UploadSessionFile:
    row = (
        await db.execute(
            select(UploadSessionFile).where(
                UploadSessionFile.session_id == session_id,
                UploadSessionFile.file_index == file_index,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="upload session file not found")
    return row


async def append_chunk(
    db: AsyncSession,
    storage: S3Storage,
    *,
    bucket: str,
    session: UploadSession,
    file_index: int,
    offset: int,
    body: bytes,
) -> UploadSessionFile:
    """Append one chunk at ``offset`` to a file's S3 multipart upload.

    Idempotent on ``offset``: a chunk whose offset is behind ``received_offset``
    was already acked → no-op (returns the file unchanged). A chunk ahead of
    ``received_offset`` is a gap → 409 carrying the authoritative offset so the
    client resyncs. The expected chunk (offset == received_offset) is uploaded
    as one part; when the file reaches its declared size the multipart upload
    is completed and the file flips to ``staged``.
    """
    if session.status not in ("awaiting_bytes", "uploading"):
        raise HTTPException(
            status_code=409,
            detail={"code": "session_not_accepting_bytes", "status": session.status},
        )
    f = await _get_file(db, session_id=session.id, file_index=file_index)
    if f.status == "staged":
        # Whole file already received (e.g. a duplicated final chunk).
        return f

    if offset < f.received_offset:
        # Already-acked prefix re-sent — idempotent no-op.
        return f
    if offset > f.received_offset:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "offset_mismatch",
                "expected_offset": f.received_offset,
                "received_offset_header": offset,
            },
        )

    end = offset + len(body)
    if end > f.declared_size:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "chunk_overflows_declared_size",
                "declared_size": f.declared_size,
                "would_reach": end,
            },
        )
    is_final = end == f.declared_size
    # Every part except the final one must be exactly CHUNK_SIZE so the
    # offset→PartNumber mapping is exact and the part is a legal ≥5 MiB part.
    if not is_final and len(body) != CHUNK_SIZE:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "bad_chunk_size",
                "expected": CHUNK_SIZE,
                "got": len(body),
                "hint": "every chunk except the last must be exactly CHUNK_SIZE bytes",
            },
        )

    # Lazily open the multipart upload on the first chunk.
    if f.s3_upload_id is None:
        upload_id = await asyncio.to_thread(storage.create_multipart, bucket=bucket, key=f.s3_key)
        f.s3_upload_id = upload_id

    part_number = offset // CHUNK_SIZE + 1
    etag = await asyncio.to_thread(
        storage.upload_part,
        bucket=bucket,
        key=f.s3_key,
        upload_id=f.s3_upload_id,
        part_number=part_number,
        body=body,
    )
    # Persist the part + advance the authoritative offset. parts is JSONB; copy
    # so SQLAlchemy detects the mutation.
    f.parts = [*f.parts, {"PartNumber": part_number, "ETag": etag, "size": len(body)}]
    f.received_offset = end
    f.status = "uploading"
    session.received_total_bytes = session.received_total_bytes + len(body)
    if session.status == "awaiting_bytes":
        session.status = "uploading"

    if is_final:
        await asyncio.to_thread(
            storage.complete_multipart,
            bucket=bucket,
            key=f.s3_key,
            upload_id=f.s3_upload_id,
            parts=f.parts,
        )
        f.status = "staged"
        # If every file is now staged, the session is ready to commit.
        remaining = (
            await db.execute(
                select(func.count())
                .select_from(UploadSessionFile)
                .where(
                    UploadSessionFile.session_id == session.id,
                    UploadSessionFile.status != "staged",
                    UploadSessionFile.id != f.id,
                )
            )
        ).scalar_one()
        if int(remaining) == 0:
            session.status = "ready"

    await db.commit()
    await db.refresh(f)
    return f


def _build_canonical_input(
    session: UploadSession, files: list[UploadSessionFile]
) -> dict[str, Any]:
    """Build the SAME canonical_input shape the legacy endpoint builds, so the
    unchanged ``ingest_bulk_files`` worker consumes it identically."""
    manifest = [
        {
            "filename": f.filename,
            "relative_path": f.relative_path,
            "s3_key": f.s3_key,
        }
        for f in files
        if f.declared_size > 0  # zero-byte files were skipped at create
    ]
    return {
        "manifest": manifest,
        "owner_subject_id": str(session.owner_subject_id),
        "patient_id": str(session.patient_id) if session.patient_id else None,
        "folder_id": str(session.folder_id) if session.folder_id else None,
        "tier": session.tier,
        "staging_prefix": _staging_prefix(session.id),
        "stage_skipped": [],
        "zip_archives_found": 0,
        "iso_archives": [],
        "wrap_iso_in_folder": session.wrap_iso_in_folder,
    }


async def commit_session(
    db: AsyncSession,
    *,
    session: UploadSession,
    is_admin: bool,
) -> jobs_service.EnqueueResult:
    """Hand a fully-staged session off to the ingest worker. Idempotent: a
    re-commit of an already-committed session returns its linked job."""
    if session.status == "committed" and session.job_id is not None:
        job = await db.get(Job, session.job_id)
        if job is not None:
            return jobs_service.EnqueueResult(job=job, deduped=True)

    files = await list_session_files(db, session_id=session.id)
    not_staged = [f.file_index for f in files if f.status != "staged"]
    if not_staged:
        raise HTTPException(
            status_code=409,
            detail={"code": "files_not_staged", "pending_file_indexes": not_staged[:50]},
        )
    if all(f.declared_size == 0 for f in files):
        raise HTTPException(status_code=400, detail={"code": "no_non_empty_files"})

    result = await jobs_service.enqueue_or_get(
        db,
        kind=JOB_KIND_BULK_UPLOAD,
        owner_subject_id=session.owner_subject_id,
        canonical_input=_build_canonical_input(session, files),
        scope_ids=(session.patient_id,) if session.patient_id else (),
        is_admin=is_admin,
    )
    session.job_id = result.job.id
    session.status = "committed"
    await db.commit()
    return result


async def abort_session(
    db: AsyncSession, storage: S3Storage, *, bucket: str, session: UploadSession
) -> None:
    """Abort every open multipart upload + delete staged objects, then flip the
    session to ``aborted``. Used by DELETE and the cleanup sweeper. Best-effort
    on the S3 side: a single failed abort must not strand the whole sweep."""
    files = await list_session_files(db, session_id=session.id)
    for f in files:
        if f.s3_upload_id and f.status != "staged":
            try:
                await asyncio.to_thread(
                    storage.abort_multipart,
                    bucket=bucket,
                    key=f.s3_key,
                    upload_id=f.s3_upload_id,
                )
            except Exception:
                pass
        elif f.status == "staged":
            try:
                await asyncio.to_thread(storage.delete_object, bucket=bucket, key=f.s3_key)
            except Exception:
                pass
    session.status = "aborted"
    await db.commit()


# --- GC: stale-session sweep (worker cron, DESIGN.md §11.6) -----------------
# A non-committed session past STALE_WINDOW on updated_at (or past expires_at)
# is abandoned. append_chunk mutates the session row (received_total_bytes /
# status), refreshing updated_at, so an actively-progressing upload never looks
# stale — only genuinely idle sessions are reaped. 1h is generous (a user may
# pause mid-upload) vs the jobs reaper's 5 min.
STALE_WINDOW = timedelta(hours=1)


async def stale_sessions(db: AsyncSession, *, batch_size: int = 200) -> list[UploadSession]:
    """Non-committed sessions that are abandoned (idle past STALE_WINDOW or past
    their hard expires_at deadline). Thresholds are computed in Python and
    bound, avoiding SQL interval rendering. ix_upload_sessions_expires covers
    the scan."""
    now = datetime.now(UTC)
    stale_before = now - STALE_WINDOW
    rows = (
        await db.execute(
            select(UploadSession)
            .where(
                UploadSession.status.in_(tuple(UPLOAD_SESSION_ACTIVE_STATUSES)),
                or_(UploadSession.expires_at < now, UploadSession.updated_at < stale_before),
            )
            .order_by(UploadSession.expires_at.asc())
            .limit(batch_size)
        )
    ).scalars()
    return list(rows)


async def delete_sessions(db: AsyncSession, session_ids: list[uuid.UUID]) -> int:
    """Bulk-delete session rows by id (ON DELETE CASCADE drops their files).
    Call AFTER abort_session has released the S3 multipart uploads."""
    if not session_ids:
        return 0
    res = await db.execute(delete(UploadSession).where(UploadSession.id.in_(session_ids)))
    return int(res.rowcount or 0)
