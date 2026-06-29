# ruff: noqa: F405
# Auto-split from api/studies.py on 2026-05-21.
# Section: ``roi_stats``.

from __future__ import annotations

from fastapi import APIRouter

from bvphoenix.api.studies import _shared
from bvphoenix.api.studies._shared import *  # noqa: F403

router = APIRouter()


async def compute_roi_stats_core(
    db: AsyncSession,
    series: Series,
    body: ROIStatsIn,
) -> ROIStatsOut:
    """Deterministic ROI statistics over a series' packed Float32 volume.

    The numpy-precise compute, factored out of the route handler so the
    finding measurement-promotion path (``POST /findings/{id}/promote-
    measurement`` with ``source='roi_stats'``) can re-run the *same* number
    server-side. No new math — this is the route body verbatim, minus auth
    (the caller checks ``READ_PIXELS``) and audit. Raises ``HTTPException``
    (422/409/413) for bad inputs or a cold cache, exactly as the route did.

    Memory-sensitive: the packed buffer (100-500 MB) and its numpy views are
    explicitly released before returning so the resident set doesn't climb
    request-after-request (glibc arena retention) into a later OOMKill.
    """
    import numpy as np

    from bvphoenix.services.memory import release_memory
    from bvphoenix.services.volumes import MAX_VOLUME_BYTES

    # Shape-specific input validation. Sphere uses center+radius; the
    # other two use the bbox. Reject the wrong combination early so we
    # don't touch S3 for malformed inputs.
    if body.kind == "sphere":
        if body.center_ijk is None or body.radius_mm is None:
            raise HTTPException(
                status_code=422,
                detail="kind=sphere requires center_ijk and radius_mm",
            )
        if any(v < 0 for v in body.center_ijk):
            raise HTTPException(status_code=422, detail="center_ijk has negative coord")
    else:
        if body.min_ijk is None or body.max_ijk is None:
            raise HTTPException(
                status_code=422,
                detail=f"kind={body.kind} requires min_ijk and max_ijk",
            )
        if any(a > b for a, b in zip(body.min_ijk, body.max_ijk, strict=True)):
            raise HTTPException(
                status_code=422,
                detail=f"min_ijk {body.min_ijk!r} must be <= max_ijk {body.max_ijk!r}",
            )
        if any(v < 0 for v in body.min_ijk):
            raise HTTPException(status_code=422, detail="min_ijk has negative coord")

    # Reuse the cached packed volume. We deliberately *don't* re-pack
    # on miss here (that's a 30-second job for a large CT) — instead
    # we 409 and tell the caller to warm the cache by GET-ing
    # ``volume.raw`` first. The viewer already does this on series
    # mount, so a cache miss is a sign the caller skipped the warm-up.
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
    if len(cached) > MAX_VOLUME_BYTES:
        raise HTTPException(
            status_code=413,
            detail="volume too large to sample on this server",
        )

    nx, ny, nz, sx, sy, sz, _vmin, _vmax = HEADER_STRUCT.unpack_from(cached, 0)
    arr = np.frombuffer(cached, dtype=np.float32, offset=HEADER_STRUCT.size).reshape(
        int(nz), int(ny), int(nx)
    )
    spacing = (float(sx), float(sy), float(sz))  # (dx, dy, dz) in mm

    # Resolve the optional exclusion mask (kidneys / bladder /
    # operator-drawn boxes). Built before we slice the ROI so any
    # exclusion shape on the volume can be intersected with the ROI
    # without an extra full-volume reshape per exclusion. ``None`` when
    # nothing was requested or every requested input resolved to no-op.
    from bvphoenix.services.exclusion_masks import build_exclusion_mask

    exclusion_full = await build_exclusion_mask(
        db=db,
        series=series,
        shape_zyx=(int(nz), int(ny), int(nx)),
        exclude_segmentation_labels=body.exclude_segmentation_labels,
        exclude_marker_ids=body.exclude_marker_ids,
    )

    # Materialise the ROI mask in volume coordinates. ``flat_values`` is
    # the 1-D float32 array of voxels inside the ROI; ``arg_*`` indices
    # are the global (k, j, i) coordinates of the voxel at ``argmax``,
    # used as the SUVpeak kernel center below. ``bbox`` is the inclusive
    # axis-aligned span we'll use for the PERCIST peak kernel lookup.
    if body.kind == "sphere":
        assert body.center_ijk is not None and body.radius_mm is not None
        ci = max(0, min(int(nx) - 1, int(body.center_ijk[0])))
        cj = max(0, min(int(ny) - 1, int(body.center_ijk[1])))
        ck = max(0, min(int(nz) - 1, int(body.center_ijk[2])))
        radius_mm = float(body.radius_mm)
        half_i = max(1, int(np.ceil(radius_mm / spacing[0])))
        half_j = max(1, int(np.ceil(radius_mm / spacing[1])))
        half_k = max(1, int(np.ceil(radius_mm / spacing[2])))
        i0 = max(0, ci - half_i)
        i1 = min(int(nx) - 1, ci + half_i)
        j0 = max(0, cj - half_j)
        j1 = min(int(ny) - 1, cj + half_j)
        k0 = max(0, ck - half_k)
        k1 = min(int(nz) - 1, ck + half_k)
        sub_bbox = arr[k0 : k1 + 1, j0 : j1 + 1, i0 : i1 + 1]
        # Per-axis physical distance from the sphere center, in mm.
        kk = (np.arange(k0, k1 + 1) - ck) * spacing[2]
        jj = (np.arange(j0, j1 + 1) - cj) * spacing[1]
        ii = (np.arange(i0, i1 + 1) - ci) * spacing[0]
        dist2 = kk[:, None, None] ** 2 + jj[None, :, None] ** 2 + ii[None, None, :] ** 2
        mask = dist2 <= (radius_mm * radius_mm)
        if exclusion_full is not None:
            mask &= ~exclusion_full[k0 : k1 + 1, j0 : j1 + 1, i0 : i1 + 1]
        flat_values = sub_bbox[mask]
        voxel_count = int(flat_values.size)
        if voxel_count == 0:
            raise HTTPException(
                status_code=422,
                detail="sphere encloses zero voxels (radius too small for spacing)",
            )
        # argmax inside the bbox masked by the sphere; recover global IJK.
        bbox_argmax_flat = int(np.argmax(np.where(mask, sub_bbox, -np.inf)))
        pk = np.unravel_index(bbox_argmax_flat, sub_bbox.shape)
        pk_global = (k0 + int(pk[0]), j0 + int(pk[1]), i0 + int(pk[2]))
        sub = flat_values  # for downstream stats below
    else:
        assert body.min_ijk is not None and body.max_ijk is not None
        i0, j0, k0 = (max(0, int(v)) for v in body.min_ijk)
        i1 = min(int(nx) - 1, int(body.max_ijk[0]))
        j1 = min(int(ny) - 1, int(body.max_ijk[1]))
        k1 = min(int(nz) - 1, int(body.max_ijk[2]))
        if i0 > i1 or j0 > j1 or k0 > k1:
            raise HTTPException(
                status_code=422,
                detail="ROI bbox is empty after clamping to volume bounds",
            )
        sub_bbox = arr[k0 : k1 + 1, j0 : j1 + 1, i0 : i1 + 1]
        if exclusion_full is not None:
            keep = ~exclusion_full[k0 : k1 + 1, j0 : j1 + 1, i0 : i1 + 1]
            sub = sub_bbox[keep]
            voxel_count = int(sub.size)
            if voxel_count == 0:
                raise HTTPException(
                    status_code=422,
                    detail="ROI bbox is fully covered by the exclusion mask",
                )
            bbox_argmax_flat = int(np.argmax(np.where(keep, sub_bbox, -np.inf)))
            pk = np.unravel_index(bbox_argmax_flat, sub_bbox.shape)
            pk_global = (k0 + int(pk[0]), j0 + int(pk[1]), i0 + int(pk[2]))
        else:
            voxel_count = int(sub_bbox.size)
            if voxel_count == 0:
                raise HTTPException(status_code=422, detail="ROI bbox encloses zero voxels")
            sub = sub_bbox
            flat_argmax = int(np.argmax(sub_bbox))
            pk = np.unravel_index(flat_argmax, sub_bbox.shape)  # (k_off, j_off, i_off)
            pk_global = (k0 + int(pk[0]), j0 + int(pk[1]), i0 + int(pk[2]))

    mean = float(sub.mean())
    std = float(sub.std())
    vmin_roi = float(sub.min())
    vmax_roi = float(sub.max())

    # SUVpeak (PERCIST 1.0): 1 cm³ sphere centered on the argmax
    # voxel of the ROI. Sphere radius = (3/(4π))^(1/3) cm ≈ 0.620 cm
    # = 6.20 mm. We sample voxels whose center lies within that
    # radius using the volume spacing in mm. When the ROI is too
    # small to contain a 1 cm³ sphere, the kernel naturally clips
    # and the peak collapses to the local mean of the available
    # voxels — still a useful "peak of the ROI" surrogate.
    sphere_radius_mm = 6.20
    # Determine the kernel half-size in voxels per axis (ceil to make
    # sure we cover the sphere; the distance check below trims).
    half_vox = (
        max(1, int(np.ceil(sphere_radius_mm / spacing[0]))),
        max(1, int(np.ceil(sphere_radius_mm / spacing[1]))),
        max(1, int(np.ceil(sphere_radius_mm / spacing[2]))),
    )
    ki0 = max(0, pk_global[0] - half_vox[2])
    ki1 = min(int(nz) - 1, pk_global[0] + half_vox[2])
    kj0 = max(0, pk_global[1] - half_vox[1])
    kj1 = min(int(ny) - 1, pk_global[1] + half_vox[1])
    kk0 = max(0, pk_global[2] - half_vox[0])
    kk1 = min(int(nx) - 1, pk_global[2] + half_vox[0])
    kernel_block = arr[ki0 : ki1 + 1, kj0 : kj1 + 1, kk0 : kk1 + 1]
    # Distance grid in mm — vectorised, no per-voxel Python loop.
    kk = (np.arange(ki0, ki1 + 1) - pk_global[0]) * spacing[2]
    jj = (np.arange(kj0, kj1 + 1) - pk_global[1]) * spacing[1]
    ii = (np.arange(kk0, kk1 + 1) - pk_global[2]) * spacing[0]
    dist2 = kk[:, None, None] ** 2 + jj[None, :, None] ** 2 + ii[None, None, :] ** 2
    inside = dist2 <= (sphere_radius_mm * sphere_radius_mm)
    peak_1cm3 = float(kernel_block[inside].mean()) if inside.any() else float(vmax_roi)

    # Optional SUV scaling. Pull the factor lazily so we only touch
    # pydicom + S3 when the caller actually asked for it. ``suv_sd`` is
    # ``std * factor`` because the factor is a constant multiplier and
    # ``Var(αX) = α² Var(X)`` ⇒ ``SD(αX) = |α| SD(X)``; PERCIST 1.0 §4.3
    # needs it to compute the measurable-lesion floor
    # ``1.5 × SUVmean + 2 × SUVsd`` over the liver reference sphere.
    suv_mean: float | None = None
    suv_sd: float | None = None
    suv_max: float | None = None
    suv_peak: float | None = None
    suv_variant_used: str | None = None
    units_native: str | None = None
    if body.suv_variant is not None:
        from bvphoenix.services.suv import compute_suv_factors

        # Read one instance header to extract the SUV factors.
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
                    suv_variant_used = body.suv_variant
                    suv_mean = mean * f
                    suv_sd = std * f
                    suv_max = vmax_roi * f
                    suv_peak = peak_1cm3 * f
            except Exception:
                # Non-fatal: the raw stats are still useful even when
                # SUV scaling is unavailable. Keep the fields None.
                pass

    # Release the packed-volume buffer (100-500 MB), its numpy views, and the
    # exclusion mask, then hand the freed pages back to the OS before
    # responding. Otherwise the resident set climbs request-after-request
    # (glibc arena retention) until a later unpack OOMKills the pod.
    del arr, sub_bbox, sub, kernel_block, cached
    if exclusion_full is not None:
        del exclusion_full
    release_memory()

    return ROIStatsOut(
        voxel_count=voxel_count,
        mean=mean,
        std=std,
        min=vmin_roi,
        max=vmax_roi,
        peak_1cm3=peak_1cm3,
        suv_mean=suv_mean,
        suv_sd=suv_sd,
        suv_max=suv_max,
        suv_peak=suv_peak,
        suv_variant_used=suv_variant_used,
        units_native=units_native,
    )


@router.post("/series/{series_id}/roi-stats", response_model=ROIStatsOut)
async def compute_series_roi_stats(
    series_id: uuid.UUID,
    body: ROIStatsIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(optional_user)],
    audit: AuditDep,
) -> ROIStatsOut:
    """Compute deterministic ROI statistics for a series volume.

    Server-side complement to the client-side cornerstone3D stats:
    the viewer label uses the JS computation for instant feedback,
    this endpoint provides the audit-traceable, numpy-precise number
    that ends up in the report. The two should agree to within
    floating-point rounding (no resampling on either side — both read
    the same packed Float32 volume).

    Permission: ``READ_PIXELS`` on the parent study, same gate as
    ``GET /series/{id}/volume.raw`` since the operation reads pixel
    data. Storage isolation preserved: the bucket / S3 key never
    appear in the response.
    """
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

    out = await compute_roi_stats_core(db, series, body)

    await audit.log(
        action="series_roi_stats",
        actor_subject_id=user.subject_id if user else None,
        resource_kind="series",
        resource_id=series.id,
    )
    return out
