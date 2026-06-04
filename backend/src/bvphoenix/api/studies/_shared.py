"""Studies / Series / Instance read API.

Every list / detail endpoint resolves visibility through
``services.permissions``. Anonymous traffic still works — DESIGN.md §2
makes anonymous-first non-negotiable — but only sees public studies.

"""

from __future__ import annotations

import asyncio
import gzip
import io
import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal

import pydicom
from arq import create_pool
from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.api._http import content_disposition as _content_disposition
from bvphoenix.api._schemas import (
    InstanceOut,
    PaginatedStudies,
    SeriesOut,
    StudyDetailOut,
    StudyOut,
)
from bvphoenix.auth import (
    active_share_grant,
    enforce_agent_scope,
    optional_user,
    require_scope_if_agent,
    require_user,
)
from bvphoenix.config import get_settings
from bvphoenix.db.models import Derivative, Grant, ImagingStudy, Instance, Series, User
from bvphoenix.db.session import get_db
from bvphoenix.middleware.audit_dependency import AuditDep
from bvphoenix.services.arq_redis import redis_settings
from bvphoenix.services.consent_auto import (
    ensure_tier_consents,
    revoke_tier_consent_for_study,
)
from bvphoenix.services.deidentify import deidentify_dicom_bytes, should_deidentify
from bvphoenix.services.derivative_keys import (
    volume_earl_key,
    volume_key,
    volume_preview_key,
    volume_stack_earl_key,
    volume_stack_key,
)
from bvphoenix.services.dicom_documents import (
    UnsupportedDocumentError,
    read_dicom_document,
)
from bvphoenix.services.embeddable import is_embeddable_modality
from bvphoenix.services.permissions import (
    DOWNLOAD_DICOM,
    READ_METADATA,
    READ_PIXELS,
    WRITE_REPORT,
    can,
    can_patient,
    visible_studies_filter,
)
from bvphoenix.services.thumbnails import (
    NoPixelDataError,
    dicom_to_jpeg,
    is_image_sop_class,
    read_dicom_wc_ww,
)
from bvphoenix.services.volumes import (
    DERIVATIVE_FORMAT,
    DERIVATIVE_KIND,
    DERIVATIVE_KIND_PREVIEW,
    HEADER_STRUCT,
    NON_VOLUMETRIC_SOP_CLASSES,
    NonVolumetricSeriesError,
    PackedVolume,
    apply_earl_harmonization,
    pack_low_res,
    pack_series,
)
from bvphoenix.storage import get_s3_storage
from bvphoenix.storage.s3 import S3Storage

_AGENT_PATIENT_IMAGES = Depends(require_scope_if_agent("patient:images"))


_AGENT_PATIENT_READ = Depends(require_scope_if_agent("patient:read"))


router = APIRouter(tags=["studies"])


_STREAM_THRESHOLD_BYTES = 20 * 1024 * 1024


_STREAM_CHUNK_BYTES = 1 * 1024 * 1024  # 1 MB per chunk


def _client_accepts_gzip(request: Request) -> bool:
    """True if the caller sent an ``Accept-Encoding`` containing gzip."""
    accept = request.headers.get("accept-encoding", "")
    return "gzip" in accept.lower()


def _chunked_iter(data: bytes, chunk: int = _STREAM_CHUNK_BYTES):
    """Yield ``data`` in fixed-size chunks for a StreamingResponse.

    ``memoryview`` slices don't copy, so we only allocate when converting
    each piece to bytes for the wire.
    """
    view = memoryview(data)
    for offset in range(0, len(view), chunk):
        yield bytes(view[offset : offset + chunk])


