# ruff: noqa: F405
"""Contrast / acquisition phase manifest for a multiphase CT study.

* ``GET  /studies/{id}/phases``        — read-only manifest of the study's
  series ordered by acquisition time, with the persisted phase label,
  confidence, source and (for the viewer's zero-job sync) the packed
  volume's FrameOfReferenceUID.
* ``POST /studies/{id}/phases/detect`` — run the classifier and persist
  ``phase_source='auto'`` labels (never clobbering a human override unless
  ``force=true``), then return the refreshed manifest.

Read rides ``imaging:read``; detect rides ``imaging:compute`` (the catalog
scope for "descriptive analyses" — phase classification is exactly that).
The phase is NOT the care-timeline ``phases:*`` scope (unrelated concept).
"""

from __future__ import annotations

from fastapi import APIRouter

from bvphoenix.api.studies import _shared
from bvphoenix.api.studies._shared import *  # noqa: F403
from bvphoenix.auth import enforce_agent_patient_scope
from bvphoenix.db.models.dicom import ACQUISITION_PHASES
from bvphoenix.services.contrast_phase import CONFIRM_THRESHOLD
from bvphoenix.services.contrast_phase_classify import classify_and_persist_study
from bvphoenix.services.roi_sampling import (
    sample_bbox_hu,
    sample_sphere_hu,
    slab_byte_range,
    slab_k_range_bbox,
    slab_k_range_sphere,
    world_to_ijk,
)
from bvphoenix.services.series_kind import is_reviewable_phase, plane_from_direction
from bvphoenix.services.washout import PhaseHu, compute_washout

router = APIRouter()


class SeriesPhaseOut(BaseModel):
    series_id: uuid.UUID
    series_number: int | None = None
    modality: str | None = None
    series_description: str | None = None
    body_part_examined: str | None = None
    acquisition_phase: str | None = None
    phase_confidence: float | None = None
    phase_source: str | None = None
    needs_confirmation: bool = False
    acquisition_time_of_day: str | None = None
    contrast_bolus_agent: str | None = None
    frame_of_reference_uid: str | None = None
    # Received instance count: lets the viewer tell a real volume apart from a
    # scout / screenshot / dose-report / bolus-prep series in the picker.
    instance_count: int | None = None
    # Acquisition plane derived from the packed volume's direction cosines
    # (axial / sagittal / coronal / oblique), or None when the series is not
    # packed yet. Distinguishes an axial source from an MPR reformat.
    series_plane: str | None = None
    # True when this series is a reviewable axial phase volume (CT, axial,
    # enough slices, not a localizer / capture / dose report / bolus-prep /
    # reformat). The viewer opens these as phase panes by default and the
    # picker offers them first; everything else is clutter to hide.
    is_reviewable_phase: bool = False


class StudyPhasesOut(BaseModel):
    study_id: uuid.UUID
    phases: list[SeriesPhaseOut]


class SeriesPhaseOverrideIn(BaseModel):
    acquisition_phase: str | None = Field(
        default=None,
        description=(
            "Phase to pin as a human decision (phase_source='human'), or "
            "null to clear the label and re-enable auto-classification."
        ),
    )
    dry_run: bool = Field(
        default=False,
        description="Validate the value and return the would-be result without writing.",
    )


def _needs_confirmation(series: Series) -> bool:
    """A persisted label the human should confirm: an auto label below the
    confidence threshold. Human-set labels are trusted by definition."""
    return (
        series.phase_source == "auto"
        and series.phase_confidence is not None
        and series.phase_confidence < CONFIRM_THRESHOLD
    )


async def _geometry_map(db: AsyncSession, series_ids: list[uuid.UUID]) -> dict[uuid.UUID, dict]:
    """series_id -> the geometry dict of its primary packed volume
    (``frame_of_reference_uid`` + ``direction`` cosines), when packed.

    Lets the viewer decide same-FoR (zero-job sync) vs needs-registration,
    and lets the manifest tell an axial source from an MPR reformat — both
    without fetching every volume."""
    if not series_ids:
        return {}
    rows = (
        await db.execute(
            select(Derivative.series_id, Derivative.geometry).where(
                Derivative.series_id.in_(series_ids),
                Derivative.kind == DERIVATIVE_KIND,
                Derivative.stack_index == 0,
            )
        )
    ).all()
    out: dict[uuid.UUID, dict] = {}
    for sid, geom in rows:
        if isinstance(geom, dict):
            out[sid] = geom
    return out


