# ruff: noqa: F405
# Auto-split from api/studies.py on 2026-05-21.
# Section: ``segmentations``.

from __future__ import annotations

from fastapi import APIRouter

from bvphoenix.api.studies import _shared
from bvphoenix.api.studies._shared import *  # noqa: F403

router = APIRouter()


@router.post(
    "/series/{series_id}/crop",
    response_model=ROICropOut,
)
async def crop_series_roi(
    series_id: uuid.UUID,
    body: ROICropIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(optional_user)],
    audit: AuditDep,
    _scope: None = _AGENT_PATIENT_IMAGES,
) -> Response:
    """Crop a rectangular ROI on a single axial slice.

    The endpoint reuses the existing ``dicom_to_jpeg`` pipeline (window
    + rescale to JPEG) and applies a PIL crop on the resulting raster.
    The cropped JPEG is returned inline with content-type
    ``image/jpeg``; the response headers carry the bbox and size for
    Header-only sniffing.

    Sprint 5b: cropping operates on the windowed raster (JPEG-domain
    coordinates), not raw pixel space. For raw-DICOM crops the agent
    can fall back to ``GET /series/:sid/dicom_meta`` + custom math; a
    Sprint 6+ ``crop_volume`` endpoint can be added when SimpleITK
    reformat is generalised.
    """
    import io as _io

    from PIL import Image as _PILImage

    from bvphoenix.middleware.problem_details import problem as _problem

    row = (
        await db.execute(
            select(Series, ImagingStudy)
            .join(ImagingStudy, ImagingStudy.id == Series.study_id)
            .where(Series.id == series_id)
        )
    ).first()
    if row is None:
        raise _problem(404, "not_found", "series not found")
    series, study = row
    if not await can(db, user=user, action=READ_PIXELS, study=study):
        raise _problem(404, "not_found", "series not found")

    instances = (
        (
            await db.execute(
                select(Instance)
                .where(Instance.series_id == series.id)
                .order_by(Instance.instance_number.asc().nullslast())
            )
        )
        .scalars()
        .all()
    )
    if not instances:
        raise _problem(409, "conflict", "series has no instances")
    if body.instance_index >= len(instances):
        raise _problem(
            422,
            "slice_index_out_of_range",
            f"instance_index out of range [0, {len(instances)})",
            extra={"slice_total": len(instances)},
        )
    inst = instances[body.instance_index]

    storage = get_s3_storage()
    dcm_bytes = await asyncio.to_thread(
        storage.get_object_bytes, bucket=inst.s3_bucket, key=inst.s3_key
    )
    try:
        jpeg = await asyncio.to_thread(
            dicom_to_jpeg,
            dcm_bytes,
            wc_delta=body.wc_delta,
            ww_delta=body.ww_delta,
            max_side=body.max_side,
        )
    except NoPixelDataError as exc:
        raise _problem(
            422,
            "no_pixel_data",
            "instance has no pixel data (structured report or key object)",
        ) from exc

    img = _PILImage.open(_io.BytesIO(jpeg))
    width, height = img.size
    x0, y0, x1, y1 = body.bbox
    if not (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height):
        raise _problem(
            422,
            "invalid_bbox",
            f"bbox must satisfy 0<=x0<x1<={width} and 0<=y0<y1<={height}",
            extra={"image_size": [width, height], "bbox": body.bbox},
        )
    cropped = img.crop((x0, y0, x1, y1))
    buf = _io.BytesIO()
    cropped.save(buf, format="JPEG", quality=85)
    crop_bytes = buf.getvalue()

    await audit.log(
        action="series_roi_crop",
        actor_subject_id=user.subject_id if user else None,
        resource_kind="series",
        resource_id=series.id,
        metadata={
            "study_id": str(study.id),
            "instance_index": body.instance_index,
            "bbox": body.bbox,
        },
    )

    headers = {
        "x-series-id": str(series.id),
        "x-instance-uid": inst.sop_instance_uid,
        "x-bbox": f"{x0},{y0},{x1},{y1}",
        "x-image-size": f"{width},{height}",
        "cache-control": "no-store",
    }
    return Response(content=crop_bytes, media_type="image/jpeg", headers=headers)


