"""DICOM upload API — drag-drop multipart + DICOMweb STOW-RS.

Two endpoints share the same ingestion pipeline (``services.dicom_ingest``):

- ``POST /api/dicom/studies`` — browser-friendly ``multipart/form-data``
  with one or many ``.dcm`` files in a single request. Used by the
  web uploader.
- ``POST /api/dicom/stow-rs`` — DICOMweb STOW-RS (PS3.18 §10.5) with
  ``multipart/related`` parts of type ``application/dicom``. Used by
  clinical workstations and other toolchains.

Both require an authenticated user; studies are owned by the caller and
default to private / tier-1 unless explicit form fields are provided.
After a successful ingest the endpoint fires post-ingest jobs (volume
pre-pack so the viewer doesn't wait on first open, plus a BiomedCLIP
embedding so the study is reachable by visual search) via the shared
:mod:`bvphoenix.services.ingest_jobs` helper.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from arq import create_pool
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from pydantic import BaseModel
from pydicom.errors import InvalidDicomError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.auth import require_user
from bvphoenix.config import get_settings
from bvphoenix.db.models import Subject, User
from bvphoenix.db.session import get_db
from bvphoenix.services.arq_redis import redis_settings
from bvphoenix.services.consent_auto import ensure_tier_consents
from bvphoenix.services.dicom_ingest import (
    DicomIngestor,
    IngestSummary,
    UploadError,
    iter_stow_rs_parts,
    parse_related_boundary,
)
from bvphoenix.services.ingest_jobs import enqueue_postprocess_jobs
from bvphoenix.services.quota import check_quota_or_raise
from bvphoenix.services.rate_limit import STOW_LIMIT, UPLOAD_LIMIT, limiter
from bvphoenix.services.storage_quota import check_storage_quota
from bvphoenix.storage import get_s3_storage

router = APIRouter(prefix="/dicom", tags=["dicom-upload"])

# Cap per-file size so a runaway upload doesn't eat all memory. 512 MiB
# is generous for a single DICOM instance (typical CT slice ≈ 0.5 MiB).
MAX_FILE_BYTES = 512 * 1024 * 1024

# Cap total request size for STOW-RS (the whole multipart blob gets read
# into memory — fine for bursts of a few thousand slices, but don't let
# a single request push past this).
MAX_STOW_BYTES = 4 * 1024 * 1024 * 1024


class UploadErrorOut(BaseModel):
    filename: str
    message: str


class UploadSummaryOut(BaseModel):
    studies_created: list[str]
    series_created: list[str]
    instances_created: int
    instances_existing: int
    errors: list[UploadErrorOut]
    # Convenience for the frontend: surface the DB ids of touched
    # studies/series so the uploader can link straight to the viewer.
    study_ids: list[str]
    series_ids: list[str]


async def _resolve_owner_subject(db: AsyncSession, user: User) -> Subject:
    row = (
        await db.execute(select(Subject).where(Subject.id == user.subject_id))
    ).scalar_one_or_none()
    if row is None:
        # Shouldn't happen (users.subject_id FK guarantees a row) but be
        # loud rather than crash inside the ingest loop.
        raise HTTPException(status_code=500, detail="owner subject missing for authenticated user")
    return row


async def _run_ingest(
    *,
    db: AsyncSession,
    owner: Subject,
    blobs: list[tuple[str, bytes]],
    tier: str,
    is_public: bool,
) -> tuple[UploadSummaryOut, list[tuple[str, str | None]]]:
    """Feed ``blobs`` (filename, bytes) into :class:`DicomIngestor` and
    build the response payload. Returns ``(summary, series_for_jobs)`` where
    each job entry is a ``(series_id, modality)`` pair — the modality lets the
    post-ingest enqueue emit ``embed_series`` only for embeddable series.
    """
    settings = get_settings()
    storage = get_s3_storage()
    storage.ensure_bucket(settings.s3_bucket_raw)

    ingestor = DicomIngestor(
        db=db,
        storage=storage,
        bucket=settings.s3_bucket_raw,
        owner=owner,
        tier=tier,
        is_public=is_public,
    )

    summary = IngestSummary()
    for filename, blob in blobs:
        if not blob:
            summary.errors.append(UploadError(filename=filename, message="empty file"))
            continue
        if len(blob) > MAX_FILE_BYTES:
            summary.errors.append(
                UploadError(
                    filename=filename,
                    message=f"file too large ({len(blob)} bytes, max {MAX_FILE_BYTES})",
                )
            )
            continue
        try:
            result = await ingestor.ingest_blob(blob)
        except InvalidDicomError as exc:
            summary.errors.append(UploadError(filename=filename, message=str(exc)))
            continue
        except Exception as exc:  # pragma: no cover — defensive: keep batch alive
            summary.errors.append(UploadError(filename=filename, message=f"ingest error: {exc}"))
            continue

        if result.created:
            summary.instances_created += 1
        else:
            summary.instances_existing += 1

    # No extra queries needed — the ingestor already loaded the rows.
    study_ids = [str(s.id) for s in ingestor.touched_studies.values()]
    series_ids = [str(s.id) for s in ingestor.touched_series.values()]
    # Carry modality alongside the id so the post-ingest enqueue can gate
    # embed_series on embeddable (image) series without a second query.
    series_for_jobs = [(str(s.id), s.modality) for s in ingestor.touched_series.values()]

    await ingestor.finalize()
    # F6.1: materialise the tier's implied consent rows inside the same
    # transaction so a subsequent audit that reads either the ImagingStudy or
    # the Consent row sees the consistent pair.
    if tier in ("t3", "t4") and study_ids:
        await ensure_tier_consents(
            db,
            user_subject_id=owner.id,
            tier=tier,
            study_ids=[uuid.UUID(s) for s in study_ids],
        )
    await db.commit()

    return (
        UploadSummaryOut(
            studies_created=list(ingestor.touched_studies.keys()),
            series_created=list(ingestor.touched_series.keys()),
            instances_created=summary.instances_created,
            instances_existing=summary.instances_existing,
            errors=[UploadErrorOut(filename=e.filename, message=e.message) for e in summary.errors],
            study_ids=study_ids,
            series_ids=series_ids,
        ),
        series_for_jobs,
    )


async def _enqueue_postprocess(series: list[tuple[str, str | None]]) -> None:
    """Fire-and-forget post-ingest jobs for every new series: a volume
    pre-pack plus a BiomedCLIP embedding for embeddable series (so visual
    search finds them). Failure here is not fatal — the ``/volume.raw``
    endpoint packs on demand, and a missed embedding can be backfilled via
    the admin ``embed-missing`` endpoint. Job selection lives in one place:
    :func:`bvphoenix.services.ingest_jobs.enqueue_postprocess_jobs`.
    """
    if not series:
        return
    try:
        settings = get_settings()
        redis = await create_pool(redis_settings(settings.redis_url))
        try:
            await enqueue_postprocess_jobs(redis, series)
        finally:
            await redis.close()
    except Exception:
        # Non-fatal: viewer packs on first open and embeddings can be
        # backfilled if the worker didn't pick the job up.
        pass


@router.post(
    "/studies",
    response_model=UploadSummaryOut,
    status_code=status.HTTP_201_CREATED,
)
@limiter.limit(UPLOAD_LIMIT)
async def upload_studies(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    files: list[UploadFile] = File(..., description="One or more .dcm files"),
    tier: str = Form("t1", description="Contribution tier (t1-t4)"),
    is_public: bool = Form(False),
) -> UploadSummaryOut:
    """Drag-drop-friendly upload: ``multipart/form-data`` with a ``files``
    list. Each file is validated against DICOM magic bytes / pydicom, the
    valid ones are grouped by ImagingStudy/Series UID and persisted. The
    endpoint never aborts on a single bad file — instead every failure is
    reported in the ``errors`` field of the summary.
    """
    if not files:
        raise HTTPException(status_code=400, detail="no files provided")
    if tier not in ("t1", "t2", "t3", "t4"):
        raise HTTPException(status_code=400, detail="invalid tier")

    owner = await _resolve_owner_subject(db, user)

    blobs: list[tuple[str, bytes]] = []
    for f in files:
        data = await f.read()
        blobs.append((f.filename or "<unnamed>", data))

    # F11.3: enforce the 10 GiB free-tier cap on T1/T2 before persisting.
    # Incoming is the bytes we just buffered; the check is skipped for
    # T3/T4 where the platform absorbs the storage cost.
    incoming = sum(len(data) for _, data in blobs)
    await check_quota_or_raise(db, user_subject_id=owner.id, tier=tier, incoming_bytes=incoming)

    # Per-subject hard storage cap (5 GB default, admin-overridable
    # via app_settings.storage.user_quota_gb:<subject_id>). Orthogonal
    # to the F11.3 OpenData tier cap above.
    await check_storage_quota(db, subject_id=owner.id, additional_bytes=incoming)

    summary, series_for_jobs = await _run_ingest(
        db=db, owner=owner, blobs=blobs, tier=tier, is_public=is_public
    )
    await _enqueue_postprocess(series_for_jobs)
    return summary


@router.post(
    "/stow-rs",
    response_model=UploadSummaryOut,
    status_code=status.HTTP_200_OK,
)
@limiter.limit(STOW_LIMIT)
async def stow_rs(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
) -> UploadSummaryOut:
    """DICOMweb STOW-RS endpoint (PS3.18 §10.5).

    Expects ``Content-Type: multipart/related; type="application/dicom";
    boundary=...`` with each part carrying a single ``.dcm`` in the body.
    The full request is buffered in memory; this is fine for the
    typical clinical use case (one study at a time, ≤ a few GiB).
    """
    content_type = request.headers.get("content-type", "")
    boundary = parse_related_boundary(content_type)
    if boundary is None:
        raise HTTPException(
            status_code=415,
            detail=("STOW-RS requires Content-Type: multipart/related with a boundary parameter"),
        )

    body = await request.body()
    if len(body) > MAX_STOW_BYTES:
        raise HTTPException(status_code=413, detail="request too large")

    parts = iter_stow_rs_parts(body, boundary)
    if not parts:
        raise HTTPException(status_code=400, detail="no DICOM parts found in request body")

    owner = await _resolve_owner_subject(db, user)

    blobs = [(f"stow-rs-part-{i}.dcm", part) for i, part in enumerate(parts)]
    # STOW-RS always ingests at tier T1. The body length is a close
    # enough upper bound for the quota check — a few per-cent of MIME
    # framing overhead rounds in the user's favour.
    await check_quota_or_raise(db, user_subject_id=owner.id, tier="t1", incoming_bytes=len(body))
    await check_storage_quota(db, subject_id=owner.id, additional_bytes=len(body))
    summary, series_for_jobs = await _run_ingest(
        db=db, owner=owner, blobs=blobs, tier="t1", is_public=False
    )
    await _enqueue_postprocess(series_for_jobs)
    return summary