def _series_phase_out(series: Series, geom: dict | None) -> SeriesPhaseOut:
    plane = plane_from_direction((geom or {}).get("direction"))
    return SeriesPhaseOut(
        series_id=series.id,
        series_number=series.series_number,
        modality=series.modality,
        series_description=series.series_description,
        body_part_examined=series.body_part_examined,
        acquisition_phase=series.acquisition_phase,
        phase_confidence=series.phase_confidence,
        phase_source=series.phase_source,
        needs_confirmation=_needs_confirmation(series),
        acquisition_time_of_day=(
            series.acquisition_time_of_day.isoformat()
            if series.acquisition_time_of_day is not None
            else None
        ),
        contrast_bolus_agent=series.contrast_bolus_agent,
        frame_of_reference_uid=(geom or {}).get("frame_of_reference_uid"),
        instance_count=series.received_instance_count,
        series_plane=plane,
        is_reviewable_phase=is_reviewable_phase(
            modality=series.modality,
            instance_count=series.received_instance_count,
            plane=plane,
            description=series.series_description,
        ),
    )


async def _load_study_series_ordered(db: AsyncSession, study_id) -> list[Series]:
    return list(
        (
            await db.execute(
                select(Series)
                .where(Series.study_id == study_id)
                .order_by(
                    Series.acquisition_time_of_day.asc().nullslast(),
                    Series.series_number.asc().nullslast(),
                    Series.id,
                )
            )
        )
        .scalars()
        .all()
    )


@router.get("/studies/{study_id}/phases", response_model=StudyPhasesOut)
async def get_study_phases(
    request: Request,
    study_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(optional_user)],
    audit: AuditDep,
) -> StudyPhasesOut:
    """Read the study's acquisition-phase manifest (persisted state only)."""
    from bvphoenix.middleware.problem_details import problem as _problem

    study = (
        await db.execute(select(ImagingStudy).where(ImagingStudy.id == study_id))
    ).scalar_one_or_none()
    if study is None:
        raise _problem(404, "not_found", "study not found")
    if not await can(db, user=user, action=READ_METADATA, study=study):
        raise _problem(404, "not_found", "study not found")
    enforce_agent_patient_scope(request, study.patient_id, scope="patient:images")

    rows = await _load_study_series_ordered(db, study.id)
    geom_map = await _geometry_map(db, [r.id for r in rows])
    phases = [_series_phase_out(r, geom_map.get(r.id)) for r in rows]

    await audit.log(
        action="study_phases_read",
        actor_subject_id=user.subject_id if user else None,
        resource_kind="study",
        resource_id=study.id,
    )
    return StudyPhasesOut(study_id=study.id, phases=phases)


@router.post("/studies/{study_id}/phases/detect", response_model=StudyPhasesOut)
async def detect_study_phases(
    request: Request,
    study_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    audit: AuditDep,
    force: bool = Query(
        False,
        description="Re-classify and OVERWRITE even human-confirmed phases. "
        "Default false: human overrides are preserved.",
    ),
) -> StudyPhasesOut:
    """Run the contrast-phase classifier over the study's CT series and
    persist the auto labels. Idempotent; preserves human overrides unless
    ``force=true``."""
    from bvphoenix.middleware.problem_details import problem as _problem

    study = (
        await db.execute(select(ImagingStudy).where(ImagingStudy.id == study_id))
    ).scalar_one_or_none()
    if study is None:
        raise _problem(404, "not_found", "study not found")
    if not await can(db, user=user, action=READ_PIXELS, study=study):
        raise _problem(404, "not_found", "study not found")
    enforce_agent_scope(request, "imaging:compute")
    enforce_agent_patient_scope(request, study.patient_id, scope="patient:images")
    is_owner = study.owner_subject_id == user.subject_id or getattr(user, "is_admin", False)
    if not is_owner:
        raise _problem(403, "forbidden", "phase detection requires study ownership")

    await classify_and_persist_study(db, study.id, force=force)
    await db.commit()

    rows = await _load_study_series_ordered(db, study.id)
    geom_map = await _geometry_map(db, [r.id for r in rows])
    phases = [_series_phase_out(r, geom_map.get(r.id)) for r in rows]

    await audit.log(
        action="study_phases_detect",
        actor_subject_id=user.subject_id,
        resource_kind="study",
        resource_id=study.id,
        metadata={"force": force, "series": len(rows)},
    )
    return StudyPhasesOut(study_id=study.id, phases=phases)


