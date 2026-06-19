"""Pathology / WSI HTTP surface (Step 1).

Two read endpoints + a list endpoint, all gated on the same visibility
check as imaging studies (own + grants + OpenData). Step 1 does *not*
ship the tile-pyramid viewer (DZI / IIIF / DICOMweb WSI) — those are
Step 2 of ``docs/pathology_wsi_spike.md``. Here we serve only the
pre-generated thumbnail + macro JPEGs so the patient page can render
a card.

Storage isolation per memory ``feedback-storage-isolation``: clients
never see S3 keys or bucket names. Reads stream the JPEG bytes
through this API.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.auth import optional_user
from bvphoenix.db.models import PathologySlide, User
from bvphoenix.db.session import get_db
from bvphoenix.services.permissions import (
    platform_owner_subject_id,
)
from bvphoenix.services.wsi_tiles import get_dzi_xml, get_tile_jpeg
from bvphoenix.storage import get_s3_storage

router = APIRouter(tags=["pathology"])


class PathologySlideOut(BaseModel):
    """Public projection of ``pathology_slides``.

    Mirrors the StudyOut shape: license / provenance / is_opendata
    bubble up so the frontend can render the same chips it already
    uses on imaging study cards.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    patient_id: uuid.UUID
    clinical_event_id: uuid.UUID | None = None
    slide_instance_uid: str
    block_label: str | None = None
    slide_label: str | None = None
    stain: str | None = None
    scanner_make: str | None = None
    scanner_model: str | None = None
    magnification: float | None = None
    mpp_x: float | None = None
    mpp_y: float | None = None
    base_width: int | None = None
    base_height: int | None = None
    pyramid_levels: int | None = None
    source_format: str
    contribution_tier: str
    is_public: bool
    ingestion_complete: bool
    label_redacted: bool
    has_macro: bool = False
    source_collection: str | None = None
    license_spdx: str | None = None
    license_url: str | None = None
    citation_required: bool = False
    citation_text: str | None = None
    # See StudyOut.commercial_use_allowed — False when license_spdx carries
    # a NonCommercial (-NC) clause.
    commercial_use_allowed: bool = True
    is_opendata: bool = False
    created_at: datetime

    @classmethod
    def model_validate(cls, obj, *args, **kwargs):  # type: ignore[override]
        from bvphoenix.services.licensing import license_allows_commercial_use

        out = super().model_validate(obj, *args, **kwargs)
        owner = getattr(obj, "owner_subject_id", None)
        if owner is not None:
            try:
                out.is_opendata = owner == platform_owner_subject_id()
            except Exception:
                out.is_opendata = False
        out.has_macro = getattr(obj, "s3_macro_key", None) is not None
        out.commercial_use_allowed = license_allows_commercial_use(out.license_spdx)
        return out


async def _load_visible_slide(
    db: AsyncSession, *, slide_id: uuid.UUID, user: User | None
) -> PathologySlide:
    """Fetch a slide and run the same visibility OR as imaging studies.

    Read-only path (no mutation), so this is the only check; the
    write surface (Step 2+) will gate on a richer permissions check.
    """
    row = (
        await db.execute(select(PathologySlide).where(PathologySlide.id == slide_id))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="slide not found")

    # Anonymous = only is_public.
    if user is None:
        if not row.is_public:
            raise HTTPException(status_code=404, detail="slide not found")
        return row
    # Authenticated: public OR OpenData OR own. Grants / shares for
    # pathology are out of scope for Step 1 and will be added when the
    # viewer ships in Step 2.
    if row.is_public or row.owner_subject_id == platform_owner_subject_id():
        return row
    if getattr(user, "is_admin", False) or row.owner_subject_id == user.subject_id:
        return row
    raise HTTPException(status_code=404, detail="slide not found")


