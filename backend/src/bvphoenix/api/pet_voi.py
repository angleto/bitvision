"""PET VOI quantification endpoints.

Exposes two POSTs that return SUVmax / SUVpeak / SUVmean / MTV / TLG
on a placed VOI. The frontend POSTs the geometry; we read the packed
volume, run the math, return numbers — no per-VOI persistence (the
client owns the lifecycle until the user signs them as findings).
"""

from __future__ import annotations

import asyncio
import io
import uuid
from typing import Annotated, Literal

import pydicom
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.auth import optional_user
from bvphoenix.config import get_settings
from bvphoenix.db.models import Derivative, ImagingStudy, Instance, Series, User
from bvphoenix.db.session import get_db
from bvphoenix.services.derivative_keys import volume_key
from bvphoenix.services.permissions import READ_PIXELS, can
from bvphoenix.services.pet_voi import (
    compute_voi_spherical,
    compute_voi_threshold,
    parse_volume_blob,
)
from bvphoenix.services.suv import compute_suv_factors
from bvphoenix.services.volumes import (
    DERIVATIVE_FORMAT,
    DERIVATIVE_KIND,
    pack_series,
)
from bvphoenix.storage import get_s3_storage

router = APIRouter(tags=["pet-voi"])


class _CenterMm(BaseModel):
    """3D coordinate in mm (column / row / slice axes, origin at voxel 0)."""

    x: float
    y: float
    z: float


class SphericalVoiIn(BaseModel):
    center_mm: _CenterMm
    radius_mm: float = Field(..., gt=0, le=200)


class ThresholdVoiIn(BaseModel):
    seed_mm: _CenterMm
    threshold: float = Field(..., gt=0)
    threshold_units: Literal["SUV", "raw"] = "SUV"


class VoiMetricsOut(BaseModel):
    suv_max: float
    suv_peak: float | None
    suv_mean: float
    mtv_ml: float
    tlg: float
    voxel_count: int
    units: str  # "SUV" or "raw"
    voi_kind: str
    notes: list[str]
    # Echoed for the client UI: useful when the page mounts on top of
    # a stale display-metadata snapshot.
    suv_factor_bw_used: float | None


async def load_pet_volume(
    db: AsyncSession,
    series_id: uuid.UUID,
    user: User | None,
) -> tuple[Series, ImagingStudy, bytes, dict]:
    """Resolve series + permission + fetch packed volume + SUV factors.

    Returns the parsed series row, parent study, the raw volume bytes
    (matches the format ``/api/series/{id}/volume.raw`` would emit),
    and a dict with the SUV factors computed from the first instance's
    DICOM header.
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

    # Re-use the packed volume the 3D viewer already builds. Look it up
    # via the ``derivatives`` row (the worker / volume.raw endpoint write
    # the actual s3 location there); fall back to an inline pack if the
    # derivative is missing so the VOI endpoint works even when the user
    # never opened the 3D view.
    storage = get_s3_storage()
    settings = get_settings()
    derivative = (
        await db.execute(
            select(Derivative).where(
                Derivative.series_id == series.id,
                Derivative.kind == DERIVATIVE_KIND,
                Derivative.format == DERIVATIVE_FORMAT,
            )
        )
    ).scalar_one_or_none()

    if derivative is not None:
        try:
            payload = await asyncio.to_thread(
                storage.get_object_bytes,
                bucket=derivative.s3_bucket,
                key=derivative.s3_key,
            )
        except Exception as exc:
            raise HTTPException(
                status_code=409,
                detail="packed volume blob unreachable in storage",
            ) from exc
    else:
        instance_rows = (
            await db.execute(
                select(Instance.s3_bucket, Instance.s3_key)
                .where(Instance.series_id == series.id)
                .order_by(Instance.instance_number.asc().nullslast())
            )
        ).all()
        if not instance_rows:
            raise HTTPException(status_code=409, detail="series has no instances yet")
        try:
            packed = await asyncio.to_thread(
                pack_series,
                storage=storage,
                instance_entries=[(b, k) for b, k in instance_rows],
            )
        except Exception as exc:
            raise HTTPException(
                status_code=409,
                detail=f"unable to pack series volume: {exc}",
            ) from exc
        cache_key = volume_key(patient_id=study.patient_id, series_id=series.id)
        await asyncio.to_thread(
            storage.upload_bytes,
            packed.bytes_,
            bucket=settings.s3_bucket_derivatives,
            key=cache_key,
        )
        db.add(
            Derivative(
                series_id=series.id,
                kind=DERIVATIVE_KIND,
                format=DERIVATIVE_FORMAT,
                s3_bucket=settings.s3_bucket_derivatives,
                s3_key=cache_key,
                size_bytes=packed.size,
                generator_version="pack_series-v1",
                geometry=packed.geometry,
            )
        )
        await db.commit()
        payload = packed.bytes_

    # SUV factors come from the first DICOM instance's header tags.
    instance = (
        await db.execute(
            select(Instance)
            .where(Instance.series_id == series.id)
            .order_by(Instance.instance_number.asc().nullslast())
            .limit(1)
        )
    ).scalar_one_or_none()
    factors_dict: dict = {"factor_bw": None}
    if instance is not None:
        try:
            dcm_bytes = await asyncio.to_thread(
                storage.get_object_bytes,
                bucket=instance.s3_bucket,
                key=instance.s3_key,
            )
            ds = await asyncio.to_thread(
                pydicom.dcmread, io.BytesIO(dcm_bytes), stop_before_pixels=True
            )
            factors = compute_suv_factors(ds)
            factors_dict = {
                "factor_bw": factors.factor_bw,
                "patient_weight_kg": factors.patient_weight_kg,
                "radionuclide": factors.radionuclide,
                "units": factors.units,
            }
        except Exception:
            pass

    return series, study, payload, factors_dict


@router.post("/series/{series_id}/voi/spherical", response_model=VoiMetricsOut)
async def voi_spherical(
    request: Request,
    series_id: uuid.UUID,
    body: SphericalVoiIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(optional_user)],
) -> VoiMetricsOut:
    del request
    _series, _study, payload, factors = await load_pet_volume(db, series_id, user)
    blob = parse_volume_blob(payload)
    metrics = compute_voi_spherical(
        blob,
        center_mm=(body.center_mm.x, body.center_mm.y, body.center_mm.z),
        radius_mm=body.radius_mm,
        suv_factor_bw=factors.get("factor_bw"),
    )
    return VoiMetricsOut(
        **metrics.__dict__,
        suv_factor_bw_used=factors.get("factor_bw"),
    )


@router.post("/series/{series_id}/voi/threshold", response_model=VoiMetricsOut)
async def voi_threshold(
    request: Request,
    series_id: uuid.UUID,
    body: ThresholdVoiIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(optional_user)],
) -> VoiMetricsOut:
    del request
    _series, _study, payload, factors = await load_pet_volume(db, series_id, user)
    blob = parse_volume_blob(payload)
    metrics = compute_voi_threshold(
        blob,
        seed_mm=(body.seed_mm.x, body.seed_mm.y, body.seed_mm.z),
        threshold_value=body.threshold,
        threshold_units=body.threshold_units,
        suv_factor_bw=factors.get("factor_bw"),
    )
    return VoiMetricsOut(
        **metrics.__dict__,
        suv_factor_bw_used=factors.get("factor_bw"),
    )
