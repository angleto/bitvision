"""Per-series display hints for the viewer.

Returns the bits the front-end needs to render pixels faithfully without
re-parsing every DICOM instance client-side: PhotometricInterpretation
(MONOCHROME1 → auto-invert so "high value = bright"), PixelSpacing (for
non-square pixel aspect correction), and the slice dimensions. Values
come from the first DICOM instance of the series — a series is expected
to be uniform in these regards.

This lives alongside ``GET /api/series/{id}/volume.raw`` but is a
separate endpoint so the volume blob's 32-byte header stays
backward-compatible with existing cached derivatives.
"""

from __future__ import annotations

import asyncio
import io
import uuid
from typing import Annotated

import pydicom
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.auth import optional_user
from bvphoenix.db.models import ImagingStudy, Instance, Series, User
from bvphoenix.db.session import get_db
from bvphoenix.services.permissions import READ_PIXELS, can
from bvphoenix.services.suv import compute_suv_factors
from bvphoenix.services.volumes import (
    NON_VOLUMETRIC_SOP_CLASSES,
    _detect_4d,
    read_display_metadata,
)
from bvphoenix.storage import get_s3_storage

router = APIRouter(tags=["display-metadata"])


class DisplayMetadata(BaseModel):
    series_id: uuid.UUID
    photometric_interpretation: str | None
    invert: bool
    pixel_spacing: tuple[float, float]  # [sx, sy] mm — column, row
    rows: int
    columns: int
    # DICOM ImageOrientationPatient (0020,0037) of the first instance.
    # Six floats: row direction cosine [Rx, Ry, Rz] then column
    # direction cosine [Cx, Cy, Cz]. ``None`` when the tag is absent
    # (legacy CR/DX). Drives ``primary_plane`` and lets the frontend
    # request an oblique MPR aligned to the slice plane.
    image_orientation_patient: tuple[float, float, float, float, float, float] | None = None
    # Primary review plane derived from the slice normal:
    # ``axial`` | ``sagittal`` | ``coronal`` | ``oblique`` | ``unknown``.
    # The viewer's hanging-protocol picker reorders the MPR panes so
    # the acquisition plane is the primary cell (a sagittal-acquired
    # spine MR opens with sagittal as the big pane).
    primary_plane: str = "unknown"
    # Number of instances in the series. Single-slice + non-volumetric
    # SOP classes route to the 2D viewer instead of MPR.
    instance_count: int = 0
    # PET-specific extras. Populated only for PT modality series with
    # a complete tag set; ``suv_factor_bw`` of None means "either not
    # PT or insufficient metadata to compute SUV". Frontend uses
    # ``suv = pixel_value_after_rescale * suv_factor_bw`` to convert.
    is_pet: bool = False
    suv_factor_bw: float | None = None
    patient_weight_kg: float | None = None
    radionuclide: str | None = None
    units: str | None = None  # typically "BQML" for PET
    suv_notes: list[str] = []
    # SUV variants per Addendum C §5–§6. ``None`` when the
    # corresponding inputs (PatientSize, PatientSex) are missing or
    # implausible. Frontend picks one from preferences; the chosen
    # factor is multiplied with the rescaled pixel value to obtain
    # the SUV displayed in the HUD / ROI labels.
    suv_factor_lbm_janmahasatian: float | None = None
    suv_factor_lbm_james: float | None = None
    suv_factor_bsa_mosteller: float | None = None
    suv_factor_bsa_dubois: float | None = None
    patient_height_m: float | None = None
    patient_sex: str | None = None
    # Tracer + nuclide metadata (Addendum C §3.4 + §8). ``tracer`` is
    # a canonical short name (FDG / PSMA / DOTATATE / FET / ...);
    # ``branching_ratio`` is the positron yield (0..1) for the
    # detected nuclide. Frontend can use the tracer to pick a default
    # SUV display range.
    tracer: str | None = None
    branching_ratio: float | None = None
    # Non-blocking sanity warnings: implausible weight, dose out of
    # clinical range, decay correction NONE, etc. Surfaced in the
    # PET HUD so the operator double-checks the numbers.
    suv_warnings: list[str] = []
    # DICOM FrameOfReferenceUID (0020,0052). Two series can be fused
    # without a registration step only when their FoR matches; the
    # frontend compares the primary's FoR to the fusion's FoR and
    # raises a warning banner when they differ (spec §1.2).
    frame_of_reference_uid: str | None = None
    # Multi-frame 4D detection (Addendum B §5). The viewer is 3D-only
    # for now; when this flag is set the frontend renders the first
    # time frame and surfaces a banner so the user knows there is
    # additional dynamic data the viewer is not yet exposing.
    is_dynamic_4d: bool = False
    n_time_frames: int = 1