@router.get("/pathology-slides", response_model=list[PathologySlideOut])
async def list_pathology_slides(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(optional_user)],
    public_only: bool = True,
    limit: Annotated[int, Query(ge=1, le=200)] = 60,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[PathologySlideOut]:
    """List ingested slides, newest first, applying the same visibility OR
    as the single-slide read. ``public_only`` (the default) drives the
    OpenData public-pathology library grid; anonymous callers are always
    restricted to public slides regardless of the flag.
    """
    stmt = select(PathologySlide).where(PathologySlide.ingestion_complete.is_(True))
    if public_only or user is None:
        stmt = stmt.where(PathologySlide.is_public.is_(True))
    elif getattr(user, "is_admin", False):
        pass  # admin sees every ingested slide
    else:
        owner = platform_owner_subject_id()
        stmt = stmt.where(
            or_(
                PathologySlide.is_public.is_(True),
                PathologySlide.owner_subject_id == owner,
                PathologySlide.owner_subject_id == user.subject_id,
            )
        )
    stmt = stmt.order_by(PathologySlide.created_at.desc()).limit(limit).offset(offset)
    rows = (await db.execute(stmt)).scalars().all()
    return [PathologySlideOut.model_validate(r) for r in rows]


@router.get("/pathology-slides/{slide_id}", response_model=PathologySlideOut)
async def get_pathology_slide(
    slide_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(optional_user)],
) -> PathologySlideOut:
    slide = await _load_visible_slide(db, slide_id=slide_id, user=user)
    return PathologySlideOut.model_validate(slide)


@router.get("/pathology-slides/{slide_id}/dzi")
async def get_pathology_slide_dzi(
    slide_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(optional_user)],
) -> Response:
    """Deep Zoom (.dzi) XML descriptor for the OpenSeadragon viewer.

    The first call for a slide downloads the source from S3 into the
    bounded tile cache (see ``services.wsi_tiles``); subsequent tile
    requests reuse the open handle. Run in a threadpool so the blocking
    OpenSlide / S3 work does not stall the event loop.
    """
    slide = await _load_visible_slide(db, slide_id=slide_id, user=user)
    if not slide.s3_source_key:
        raise HTTPException(status_code=404, detail="slide source not available")
    xml = await run_in_threadpool(get_dzi_xml, slide)
    return Response(
        content=xml,
        media_type="application/xml",
        headers={"Cache-Control": "private, max-age=86400"},
    )


@router.get("/pathology-slides/{slide_id}/tiles/{level}/{col}/{row}")
async def get_pathology_slide_tile(
    slide_id: uuid.UUID,
    level: int,
    col: int,
    row: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(optional_user)],
) -> Response:
    """Stream one Deep Zoom JPEG tile (visibility-gated, S3-isolated)."""
    slide = await _load_visible_slide(db, slide_id=slide_id, user=user)
    if not slide.s3_source_key:
        raise HTTPException(status_code=404, detail="slide source not available")
    try:
        body = await run_in_threadpool(get_tile_jpeg, slide, level=level, col=col, row=row)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="tile out of range") from exc
    return Response(
        content=body,
        media_type="image/jpeg",
        headers={"Cache-Control": "private, max-age=86400, immutable"},
    )


@router.get("/pathology-slides/{slide_id}/thumbnail")
async def get_pathology_slide_thumbnail(
    slide_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(optional_user)],
) -> Response:
    """Stream the pre-generated thumbnail JPEG.

    The bytes were rendered at ingest time (see
    ``services.wsi_deid.generate_thumbnail_jpeg``) so this endpoint
    is a thin S3 GET; no on-the-fly pyvips call. Returns 404 when the
    slide exists but the ingestion did not produce a thumbnail (e.g.
    aborted half-way).
    """
    slide = await _load_visible_slide(db, slide_id=slide_id, user=user)
    if not slide.s3_thumbnail_key:
        raise HTTPException(status_code=404, detail="thumbnail not generated")
    storage = get_s3_storage()
    body = storage.get_object_bytes(bucket=slide.s3_bucket, key=slide.s3_thumbnail_key)
    return Response(
        content=body,
        media_type="image/jpeg",
        # 1 day immutable cache: thumbnails are content-addressed via
        # slide_id, never regenerated in place.
        headers={"Cache-Control": "private, max-age=86400, immutable"},
    )


@router.get("/pathology-slides/{slide_id}/macro")
async def get_pathology_slide_macro(
    slide_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(optional_user)],
) -> Response:
    """Stream the macro overview JPEG, if the scanner embedded one."""
    slide = await _load_visible_slide(db, slide_id=slide_id, user=user)
    if not slide.s3_macro_key:
        raise HTTPException(status_code=404, detail="no macro image for this slide")
    storage = get_s3_storage()
    body = storage.get_object_bytes(bucket=slide.s3_bucket, key=slide.s3_macro_key)
    return Response(
        content=body,
        media_type="image/jpeg",
        headers={"Cache-Control": "private, max-age=86400, immutable"},
    )