def _geometry_headers(geometry: dict | None) -> dict[str, str]:
    """Encode a packed volume's patient-space geometry as ``X-Volume-*``
    response headers.

    The 32-byte blob header is frozen for backward compat, so the real
    DICOM origin / direction cosines / FrameOfReferenceUID travel
    out-of-band here. The viewer reads them in ``fetchVolume`` and builds
    its Cornerstone volume in true LPS space (see
    ``services.volumes.compute_volume_geometry``). Empty dict when the
    geometry is absent or partial — the viewer keeps its identity-frame
    fallback. Floats are joined with ``,`` so the client splits and
    ``parseFloat``s them; these are non-PHI scanner geometry numbers.
    """
    if not geometry:
        return {}
    headers: dict[str, str] = {}
    origin = geometry.get("origin")
    direction = geometry.get("direction")
    for_uid = geometry.get("frame_of_reference_uid")
    if isinstance(origin, (list, tuple)) and len(origin) == 3:
        headers["x-volume-origin"] = ",".join(repr(float(v)) for v in origin)
    if isinstance(direction, (list, tuple)) and len(direction) == 9:
        headers["x-volume-direction"] = ",".join(repr(float(v)) for v in direction)
    if for_uid:
        headers["x-volume-frame-of-reference"] = str(for_uid)
    return headers


def _volume_response(data: bytes, *, accept_gzip: bool, geometry: dict | None = None) -> Response:
    """Serve a volume blob with optional gzip + chunked streaming.

    - If the client accepts gzip, compress with level 1 (fast; typical
      ratio on Float32 medical volumes is ~1.8x — the speed is worth more
      than the extra few percent of level 6).
    - If the resulting payload is larger than ``_STREAM_THRESHOLD_BYTES``,
      return a StreamingResponse so we don't hold the full blob in a
      second buffer inside Starlette.
    - ``geometry`` (when present) is attached as ``X-Volume-*`` headers so
      the viewer builds its volume in true patient space.
    """
    headers = {"cache-control": "private, max-age=3600"}
    headers.update(_geometry_headers(geometry))
    if accept_gzip:
        data = gzip.compress(data, compresslevel=1)
        headers["content-encoding"] = "gzip"
    headers["content-length"] = str(len(data))

    if len(data) > _STREAM_THRESHOLD_BYTES:
        return StreamingResponse(
            _chunked_iter(data),
            media_type="application/octet-stream",
            headers=headers,
        )
    return Response(content=data, media_type="application/octet-stream", headers=headers)


class TierChangeIn(BaseModel):
    tier: str = Field(description="New contribution tier. Must be one of t1, t2, t3, t4.")


class TierChangeOut(BaseModel):
    study_id: str
    old_tier: str
    new_tier: str
    reindex_enqueued: bool = Field(
        description=(
            "True when the transition triggered a de-identify / reindex job "
            "(T1/T2 -> T3/T4). Consumers can poll the study to see the new "
            "contribution_tier and the derivatives refresh status."
        ),
    )


_VALID_TIERS = ("t1", "t2", "t3", "t4")


_QUOTA_TIERS = ("t1", "t2")


_COMMONS_TIERS = ("t3", "t4")


async def _enqueue_tier_reindex(study_id: uuid.UUID) -> bool:
    """Enqueue the worker that re-applies de-identification + refreshes
    derivatives after a tier upgrade. Returns True on successful
    enqueue; False if Redis is down (non-fatal — the worker can be
    triggered manually from the admin CLI)."""
    try:
        settings = get_settings()
        redis = await create_pool(redis_settings(settings.redis_url))
        await redis.enqueue_job("deidentify_reindex_study", str(study_id))
        await redis.close()
        return True
    except Exception:
        return False


class ConsentRevokeIn(BaseModel):
    reason: str | None = Field(
        default=None,
        max_length=2000,
        description="Optional free-text captured on the revoked consent row.",
    )


class ConsentRevokeOut(BaseModel):
    study_id: str
    old_tier: str
    new_tier: str
    consent_rows_updated: int


def _suggested_wl_from_middle_instance(
    storage: S3Storage, bucket: str, key: str
) -> tuple[float, float] | None:
    """Stream just the header of one DICOM and read WC/WW.

    ``stop_before_pixels=True`` skips the pixel blob — we only want the
    window-level tags here, unlike :func:`dicom_to_jpeg` which needs
    the pixels.
    """
    dcm_bytes = storage.get_object_bytes(bucket=bucket, key=key)
    ds = pydicom.dcmread(io.BytesIO(dcm_bytes), stop_before_pixels=True)
    return read_dicom_wc_ww(ds)


