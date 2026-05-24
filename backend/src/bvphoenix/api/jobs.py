"""Generic Jobs API (DESIGN.md §11.7).

Exposes the lifecycle that consumers share once they have enqueued a
``Job``: status snapshot, active-jobs list, cancellation. Enqueue
itself is intentionally *not* generic here; each consumer (Fascicolo
export, GDPR export, ...) owns a domain-specific POST that performs
its own permission check and then delegates to
:func:`bvphoenix.services.jobs.enqueue_or_get`. A generic POST would
need a per-kind permission registry that we don't have a use case
for yet.

Authorization model: the owner sees their own jobs; admins see any
job. A non-owner asking for a job they don't own gets 404, not 403,
to avoid leaking the existence of someone else's work.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.api._http import proxy_s3_object
from bvphoenix.auth import require_user
from bvphoenix.auth.deps import public_user
from bvphoenix.db.models import User
from bvphoenix.db.models.jobs import JOB_ACTIVE_STATUSES, Job
from bvphoenix.db.session import get_db
from bvphoenix.services import jobs as jobs_service
from bvphoenix.services.download_tokens import resolve_download_user
from bvphoenix.storage import get_s3_storage

router = APIRouter(prefix="/jobs", tags=["jobs"])


# Result download URLs are now backend-relative. The bytes are streamed
# through this process at GET time (storage isolation, memoria
# ``feedback_storage_isolation``); we no longer hand the caller a
# presigned object-storage URL. The ``_DOWNLOAD_URL_TTL_SECONDS``
# constant became dead with the switch and was removed.


class JobOut(BaseModel):
    """Status snapshot returned by GET endpoints. Mirrors the row,
    minus the canonical input (which can be large and is not useful
    on the read path).

    ``result_download_url`` is a freshly-signed presigned GET URL
    derived from ``result_uri`` when the latter starts with ``s3://``.
    It is *not* persisted; it is recomputed on every read so the link
    cannot outlive the parent Job's ``expires_at``.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kind: str
    owner_subject_id: uuid.UUID
    status: str
    progress_total: int | None
    progress_done: int
    stage: str | None
    result_uri: str | None
    result_download_url: str | None = None
    # Optional structured result payload — workers stash it in
    # ``Job.input["result"]`` since the model has no dedicated
    # ``result`` JSONB column. ``_job_to_out`` lifts it here so the
    # API caller doesn't see ``input`` (which can be large and may
    # contain credentials-adjacent staging metadata).
    result: dict[str, Any] | None = None
    # Human-readable label set by the enqueue endpoint
    # (``"TC torace 2024-12-01"``, ``"Fascicolo: Mamma"``, …). The FE
    # uses this in the JobsTray so two parallel ``study_export`` rows
    # are visually distinguishable instead of "Esporta studio DICOM"
    # repeated. Stored under ``Job.input["_display_label"]`` (the
    # underscore prefix excludes it from the dedup hash).
    display_label: str | None = None
    error: dict[str, Any] | None
    # Resource ids the job is scoped to (mirrors what was hashed into
    # the dedup key). Lets cross-device UIs match a Job to a card via
    # ``GET /api/jobs?kind=...&scope_id=...`` without a localStorage
    # hint. NULL on legacy rows from before migration 0092.
    scope_ids: list[uuid.UUID] | None = None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    expires_at: datetime


class JobListOut(BaseModel):
    items: list[JobOut]


def _resolve_download_url(job: Job) -> str | None:
    """Backend-relative download URL for the job's stored result.

    Returns ``/api/jobs/{id}/result_download`` when ``result_uri`` is an
    ``s3://...`` pointer, ``None`` otherwise. The actual stream is
    served by :func:`download_job_result` with the same ownership gate
    as the rest of the jobs API.
    """
    if not job.result_uri or not job.result_uri.startswith("s3://"):
        return None
    rest = job.result_uri[len("s3://") :]
    if "/" not in rest:
        return None
    return f"/api/jobs/{job.id}/result_download"


def _job_to_out(job: Job) -> JobOut:
    out = JobOut.model_validate(job)
    out.result_download_url = _resolve_download_url(job)
    # Workers may stash a structured result payload under
    # ``Job.input["result"]`` (see services/bulk_ingest.py for the
    # bulk-upload kind). Surface it on the JobOut so the frontend can
    # render it on completion without a second fetch.
    payload = (job.input or {}).get("result")
    if isinstance(payload, dict):
        out.result = payload
    label = (job.input or {}).get("_display_label")
    if isinstance(label, str) and label:
        out.display_label = label
    return out


def cap_exceeded_to_http(exc: jobs_service.JobCapExceededError) -> HTTPException:
    """Translate a service-layer cap error into a 429. Consumers call
    this in their try/except around :func:`enqueue_or_get` so the
    response shape stays consistent across kinds.
    """
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail={
            "error": "job_cap_exceeded",
            "scope": exc.scope,
            "used": exc.used,
            "cap": exc.cap,
            "hint": (
                f"Too many active jobs ({exc.used}/{exc.cap}). "
                "Wait for in-flight work to finish or cancel "
                "queued jobs from the active-jobs panel."
            ),
        },
        headers={"Retry-After": str(exc.retry_after_seconds)},
    )


