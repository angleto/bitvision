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
from bvphoenix.services.roi_sampling import sample_bbox_hu, sample_sphere_hu, world_to_ijk
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


class PhaseWashoutOut(BaseModel):
    unenhanced_phase: str | None = None
    enhanced_phase: str | None = None
    delayed_phase: str | None = None
    unenhanced_hu: float | None = None
    enhanced_hu: float | None = None
    delayed_hu: float | None = None
    absolute_enhancement_hu: float | None = None
    apw: float | None = None
    rpw: float | None = None
    apw_ge_60: bool | None = None
    rpw_ge_40: bool | None = None
    unenhanced_below_10hu: bool | None = None
    curve: list[PhaseCurvePointOut]


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
            cached = await asyncio.to_thread(storage.get_object_bytes, bucket=bucket, key=key)
            nx, ny, nz, sx, sy, sz, _vmin, _vmax = HEADER_STRUCT.unpack_from(cached, 0)
            arr = np.frombuffer(cached, dtype=np.float32, offset=HEADER_STRUCT.size).reshape(
                int(nz), int(ny), int(nx)
            )
            spacing = (float(sx), float(sy), float(sz))
            if body.kind == "sphere":
                assert body.center_lps is not None and body.radius_mm is not None
                ijk = world_to_ijk(tuple(body.center_lps), geom, spacing)
                if ijk is None:
                    raise ValueError("volume has no geometry")
                stats = sample_sphere_hu(arr, spacing, ijk, float(body.radius_mm))
            else:
                assert body.min_lps is not None and body.max_lps is not None
                ijk_min = world_to_ijk(tuple(body.min_lps), geom, spacing)
                ijk_max = world_to_ijk(tuple(body.max_lps), geom, spacing)
                if ijk_min is None or ijk_max is None:
                    raise ValueError("volume has no geometry")
                stats = sample_bbox_hu(arr, ijk_min, ijk_max)
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

    result = compute_washout(points)
    washout = PhaseWashoutOut(
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
    )

    await audit.log(
        action="phase_roi_stats",
        actor_subject_id=actor_subject_id,
        resource_kind="study",
        resource_id=study_id_val,
        metadata={"kind": body.kind, "phases_sampled": len(samples), "skipped": len(skipped)},
    )
    return PhaseRoiStatsOut(
        study_id=study_id_val,
        reference_frame_of_reference_uid=ref_for,
        samples=samples,
        skipped=skipped,
        washout=washout,
    )