_STUDY_EDITABLE = {"study_description"}


_SERIES_EDITABLE = {"series_description", "body_part_examined", "modality_corrected"}


class StudyMetadataPatchIn(BaseModel):
    study_description: str | None = Field(default=None, max_length=2048)


class SeriesMetadataPatchIn(BaseModel):
    series_description: str | None = Field(default=None, max_length=2048)
    body_part_examined: str | None = Field(default=None, max_length=64)
    # Override the DICOM-acquired modality (e.g. "MR" -> "PT/MR" for a
    # PET-MR fusion). The DICOM authoritative ``modality`` column is
    # not touched; we expose this through a tag so search picks it up.
    modality_corrected: str | None = Field(default=None, max_length=16)


class SUVOut(BaseModel):
    series_id: str
    is_pet: bool
    suv_factor_bw: float | None = None
    suv_factor_lbm_janmahasatian: float | None = None
    suv_factor_lbm_james: float | None = None
    suv_factor_bsa_mosteller: float | None = None
    suv_factor_bsa_dubois: float | None = None
    patient_weight_kg: float | None = None
    patient_height_m: float | None = None
    patient_sex: str | None = None
    radionuclide: str | None = None
    tracer: str | None = None
    branching_ratio: float | None = None
    half_life_s: float | None = None
    injected_dose_bq: float | None = None
    decay_corrected_dose_bq: float | None = None
    delta_t_s: float | None = None
    units: str | None = None
    notes: list[str] = []
    warnings: list[str] = []


class _Point3DIn(BaseModel):
    i: float = Field(description="Column / x in pixel space.")
    j: float = Field(description="Row / y in pixel space.")
    k: float = Field(description="Slice index / z.")


class MeasureDistanceIn(BaseModel):
    a: _Point3DIn
    b: _Point3DIn


class MeasureVolumeIn(BaseModel):
    p0: _Point3DIn
    p1: _Point3DIn


async def _meta_for_series(
    db: AsyncSession,
    series_id: uuid.UUID,
    user: User | None,
) -> tuple[Series, ImagingStudy, dict]:
    """Resolve a series + the allowlisted DICOM meta of its first instance.

    Used by every measurement / SUV endpoint that needs spacing or
    PET tags. Raises ``HTTPException`` directly so the caller can let
    the handler bubble up via the Problem Details middleware.
    """
    from bvphoenix.middleware.problem_details import problem as _problem
    from bvphoenix.services.dicom_meta_allowlist import extract_allowlisted

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
    meta = await asyncio.to_thread(extract_allowlisted, ds, version="v1")
    return series, study, meta


class ROICropIn(BaseModel):
    instance_index: int = Field(default=0, ge=0)
    bbox: list[int] = Field(
        min_length=4,
        max_length=4,
        description=(
            "Pixel-space bounding box as ``[x0, y0, x1, y1]``; 0,0 is "
            "the top-left of the slice image after windowing."
        ),
    )
    wc_delta: float = 0.0
    ww_delta: float = 0.0
    max_side: int = Field(default=512, ge=64, le=2048)


class ROICropOut(BaseModel):
    series_id: str
    sop_instance_uid: str
    instance_index: int
    bbox: list[int]
    width: int
    height: int
    content_type: str = "image/jpeg"
    size_bytes: int


class DicomMetaOut(BaseModel):
    series_id: str
    sop_instance_uid: str
    instance_number: int | None
    allowlist_version: str
    meta: dict


class SegmentationOut(BaseModel):
    id: str
    series_id: str
    producer: str
    producer_version: str | None
    label: str
    label_map: dict
    size_bytes: int | None
    download_url: str | None
    created_at: str


class RegistrationCreateIn(BaseModel):
    fixed_series_id: uuid.UUID
    moving_series_id: uuid.UUID
    kind: str = Field(default="rigid", description="rigid | demons (demons not yet implemented)")


class RegistrationOut(BaseModel):
    id: str
    fixed_series_id: str
    moving_series_id: str
    kind: str
    status: str
    job_id: str | None
    download_url: str | None
    result_meta: dict
    error: str | None
    created_at: str
    finished_at: str | None


