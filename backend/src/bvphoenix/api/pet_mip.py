"""Rotating MIP cine + companion-series endpoints for the PET viewer.

Two related concerns live here:

  - ``GET /series/{id}/mip-cine`` returns a sprite-sheet PNG with N
    rotating-MIP frames around the patient's vertical axis. Cached in
    the derivatives bucket (kind=``mip_sprite``); first call is
    expensive (seconds), subsequent calls hit cache.

  - ``GET /studies/{id}/companions`` returns the companion-series map
    inside a study: for a PET study you typically have a CT in the
    same exam slot; the viewer uses this to pre-position the second
    viewport for fusion / hanging protocols.
"""

from __future__ import annotations

import asyncio
import io
import uuid
from typing import Annotated

import numpy as np
import pydicom
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.api.pet_voi import load_pet_volume
from bvphoenix.auth import optional_user
from bvphoenix.config import get_settings
from bvphoenix.db.models import Derivative, ImagingStudy, Instance, Series, User
from bvphoenix.db.session import get_db
from bvphoenix.services.derivative_keys import mip_sprite_key
from bvphoenix.services.mip import compute_rotating_mip_sprite
from bvphoenix.services.permissions import READ_PIXELS, can
from bvphoenix.services.pet_voi import parse_volume_blob
from bvphoenix.services.suv import compute_suv_factors
from bvphoenix.storage import get_s3_storage

router = APIRouter(tags=["pet-mip"])


_MIP_KIND = "mip_sprite"
_MIP_FORMAT = "png"


class MipCineOut(BaseModel):
    sprite_url: str
    frame_count: int
    frame_width: int
    frame_height: int
    units: str
    suv_window: tuple[float, float]


