# ruff: noqa: F405
# Auto-split from api/studies.py on 2026-05-21.
# Section: ``bulk``.

from __future__ import annotations

from fastapi import APIRouter

from bvphoenix.api.studies import _shared
from bvphoenix.api.studies._shared import *  # noqa: F403

router = APIRouter()


@router.post(
    "/series/{series_id}/hot-spots",
    response_model=HotSpotsOut,
)
async def find_series_hot_spots(
    series_id: uuid.UUID,
    body: HotSpotsIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(optional_user)],
    audit: AuditDep,
) -> HotSpotsOut:
    """Find the top-N high-uptake connected regions in a PET (or
    raw-intensity) volume. Modeled on Hermes / Mirada "lesion
    finder" tools: threshold (default 50% of global max, PERCIST
    1.0), 3D 6-connected components via ``scipy.ndimage.label``,
    filter by ``min_volume_ml``, sort descending by per-component
    max, return top-N with SUVmax/mean/peak (1 cm³).

    Permission: ``READ_PIXELS`` (same as volume.raw). 409 when the
    derivative cache is cold."""
    import numpy as np
    from scipy import ndimage as _ndi

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

    storage = get_s3_storage()
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
    if derivative is None:
        raise HTTPException(
            status_code=409,
            detail="volume not yet packed; GET /series/{id}/volume.raw first",
        )
    cached = await asyncio.to_thread(
        storage.get_object_bytes,
        bucket=derivative.s3_bucket,
        key=derivative.s3_key,
    )

    nx, ny, nz, sx, sy, sz, _vmin, _vmax = HEADER_STRUCT.unpack_from(cached, 0)
    arr = np.frombuffer(cached, dtype=np.float32, offset=HEADER_STRUCT.size).reshape(
        int(nz), int(ny), int(nx)
    )

    suv_factor: float | None = None
    suv_variant_used: str | None = None
    units_native: str | None = None
    if body.suv_variant is not None:
        from bvphoenix.services.suv import compute_suv_factors

        first_inst = (
            await db.execute(
                select(Instance.s3_bucket, Instance.s3_key)
                .where(Instance.series_id == series.id)
                .order_by(Instance.instance_number.asc().nullslast())
                .limit(1)
            )
        ).first()
        if first_inst is not None:
            inst_bytes = await asyncio.to_thread(
                storage.get_object_bytes,
                bucket=first_inst[0],
                key=first_inst[1],
            )
            try:
                ds = pydicom.dcmread(io.BytesIO(inst_bytes), stop_before_pixels=True)
                factors = compute_suv_factors(ds)
                units_native = factors.units
                factor_map = {
                    "bw": factors.factor_bw,
                    "lbm-janma": factors.factor_lbm_janmahasatian,
                    "lbm-james": factors.factor_lbm_james,
                    "bsa-mosteller": factors.factor_bsa_mosteller,
                    "bsa-dubois": factors.factor_bsa_dubois,
                }
                f = factor_map.get(body.suv_variant)
                if f is not None and f > 0:
                    suv_factor = f
                    suv_variant_used = body.suv_variant
            except Exception:
                pass

    global_max_raw = float(arr.max())
    global_max_suv = global_max_raw * suv_factor if suv_factor is not None else None

    if body.threshold_mode == "absolute_suv":
        if suv_factor is None:
            raise HTTPException(
                status_code=422,
                detail="absolute_suv threshold requires SUV factors",
            )
        threshold_raw = body.threshold_value / suv_factor
    else:
        threshold_raw = body.threshold_value * global_max_raw
    mask = arr >= threshold_raw

    # Apply optional slice-range restriction. The mask is shape
    # (nz, ny, nx); zero out slices outside [slice_min, slice_max] so
    # connected-components only sees the slab the operator wants.
    z_lo = max(0, body.slice_min if body.slice_min is not None else 0)
    z_hi = min(
        int(nz) - 1,
        body.slice_max if body.slice_max is not None else int(nz) - 1,
    )
    if z_hi < z_lo:
        # Operator passed an inverted range: degenerate to empty.
        mask[:, :, :] = False
    else:
        if z_lo > 0:
            mask[:z_lo, :, :] = False
        if z_hi < int(nz) - 1:
            mask[z_hi + 1 :, :, :] = False

    # Apply optional anatomic / operator-drawn exclusion masks. ``True``
    # voxels in ``exclusion_full`` are dropped from the search volume:
    # high-uptake organs (kidneys, bladder on PET) and operator-drawn
    # ``bbox.exclusion`` regions. Built once before connected-components
    # so a single ``mask &=`` removes them from every component.
    from bvphoenix.services.exclusion_masks import build_exclusion_mask

    exclusion_full = await build_exclusion_mask(
        db=db,
        series=series,
        shape_zyx=(int(nz), int(ny), int(nx)),
        exclude_segmentation_labels=body.exclude_segmentation_labels,
        exclude_marker_ids=body.exclude_marker_ids,
    )
    if exclusion_full is not None:
        mask &= ~exclusion_full

    structure = _ndi.generate_binary_structure(3, 1)  # 6-connectivity
    labels, n_components = _ndi.label(mask, structure=structure)
    base_response = {
        "threshold_used": float(threshold_raw),
        "threshold_kind": body.threshold_mode,
        "suv_variant_used": suv_variant_used,
        "units_native": units_native,
        "global_max_raw": global_max_raw,
        "global_max_suv": global_max_suv,
        "volume_nz": int(nz),
        "slice_min_used": int(z_lo),
        "slice_max_used": int(z_hi),
    }
    if n_components == 0:
        await audit.log(
            action="series_hot_spots",
            actor_subject_id=user.subject_id if user else None,
            resource_kind="series",
            resource_id=series.id,
        )
        return HotSpotsOut(spots=[], **base_response)

    voxel_volume_ml = (float(sx) * float(sy) * float(sz)) / 1000.0
    min_voxels = max(1, round(body.min_volume_ml / voxel_volume_ml))

    component_ids = np.arange(1, n_components + 1)
    sums = _ndi.sum_labels(arr, labels, component_ids)
    counts = _ndi.sum_labels(np.ones_like(arr, dtype=np.float32), labels, component_ids)
    maxs = _ndi.maximum(arr, labels, component_ids)
    centroids_kji = _ndi.center_of_mass(arr, labels, component_ids)
    bboxes = _ndi.find_objects(labels)

    rows: list[tuple[float, int]] = []
    for idx in range(n_components):
        if counts[idx] < min_voxels:
            continue
        rows.append((float(maxs[idx]), idx))
    rows.sort(key=lambda r: -r[0])
    rows = rows[: body.top_n]

    spots: list[HotSpot] = []
    for rank_, (cmax, idx) in enumerate(rows, start=1):
        slc = bboxes[idx]
        if slc is None:
            continue
        zsl, ysl, xsl = slc
        cz, cy, cx = centroids_kji[idx]
        cnt = int(counts[idx])
        comp_mean = float(sums[idx] / cnt) if cnt > 0 else 0.0
        peak_native: float | None = None
        if suv_factor is not None:
            kb_full = arr[zsl, ysl, xsl]
            mask_block = labels[zsl, ysl, xsl] == (idx + 1)
            if mask_block.any():
                masked = np.where(mask_block, kb_full, -np.inf)
                argmax_off = np.unravel_index(int(np.argmax(masked)), masked.shape)
                pk_global = (
                    zsl.start + argmax_off[0],
                    ysl.start + argmax_off[1],
                    xsl.start + argmax_off[2],
                )
                radius_mm = 6.20  # 1 cm³ sphere
                spacing = (float(sx), float(sy), float(sz))
                half_vox = (
                    max(1, int(np.ceil(radius_mm / spacing[2]))),
                    max(1, int(np.ceil(radius_mm / spacing[1]))),
                    max(1, int(np.ceil(radius_mm / spacing[0]))),
                )
                ki0 = max(0, pk_global[0] - half_vox[0])
                ki1 = min(int(nz) - 1, pk_global[0] + half_vox[0])
                kj0 = max(0, pk_global[1] - half_vox[1])
                kj1 = min(int(ny) - 1, pk_global[1] + half_vox[1])
                kk0 = max(0, pk_global[2] - half_vox[2])
                kk1 = min(int(nx) - 1, pk_global[2] + half_vox[2])
                kb = arr[ki0 : ki1 + 1, kj0 : kj1 + 1, kk0 : kk1 + 1]
                kk_d = (np.arange(ki0, ki1 + 1) - pk_global[0]) * spacing[2]
                jj_d = (np.arange(kj0, kj1 + 1) - pk_global[1]) * spacing[1]
                ii_d = (np.arange(kk0, kk1 + 1) - pk_global[2]) * spacing[0]
                d2 = kk_d[:, None, None] ** 2 + jj_d[None, :, None] ** 2 + ii_d[None, None, :] ** 2
                inside = d2 <= radius_mm * radius_mm
                if inside.any():
                    peak_native = float(kb[inside].mean())
        spots.append(
            HotSpot(
                rank=rank_,
                centroid_ijk=[round(float(cx)), round(float(cy)), round(float(cz))],
                bbox_min_ijk=[int(xsl.start), int(ysl.start), int(zsl.start)],
                bbox_max_ijk=[
                    int(xsl.stop) - 1,
                    int(ysl.stop) - 1,
                    int(zsl.stop) - 1,
                ],
                voxel_count=cnt,
                volume_ml=cnt * voxel_volume_ml,
                raw_max=cmax,
                raw_mean=comp_mean,
                suv_max=cmax * suv_factor if suv_factor is not None else None,
                suv_mean=(comp_mean * suv_factor if suv_factor is not None else None),
                suv_peak=(
                    peak_native * suv_factor
                    if (peak_native is not None and suv_factor is not None)
                    else None
                ),
            )
        )

    await audit.log(
        action="series_hot_spots",
        actor_subject_id=user.subject_id if user else None,
        resource_kind="series",
        resource_id=series.id,
    )
    return HotSpotsOut(spots=spots, **base_response)


