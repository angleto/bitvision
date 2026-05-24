# ruff: noqa: F405
# Auto-split from api/studies.py on 2026-05-21.
# Section: ``pet``.

from __future__ import annotations

from fastapi import APIRouter

from bvphoenix.api.studies import _shared
from bvphoenix.api.studies._shared import *  # noqa: F403

router = APIRouter()


@router.get(
    "/series/{series_id}/suv",
    response_model=SUVOut,
)
async def get_series_suv(
    series_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(optional_user)],
    audit: AuditDep,
) -> SUVOut:
    """Return SUV scaling factors for a PET series.

    Sprint 6 (P2): wraps :func:`bvphoenix.services.suv.compute_suv_factors`
    on the first instance of the series. Non-PET modalities return
    ``is_pet=false`` with all factor fields ``None``. PET series with
    incomplete metadata (missing weight / dose / half-life) raise 422
    ``suv_unavailable`` with the missing-tag list.
    """
    from bvphoenix.middleware.problem_details import problem as _problem
    from bvphoenix.services.suv import compute_suv_factors

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

    is_pet = (series.modality or "").upper() == "PT"

    instances = (
        (
            await db.execute(
                select(Instance)
                .where(Instance.series_id == series.id)
                .order_by(Instance.instance_number.asc().nullslast())
                .limit(1)
            )
        )
        .scalars()
        .all()
    )
    if not instances:
        raise _problem(409, "conflict", "series has no instances")
    inst = instances[0]

    storage = get_s3_storage()
    dcm_bytes = await asyncio.to_thread(
        storage.get_object_bytes, bucket=inst.s3_bucket, key=inst.s3_key
    )
    ds = await asyncio.to_thread(pydicom.dcmread, io.BytesIO(dcm_bytes), stop_before_pixels=True)
    factors = await asyncio.to_thread(compute_suv_factors, ds)

    if is_pet and factors.factor_bw is None:
        missing: list[str] = []
        if factors.patient_weight_kg is None:
            missing.append("PatientWeight")
        if factors.injected_dose_bq is None:
            missing.append("RadionuclideTotalDose")
        if factors.half_life_s is None:
            missing.append("RadionuclideHalfLife")
        if factors.units is None:
            missing.append("Units")
        raise _problem(
            422,
            "suv_unavailable",
            "PET series lacks the metadata required to compute SUV",
            extra={
                "missing_fields": missing or ["unknown"],
                "notes": list(factors.notes),
            },
        )

    await audit.log(
        action="series_suv_read",
        actor_subject_id=user.subject_id if user else None,
        resource_kind="series",
        resource_id=series.id,
        metadata={"is_pet": is_pet},
    )

    return SUVOut(
        series_id=str(series.id),
        is_pet=is_pet,
        suv_factor_bw=factors.factor_bw,
        suv_factor_lbm_janmahasatian=factors.factor_lbm_janmahasatian,
        suv_factor_lbm_james=factors.factor_lbm_james,
        suv_factor_bsa_mosteller=factors.factor_bsa_mosteller,
        suv_factor_bsa_dubois=factors.factor_bsa_dubois,
        patient_weight_kg=factors.patient_weight_kg,
        patient_height_m=factors.patient_height_m,
        patient_sex=factors.patient_sex,
        radionuclide=factors.radionuclide,
        tracer=factors.tracer,
        branching_ratio=factors.branching_ratio,
        half_life_s=factors.half_life_s,
        injected_dose_bq=factors.injected_dose_bq,
        decay_corrected_dose_bq=factors.decay_corrected_dose_bq,
        delta_t_s=factors.delta_t_s,
        units=factors.units,
        notes=list(factors.notes),
        warnings=list(factors.warnings),
    )


@router.post(
    "/series/{series_id}/measure/distance",
    response_model=dict,
)
async def measure_series_distance(
    series_id: uuid.UUID,
    body: MeasureDistanceIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(optional_user)],
    audit: AuditDep,
) -> dict:
    """Euclidean distance between two pixel-space points in mm.

    422 ``measurement_unavailable`` when the DICOM metadata lacks
    ``PixelSpacing`` or any slice-thickness derivable spacing.
    """
    from bvphoenix.middleware.problem_details import problem as _problem
    from bvphoenix.services.measurements import (
        MissingSpacingError,
        compute_distance,
        spacing_from_meta,
    )

    series, study, meta = await _meta_for_series(db, series_id, user)
    try:
        spacing = spacing_from_meta(meta)
    except MissingSpacingError as exc:
        raise _problem(
            422,
            "measurement_unavailable",
            "DICOM metadata is too sparse to compute a mm distance",
            extra={"missing_fields": exc.missing},
        ) from exc

    result = compute_distance(
        (body.a.i, body.a.j, body.a.k),
        (body.b.i, body.b.j, body.b.k),
        spacing,
    )
    result["series_id"] = str(series.id)

    await audit.log(
        action="series_measure_distance",
        actor_subject_id=user.subject_id if user else None,
        resource_kind="series",
        resource_id=series.id,
        metadata={
            "study_id": str(study.id),
            "distance_mm": result["distance_mm"],
        },
    )
    return result


@router.post(
    "/series/{series_id}/measure/volume",
    response_model=dict,
)
async def measure_series_volume(
    series_id: uuid.UUID,
    body: MeasureVolumeIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(optional_user)],
    audit: AuditDep,
) -> dict:
    """Axis-aligned bounding-box volume in mm^3 + ml."""
    from bvphoenix.middleware.problem_details import problem as _problem
    from bvphoenix.services.measurements import (
        MissingSpacingError,
        compute_volume,
        spacing_from_meta,
    )

    series, study, meta = await _meta_for_series(db, series_id, user)
    try:
        spacing = spacing_from_meta(meta)
    except MissingSpacingError as exc:
        raise _problem(
            422,
            "measurement_unavailable",
            "DICOM metadata is too sparse to compute a mm^3 volume",
            extra={"missing_fields": exc.missing},
        ) from exc

    result = compute_volume(
        (body.p0.i, body.p0.j, body.p0.k),
        (body.p1.i, body.p1.j, body.p1.k),
        spacing,
    )
    result["series_id"] = str(series.id)

    await audit.log(
        action="series_measure_volume",
        actor_subject_id=user.subject_id if user else None,
        resource_kind="series",
        resource_id=series.id,
        metadata={"study_id": str(study.id), "volume_mm3": result["volume_mm3"]},
    )
    return result