def _registration_to_out(reg, *, download_url: str | None = None) -> RegistrationOut:
    return RegistrationOut(
        id=str(reg.id),
        fixed_series_id=str(reg.fixed_series_id),
        moving_series_id=str(reg.moving_series_id),
        kind=reg.kind,
        status=reg.status,
        job_id=str(reg.job_id) if reg.job_id else None,
        download_url=download_url,
        result_meta=reg.result_meta or {},
        error=reg.error,
        created_at=reg.created_at.isoformat(),
        finished_at=reg.finished_at.isoformat() if reg.finished_at else None,
    )


class ROIStatsIn(BaseModel):
    """Bbox-or-sphere + (optional) SUV variant request for the ROI stats endpoint.

    Coordinates are in **voxel indices** (i, j, k) on the same packed
    Float32 volume the viewer streams via
    ``GET /series/{id}/volume.raw``; indices are clamped server-side.

    ``kind="rectangle" | "ellipse"`` use the bbox defined by
    ``min_ijk / max_ijk`` (inclusive). ``kind="sphere"`` ignores the
    bbox fields and uses ``center_ijk`` + ``radius_mm``: the server
    builds a 3D spherical mask in physical space (using the volume's
    DICOM spacing) and computes stats over the masked voxels. This is
    the PERCIST 1.0 §4.3 liver-reference workflow (3 cm diameter
    sphere, radius 1.5 cm).
    """

    kind: Literal["rectangle", "ellipse", "sphere"] = Field(
        "rectangle",
        description=(
            "ROI shape. ``rectangle`` / ``ellipse`` use the bbox; "
            "``sphere`` uses center+radius and ignores the bbox."
        ),
    )
    min_ijk: list[int] | None = Field(
        None,
        min_length=3,
        max_length=3,
        description="[i_min, j_min, k_min] inclusive — required for rectangle/ellipse",
    )
    max_ijk: list[int] | None = Field(
        None,
        min_length=3,
        max_length=3,
        description="[i_max, j_max, k_max] inclusive — required for rectangle/ellipse",
    )
    center_ijk: list[int] | None = Field(
        None,
        min_length=3,
        max_length=3,
        description="Sphere center as voxel indices — required when kind=sphere",
    )
    radius_mm: float | None = Field(
        None,
        gt=0,
        le=200.0,
        description="Sphere radius in millimetres — required when kind=sphere",
    )
    suv_variant: (
        Literal[
            "bw",
            "lbm-janma",
            "lbm-james",
            "bsa-mosteller",
            "bsa-dubois",
        ]
        | None
    ) = Field(
        None,
        description=(
            "When set on a PET series, server applies the corresponding SUV "
            "factor to mean/max/peak/sd and reports them in suv_* fields"
        ),
    )
    exclude_segmentation_labels: list[str] | None = Field(
        None,
        max_length=32,
        description=(
            "TotalSegmentator labels to subtract from the ROI before stats. "
            "Typical PET use: ['kidney_left','kidney_right','urinary_bladder']. "
            "Resolved as segmentations/{series_id}/{label}.bin in the "
            "derivatives bucket; missing labels are dropped silently."
        ),
    )
    exclude_marker_ids: list[uuid.UUID] | None = Field(
        None,
        max_length=16,
        description=(
            "Marker ids of kind='bbox.exclusion' whose ijk-bbox is subtracted "
            "from the ROI before stats. Fallback when automatic segmentation "
            "is unavailable."
        ),
    )


class ROIStatsOut(BaseModel):
    """Aggregate stats over the ROI voxels. ``mean / std / min / max``
    are in the volume's native units (Bq/mL for PET BQML, HU for CT).
    ``peak_1cm3`` is the PERCIST 1.0 SUVpeak surrogate: mean of the
    voxels inside a sphere of radius 6.2 mm (≈ 1 cm³) centered on the
    voxel at ``argmax``. ``suv_*`` are populated only when the request
    asked for a variant and the series is PET with computable factors;
    ``suv_sd`` is the standard deviation of SUV across the ROI voxels,
    needed by the PERCIST measurable-lesion floor
    ``1.5 × SUVmean + 2 × SUVsd``.
    """

    voxel_count: int
    mean: float
    std: float
    min: float
    max: float
    peak_1cm3: float | None
    suv_mean: float | None
    suv_sd: float | None
    suv_max: float | None
    suv_peak: float | None
    suv_variant_used: str | None
    units_native: str | None