@router.post(
    "/studies/{study_id}/screenshots",
    response_model=StudyScreenshotOut,
    status_code=status.HTTP_201_CREATED,
)
async def upload_study_screenshot(
    study_id: uuid.UUID,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    audit: AuditDep,
    file: Annotated[UploadFile, File(..., description="PNG image of the captured viewport")],
    viewport: Annotated[
        str,
        Form(description="Which viewport was captured (axial/sagittal/coronal/3d/mip/oblique)"),
    ],
    series_id: Annotated[
        uuid.UUID | None,
        Form(description="Optional source series id (the volume rendered in the viewport)"),
    ] = None,
    note: Annotated[
        str | None,
        Form(description="Free-text note the user added to the capture"),
    ] = None,
) -> StudyScreenshotOut:
    """Save a viewer screenshot as a Document linked to ``study_id``.

    The capture is uploaded as a PNG by the frontend (the camera
    overlay button in ``CornerstoneMPRLayout``); this endpoint
    stores it in the patient's S3 raw bucket and creates a
    ``Document`` row with ``kind_id='unclassified'`` and
    ``provenance_id='manual_entry'`` (the catalogue does not yet
    carry a dedicated ``viewer_screenshot`` kind — adding one is a
    follow-up migration; the descriptive ``title`` keeps the intent
    legible until then).

    ``DocumentStudyLink`` is recorded with ``link_kind='extracted_from'``
    (the screenshot is derived from the study), the closest fit in
    the existing CHECK constraint.

    Permission: ``WRITE_REPORT`` on the patient owning the study.
    Same gate as ``ingest_document`` so anyone who can attach reports
    can also attach screenshots.
    """
    import hashlib

    from bvphoenix.db.models import Document, DocumentStudyLink, Patient
    from bvphoenix.services.provenance_log import record_provenance

    if viewport not in _VIEWPORT_VALUES:
        raise HTTPException(
            status_code=422,
            detail=f"viewport must be one of {_VIEWPORT_VALUES!r}",
        )

    study = (
        await db.execute(select(ImagingStudy).where(ImagingStudy.id == study_id))
    ).scalar_one_or_none()
    if study is None:
        raise HTTPException(status_code=404, detail="study not found")

    patient = (
        await db.execute(select(Patient).where(Patient.id == study.patient_id))
    ).scalar_one_or_none()
    if patient is None or not await can_patient(
        db, user=user, action=WRITE_REPORT, patient=patient
    ):
        raise HTTPException(status_code=404, detail="study not found")

    binary = await file.read()
    if not binary:
        raise HTTPException(status_code=422, detail="empty file")
    if len(binary) > 25 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="screenshot exceeds 25 MB cap")
    sha256 = hashlib.sha256(binary).hexdigest()

    content_type = (file.content_type or "image/png").lower()
    if content_type not in ("image/png", "image/jpeg", "image/webp"):
        raise HTTPException(
            status_code=422,
            detail=f"unsupported content_type {content_type!r}",
        )
    ext = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}[content_type]

    captured_at = datetime.now(UTC)
    timestamp = captured_at.strftime("%Y%m%d-%H%M%S")
    title = f"Viewer screenshot · {viewport} · {timestamp}"
    if note:
        clip = note.strip().replace("\n", " ")
        if clip:
            title = (f"{title} · {clip}")[:255]

    doc_id = uuid.uuid4()
    s3_key = f"patient-docs/{patient.id}/{doc_id}.{ext}"
    settings = get_settings()
    storage = get_s3_storage()
    await asyncio.to_thread(
        storage.upload_bytes,
        binary,
        bucket=settings.s3_bucket_raw,
        key=s3_key,
    )

    body_text = (
        f"viewport: {viewport}\n"
        f"study_id: {study_id}\n"
        + (f"series_id: {series_id}\n" if series_id else "")
        + (f"note: {note}\n" if note else "")
    )

    doc = Document(
        id=doc_id,
        patient_id=patient.id,
        uploaded_by_subject_id=user.subject_id,
        kind_id="unclassified",
        provenance_id="manual_entry",
        authority_id="derived",
        title=title,
        text=body_text,
        file_s3_key=s3_key,
        file_content_type=content_type,
        document_date=captured_at.date(),
        content_sha256=sha256,
        original_blob_hash=sha256,
    )
    db.add(doc)
    await db.flush()

    db.add(
        DocumentStudyLink(
            document_id=doc.id,
            study_id=study.id,
            link_kind="extracted_from",
            created_by_subject_id=user.subject_id,
        )
    )

    record_provenance(
        db,
        target_kind="document",
        target_id=doc.id,
        activity="create",
        user=user,
        request=request,
        diff={
            "source": "viewer_screenshot",
            "viewport": viewport,
            "study_id": str(study_id),
            "series_id": str(series_id) if series_id else None,
            "size_bytes": len(binary),
        },
    )
    await audit.log(
        action="study_screenshot_upload",
        actor_subject_id=user.subject_id,
        resource_kind="study",
        resource_id=study.id,
    )
    await db.commit()
    return StudyScreenshotOut(
        document_id=str(doc.id),
        title=doc.title,
        sha256=sha256,
        size_bytes=len(binary),
    )