@router.patch("/series/{series_id}/acquisition-phase", response_model=SeriesPhaseOut)
async def set_series_acquisition_phase(
    request: Request,
    series_id: uuid.UUID,
    body: SeriesPhaseOverrideIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    audit: AuditDep,
) -> SeriesPhaseOut:
    """Human override of a series' contrast phase (the confirm/correct half
    of the candidate+confirm model).

    Setting a phase pins it as ``phase_source='human'`` so the auto
    classifier never clobbers it (``detect`` skips human rows unless
    ``force``). Setting null clears the label and re-enables auto. Writes
    the phase column directly (it is a derived attribute, not a
    DICOM-authoritative field — unlike ``modality``, which is overridden
    via a tag)."""
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
    enforce_agent_scope(request, "imaging:compute")
    enforce_agent_patient_scope(request, study.patient_id, scope="patient:images")
    is_owner = study.owner_subject_id == user.subject_id or getattr(user, "is_admin", False)
    if not is_owner:
        raise _problem(403, "forbidden", "phase override requires study ownership")

    val = body.acquisition_phase
    if val is not None and val not in ACQUISITION_PHASES:
        raise _problem(
            422,
            "invalid_phase",
            f"unknown acquisition_phase {val!r}",
            extra={"allowed": list(ACQUISITION_PHASES)},
        )

    geom = (await _geometry_map(db, [series.id])).get(series.id)

    if body.dry_run:
        # Reflect the would-be state without persisting. Start from the shared
        # serializer (so plane / is_reviewable_phase / FoR stay consistent) and
        # overlay the would-be human override.
        out = _series_phase_out(series, geom)
        out.acquisition_phase = val
        out.phase_confidence = None
        out.phase_source = None if val is None else "human"
        out.needs_confirmation = False
        return out

    # A human decision carries no model confidence; clearing reverts to
    # the unclassified state so a later detect can re-label it.
    series.acquisition_phase = val
    series.phase_source = None if val is None else "human"
    series.phase_confidence = None
    await db.commit()
    await db.refresh(series)

    await audit.log(
        action="series_acquisition_phase_set",
        actor_subject_id=user.subject_id,
        resource_kind="series",
        resource_id=series.id,
        metadata={"acquisition_phase": val},
    )
    return _series_phase_out(series, geom)


# ---- cross-phase HU + wash-out ----------------------------------------


class PhaseRoiIn(BaseModel):
    kind: str = Field(default="sphere", description="sphere | bbox")
    # Sphere: a centre in patient space (LPS) + a radius in mm.
    center_lps: list[float] | None = Field(default=None, description="[x, y, z] LPS, for sphere")
    radius_mm: float | None = None
    # Bbox: two opposite corners in patient space (LPS).
    min_lps: list[float] | None = Field(default=None, description="[x, y, z] LPS, for bbox")
    max_lps: list[float] | None = None
    # The frame the ROI is defined in. Defaults to the first packed phase's
    # FrameOfReferenceUID; phases in a different frame are skipped (v1: no
    # per-phase ROI registration).
    frame_of_reference_uid: str | None = None
    # Anatomical region the operator is measuring — scopes the wash-out
    # interpretation. "adrenal" emits the APW/RPW adenoma flags; "liver"
    # withholds them and uses the parenchyma-relative read; None/other returns
    # the raw indices without verdict flags. Free-form (forward-compatible).
    region: str | None = Field(default=None, description="adrenal | liver | other")
    # Optional reference-parenchyma ROI (the liver wash-out workflow): a sphere
    # in LPS sampled in every phase alongside the lesion ROI, so the panel can
    # report lesion-minus-parenchyma HU per phase.
    parenchyma_center_lps: list[float] | None = Field(
        default=None, description="[x, y, z] LPS centre of the reference-parenchyma sphere"
    )
    parenchyma_radius_mm: float | None = None


class PhaseSampleOut(BaseModel):
    series_id: uuid.UUID
    acquisition_phase: str | None
    hu_mean: float
    hu_std: float
    voxel_count: int
    frame_of_reference_uid: str | None = None


class PhaseSkippedOut(BaseModel):
    series_id: uuid.UUID
    acquisition_phase: str | None
    reason: str


class PhaseCurvePointOut(BaseModel):
    acquisition_phase: str
    hu_mean: float


class PhaseRelativePointOut(BaseModel):
    acquisition_phase: str
    lesion_hu: float
    parenchyma_hu: float
    delta_hu: float  # lesion - parenchyma (negative => lesion hypodense vs ref)


class PhaseWashoutOut(BaseModel):
    # Region scoping the interpretation (adrenal | liver | other/None).
    region: str | None = None
    unenhanced_phase: str | None = None
    enhanced_phase: str | None = None
    delayed_phase: str | None = None
    unenhanced_hu: float | None = None
    enhanced_hu: float | None = None
    delayed_hu: float | None = None
    absolute_enhancement_hu: float | None = None
    # APW/RPW are adrenal indices: present for adrenal/other, withheld (None)
    # for liver. The *_ge_* flags are adenoma verdicts: adrenal-only.
    apw: float | None = None
    rpw: float | None = None
    apw_ge_60: bool | None = None
    rpw_ge_40: bool | None = None
    unenhanced_below_10hu: bool | None = None
    curve: list[PhaseCurvePointOut]
    # Liver workflow: reference-parenchyma HU per phase + lesion-minus-
    # parenchyma per phase (the qualitative LI-RADS wash-out signal).
    parenchyma_curve: list[PhaseCurvePointOut] = []
    relative_curve: list[PhaseRelativePointOut] = []