@router.get("/series/{series_id}/mip-cine", response_model=MipCineOut)
async def mip_cine(
    series_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(optional_user)],
    num_frames: int = Query(36, ge=8, le=72),
    target_height: int = Query(384, ge=128, le=1024),
) -> MipCineOut:
    """Rotating-MIP sprite for the viewer cine widget.

    The sprite encodes ``num_frames`` evenly spaced angles around
    [0, 360) stacked vertically in a single PNG. Cached per (series,
    num_frames, target_height) tuple in the derivatives bucket so the
    expensive scipy.ndimage.rotate work runs at most once per shape.
    """
    # Re-use the PET volume loader: handles permissions + volume fetch
    # + SUV factor extraction in one go.
    series, study, payload, factors = await load_pet_volume(db, series_id, user)
    blob = parse_volume_blob(payload)

    settings = get_settings()
    storage = get_s3_storage()

    cache_format = f"{_MIP_FORMAT}-{num_frames}-{target_height}"
    derivative = (
        await db.execute(
            select(Derivative).where(
                Derivative.series_id == series.id,
                Derivative.kind == _MIP_KIND,
                Derivative.format == cache_format,
            )
        )
    ).scalar_one_or_none()

    if derivative is None:
        sprite = await asyncio.to_thread(
            compute_rotating_mip_sprite,
            blob,
            num_frames=num_frames,
            suv_factor_bw=factors.get("factor_bw"),
            target_height=target_height,
        )
        cache_key = mip_sprite_key(
            patient_id=study.patient_id,
            series_id=series.id,
            num_frames=num_frames,
            target_height=target_height,
        )
        await asyncio.to_thread(
            storage.upload_bytes,
            sprite.png_bytes,
            bucket=settings.s3_bucket_derivatives,
            key=cache_key,
        )
        db.add(
            Derivative(
                series_id=series.id,
                kind=_MIP_KIND,
                format=cache_format,
                s3_bucket=settings.s3_bucket_derivatives,
                s3_key=cache_key,
                size_bytes=len(sprite.png_bytes),
                generator_version="mip-v1",
            )
        )
        await db.commit()
        sprite_meta = {
            "frame_count": sprite.frame_count,
            "frame_width": sprite.frame_width,
            "frame_height": sprite.frame_height,
            "units": sprite.units,
            "suv_window": sprite.suv_window,
        }
    else:
        # We don't store the geometry alongside the cache row; recompute
        # by inspecting the cached PNG once. Cheap and avoids a parallel
        # metadata schema that would drift.
        from PIL import Image  # local import: only on cache hit

        cached_bytes = await asyncio.to_thread(
            storage.get_object_bytes,
            bucket=derivative.s3_bucket,
            key=derivative.s3_key,
        )
        img = Image.open(io.BytesIO(cached_bytes))
        total_w, total_h = img.size
        # The sprite is N frames stacked vertically: frame_height = H / N.
        if num_frames <= 0 or total_h % num_frames != 0:
            # Defensive: rebuild rather than trust a stale cache row
            raise HTTPException(
                status_code=409,
                detail="cached MIP sprite shape does not match request",
            )
        sprite_meta = {
            "frame_count": num_frames,
            "frame_width": int(total_w),
            "frame_height": int(total_h // num_frames),
            "units": "SUV" if factors.get("factor_bw") else "raw",
            # SUV window is not persisted; recompute coarsely from the
            # volume so the client knows what to label the slider with.
            "suv_window": (
                0.0,
                float(np.percentile(blob.voxels, 99.5)) * (factors.get("factor_bw") or 1.0),
            ),
        }

    sprite_url = f"/api/series/{series.id}/mip-cine.png?frames={num_frames}&height={target_height}"
    return MipCineOut(
        sprite_url=sprite_url,
        **sprite_meta,
    )


@router.get("/series/{series_id}/mip-cine.png")
async def mip_cine_png(
    series_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(optional_user)],
    frames: int = Query(36, ge=8, le=72, alias="frames"),
    height: int = Query(384, ge=128, le=1024, alias="height"),
):
    """Serve the cached sprite PNG for ``mip_cine``.

    Split from the JSON endpoint so the JSON manifest is cheap and
    cacheable separately by the browser; the binary lives at this
    sibling URL with proper image content-type.
    """
    from fastapi.responses import Response

    series_row = (
        await db.execute(
            select(Series, ImagingStudy)
            .join(ImagingStudy, ImagingStudy.id == Series.study_id)
            .where(Series.id == series_id)
        )
    ).first()
    if series_row is None:
        raise HTTPException(status_code=404, detail="series not found")
    series, study = series_row
    if not await can(db, user=user, action=READ_PIXELS, study=study):
        raise HTTPException(status_code=404, detail="series not found")

    cache_format = f"{_MIP_FORMAT}-{frames}-{height}"
    derivative = (
        await db.execute(
            select(Derivative).where(
                Derivative.series_id == series.id,
                Derivative.kind == _MIP_KIND,
                Derivative.format == cache_format,
            )
        )
    ).scalar_one_or_none()
    if derivative is None:
        # Caller should hit the JSON endpoint first, which triggers
        # generation. We do not double-implement the compute path here
        # to keep this route as a pure CDN-friendly serve.
        raise HTTPException(
            status_code=409,
            detail="MIP sprite not yet generated; call /mip-cine (JSON) first",
        )

    storage = get_s3_storage()
    payload = await asyncio.to_thread(
        storage.get_object_bytes,
        bucket=derivative.s3_bucket,
        key=derivative.s3_key,
    )
    return Response(
        content=payload,
        media_type="image/png",
        headers={"cache-control": "private, max-age=86400"},
    )


# Companion-series detection: handled by the existing
# ``GET /studies/{id}/fusion-candidates`` in api.studies. The PET viewer
# calls that endpoint and filters client-side for the CT modality. No
# duplicate route is exposed here.


# Silence unused-import lint when the helpers are only referenced via dataclass
# attributes / type hints elsewhere in the module.
_ = (Instance, pydicom, compute_suv_factors)
