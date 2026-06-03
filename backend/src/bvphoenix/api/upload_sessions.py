"""Resumable upload session API (DESIGN.md §11.6).

Five endpoints that make a bulk upload recoverable from byte zero, replacing
the single non-resumable ``POST /api/upload/bulk`` (which stays as the legacy
path during the transition):

    POST   /api/upload/sessions                 create (no bytes)  -> SessionOut
    PATCH  /api/upload/sessions/{sid}/files/{i} append one chunk   -> FileStateOut
    GET    /api/upload/sessions/{sid}           read offsets (resume) -> SessionOut
    POST   /api/upload/sessions/{sid}/commit    hand off to worker -> JobOut
    DELETE /api/upload/sessions/{sid}           abort + clean up   -> 204

The client persists ``session_id`` locally BEFORE the first chunk, uploads
fixed ``CHUNK_SIZE`` chunks carrying ``Upload-Offset``, and on reconnect GETs
the session to read the authoritative per-file ``received_offset`` and resumes
from there. Commit reuses the unchanged jobs/ingest pipeline. All bytes flow
through the backend (storage isolation); the client never sees S3 keys.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from arq import create_pool
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.api._upload_common import resolve_folder, resolve_owner_subject, resolve_patient
from bvphoenix.api.jobs import JobOut, _job_to_out
from bvphoenix.auth import require_user
from bvphoenix.config import get_settings
from bvphoenix.db.models import User
from bvphoenix.db.session import get_db
from bvphoenix.services import jobs as jobs_service
from bvphoenix.services import upload_sessions as svc
from bvphoenix.services.arq_redis import redis_settings
from bvphoenix.storage import get_s3_storage

router = APIRouter(tags=["upload-sessions"])


# ---- Schemas ----


class FileDeclIn(BaseModel):
    filename: str = Field(max_length=1024)
    relative_path: str | None = Field(default=None, max_length=4096)
    size: int = Field(ge=0)
    # Optional client-computed content hash; verified server-side later.
    sha256: str | None = Field(default=None, max_length=64)


class CreateSessionIn(BaseModel):
    files: list[FileDeclIn] = Field(min_length=1)
    tier: str = "t1"
    patient_id: uuid.UUID | None = None
    folder_id: uuid.UUID | None = None
    keep_iso_archive: bool = True
    wrap_iso_in_folder: bool = True
    extract_iso_contents: bool = True


class FileStateOut(BaseModel):
    file_index: int
    filename: str
    relative_path: str | None
    declared_size: int
    received_offset: int
    status: str


class SessionOut(BaseModel):
    id: str
    status: str
    chunk_size: int
    declared_total_bytes: int
    received_total_bytes: int
    job_id: str | None
    files: list[FileStateOut]


def _session_out(session: svc.UploadSession, files: list[svc.UploadSessionFile]) -> SessionOut:
    return SessionOut(
        id=str(session.id),
        status=session.status,
        chunk_size=svc.CHUNK_SIZE,
        declared_total_bytes=session.declared_total_bytes,
        received_total_bytes=session.received_total_bytes,
        job_id=str(session.job_id) if session.job_id else None,
        files=[
            FileStateOut(
                file_index=f.file_index,
                filename=f.filename,
                relative_path=f.relative_path,
                declared_size=f.declared_size,
                received_offset=f.received_offset,
                status=f.status,
            )
            for f in files
        ],
    )


# ---- Endpoints ----


@router.post("/upload/sessions", response_model=SessionOut, status_code=status.HTTP_201_CREATED)
async def create_upload_session(
    body: CreateSessionIn,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
) -> SessionOut:
    """Open a durable upload session BEFORE any bytes. The same owner / patient
    / folder resolution + WRITE gate as the legacy endpoint."""
    owner = await resolve_owner_subject(db, user)
    patient = await resolve_patient(db, user, body.patient_id, request)
    folder = await resolve_folder(db, user, body.folder_id)

    session = await svc.create_session(
        db,
        owner=owner,
        patient=patient,
        folder=folder,
        tier=body.tier,
        files=[
            svc.FileDecl(
                filename=f.filename,
                relative_path=f.relative_path,
                size=f.size,
                sha256=f.sha256,
            )
            for f in body.files
        ],
        keep_iso_archive=body.keep_iso_archive,
        wrap_iso_in_folder=body.wrap_iso_in_folder,
        extract_iso_contents=body.extract_iso_contents,
    )
    files = await svc.list_session_files(db, session_id=session.id)
    return _session_out(session, files)


@router.patch("/upload/sessions/{session_id}/files/{file_index}", response_model=FileStateOut)
async def append_session_chunk(
    session_id: uuid.UUID,
    file_index: int,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    upload_offset: Annotated[int, Header(alias="Upload-Offset", ge=0)],
) -> FileStateOut:
    """Append one chunk at ``Upload-Offset`` to a file. The raw request body is
    the chunk. Idempotent on the offset; a gap returns 409 with the expected
    offset so the client resyncs."""
    session = await svc.get_session_for_owner(
        db, session_id=session_id, owner_subject_id=user.subject_id
    )
    chunk = await request.body()
    storage = get_s3_storage()
    settings = get_settings()
    f = await svc.append_chunk(
        db,
        storage,
        bucket=settings.s3_bucket_raw,
        session=session,
        file_index=file_index,
        offset=upload_offset,
        body=chunk,
    )
    return FileStateOut(
        file_index=f.file_index,
        filename=f.filename,
        relative_path=f.relative_path,
        declared_size=f.declared_size,
        received_offset=f.received_offset,
        status=f.status,
    )


@router.get("/upload/sessions/{session_id}", response_model=SessionOut)
async def get_upload_session(
    session_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
) -> SessionOut:
    """Read the session + per-file authoritative offsets so the client can
    resume after a disconnect / tab reopen."""
    session = await svc.get_session_for_owner(
        db, session_id=session_id, owner_subject_id=user.subject_id
    )
    files = await svc.list_session_files(db, session_id=session.id)
    return _session_out(session, files)


@router.post("/upload/sessions/{session_id}/commit", response_model=JobOut, status_code=202)
async def commit_upload_session(
    session_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
) -> JobOut:
    """Hand a fully-staged session off to the ingest worker. Idempotent: a
    re-commit returns the already-linked job (no second ingest)."""
    session = await svc.get_session_for_owner(
        db, session_id=session_id, owner_subject_id=user.subject_id
    )
    result = await svc.commit_session(db, session=session, is_admin=bool(user.is_admin))

    # Mirror the legacy enqueue tail: only fire arq for a freshly-created job;
    # a deduped/idempotent re-commit already has its worker running.
    if not result.deduped:
        try:
            settings = get_settings()
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

    await db.refresh(result.job)
    return _job_to_out(result.job)


@router.delete("/upload/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def abort_upload_session(
    session_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
) -> Response:
    """Abort an in-flight session: drop every open S3 multipart upload + delete
    staged objects. A committed session cannot be aborted (the job owns it)."""
    session = await svc.get_session_for_owner(
        db, session_id=session_id, owner_subject_id=user.subject_id
    )
    if session.status == "committed":
        raise HTTPException(
            status_code=409,
            detail={"code": "session_already_committed", "job_id": str(session.job_id)},
        )
    storage = get_s3_storage()
    settings = get_settings()
    await svc.abort_session(db, storage, bucket=settings.s3_bucket_raw, session=session)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