@router.get(
    "/series/{series_id}/dicom_meta",
    response_model=DicomMetaOut,
)
async def get_series_dicom_meta(
    series_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(optional_user)],
    audit: AuditDep,
    instance_index: int = Query(
        default=0,
        ge=0,
        description=(
            "0-based index into the series' instance list. Default 0 — "
            "the first slice usually carries the geometry tags relevant "
            "to the whole series."
        ),
    ),
    version: str = Query(
        default="v1",
        description="Allowlist version pin (currently only ``v1`` is implemented).",
    ),
    _scope: None = _AGENT_PATIENT_READ,
) -> DicomMetaOut:
    """Return the allowlisted DICOM metadata for one instance of a series.

    Sprint 5 (ADR 0011): the response is filtered through
    :data:`DICOM_META_ALLOWLIST_V1` — PHI-bearing tags
    (PatientName, StudyDescription, AccessionNumber, …) and private
    tags are silently dropped at the API boundary, regardless of what
    the underlying DICOM dataset carries.
    """
    from bvphoenix.services.dicom_meta_allowlist import extract_allowlisted

    row = (
        await db.execute(
            select(Series, ImagingStudy)
            .join(ImagingStudy, ImagingStudy.id == Series.study_id)
            .where(Series.id == series_id)
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="series not found")
    series, study = row
    if not await can(db, user=user, action=READ_PIXELS, study=study):
        raise HTTPException(status_code=404, detail="series not found")

    instances = (
        (
            await db.execute(
                select(Instance)
                .where(Instance.series_id == series.id)
                .order_by(Instance.instance_number.asc().nullslast())
            )
        )
        .scalars()
        .all()
    )
    if not instances:
        raise HTTPException(status_code=409, detail="series has no instances yet")
    if instance_index >= len(instances):
        raise HTTPException(
            status_code=422,
            detail=f"instance_index out of range [0, {len(instances)})",
        )
    inst = instances[instance_index]

    storage = get_s3_storage()
    dcm_bytes = await asyncio.to_thread(
        storage.get_object_bytes, bucket=inst.s3_bucket, key=inst.s3_key
    )
    # Read the DICOM dataset *without* loading pixel data — the meta
    # endpoint never needs it and the allowlist drops PixelData anyway.
    ds = await asyncio.to_thread(pydicom.dcmread, io.BytesIO(dcm_bytes), stop_before_pixels=True)
    meta = await asyncio.to_thread(extract_allowlisted, ds, version=version)

    await audit.log(
        action="series_dicom_meta_read",
        actor_subject_id=user.subject_id if user else None,
        resource_kind="series",
        resource_id=series.id,
        metadata={
            "study_id": str(study.id),
            "instance_index": instance_index,
            "allowlist_version": version,
        },
    )

    return DicomMetaOut(
        series_id=str(series.id),
        sop_instance_uid=inst.sop_instance_uid,
        instance_number=inst.instance_number,
        allowlist_version=version,
        meta=meta,
    )


@router.get(
    "/series/{series_id}/segmentation-records",
    response_model=list[SegmentationOut],
)
async def list_series_segmentations(
    series_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(optional_user)],
    audit: AuditDep,
    producer: str | None = Query(default=None, max_length=32),
    ttl_seconds: int = Query(default=300, ge=60, le=900),
) -> list[SegmentationOut]:
    """Return every segmentation record registered against the series.

    Lives at ``/segmentation-records`` (not ``/segmentations``) because
    ``api/segmentations.py`` already owns the ``/series/{id}/segmentations``
    surface — that older endpoint backs the FE viewer's S3-only label
    picker (legacy ``.bin`` blobs) and returns an envelope-shaped
    ``{series_id, items}`` body. Co-locating both routes at the same
    path silently let whichever router was registered first win and
    crashed the SegmentationImporter (it expected ``items`` and got a
    bare list). The two surfaces will be unified once the older S3
    listing migrates to the ``Segmentation`` ORM table; until then,
    keep the paths distinct.

    Sprint 6: read-only endpoint. The producer set today is empty for
    most series — TotalSegmentator (ADR 0013) is the planned producer
    but the ARM64 wheel spike is still pending. Manual masks uploaded
    via the viewer (future) and external NIfTI imports already work
    against this table.

    Each row carries a backend-relative ``download_url``
    (``/api/segmentations/{id}/file``) that streams the NIfTI through
    the backend with the same auth as this call. Storage hosts and
    bucket names never appear in the response (memoria
    ``feedback_storage_isolation``).
    """
    from bvphoenix.db.models import Segmentation
    from bvphoenix.middleware.problem_details import problem as _problem

    row = (
        await db.execute(
            select(Series, ImagingStudy)
            .join(ImagingStudy, ImagingStudy.id == Series.study_id)
            .where(Series.id == series_id)
        )
    ).first()
    if row is None:
        raise _problem(404, "not_found", "series not found")
    series, study = row
    if not await can(db, user=user, action=READ_PIXELS, study=study):
        raise _problem(404, "not_found", "series not found")

    stmt = select(Segmentation).where(Segmentation.series_id == series.id)
    if producer:
        stmt = stmt.where(Segmentation.producer == producer)
    rows = (await db.execute(stmt.order_by(Segmentation.created_at.desc()))).scalars().all()

    out: list[SegmentationOut] = []
    for r in rows:
        url = f"/api/segmentations/{r.id}/file" if r.s3_bucket and r.s3_key else None
        out.append(
            SegmentationOut(
                id=str(r.id),
                series_id=str(r.series_id),
                producer=r.producer,
                producer_version=r.producer_version,
                label=r.label,
                label_map=r.label_map or {},
                size_bytes=r.size_bytes,
                download_url=url,
                created_at=r.created_at.isoformat(),
            )
        )

    await audit.log(
        action="series_segmentations_read",
        actor_subject_id=user.subject_id if user else None,
        resource_kind="series",
        resource_id=series.id,
        metadata={"n_segmentations": len(out)},
    )
    return out