@router.get("/series/{series_id}/display-metadata", response_model=DisplayMetadata)
async def get_display_metadata(
    series_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(optional_user)],
    response: Response,
) -> DisplayMetadata:
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

    # First instance suffices — photometric / pixel spacing are
    # series-wide invariants for the modalities we support.
    instance = (
        await db.execute(
            select(Instance)
            .where(Instance.series_id == series.id)
            .order_by(Instance.instance_number.asc().nullslast())
            .limit(1)
        )
    ).scalar_one_or_none()
    if instance is None:
        raise HTTPException(status_code=409, detail="series has no instances yet")

    storage = get_s3_storage()
    dcm_bytes = await asyncio.to_thread(
        storage.get_object_bytes, bucket=instance.s3_bucket, key=instance.s3_key
    )
    # stop_before_pixels keeps this fast: we only read the header tags.
    ds = await asyncio.to_thread(pydicom.dcmread, io.BytesIO(dcm_bytes), stop_before_pixels=True)
    meta = read_display_metadata(ds)
    frame_of_reference_uid = getattr(ds, "FrameOfReferenceUID", None)
    if frame_of_reference_uid is not None:
        frame_of_reference_uid = str(frame_of_reference_uid)
    is_dynamic_4d, n_time_frames = _detect_4d(ds)
    # Single-slice / non-volumetric series routing hint: when every
    # instance is a SOP class with no z-axis (SC, PR, SR, ...), force
    # the primary plane to "unknown" so the frontend skips MPR setup
    # and goes straight to ``<Series2DViewer>``. The volume endpoint
    # already 404s on this case, so this is purely a UX nudge.
    if instance.sop_class_uid is not None and instance.sop_class_uid in NON_VOLUMETRIC_SOP_CLASSES:
        meta["primary_plane"] = "unknown"

    # PET-specific enrichment. SUV factors come straight from the
    # header tags; we don't touch pixels here. For non-PT modalities
    # the factor stays None.
    is_pet = (series.modality or "").upper() == "PT"
    suv_factor_bw: float | None = None
    patient_weight_kg: float | None = None
    radionuclide: str | None = None
    units: str | None = None
    suv_notes: list[str] = []
    suv_factor_lbm_janmahasatian: float | None = None
    suv_factor_lbm_james: float | None = None
    suv_factor_bsa_mosteller: float | None = None
    suv_factor_bsa_dubois: float | None = None
    patient_height_m: float | None = None
    patient_sex: str | None = None
    tracer: str | None = None
    branching_ratio: float | None = None
    suv_warnings: list[str] = []
    if is_pet:
        factors = compute_suv_factors(ds)
        suv_factor_bw = factors.factor_bw
        patient_weight_kg = factors.patient_weight_kg
        radionuclide = factors.radionuclide
        units = factors.units
        suv_notes = list(factors.notes)
        suv_factor_lbm_janmahasatian = factors.factor_lbm_janmahasatian
        suv_factor_lbm_james = factors.factor_lbm_james
        suv_factor_bsa_mosteller = factors.factor_bsa_mosteller
        suv_factor_bsa_dubois = factors.factor_bsa_dubois
        patient_height_m = factors.patient_height_m
        patient_sex = factors.patient_sex
        tracer = factors.tracer
        branching_ratio = factors.branching_ratio
        suv_warnings = list(factors.warnings)

    # PhotometricInterpretation / PixelSpacing / SUV factors are
    # immutable per series — short cache to absorb HUD repaint flurries.
    response.headers["cache-control"] = "public, max-age=86400"
    return DisplayMetadata(
        series_id=series.id,
        instance_count=int(series.received_instance_count or 0),
        is_pet=is_pet,
        suv_factor_bw=suv_factor_bw,
        patient_weight_kg=patient_weight_kg,
        radionuclide=radionuclide,
        units=units,
        suv_notes=suv_notes,
        suv_factor_lbm_janmahasatian=suv_factor_lbm_janmahasatian,
        suv_factor_lbm_james=suv_factor_lbm_james,
        suv_factor_bsa_mosteller=suv_factor_bsa_mosteller,
        suv_factor_bsa_dubois=suv_factor_bsa_dubois,
        patient_height_m=patient_height_m,
        patient_sex=patient_sex,
        tracer=tracer,
        branching_ratio=branching_ratio,
        suv_warnings=suv_warnings,
        frame_of_reference_uid=frame_of_reference_uid,
        is_dynamic_4d=is_dynamic_4d,
        n_time_frames=n_time_frames,
        **meta,
    )
