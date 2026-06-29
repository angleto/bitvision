"""Segmentation import / listing API.

External tools (3D Slicer, ITK-SNAP, MONAI) export labelled volumes as
NIfTI or NRRD. The viewer's native mask format is a raw uint8 buffer,
one byte per voxel, x-fastest. This module accepts either external
format, binarises it, resamples to the source volume's dims if needed,
and stores the result in the derivatives bucket under
``segmentations/{series_id}/{label}.bin``.

Listing returns every label stored for a series so the frontend can
hydrate the segmentation picker. Get returns the raw bytes directly so
the viewer can hand them to ``vtkImageData`` without a second roundtrip.
"""

from __future__ import annotations

import asyncio
import io
import json
import re
import uuid
from typing import Annotated, Any

import pydicom
from arq import create_pool
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.api.jobs import JobOut, cap_exceeded_to_http
from bvphoenix.api.markers import _agent_provenance
from bvphoenix.auth import enforce_agent_patient_scope, optional_user, require_user
from bvphoenix.config import get_settings
from bvphoenix.db.models import Derivative, ImagingStudy, Instance, Segmentation, Series, User
from bvphoenix.db.session import get_db
from bvphoenix.services import jobs as jobs_service
from bvphoenix.services.arq_redis import redis_settings
from bvphoenix.services.dicom_seg_export import SegExportError, export_segmentation_seg
from bvphoenix.services.permissions import READ_PIXELS, WRITE_ANNOTATIONS, can
from bvphoenix.services.segmentation_import import (
    SegmentationImportError,
    import_segmentation,
)
from bvphoenix.services.volumes import (
    DERIVATIVE_FORMAT,
    DERIVATIVE_KIND,
    HEADER_STRUCT,
)
from bvphoenix.storage import get_s3_storage

router = APIRouter(tags=["segmentations"])

# Labels show up in S3 keys and in URLs — keep them filesystem-safe.
_LABEL_RE = re.compile(r"^[a-zA-Z0-9._-]{1,64}$")

_MAX_UPLOAD_BYTES = 200 * 1024 * 1024  # 200 MiB; Slicer .seg.nrrd can be big


class SegmentationOut(BaseModel):
    """Public listing entry for a viewer-imported mask.

    ``s3_key`` is intentionally absent: bucket layout is internal storage,
    leaking it to the FE / MCP / share-link surface would violate
    ``feedback_storage_isolation``. The mask binary is fetched via
    ``GET /series/{series_id}/segmentations/{label}`` (auth-checked
    streaming proxy), no presigned URL or storage path is ever served.
    """

    label: str
    size_bytes: int
    nonzero_voxels: int | None = None
    # Provenance (present for masks promoted to the Segmentation ORM row;
    # absent for legacy S3-only blobs surfaced as a fallback).
    id: str | None = None
    producer: str | None = None
    author_kind: str | None = None
    created_at: str | None = None


class SegmentationListOut(BaseModel):
    series_id: str
    items: list[SegmentationOut]