@router.get("/segmentations/{segmentation_id}/file")
async def download_segmentation(
    segmentation_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(optional_user)],
    audit: AuditDep,
) -> StreamingResponse:
    """Stream a segmentation NIfTI through the backend.

    No presigned URL or storage host crosses the response. The same
    permission gate as ``list_series_segmentations`` applies.
    """
    from bvphoenix.db.models import Segmentation
    from bvphoenix.middleware.problem_details import problem as _problem

    row = (
        await db.execute(
            select(Segmentation, Series, ImagingStudy)
            .join(Series, Series.id == Segmentation.series_id)
            .join(ImagingStudy, ImagingStudy.id == Series.study_id)
            .where(Segmentation.id == segmentation_id)
        )
    ).first()
    if row is None:
        raise _problem(404, "not_found", "segmentation not found")
    seg, _series, study = row
    if not await can(db, user=user, action=READ_PIXELS, study=study):
        raise _problem(404, "not_found", "segmentation not found")
    if not seg.s3_bucket or not seg.s3_key:
        raise _problem(404, "not_found", "segmentation has no payload")

    storage = get_s3_storage()
    try:
        body_iter, length, _ = await asyncio.to_thread(
            storage.iter_object,
            bucket=seg.s3_bucket,
            key=seg.s3_key,
        )
    except Exception as exc:
        raise _problem(
            404,
            "binary_unavailable",
            "segmentation binary unavailable",
        ) from exc

    await audit.log(
        action="segmentation_download",
        actor_subject_id=user.subject_id if user else None,
        resource_kind="segmentation",
        resource_id=seg.id,
    )

    headers: dict[str, str] = {
        "content-disposition": _content_disposition(
            f"{seg.label}.nii.gz", disposition="attachment"
        ),
        "cache-control": "private, max-age=0",
    }
    if length is not None:
        headers["content-length"] = str(length)
    elif seg.size_bytes is not None:
        headers["content-length"] = str(seg.size_bytes)
    return StreamingResponse(
        body_iter,
        media_type="application/gzip",
        headers=headers,
    )