class HotSpot(BaseModel):
    """One connected high-uptake region. Coordinates are voxel
    indices (i, j, k) into the packed Float32 volume the viewer
    streams; ``volume_ml`` is the connected-component volume in
    millilitres (1 cm³ = 1 mL); ``suv_*`` are populated only when
    the request asked for a variant and the series is PET with
    computable factors."""

    rank: int
    centroid_ijk: list[int]
    bbox_min_ijk: list[int]
    bbox_max_ijk: list[int]
    voxel_count: int
    volume_ml: float
    raw_max: float
    raw_mean: float
    suv_max: float | None
    suv_mean: float | None
    suv_peak: float | None


class HotSpotsOut(BaseModel):
    spots: list[HotSpot]
    threshold_used: float
    threshold_kind: str
    suv_variant_used: str | None
    units_native: str | None
    global_max_raw: float
    global_max_suv: float | None
    # Volume Z extent so the frontend can show "of N total slices" next
    # to the slice-range inputs and clamp values without a separate
    # /describe-series round-trip.
    volume_nz: int
    # Slice range actually scanned (after clamping). Both inclusive.
    # Equal to (0, nz-1) when the operator did not pass a range.
    slice_min_used: int
    slice_max_used: int


class HotSpotsIn(BaseModel):
    """Request body for ``/series/{id}/hot-spots``.

    ``threshold_mode = "percent_of_max"`` (default, PERCIST 1.0
    convention): keep voxels above ``threshold_value`` × global max
    of the volume. ``threshold_mode = "absolute_suv"`` keeps voxels
    above the literal SUV cut-off (requires ``suv_variant``).
    ``min_volume_ml`` filters out single-voxel noise; clinical
    PERCIST uses 1 mL as the lower bound for measurable lesions.

    ``slice_min`` / ``slice_max`` restrict the connected-component
    search to a Z-axis slab (inclusive, voxel indices into the volume).
    When the operator wants to limit the search to a specific
    anatomical region they read off the MPR (e.g. liver = slices
    180-240) this is the canonical knob: relative-band heuristics are
    fragile across patient proportions, slice indices are exact.
    Out-of-range values are clamped; ``slice_max < slice_min`` returns
    an empty result."""

    threshold_mode: Literal["percent_of_max", "absolute_suv"] = "percent_of_max"
    threshold_value: float = Field(0.5, gt=0)
    min_volume_ml: float = Field(0.5, ge=0)
    top_n: int = Field(50, ge=1, le=200)
    slice_min: int | None = Field(None, ge=0)
    slice_max: int | None = Field(None, ge=0)
    suv_variant: (
        Literal[
            "bw",
            "lbm-janma",
            "lbm-james",
            "bsa-mosteller",
            "bsa-dubois",
        ]
        | None
    ) = "bw"
    exclude_segmentation_labels: list[str] | None = Field(
        None,
        max_length=32,
        description=(
            "TotalSegmentator labels to subtract from the search volume "
            "before connected-components. Typical PET use: "
            "['kidney_left','kidney_right','urinary_bladder']. Resolved "
            "as segmentations/{series_id}/{label}.bin in the derivatives "
            "bucket; missing labels are dropped silently so a search can "
            "still run while a segmentation job is in flight."
        ),
    )
    exclude_marker_ids: list[uuid.UUID] | None = Field(
        None,
        max_length=16,
        description=(
            "Marker ids of kind='bbox.exclusion' whose ijk-bbox is "
            "excluded from the search volume. Day-1 fallback for series "
            "without automatic segmentation: the operator draws once, "
            "subsequent searches skip that region."
        ),
    )