class PhaseRoiStatsOut(BaseModel):
    study_id: uuid.UUID
    reference_frame_of_reference_uid: str | None
    samples: list[PhaseSampleOut]
    skipped: list[PhaseSkippedOut]
    washout: PhaseWashoutOut


@router.post("/studies/{study_id}/phase-roi-stats", response_model=PhaseRoiStatsOut)
async def compute_phase_roi_stats(
    request: Request,
    study_id: uuid.UUID,
    body: PhaseRoiIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(optional_user)],
    audit: AuditDep,
) -> PhaseRoiStatsOut:
    """Sample ONE patient-space (LPS) ROI in every classified CT phase of the
    study and compute the wash-out indices (time-attenuation curve + APW/RPW).

    The ROI is mapped into each phase's own grid via that volume's geometry,
    so phases sharing a FrameOfReferenceUID are compared at the same anatomy
    with no registration. Phases in a different frame are reported under
    ``skipped`` (per-phase ROI registration is a follow-up). Read-only +
    audit-logged, mirroring ``/series/{id}/roi-stats``."""
    import numpy as np

    from bvphoenix.middleware.problem_details import problem as _problem
    from bvphoenix.services.memory import release_memory
    from bvphoenix.services.volumes import MAX_VOLUME_BYTES

    if body.kind == "sphere":
        if body.center_lps is None or len(body.center_lps) != 3 or body.radius_mm is None:
            raise _problem(422, "invalid_roi", "kind=sphere requires center_lps[3] and radius_mm")
    elif body.kind == "bbox":
        if (
            body.min_lps is None
            or body.max_lps is None
            or len(body.min_lps) != 3
            or len(body.max_lps) != 3
        ):
            raise _problem(422, "invalid_roi", "kind=bbox requires min_lps[3] and max_lps[3]")
    else:
        raise _problem(422, "invalid_roi", f"unknown kind {body.kind!r} (sphere | bbox)")

    study = (
        await db.execute(select(ImagingStudy).where(ImagingStudy.id == study_id))
    ).scalar_one_or_none()
    if study is None:
        raise _problem(404, "not_found", "study not found")
    if not await can(db, user=user, action=READ_PIXELS, study=study):
        raise _problem(404, "not_found", "study not found")
    enforce_agent_patient_scope(request, study.patient_id, scope="patient:images")

    # Classified CT phases, in acquisition order.
    rows = [
        s
        for s in await _load_study_series_ordered(db, study.id)
        if (s.modality or "").upper() == "CT" and s.acquisition_phase
    ]

    # Load every phase's packed-volume derivative in ONE query, capture the
    # plain data the rest of the handler needs, then RELEASE the pooled DB
    # connection BEFORE the S3 fan-out below. Each phase volume is a multi-MB
    # ranged GET through the bandwidth-limited egress gateway; holding the
    # connection across N of them drains the pool under even light concurrency,
    # surfacing as `QueuePool limit ... TimeoutError` 500s here AND on sibling
    # endpoints that then cannot get a connection. Mirrors api/display_metadata.
    deriv_rows = (
        (
            await db.execute(
                select(Derivative).where(
                    Derivative.series_id.in_([s.id for s in rows]),
                    Derivative.kind == DERIVATIVE_KIND,
                    Derivative.format == DERIVATIVE_FORMAT,
                    Derivative.stack_index == 0,
                )
            )
        )
        .scalars()
        .all()
        if rows
        else []
    )
    deriv_by_series = {d.series_id: d for d in deriv_rows}
    # (series_id, acquisition_phase, s3_bucket, s3_key, geometry) per phase.
    work: list[tuple[uuid.UUID, str | None, str | None, str | None, dict | None]] = []
    for s in rows:
        d = deriv_by_series.get(s.id)
        geom = d.geometry if (d is not None and isinstance(d.geometry, dict)) else None
        work.append(
            (
                s.id,
                s.acquisition_phase,
                d.s3_bucket if d is not None else None,
                d.s3_key if d is not None else None,
                geom,
            )
        )
    study_id_val = study.id
    actor_subject_id = user.subject_id if user else None
    await db.close()

    storage = get_s3_storage()
    ref_for = body.frame_of_reference_uid
    samples: list[PhaseSampleOut] = []
    skipped: list[PhaseSkippedOut] = []
    points: list[PhaseHu] = []
    parenchyma_points: list[PhaseHu] = []

    for series_id, phase, bucket, key, geom in work:
        if bucket is None or key is None:
            skipped.append(
                PhaseSkippedOut(series_id=series_id, acquisition_phase=phase, reason="not packed")
            )
            continue
        for_uid = geom.get("frame_of_reference_uid") if geom else None
        if ref_for is None:
            ref_for = for_uid  # first packed phase defines the reference frame
        if ref_for and for_uid and for_uid != ref_for:
            skipped.append(
                PhaseSkippedOut(
                    series_id=series_id,
                    acquisition_phase=phase,
                    reason="different frame of reference",
                )
            )
            continue

        # The S3 fetch + decode + sampling all live inside the try: a missing
        # blob (S3 NoSuchKey), a truncated/corrupt volume (struct/reshape
        # error), or a geometry/ROI mismatch (ValueError) must degrade THAT
        # phase to ``skipped`` — never 500 the whole wash-out (the symptom the
        # radiologist hit). Only one phase failing should not lose the rest.
        try:
            # Read only the 32-byte header first to learn the geometry/size,
            # then ranged-GET just the ROI's slice slab — NOT the whole 100-500
            # MB volume. This is the dominant wash-out latency over the
            # bandwidth-limited egress, and it also keeps the resident set tiny.
            header = await asyncio.to_thread(
                storage.get_object_range, bucket=bucket, key=key, start=0, length=HEADER_STRUCT.size
            )
            nx, ny, nz, sx, sy, sz, _vmin, _vmax = HEADER_STRUCT.unpack(header)
            nx, ny, nz = int(nx), int(ny), int(nz)
            spacing = (float(sx), float(sy), float(sz))
            if nx <= 0 or ny <= 0 or nz <= 0 or nx * ny * nz * 4 > MAX_VOLUME_BYTES:
                # Header-only reject (no download) of a malformed / pathological
                # volume instead of slabbing a corrupt size.
                raise ValueError("volume malformed or too large to sample")

            # Map the lesion ROI into this phase's grid to find the slice slab.
            ijk: tuple[float, float, float] | None = None
            ijk_min: tuple[float, float, float] | None = None
            ijk_max: tuple[float, float, float] | None = None
            lesion_radius = 0.0
            if body.kind == "sphere":
                assert body.center_lps is not None and body.radius_mm is not None
                lesion_radius = float(body.radius_mm)
                ijk = world_to_ijk(
                    (body.center_lps[0], body.center_lps[1], body.center_lps[2]), geom, spacing
                )
                if ijk is None:
                    raise ValueError("volume has no geometry")
                k0, k1 = slab_k_range_sphere(ijk[2], lesion_radius, spacing[2], nz)
            else:
                assert body.min_lps is not None and body.max_lps is not None
                ijk_min = world_to_ijk(
                    (body.min_lps[0], body.min_lps[1], body.min_lps[2]), geom, spacing
                )
                ijk_max = world_to_ijk(
                    (body.max_lps[0], body.max_lps[1], body.max_lps[2]), geom, spacing
                )
                if ijk_min is None or ijk_max is None:
                    raise ValueError("volume has no geometry")
                k0, k1 = slab_k_range_bbox(ijk_min[2], ijk_max[2], nz)

            # Widen the slab to also cover the parenchyma ROI (liver workflow)
            # so a single ranged read serves both spheres.
            p_centre = body.parenchyma_center_lps
            p_radius = body.parenchyma_radius_mm
            p_ijk: tuple[float, float, float] | None = None
            if phase and p_centre is not None and len(p_centre) == 3 and p_radius is not None:
                p_ijk = world_to_ijk((p_centre[0], p_centre[1], p_centre[2]), geom, spacing)
                if p_ijk is not None:
                    pk0, pk1 = slab_k_range_sphere(p_ijk[2], float(p_radius), spacing[2], nz)
                    k0, k1 = min(k0, pk0), max(k1, pk1)

            # An ROI whose centre maps OUTSIDE this phase's z-extent yields an
            # inverted slab (k0 > k1): slab_k_range clamps each end to
            # [0, nz-1] independently, so a centre past the volume gives
            # k0 > k1. Left unchecked that produced a negative byte length, a
            # malformed S3 range (whole-object read), and a cryptic reshape
            # error. Degrade THIS phase with a clear reason instead.
            if k0 > k1:
                raise ValueError("ROI maps outside this phase's z-range")

            start, length = slab_byte_range(k0, k1, ny, nx, HEADER_STRUCT.size)
            slab = await asyncio.to_thread(
                storage.get_object_range, bucket=bucket, key=key, start=start, length=length
            )
            arr = np.frombuffer(slab, dtype=np.float32).reshape(k1 - k0 + 1, ny, nx)

            # Sample in slab-local coordinates (slice index shifted by k0).
            if ijk is not None:
                stats = sample_sphere_hu(arr, spacing, (ijk[0], ijk[1], ijk[2] - k0), lesion_radius)
            elif ijk_min is not None and ijk_max is not None:
                stats = sample_bbox_hu(
                    arr,
                    (ijk_min[0], ijk_min[1], ijk_min[2] - k0),
                    (ijk_max[0], ijk_max[1], ijk_max[2] - k0),
                )
            else:  # unreachable: one branch above always sets a lesion ROI
                raise ValueError("no ROI resolved")
        except ValueError as exc:
            skipped.append(
                PhaseSkippedOut(series_id=series_id, acquisition_phase=phase, reason=str(exc))
            )
            continue
        except Exception as exc:  # degrade a bad volume, don't 500 the request
            skipped.append(
                PhaseSkippedOut(
                    series_id=series_id,
                    acquisition_phase=phase,
                    reason=f"volume unreadable ({type(exc).__name__})",
                )
            )
            continue

        samples.append(
            PhaseSampleOut(
                series_id=series_id,
                acquisition_phase=phase,
                hu_mean=stats.mean,
                hu_std=stats.std,
                voxel_count=stats.voxel_count,
                frame_of_reference_uid=for_uid,
            )
        )
        if phase:
            points.append(PhaseHu(acquisition_phase=phase, hu_mean=stats.mean))

        # Reference-parenchyma ROI (liver workflow): sample the SAME slab at the
        # second sphere (its slices were folded into the slab range above).
        # Out-of-bounds in this phase simply drops the parenchyma point; it
        # never fails the lesion measurement.
        if p_ijk is not None and p_radius is not None:
            try:
                p_stats = sample_sphere_hu(
                    arr, spacing, (p_ijk[0], p_ijk[1], p_ijk[2] - k0), float(p_radius)
                )
                parenchyma_points.append(PhaseHu(acquisition_phase=phase, hu_mean=p_stats.mean))
            except ValueError:
                pass

        # Drop the slab + its view before the next iteration.
        del slab, arr

    # Hand the freed volume pages back to the OS. Without this the resident set
    # climbs request-after-request (glibc arena retention) until a later unpack
    # OOMKills the pod (the 502s the radiologist hit). MALLOC_ARENA_MAX bounds
    # the pools; release_memory() (malloc_trim) returns the pages now.
    release_memory()

    result = compute_washout(
        points,
        region=body.region,
        parenchyma=parenchyma_points or None,
    )
    washout = PhaseWashoutOut(
        region=result.region,
        unenhanced_phase=result.unenhanced_phase,
        enhanced_phase=result.enhanced_phase,
        delayed_phase=result.delayed_phase,
        unenhanced_hu=result.unenhanced_hu,
        enhanced_hu=result.enhanced_hu,
        delayed_hu=result.delayed_hu,
        absolute_enhancement_hu=result.absolute_enhancement_hu,
        apw=result.apw,
        rpw=result.rpw,
        apw_ge_60=result.apw_ge_60,
        rpw_ge_40=result.rpw_ge_40,
        unenhanced_below_10hu=result.unenhanced_below_10hu,
        curve=[
            PhaseCurvePointOut(acquisition_phase=p.acquisition_phase, hu_mean=p.hu_mean)
            for p in result.curve
        ],
        parenchyma_curve=[
            PhaseCurvePointOut(acquisition_phase=p.acquisition_phase, hu_mean=p.hu_mean)
            for p in result.parenchyma_curve
        ],
        relative_curve=[
            PhaseRelativePointOut(
                acquisition_phase=r.acquisition_phase,
                lesion_hu=r.lesion_hu,
                parenchyma_hu=r.parenchyma_hu,
                delta_hu=r.delta_hu,
            )
            for r in result.relative_curve
        ],
    )

    await audit.log(
        action="phase_roi_stats",
        actor_subject_id=actor_subject_id,
        resource_kind="study",
        resource_id=study_id_val,
        metadata={
            "kind": body.kind,
            "region": body.region,
            "phases_sampled": len(samples),
            "skipped": len(skipped),
        },
    )
    return PhaseRoiStatsOut(
        study_id=study_id_val,
        reference_frame_of_reference_uid=ref_for,
        samples=samples,
        skipped=skipped,
        washout=washout,
    )