def _is_owner_or_admin(*, user: User, owner_subject_id: uuid.UUID) -> bool:
    return user.subject_id == owner_subject_id or bool(user.is_admin)


@router.get("", response_model=JobListOut)
async def list_jobs(
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    active: Annotated[
        bool,
        Query(
            description=(
                "When true (default), return only queued + running jobs. "
                "When false, return the most recent jobs regardless of state."
            )
        ),
    ] = True,
    kind: Annotated[str | None, Query(description="Optional filter by job kind.")] = None,
    scope_id: Annotated[
        uuid.UUID | None,
        Query(
            description=(
                "Optional resource id filter; matches rows where ``scope_ids`` "
                "contains the given uuid. Used by per-resource UIs (study card, "
                "document row) to recover an in-flight or recent job started "
                "from another browser/device."
            ),
        ),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> JobListOut:
    """List the caller's jobs. Admin callers see only their own jobs
    here; the admin all-jobs view is a separate endpoint we will add
    when there is a need."""
    from sqlalchemy import select

    q = select(Job).where(Job.owner_subject_id == user.subject_id)
    if active:
        q = q.where(Job.status.in_(JOB_ACTIVE_STATUSES))
    q = q.order_by(Job.created_at.desc()).limit(limit)
    if kind is not None:
        q = q.where(Job.kind == kind)
    if scope_id is not None:
        # ``scope_ids @> ARRAY[id]`` lights up the GIN index added by
        # migration 0092. Postgres-only; the model is defined as
        # nullable so legacy rows just don't match.
        q = q.where(Job.scope_ids.contains([scope_id]))
    rows = list((await db.execute(q)).scalars().all())
    return JobListOut(items=[_job_to_out(j) for j in rows])


@router.get("/{job_id}", response_model=JobOut)
async def get_job(
    job_id: uuid.UUID,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> JobOut:
    try:
        job = await jobs_service.get_job(db, job_id)
    except jobs_service.JobNotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "job_not_found"},
        ) from e
    if not _is_owner_or_admin(user=user, owner_subject_id=job.owner_subject_id):
        # Mask non-owner reads as 404, not 403, to avoid disclosing
        # job existence.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "job_not_found"},
        )
    return _job_to_out(job)


@router.delete("/{job_id}", response_model=JobOut)
async def cancel_job(
    job_id: uuid.UUID,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> JobOut:
    """Request cancellation. The DB row transitions to ``cancelled``
    immediately; running workers honour the cancel at their next
    checkpoint. Consumers that cannot interrupt mid-step document
    that limitation in their kind's notes."""
    try:
        job = await jobs_service.request_cancellation(
            db,
            job_id,
            user.subject_id,
            is_admin=bool(user.is_admin),
        )
    except jobs_service.JobNotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "job_not_found"},
        ) from e
    except jobs_service.JobAlreadyTerminalError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "job_already_terminal",
                "status": str(e),
            },
        ) from e
    await db.commit()
    return _job_to_out(job)


@router.get("/{job_id}/result_download")
async def download_job_result(
    job_id: uuid.UUID,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    user_or_none: Annotated[User | None, Depends(public_user)] = None,
    dt: str | None = None,
) -> StreamingResponse:
    """Stream the job's saved result through the backend.

    Auth: standard ``Authorization: Bearer`` header *or* a single-use
    ``?dt=<token>`` from ``POST /auth/download-token`` (resource_kind
    ``job_result``). The dt path lets the export-success dialog use a
    plain ``<a href>`` so multi-GB ZIPs stream straight to disk
    instead of fetch+Blob (the prior pattern silently 401'd the
    anchor click because the browser doesn't send Authorization on
    top-level navigation).

    Storage isolation: the bucket/key pair encoded in ``result_uri``
    never appears in the response. Same ownership gate as
    :func:`get_job` — non-owner non-admins receive 404 to avoid
    confirming that someone else's job exists.
    """
    user = await resolve_download_user(
        user=user_or_none,
        dt=dt,
        db=db,
        resource_kind="job_result",
        resource_id=job_id,
    )
    try:
        job = await jobs_service.get_job(db, job_id)
    except jobs_service.JobNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "job_not_found"},
        ) from exc
    if not _is_owner_or_admin(user=user, owner_subject_id=job.owner_subject_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "job_not_found"},
        )
    if not job.result_uri or not job.result_uri.startswith("s3://"):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "no_downloadable_result"},
        )
    rest = job.result_uri[len("s3://") :]
    if "/" not in rest:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "no_downloadable_result"},
        )
    bucket, key = rest.split("/", 1)
    filename = key.rsplit("/", 1)[-1] or "download"
    return await proxy_s3_object(
        request=request,
        bucket=bucket,
        key=key,
        filename=filename,
    )


__all__ = ["JobListOut", "JobOut", "cap_exceeded_to_http", "router"]