class StudyScreenshotOut(BaseModel):
    """Response for ``POST /studies/{study_id}/screenshots``: opaque
    document id the caller can use to fetch the binary later. The S3
    key and bucket are deliberately not surfaced (storage isolation:
    only the backend ever speaks to S3)."""

    document_id: str
    title: str
    sha256: str
    size_bytes: int


_VIEWPORT_VALUES = ("axial", "sagittal", "coronal", "3d", "mip", "oblique")


# Auto-generated __all__: ensures child modules' `from ._shared
# import *` pulls in the underscore-prefixed helpers (constants like
# _AGENT_PATIENT_IMAGES and private guards like _get_patient_or_404)
# that python's default `import *` semantics would otherwise drop.
__all__ = [
    "DERIVATIVE_FORMAT",
    "DERIVATIVE_KIND",
    "DERIVATIVE_KIND_PREVIEW",
    "DOWNLOAD_DICOM",
    "HEADER_STRUCT",
    "NON_VOLUMETRIC_SOP_CLASSES",
    "READ_METADATA",
    "READ_PIXELS",
    "UTC",
    "WRITE_REPORT",
    "_AGENT_PATIENT_IMAGES",
    "_AGENT_PATIENT_READ",
    "_COMMONS_TIERS",
    "_QUOTA_TIERS",
    "_SERIES_EDITABLE",
    "_STREAM_CHUNK_BYTES",
    "_STREAM_THRESHOLD_BYTES",
    "_STUDY_EDITABLE",
    "_VALID_TIERS",
    "_VIEWPORT_VALUES",
    "APIRouter",
    "Annotated",
    "AsyncSession",
    "AuditDep",
    "BaseModel",
    "ConsentRevokeIn",
    "ConsentRevokeOut",
    "Depends",
    "Derivative",
    "DicomMetaOut",
    "Field",
    "File",
    "Form",
    "Grant",
    "HTTPException",
    "HotSpot",
    "HotSpotsIn",
    "HotSpotsOut",
    "ImagingStudy",
    "Instance",
    "InstanceOut",
    "Literal",
    "MeasureDistanceIn",
    "MeasureVolumeIn",
    "NoPixelDataError",
    "NonVolumetricSeriesError",
    "PackedVolume",
    "PaginatedStudies",
    "Query",
    "ROICropIn",
    "ROICropOut",
    "ROIStatsIn",
    "ROIStatsOut",
    "RegistrationCreateIn",
    "RegistrationOut",
    "Request",
    "Response",
    "S3Storage",
    "SUVOut",
    "SegmentationOut",
    "Series",
    "SeriesMetadataPatchIn",
    "SeriesOut",
    "StreamingResponse",
    "StudyDetailOut",
    "StudyMetadataPatchIn",
    "StudyOut",
    "StudyScreenshotOut",
    "TierChangeIn",
    "TierChangeOut",
    "UnsupportedDocumentError",
    "UploadFile",
    "User",
    "_Point3DIn",
    "_chunked_iter",
    "_client_accepts_gzip",
    "_content_disposition",
    "_enqueue_tier_reindex",
    "_meta_for_series",
    "_registration_to_out",
    "_suggested_wl_from_middle_instance",
    "_volume_response",
    "active_share_grant",
    "annotations",
    "apply_earl_harmonization",
    "asyncio",
    "can",
    "can_patient",
    "create_pool",
    "datetime",
    "deidentify_dicom_bytes",
    "dicom_to_jpeg",
    "enforce_agent_scope",
    "ensure_tier_consents",
    "func",
    "get_db",
    "get_s3_storage",
    "get_settings",
    "gzip",
    "io",
    "is_embeddable_modality",
    "is_image_sop_class",
    "optional_user",
    "pack_low_res",
    "pack_series",
    "pydicom",
    "read_dicom_document",
    "read_dicom_wc_ww",
    "redis_settings",
    "require_scope_if_agent",
    "require_user",
    "revoke_tier_consent_for_study",
    "router",
    "select",
    "should_deidentify",
    "status",
    "text",
    "uuid",
    "visible_studies_filter",
    "volume_earl_key",
    "volume_key",
    "volume_preview_key",
    "volume_stack_earl_key",
    "volume_stack_key",
]