async def _load_series_with_study(
    db: AsyncSession, series_id: uuid.UUID
) -> tuple[Series, ImagingStudy]:
    row = (
        await db.execute(
            select(Series, ImagingStudy)
            .join(ImagingStudy, ImagingStudy.id == Series.study_id)
            .where(Series.id == series_id)
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="series not found")
    return row[0], row[1]


def _seg_prefix(series_id: uuid.UUID) -> str:
    return f"segmentations/{series_id}/"


def _seg_key(series_id: uuid.UUID, label: str) -> str:
    return f"{_seg_prefix(series_id)}{label}.bin"


async def _upsert_segmentation(
    db: AsyncSession,
    request: Request,
    user: User,
    *,
    series_id: uuid.UUID,
    patient_id: uuid.UUID | None,
    producer: str,
    label: str,
    s3_bucket: str,
    s3_key: str,
    size_bytes: int | None,
    nonzero_voxels: int | None,
    label_map: dict | None = None,
) -> Segmentation:
    """Promote a mask onto the ``Segmentation`` ORM row with provenance.

    Idempotent on (series_id, producer, label): a re-write replaces the
    pointer + metrics. An agent token cannot overwrite a human-authored
    mask (mirrors the marker write gate); admins are exempt.
    """
    author_kind, model_id, _provider, agent_token_id = _agent_provenance(request)

    existing = (
        await db.execute(
            select(Segmentation).where(
                Segmentation.series_id == series_id,
                Segmentation.producer == producer,
                Segmentation.label == label,
            )
        )
    ).scalar_one_or_none()
    if (
        existing is not None
        and getattr(request.state, "is_agent", False)
        and existing.author_kind == "human"
        and not getattr(user, "is_admin", False)
    ):
        raise HTTPException(
            status_code=403,
            detail="agent tokens cannot overwrite a human-authored segmentation",
        )

    values = {
        "series_id": series_id,
        "patient_id": patient_id,
        "producer": producer,
        "label": label,
        "s3_bucket": s3_bucket,
        "s3_key": s3_key,
        "size_bytes": size_bytes,
        "nonzero_voxels": nonzero_voxels,
        "label_map": label_map or {},
        "author_kind": author_kind,
        "model_id": model_id,
        "agent_token_id": agent_token_id,
        "created_by_subject_id": user.subject_id,
    }
    stmt = (
        pg_insert(Segmentation)
        .values(**values)
        .on_conflict_do_update(
            constraint="uq_segmentations_series_producer_label",
            set_={
                "patient_id": patient_id,
                "s3_bucket": s3_bucket,
                "s3_key": s3_key,
                "size_bytes": size_bytes,
                "nonzero_voxels": nonzero_voxels,
                "author_kind": author_kind,
                "model_id": model_id,
                "agent_token_id": agent_token_id,
                "created_by_subject_id": user.subject_id,
                "created_at": func.now(),
            },
        )
        .returning(Segmentation.id)
    )
    seg_id = (await db.execute(stmt)).scalar_one()
    await db.commit()
    return (await db.execute(select(Segmentation).where(Segmentation.id == seg_id))).scalar_one()


async def _series_dims(db: AsyncSession, series: Series) -> tuple[int, int, int]:
    """Derive ``(nx, ny, nz)`` the same way the packed volume does.

    Fast path: if the ``volume_f32`` derivative is already cached
    (almost always, since the viewer triggers packing on open), parse
    the first 12 bytes of the header — that's the dimensions alone.

    Slow path: count instances for ``nz`` and read the first instance
    to get ``Rows`` / ``Columns``. Still cheaper than re-packing the
    whole volume just to validate a shape.
    """
    derivative = (
        await db.execute(
            select(Derivative).where(
                Derivative.series_id == series.id,
                Derivative.kind == DERIVATIVE_KIND,
                Derivative.format == DERIVATIVE_FORMAT,
                Derivative.stack_index == 0,  # primary sub-stack
            )
        )
    ).scalar_one_or_none()
    storage = get_s3_storage()
    if derivative is not None:
        # Ranged GET so we don't pull the whole packed volume (often
        # hundreds of MiB) just to read a 32-byte header.
        try:
            head = await asyncio.to_thread(
                storage.get_object_range,
                bucket=derivative.s3_bucket,
                key=derivative.s3_key,
                start=0,
                length=HEADER_STRUCT.size,
            )
            nx, ny, nz, *_ = HEADER_STRUCT.unpack(head[: HEADER_STRUCT.size])
            return int(nx), int(ny), int(nz)
        except Exception:
            pass  # fall through to slow path

    # Slow path: count + first-slice read.
    instance_rows = (
        await db.execute(
            select(Instance.s3_bucket, Instance.s3_key)
            .where(Instance.series_id == series.id)
            .order_by(Instance.instance_number.asc().nullslast())
        )
    ).all()
    if not instance_rows:
        raise HTTPException(status_code=409, detail="series has no instances yet")

    first_bucket, first_key = instance_rows[0]
    first_bytes = await asyncio.to_thread(
        storage.get_object_bytes, bucket=first_bucket, key=first_key
    )
    ds = pydicom.dcmread(io.BytesIO(first_bytes), stop_before_pixels=True)
    rows = int(getattr(ds, "Rows", 0))
    cols = int(getattr(ds, "Columns", 0))
    if rows == 0 or cols == 0:
        raise HTTPException(status_code=422, detail="cannot determine series dims")
    return (cols, rows, len(instance_rows))


@router.get(
    "/series/{series_id}/segmentations",
    response_model=SegmentationListOut,
)
async def list_segmentations(
    series_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(optional_user)],
) -> SegmentationListOut:
    _series, study = await _load_series_with_study(db, series_id)
    if not await can(db, user=user, action=READ_PIXELS, study=study):
        raise HTTPException(status_code=404, detail="series not found")

    # The Segmentation ORM rows are authoritative (they carry provenance +
    # metrics). Legacy S3-only ``.bin`` blobs with no ORM row are merged in
    # as fallback entries so masks produced before P1 still appear.
    rows = (
        (
            await db.execute(
                select(Segmentation)
                .where(Segmentation.series_id == series_id)
                .order_by(Segmentation.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    items: list[SegmentationOut] = []
    orm_labels: set[str] = set()
    for r in rows:
        orm_labels.add(r.label)
        items.append(
            SegmentationOut(
                label=r.label,
                size_bytes=r.size_bytes or 0,
                nonzero_voxels=r.nonzero_voxels,
                id=str(r.id),
                producer=r.producer,
                author_kind=r.author_kind,
                created_at=r.created_at.isoformat(),
            )
        )

    settings = get_settings()
    storage = get_s3_storage()
    prefix = _seg_prefix(series_id)
    entries = await asyncio.to_thread(
        storage.list_objects, bucket=settings.s3_bucket_derivatives, prefix=prefix
    )
    for key, size in entries:
        rel = key[len(prefix) :]
        if not rel.endswith(".bin"):
            continue
        label = rel[: -len(".bin")]
        if not _LABEL_RE.match(label) or label in orm_labels:
            continue
        items.append(SegmentationOut(label=label, size_bytes=size))

    items.sort(key=lambda s: s.label)
    return SegmentationListOut(series_id=str(series_id), items=items)


@router.get("/series/{series_id}/segmentations/{label}")
async def get_segmentation(
    series_id: uuid.UUID,
    label: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(optional_user)],
) -> Response:
    if not _LABEL_RE.match(label):
        raise HTTPException(status_code=400, detail="invalid label")
    _series, study = await _load_series_with_study(db, series_id)
    if not await can(db, user=user, action=READ_PIXELS, study=study):
        raise HTTPException(status_code=404, detail="series not found")

    settings = get_settings()
    storage = get_s3_storage()
    try:
        body = await asyncio.to_thread(
            storage.get_object_bytes,
            bucket=settings.s3_bucket_derivatives,
            key=_seg_key(series_id, label),
        )
    except Exception as exc:
        raise HTTPException(status_code=404, detail="segmentation not found") from exc
    return Response(content=body, media_type="application/octet-stream")


@router.get("/series/{series_id}/segmentations/{label}/dicom-seg")
async def export_segmentation_dicom_seg(
    series_id: uuid.UUID,
    label: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(optional_user)],
) -> Response:
    """Export a stored mask as a conformant, geo-referenced DICOM SEG object
    (SOP class 1.2.840.10008.5.1.4.1.1.66.4) that references the source series,
    so it can be opened in any DICOM-aware tool (3D Slicer, OHIF, ...) — unlike
    the raw ``.bin``. Same read gate as the mask; the bytes are returned inline
    (storage-isolated, no presigned URL / bucket name leaves the platform)."""
    if not _LABEL_RE.match(label):
        raise HTTPException(status_code=400, detail="invalid label")
    _series, study = await _load_series_with_study(db, series_id)
    if not await can(db, user=user, action=READ_PIXELS, study=study):
        raise HTTPException(status_code=404, detail="series not found")

    seg = (
        await db.execute(
            select(Segmentation).where(
                Segmentation.series_id == series_id, Segmentation.label == label
            )
        )
    ).scalar_one_or_none()
    if seg is None:
        raise HTTPException(status_code=404, detail="segmentation not found")
    try:
        body = await export_segmentation_seg(db, seg)
    except SegExportError as exc:
        # The mask cannot be expressed as a conformant SEG (e.g. it does not
        # line up 1:1 with the source slices — multi-stack / resampled series).
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return Response(
        content=body,
        media_type="application/dicom",
        headers={"Content-Disposition": f'attachment; filename="{label}.seg.dcm"'},
    )


JOB_KIND_SEG_EXPORT = "segmentation_seg_export"
_SEG_EXPORT_TTL_HOURS = 48


@router.post(
    "/series/{series_id}/segmentations/{label}/dicom-seg/export",
    response_model=JobOut,
    status_code=202,
)
async def export_segmentation_dicom_seg_async(
    request: Request,
    series_id: uuid.UUID,
    label: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
) -> JobOut:
    """Enqueue an async DICOM SEG export Job whose result (an S3 artifact) is
    downloadable off-platform via the standard job-result download token. The
    MCP-reachable twin of the synchronous ``/dicom-seg`` route, for agents and
    large series. Idempotent: a fresh request for the same segmentation dedups
    onto the in-flight/recent Job."""
    if not _LABEL_RE.match(label):
        raise HTTPException(status_code=400, detail="invalid label")
    _series, study = await _load_series_with_study(db, series_id)
    enforce_agent_patient_scope(request, study.patient_id, scope="patient:images")
    if not await can(db, user=user, action=READ_PIXELS, study=study):
        raise HTTPException(status_code=404, detail="series not found")

    seg = (
        await db.execute(
            select(Segmentation).where(
                Segmentation.series_id == series_id, Segmentation.label == label
            )
        )
    ).scalar_one_or_none()
    if seg is None:
        raise HTTPException(status_code=404, detail="segmentation not found")

    canonical_input: dict[str, Any] = {"label": label, "_display_label": f"DICOM SEG: {label}"}
    try:
        result = await jobs_service.enqueue_or_get(
            db,
            kind=JOB_KIND_SEG_EXPORT,
            owner_subject_id=user.subject_id,
            canonical_input=canonical_input,
            scope_ids=(seg.id,),
            expires_in_hours=_SEG_EXPORT_TTL_HOURS,
            is_admin=bool(user.is_admin),
        )
    except jobs_service.JobCapExceededError as e:
        raise cap_exceeded_to_http(e) from e

    if not result.deduped:
        try:
            settings = get_settings()
            redis = await create_pool(redis_settings(settings.redis_url))
            arq_handle = await redis.enqueue_job(
                "export_segmentation_seg_dicom",
                str(result.job.id),
                str(seg.id),
                str(user.subject_id),
                json.dumps(canonical_input),
            )
            await redis.close()
            if arq_handle is not None:
                await jobs_service.set_arq_job_id(db, result.job.id, arq_handle.job_id)
        except Exception as exc:
            await jobs_service.mark_failed(
                db, result.job.id, error={"code": "enqueue_failed", "message": str(exc)}
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
    await db.refresh(result.job)
    return JobOut.model_validate(result.job)


@router.post(
    "/series/{series_id}/segmentations",
    response_model=SegmentationOut,
    status_code=status.HTTP_201_CREATED,
)
async def upload_segmentation(
    request: Request,
    series_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    label: Annotated[str, Form(...)],
    file: Annotated[UploadFile, File(...)],
) -> SegmentationOut:
    """Accept a NIfTI (.nii/.nii.gz) or NRRD (.nrrd) file, binarise it,
    resample to the source volume's dims, and upload as a raw uint8
    buffer to the derivatives bucket. Requires ``write:annotations``
    because segmentations are treated as a form of annotation for
    permission purposes."""
    if not _LABEL_RE.match(label):
        raise HTTPException(
            status_code=400,
            detail="label must match [a-zA-Z0-9._-]{1,64}",
        )
    series, study = await _load_series_with_study(db, series_id)
    if not await can(db, user=user, action=WRITE_ANNOTATIONS, study=study):
        raise HTTPException(status_code=403, detail="cannot annotate this series")

    if not file.filename:
        raise HTTPException(status_code=400, detail="missing filename")

    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="empty upload")
    if len(data) > _MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"file exceeds {_MAX_UPLOAD_BYTES // (1024 * 1024)} MiB limit",
        )

    dims = await _series_dims(db, series)

    try:
        imported = await asyncio.to_thread(
            import_segmentation,
            data=data,
            filename=file.filename,
            target_dims=dims,
        )
    except SegmentationImportError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    settings = get_settings()
    storage = get_s3_storage()
    key = _seg_key(series_id, label)
    await asyncio.to_thread(
        storage.upload_bytes,
        imported.data,
        bucket=settings.s3_bucket_derivatives,
        key=key,
    )
    seg = await _upsert_segmentation(
        db,
        request,
        user,
        series_id=series_id,
        patient_id=study.patient_id,
        producer="manual",
        label=label,
        s3_bucket=settings.s3_bucket_derivatives,
        s3_key=key,
        size_bytes=len(imported.data),
        nonzero_voxels=imported.nonzero_voxels,
    )
    return SegmentationOut(
        label=seg.label,
        size_bytes=seg.size_bytes or len(imported.data),
        nonzero_voxels=seg.nonzero_voxels,
        id=str(seg.id),
        producer=seg.producer,
        author_kind=seg.author_kind,
        created_at=seg.created_at.isoformat(),
    )


@router.delete(
    "/series/{series_id}/segmentations/{label}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_segmentation(
    series_id: uuid.UUID,
    label: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
) -> Response:
    if not _LABEL_RE.match(label):
        raise HTTPException(status_code=400, detail="invalid label")
    _series, study = await _load_series_with_study(db, series_id)
    if not await can(db, user=user, action=WRITE_ANNOTATIONS, study=study):
        raise HTTPException(status_code=403, detail="cannot delete this segmentation")

    settings = get_settings()
    storage = get_s3_storage()
    await asyncio.to_thread(
        storage.delete_object,
        bucket=settings.s3_bucket_derivatives,
        key=_seg_key(series_id, label),
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ----------------------------------------------------------------------
# Automated / interactive segmentation endpoints (V7).
#
# The viewer can either ask for a full automatic multi-organ pass via
# TotalSegmentator (background job, polling-based status) or for a
# single click-driven prediction via MedSAM-2 (synchronous, returns a
# 2D mask). Both run in the worker tier — the backend just enqueues
# and (for the interactive path) awaits the result. When the worker
# host doesn't have the optional ``seg`` extra installed, the
# corresponding job returns an error payload that the frontend
# surfaces verbatim.
# ----------------------------------------------------------------------


class AutoSegmentIn(BaseModel):
    roi_subset: list[str] | None = None
    overwrite: bool = False
    fast: bool = True


class InteractivePredictIn(BaseModel):
    """Per-click prediction request payload.

    ``axis``: 0 = sagittal (X-fixed), 1 = coronal (Y-fixed),
              2 = axial (Z-fixed) — matches the worker's expectation
              and the viewport-axis convention used elsewhere in the
              app.
    ``slice_idx``: integer voxel index along ``axis`` (so e.g. a
                   click on slice 42 of the axial view sends 42 with
                   axis=2).
    ``points``: list of (x, y) tuples in slice coordinates of the
                slice picked by ``axis``+``slice_idx``.
    ``labels``: optional 0/1 per point (1 = include, 0 = exclude).
                Defaults to all-1 when omitted.
    ``label``: optional label name to also persist the resulting 3D
               mask (currently a single-slice mask, the rest of the
               volume is zero) under ``segmentations/{id}/{label}.bin``
               so the viewer can hand it to ``setSegmentationMask``
               without uploading a separate file.
    """

    axis: int
    slice_idx: int
    points: list[tuple[float, float]]
    labels: list[int] | None = None
    label: str | None = None


@router.post(
    "/series/{series_id}/segmentations/auto",
    status_code=status.HTTP_202_ACCEPTED,
)
async def enqueue_auto_segment(
    request: Request,
    series_id: uuid.UUID,
    body: AutoSegmentIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
) -> dict[str, str | list[str]]:
    """Enqueue a TotalSegmentator job for the given series. The job
    produces one binary mask per ROI in ``body.roi_subset`` (defaults
    to a curated CT abdomen + thorax subset on the worker side) and
    uploads them under the same prefix the regular upload endpoint
    uses, so the viewer's existing listing path picks them up
    automatically.

    Returns immediately with 202; the frontend polls
    ``GET /series/{id}/segmentations`` to see new labels appear.
    """
    _series, study = await _load_series_with_study(db, series_id)
    if not await can(db, user=user, action=WRITE_ANNOTATIONS, study=study):
        raise HTTPException(status_code=403, detail="cannot annotate this series")

    # Validate ROI names against the same regex used everywhere else
    # — TotalSegmentator's official ROI list uses snake_case which
    # already passes ``[a-zA-Z0-9._-]{1,64}``.
    rois = body.roi_subset
    if rois is not None:
        for r in rois:
            if not _LABEL_RE.match(r):
                raise HTTPException(
                    status_code=400,
                    detail=f"invalid roi name: {r}",
                )

    from arq import create_pool

    from bvphoenix.services.arq_redis import redis_settings

    # Provenance for the worker-written Segmentation rows: the worker has
    # no request context, so resolve the acting principal + patient scope
    # here and thread them through the job args.
    author_kind, _model_id, _provider, agent_token_id = _agent_provenance(request)

    settings = get_settings()
    redis = await create_pool(redis_settings(settings.redis_url))
    job = await redis.enqueue_job(
        "segment_auto",
        str(series_id),
        rois,
        body.overwrite,
        body.fast,
        author_kind=author_kind,
        agent_token_id=str(agent_token_id) if agent_token_id else None,
        created_by=str(user.subject_id) if user.subject_id else None,
        patient_id=str(study.patient_id) if study.patient_id else None,
    )
    job_id = job.job_id if job else ""
    await redis.close()
    return {
        "status": "enqueued",
        "series_id": str(series_id),
        "job_id": job_id,
        "rois": rois or [],
    }


class AutoSegmentStatusOut(BaseModel):
    """Status snapshot for an auto-seg job. ``state`` mirrors Arq's own
    JobStatus enum (``deferred|queued|in_progress|complete|not_found``)
    so the frontend can drive its UI without a translation layer.

    ``failed`` is True when ``state == "complete"`` and the task either
    raised (``error`` populated) or returned a result with ``status``
    other than ``ok|all_present``. Splitting "complete-and-ok" from
    "complete-but-failed" client-side would require duplicating the
    mapping; doing it once here keeps the contract narrow.
    """

    job_id: str
    state: str
    failed: bool = False
    error: str | None = None
    # When the task returned successfully, this is the dict the worker
    # built (``produced``, ``skipped``, ``status="all_present"``…).
    # Useful to surface "produced 17 organs in 8 m" in the UI without a
    # second fetch to /segmentations.
    result: dict[str, Any] | None = None


@router.get(
    "/series/{series_id}/segmentations/auto/status",
    response_model=AutoSegmentStatusOut,
)
async def auto_segment_status(
    series_id: uuid.UUID,
    job_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
) -> AutoSegmentStatusOut:
    """Look up the status of a previously enqueued auto-seg job.

    The frontend stores the ``job_id`` returned by ``POST .../auto`` in
    localStorage (so the indicator survives a page reload) and polls
    this endpoint while the job is in flight. Without it, a failed
    task (engine_error, OOM, missing volume) leaves the HotSpotsPanel
    in a permanent "in progress" spinner because the panel only watches
    the segmentations listing for newly-uploaded labels.

    Permission: same gate as enqueue (``WRITE_ANNOTATIONS``) — the
    caller had to be authorised to start the job to be looking at it.
    Using a stronger gate would make perfectly legitimate polling fail
    after a permission downgrade; a weaker one would leak job
    existence.
    """
    _series, study = await _load_series_with_study(db, series_id)
    if not await can(db, user=user, action=WRITE_ANNOTATIONS, study=study):
        raise HTTPException(status_code=403, detail="cannot annotate this series")

    from arq import create_pool
    from arq.jobs import Job as ArqJob
    from arq.jobs import JobStatus

    from bvphoenix.services.arq_redis import redis_settings

    settings = get_settings()
    redis = await create_pool(redis_settings(settings.redis_url))
    try:
        arq_job = ArqJob(job_id=job_id, redis=redis)
        state = await arq_job.status()
        out = AutoSegmentStatusOut(job_id=job_id, state=state.value)
        if state == JobStatus.complete:
            # ``result()`` re-raises the worker exception if the task
            # itself raised. ``segment_auto`` swallows the
            # totalsegmentator failure and returns ``{"status":
            # "engine_error", ...}``, but a true crash (OOM, killed)
            # surfaces here as an exception the FE needs to see.
            try:
                payload = await arq_job.result(timeout=0.0)
            except Exception as exc:
                out.failed = True
                out.error = f"{type(exc).__name__}: {exc}"
                return out
            if isinstance(payload, dict):
                out.result = payload
                inner = payload.get("status")
                # Success cases: ``ok`` (produced ≥ 1 mask), ``all_present``
                # (every requested ROI was already cached, the task was a
                # no-op by design), ``no_rois`` (caller asked for an empty
                # subset). Everything else — ``engine_error``,
                # ``volume_not_packed``, ``series_not_found``, ``no_output``
                # — surfaces as failed with the worker-supplied error
                # string when present.
                if inner not in (None, "ok", "all_present", "no_rois"):
                    out.failed = True
                    inner_error = payload.get("error")
                    if isinstance(inner_error, str) and inner_error:
                        out.error = inner_error
                    else:
                        # ``no_output`` / ``volume_not_packed`` ship a
                        # ``hint`` string instead of ``error``; bubble
                        # whichever one is set so the operator sees
                        # something actionable.
                        hint = payload.get("hint")
                        out.error = str(hint) if isinstance(hint, str) and hint else str(inner)
        return out
    finally:
        await redis.close()


@router.post("/series/{series_id}/segmentations/interactive/predict")
async def interactive_predict(
    request: Request,
    series_id: uuid.UUID,
    body: InteractivePredictIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
) -> dict:
    """Run MedSAM-2 on a single slice and return the resulting 2D
    mask. Synchronous from the caller's perspective: we enqueue the
    arq job and ``await`` the result with a generous timeout because
    on CPU the inference itself takes ~3-10 seconds.

    The caller passes pixel coordinates within the slice; the
    response carries the mask base64-encoded for transport. When
    ``body.label`` is provided the backend ALSO embeds the 2D mask
    into a full-volume binary (zeros elsewhere) and stores it under
    the standard segmentations prefix, so the viewer can switch to
    using ``setSegmentationMask`` without an extra round-trip.
    """
    series, study = await _load_series_with_study(db, series_id)
    if not await can(db, user=user, action=WRITE_ANNOTATIONS, study=study):
        raise HTTPException(status_code=403, detail="cannot annotate this series")
    if body.label is not None and not _LABEL_RE.match(body.label):
        raise HTTPException(
            status_code=400,
            detail="label must match [a-zA-Z0-9._-]{1,64}",
        )
    if body.axis not in (0, 1, 2):
        raise HTTPException(status_code=400, detail="axis must be 0, 1, or 2")
    if not body.points:
        raise HTTPException(status_code=400, detail="at least one point required")

    from arq import create_pool

    from bvphoenix.services.arq_redis import redis_settings

    settings = get_settings()
    redis = await create_pool(redis_settings(settings.redis_url))
    try:
        job = await redis.enqueue_job(
            "medsam_predict_2d",
            str(series_id),
            body.axis,
            body.slice_idx,
            [list(p) for p in body.points],
            body.labels,
        )
        if job is None:
            raise HTTPException(status_code=503, detail="failed to enqueue prediction job")
        try:
            # CPU MedSAM-2 inference is ~3-10s on a small slice; cap
            # at 60s so a model-load pause on the first call doesn't
            # leave the user staring at a spinner forever.
            result = await job.result(timeout=60)
        except TimeoutError as exc:
            raise HTTPException(status_code=504, detail="prediction timed out") from exc
    finally:
        await redis.close()

    if not isinstance(result, dict) or result.get("status") != "ok":
        # Surface the worker's error verbatim — easier to debug a
        # missing optional dep than a generic 502.
        msg = "prediction failed"
        if isinstance(result, dict) and "error" in result:
            msg = str(result["error"])
        raise HTTPException(status_code=502, detail=msg)

    # Optional persistence into a full-volume binary mask. Useful for
    # the viewer's "magic-wand" tool: one click → mask drawn in the
    # 3D view immediately. The 2D mask is embedded into a (nz, ny, nx)
    # zeros volume at the correct slice; non-active voxels stay 0.
    if body.label is not None:
        import base64

        dims = await _series_dims(db, series)
        nx, ny, nz = dims
        mask_b64 = result.get("mask_b64")
        slice_shape = result.get("shape") or []
        if mask_b64 and len(slice_shape) == 2:
            mask_2d = bytes(base64.b64decode(mask_b64))

            import numpy as np

            slice_arr = np.frombuffer(mask_2d, dtype=np.uint8).reshape(
                slice_shape[0], slice_shape[1]
            )
            volume = np.zeros((nz, ny, nx), dtype=np.uint8)
            ax = body.axis
            sidx = body.slice_idx
            try:
                if ax == 2 and 0 <= sidx < nz:
                    volume[sidx, :, :] = _resize_mask(slice_arr, (ny, nx))
                elif ax == 1 and 0 <= sidx < ny:
                    volume[:, sidx, :] = _resize_mask(slice_arr, (nz, nx))
                elif ax == 0 and 0 <= sidx < nx:
                    volume[:, :, sidx] = _resize_mask(slice_arr, (nz, ny))
            except Exception:
                pass
            else:
                storage = get_s3_storage()
                key = _seg_key(series_id, body.label)
                mask_bytes = np.ascontiguousarray(volume).tobytes()
                await asyncio.to_thread(
                    storage.upload_bytes,
                    mask_bytes,
                    bucket=settings.s3_bucket_derivatives,
                    key=key,
                )
                # Promote to the Segmentation ORM row with provenance
                # (producer 'medsam' — MedSAM-2 prediction, operator-driven).
                await _upsert_segmentation(
                    db,
                    request,
                    user,
                    series_id=series_id,
                    patient_id=study.patient_id,
                    producer="medsam",
                    label=body.label,
                    s3_bucket=settings.s3_bucket_derivatives,
                    s3_key=key,
                    size_bytes=len(mask_bytes),
                    nonzero_voxels=int(volume.sum()),
                )
                # ``s3_key`` deliberately omitted — bucket layout is
                # internal storage (``feedback_storage_isolation``); the
                # FE only needs to know the persisted label so it can
                # re-fetch the mask via the auth-checked GET endpoint.
                result = {**result, "persisted_label": body.label}
    return result


def _resize_mask(arr: Any, target_shape: tuple[int, int]) -> Any:
    """Nearest-neighbour resample a 2D uint8 mask. ``arr`` is a
    ``numpy.ndarray`` at runtime; the annotation is ``Any`` so that
    importing this module doesn't require numpy at module load
    (numpy is pulled in lazily inside the function body)."""
    import numpy as np

    if arr.shape == target_shape:
        return arr
    src_h, src_w = arr.shape
    tgt_h, tgt_w = target_shape
    h_idx = np.clip((np.arange(tgt_h) * src_h / tgt_h).astype(np.int64), 0, src_h - 1)
    w_idx = np.clip((np.arange(tgt_w) * src_w / tgt_w).astype(np.int64), 0, src_w - 1)
    return arr[np.ix_(h_idx, w_idx)]


# ----------------------------------------------------------------------
# MONAI Label proxy. Forwards selected GET / POST requests to an
# externally-managed MONAI Label server. Configured via the
# ``BVP_MONAI_LABEL_URL`` env var; absent → endpoint returns 503 with
# a clear hint instead of crashing.
# ----------------------------------------------------------------------


@router.get("/segmentations/monai_label/info")
async def monai_label_info(
    user: Annotated[User, Depends(require_user)],
) -> dict:
    """Forward to MONAI Label's ``/info`` endpoint to discover which
    models the connected server exposes. The frontend calls this on
    mount of the magic-wand panel to populate a model picker."""
    settings = get_settings()
    upstream = getattr(settings, "monai_label_url", None) or _env_monai_url()
    if not upstream:
        raise HTTPException(
            status_code=503,
            detail=(
                "MONAI Label not configured. Set BVP_MONAI_LABEL_URL to a "
                "reachable MONAI Label server URL on the backend host."
            ),
        )
    import httpx

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(f"{upstream.rstrip('/')}/info")
        if resp.status_code != 200:
            raise HTTPException(status_code=502, detail=f"MONAI Label returned {resp.status_code}")
        return resp.json()


def _env_monai_url() -> str | None:
    """Fallback: read the URL straight from the environment when the
    settings model hasn't been extended (keeps this module
    upgrade-safe before the config schema lands)."""
    import os as _os

    return _os.environ.get("BVP_MONAI_LABEL_URL")