# ---- per-voxel wash-out / subtraction heat map ------------------------


class PhaseMapIn(BaseModel):
    center_lps: list[float] = Field(description="[x, y, z] LPS centre of the lesion region")
    radius_mm: float = Field(description="in-plane half-extent of the cropped map, in mm")
    # "washout" = enhanced - delayed (green where the lesion clears contrast);
    # "subtraction" = enhanced - unenhanced (the enhancement map).
    metric: str = Field(default="washout", description="washout | subtraction")
    frame_of_reference_uid: str | None = None


class PhaseMapOut(BaseModel):
    metric: str
    phase_a: str
    phase_b: str
    vabs: float  # symmetric HU colour scale
    width: int
    height: int
    png_base64: str


_ENHANCED_PHASES = ("portal_venous", "arterial", "corticomedullary")
_DELAYED_PHASES = ("delayed", "excretory", "hepatobiliary")


@router.post("/studies/{study_id}/washout-map", response_model=PhaseMapOut)
async def compute_washout_map(
    request: Request,
    study_id: uuid.UUID,
    body: PhaseMapIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(optional_user)],
    audit: AuditDep,
) -> PhaseMapOut:
    """Per-voxel wash-out / subtraction heat map over the lesion region.

    Highlights WHERE a lesion washes out: the difference between two phases
    (enhanced-delayed for "washout", enhanced-unenhanced for "subtraction")
    colour-mapped over the ROI's central slice (green=wash-out, red=uptake).
    Reuses the ranged-slab read. v1 needs the two phases on a COMMON voxel
    grid (same geometry/spacing); otherwise 422 (no resampling yet)."""
    import base64

    import numpy as np

    from bvphoenix.middleware.problem_details import problem as _problem
    from bvphoenix.services.memory import release_memory
    from bvphoenix.services.roi_sampling import slab_byte_range, slab_k_range_sphere
    from bvphoenix.services.volumes import MAX_VOLUME_BYTES
    from bvphoenix.services.washout_map import diff_map_rgba, encode_png

    if len(body.center_lps) != 3 or body.radius_mm <= 0:
        raise _problem(422, "invalid_roi", "center_lps[3] and radius_mm > 0 required")
    metric = body.metric if body.metric in ("washout", "subtraction") else "washout"

    study = (
        await db.execute(select(ImagingStudy).where(ImagingStudy.id == study_id))
    ).scalar_one_or_none()
    if study is None:
        raise _problem(404, "not_found", "study not found")
    if not await can(db, user=user, action=READ_PIXELS, study=study):
        raise _problem(404, "not_found", "study not found")
    enforce_agent_patient_scope(request, study.patient_id, scope="patient:images")

    rows = [
        s
        for s in await _load_study_series_ordered(db, study.id)
        if (s.modality or "").upper() == "CT" and s.acquisition_phase
    ]
    deriv_rows = (
        (
            await db.execute(
                select(Derivative).where(
                    Derivative.series_id.in_([s.id for s in rows]),
                    Derivative.kind == DERIVATIVE_KIND,
                    Derivative.format == DERIVATIVE_FORMAT,
                    Derivative.stack_index == 0,
                )
            )
        )
        .scalars()
        .all()
        if rows
        else []
    )
    deriv_by_series = {d.series_id: d for d in deriv_rows}
    # phase -> (bucket, key, geometry) for the first series carrying that phase.
    by_phase: dict[str, tuple[str, str, dict]] = {}
    for s in rows:
        ph = s.acquisition_phase
        d = deriv_by_series.get(s.id)
        if (
            ph is None
            or d is None
            or not d.s3_bucket
            or not d.s3_key
            or not isinstance(d.geometry, dict)
        ):
            continue
        by_phase.setdefault(ph, (d.s3_bucket, d.s3_key, d.geometry))
    actor_subject_id = user.subject_id if user else None
    await db.close()

    def _pick(prefs: tuple[str, ...]) -> tuple[str, str, str, dict] | None:
        for p in prefs:
            if p in by_phase:
                bucket, key, geom = by_phase[p]
                return (p, bucket, key, geom)
        return None

    a = _pick(_ENHANCED_PHASES)
    b = (
        _pick(_DELAYED_PHASES)
        if metric == "washout"
        else (("unenhanced", *by_phase["unenhanced"]) if "unenhanced" in by_phase else None)
    )
    if a is None or b is None:
        raise _problem(
            422,
            "phases_unavailable",
            "the map needs an enhanced phase plus a "
            + ("delayed" if metric == "washout" else "unenhanced")
            + " phase; not both present",
        )
    phase_a, bucket_a, key_a, geom_a = a
    phase_b, bucket_b, key_b, geom_b = b

    storage = get_s3_storage()
    try:
        hdr_a = await asyncio.to_thread(
            storage.get_object_range, bucket=bucket_a, key=key_a, start=0, length=HEADER_STRUCT.size
        )
        hdr_b = await asyncio.to_thread(
            storage.get_object_range, bucket=bucket_b, key=key_b, start=0, length=HEADER_STRUCT.size
        )
        nxa, nya, nza, sxa, sya, sza, _va0, _va1 = HEADER_STRUCT.unpack(hdr_a)
        nxb, nyb, nzb, sxb, syb, szb, _vb0, _vb1 = HEADER_STRUCT.unpack(hdr_b)
    except Exception as exc:
        raise _problem(422, "volume_unreadable", f"phase volume unreadable ({type(exc).__name__})")

    nxa, nya, nza = int(nxa), int(nya), int(nza)
    same_grid = (
        (nxa, nya, nza) == (int(nxb), int(nyb), int(nzb))
        and np.allclose([sxa, sya, sza], [sxb, syb, szb], atol=1e-3)
        and geom_a.get("frame_of_reference_uid") == geom_b.get("frame_of_reference_uid")
        and np.allclose(geom_a.get("origin") or [0, 0, 0], geom_b.get("origin") or [1, 1, 1])
        and np.allclose(geom_a.get("direction") or [0] * 9, geom_b.get("direction") or [1] * 9)
    )
    if not same_grid:
        raise _problem(
            422,
            "not_coregistered",
            "wash-out map v1 needs the two phases on a common voxel grid "
            "(same geometry); these differ — registration/resampling is a follow-up",
        )

    spacing = (float(sxa), float(sya), float(sza))
    if nxa <= 0 or nya <= 0 or nza <= 0 or nxa * nya * nza * 4 > MAX_VOLUME_BYTES:
        raise _problem(422, "volume_malformed", "volume malformed or too large")

    ijk = world_to_ijk(
        (body.center_lps[0], body.center_lps[1], body.center_lps[2]), geom_a, spacing
    )
    if ijk is None:
        raise _problem(422, "no_geometry", "phase volume has no geometry")
    ci, cj, ck = round(ijk[0]), round(ijk[1]), ijk[2]
    half_i = max(2, int(np.ceil(body.radius_mm / spacing[0])))
    half_j = max(2, int(np.ceil(body.radius_mm / spacing[1])))
    i0, i1 = max(0, ci - half_i), min(nxa - 1, ci + half_i)
    j0, j1 = max(0, cj - half_j), min(nya - 1, cj + half_j)
    if i0 > i1 or j0 > j1:
        raise _problem(422, "roi_outside", "ROI centre falls outside the volume")

    k0, k1 = slab_k_range_sphere(ck, spacing[2], spacing[2], nza)  # 1-slice slab around ck
    if k0 > k1:  # ck past the volume in z -> inverted slab; guard like i/j above
        raise _problem(422, "roi_outside", "ROI centre falls outside the volume")
    start, length = slab_byte_range(k0, k1, nya, nxa, HEADER_STRUCT.size)
    try:
        slab_a = await asyncio.to_thread(
            storage.get_object_range, bucket=bucket_a, key=key_a, start=start, length=length
        )
        slab_b = await asyncio.to_thread(
            storage.get_object_range, bucket=bucket_b, key=key_b, start=start, length=length
        )
        arr_a = np.frombuffer(slab_a, dtype=np.float32).reshape(k1 - k0 + 1, nya, nxa)
        arr_b = np.frombuffer(slab_b, dtype=np.float32).reshape(k1 - k0 + 1, nya, nxa)
    except Exception as exc:
        raise _problem(422, "volume_unreadable", f"phase slab unreadable ({type(exc).__name__})")

    kk = max(0, min(k1 - k0, round(ck) - k0))
    crop_a = arr_a[kk, j0 : j1 + 1, i0 : i1 + 1]
    crop_b = arr_b[kk, j0 : j1 + 1, i0 : i1 + 1]
    rgba, vabs = diff_map_rgba(crop_a, crop_b)
    scale = max(1, 160 // max(1, rgba.shape[1]))
    png = encode_png(rgba, scale=scale)
    del slab_a, slab_b, arr_a, arr_b
    release_memory()

    await audit.log(
        action="washout_map",
        actor_subject_id=actor_subject_id,
        resource_kind="study",
        resource_id=study.id,
        metadata={"metric": metric, "phase_a": phase_a, "phase_b": phase_b},
    )
    return PhaseMapOut(
        metric=metric,
        phase_a=phase_a,
        phase_b=phase_b,
        vabs=round(vabs, 1),
        width=int(crop_a.shape[1]),
        height=int(crop_a.shape[0]),
        png_base64=base64.b64encode(png).decode("ascii"),
    )
