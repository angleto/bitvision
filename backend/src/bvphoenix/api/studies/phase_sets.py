# ruff: noqa: F405
"""Persisted cross-phase wash-out measurements (PhaseEnhancementSet).

CRUD over a saved wash-out: an ROI + per-phase HU samples + the derived
APW/RPW, stored as an auditable, provenance-stamped, soft-deletable record
that can land in a report. The wash-out indices are recomputed (purely,
``services.washout``) from the supplied samples on create, so the stored
numbers are always consistent with the samples — the heavy S3 sampling is
done once by ``POST /studies/{id}/phase-roi-stats`` and the measured values
are saved here (no S3 re-read on persist).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter

from bvphoenix.api.studies import _shared
from bvphoenix.api.studies._shared import *  # noqa: F403
from bvphoenix.auth import enforce_agent_patient_scope
from bvphoenix.db.models import PhaseEnhancementSet
from bvphoenix.db.models.phase_enhancement import PHASE_ENH_ROI_KINDS
from bvphoenix.services.washout import PhaseHu, compute_washout

router = APIRouter()

_PURGE_GRACE = timedelta(days=30)


class PhaseSampleIn(BaseModel):
    series_id: uuid.UUID
    acquisition_phase: str
    hu_mean: float
    hu_std: float | None = None
    voxel_count: int | None = None


class PhaseEnhancementCreateIn(BaseModel):
    roi_kind: str = Field(description="sphere | bbox")
    roi: dict = Field(description="ROI coords in LPS (center_lps+radius_mm | min_lps+max_lps)")
    label: str | None = None
    samples: list[PhaseSampleIn]
    # Scopes the recomputed indices/flags: "adrenal" emits the adenoma verdict
    # flags, "liver" withholds the adrenal indices, None/other returns the raw
    # numbers without flags. Mirrors POST /phase-roi-stats.
    region: str | None = Field(default=None, description="adrenal | liver | other")
    dry_run: bool = False


class PhaseEnhancementOut(BaseModel):
    id: uuid.UUID
    study_id: uuid.UUID
    patient_id: uuid.UUID
    label: str | None
    roi_kind: str
    roi: dict
    samples: list[dict] | None
    washout: dict | None
    apw: float | None
    rpw: float | None
    enhanced_phase: str | None
    delayed_phase: str | None
    author_kind: str
    etag: str
    deleted_at: str | None
    created_at: str | None


def _provenance(request: Request) -> tuple[str, uuid.UUID | None]:
    if getattr(request.state, "is_agent", False):
        token = getattr(request.state, "agent_token", None)
        return "agent", getattr(token, "id", None)
    return "human", None


def _washout_to_dict(r) -> dict:  # type: ignore[no-untyped-def]
    return {
        "region": r.region,
        "unenhanced_phase": r.unenhanced_phase,
        "enhanced_phase": r.enhanced_phase,
        "delayed_phase": r.delayed_phase,
        "unenhanced_hu": r.unenhanced_hu,
        "enhanced_hu": r.enhanced_hu,
        "delayed_hu": r.delayed_hu,
        "absolute_enhancement_hu": r.absolute_enhancement_hu,
        "apw": r.apw,
        "rpw": r.rpw,
        "apw_ge_60": r.apw_ge_60,
        "rpw_ge_40": r.rpw_ge_40,
        "unenhanced_below_10hu": r.unenhanced_below_10hu,
        "curve": [
            {"acquisition_phase": p.acquisition_phase, "hu_mean": p.hu_mean} for p in r.curve
        ],
        "parenchyma_curve": [
            {"acquisition_phase": p.acquisition_phase, "hu_mean": p.hu_mean}
            for p in r.parenchyma_curve
        ],
        "relative_curve": [
            {
                "acquisition_phase": x.acquisition_phase,
                "lesion_hu": x.lesion_hu,
                "parenchyma_hu": x.parenchyma_hu,
                "delta_hu": x.delta_hu,
            }
            for x in r.relative_curve
        ],
    }


def _out(pes: PhaseEnhancementSet) -> PhaseEnhancementOut:
    return PhaseEnhancementOut(
        id=pes.id,
        study_id=pes.study_id,
        patient_id=pes.patient_id,
        label=pes.label,
        roi_kind=pes.roi_kind,
        roi=pes.roi,
        samples=pes.samples,
        washout=pes.washout,
        apw=pes.apw,
        rpw=pes.rpw,
        enhanced_phase=pes.enhanced_phase,
        delayed_phase=pes.delayed_phase,
        author_kind=pes.author_kind,
        etag=str(pes.etag),
        deleted_at=pes.deleted_at.isoformat() if pes.deleted_at else None,
        created_at=pes.created_at.isoformat() if pes.created_at else None,
    )


async def _set_for_write(
    db: AsyncSession,
    request: Request,
    user: User,
    set_id: uuid.UUID,
    *,
    allow_deleted: bool = False,
) -> tuple[PhaseEnhancementSet, ImagingStudy]:
    from bvphoenix.middleware.problem_details import problem as _problem

    pes = (
        await db.execute(select(PhaseEnhancementSet).where(PhaseEnhancementSet.id == set_id))
    ).scalar_one_or_none()
    if pes is None or (pes.deleted_at is not None and not allow_deleted):
        raise _problem(404, "not_found", "phase-enhancement-set not found")
    study = (
        await db.execute(select(ImagingStudy).where(ImagingStudy.id == pes.study_id))
    ).scalar_one_or_none()
    if study is None or not await can(db, user=user, action=READ_PIXELS, study=study):
        raise _problem(404, "not_found", "phase-enhancement-set not found")
    enforce_agent_patient_scope(request, study.patient_id, scope="patient:images")
    is_owner = study.owner_subject_id == user.subject_id or getattr(user, "is_admin", False)
    if not is_owner:
        raise _problem(403, "forbidden", "phase-enhancement-set write requires study ownership")
    # An agent token cannot mutate a human-authored measurement.
    if (
        getattr(request.state, "is_agent", False)
        and pes.author_kind == "human"
        and not getattr(user, "is_admin", False)
    ):
        raise _problem(403, "forbidden", "agent tokens cannot mutate human-authored measurements")
    return pes, study


@router.post("/studies/{study_id}/phase-enhancement-sets", response_model=PhaseEnhancementOut)
async def create_phase_enhancement_set(
    request: Request,
    study_id: uuid.UUID,
    body: PhaseEnhancementCreateIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    audit: AuditDep,
) -> PhaseEnhancementOut:
    """Persist a wash-out measurement. Recomputes APW/RPW from the supplied
    per-phase samples (pure) so the stored indices match the samples."""
    from bvphoenix.middleware.problem_details import problem as _problem

    if body.roi_kind not in PHASE_ENH_ROI_KINDS:
        raise _problem(422, "invalid_roi", f"roi_kind must be one of {list(PHASE_ENH_ROI_KINDS)}")

    study = (
        await db.execute(select(ImagingStudy).where(ImagingStudy.id == study_id))
    ).scalar_one_or_none()
    if study is None:
        raise _problem(404, "not_found", "study not found")
    if not await can(db, user=user, action=READ_PIXELS, study=study):
        raise _problem(404, "not_found", "study not found")
    enforce_agent_scope(request, "findings:write")
    enforce_agent_patient_scope(request, study.patient_id, scope="patient:images")
    is_owner = study.owner_subject_id == user.subject_id or getattr(user, "is_admin", False)
    if not is_owner:
        raise _problem(403, "forbidden", "phase-enhancement-set write requires study ownership")
    if study.patient_id is None:
        raise _problem(422, "no_patient", "study has no patient; cannot persist a measurement")

    # Cross-study safety: every sampled series must belong to this study.
    sample_sids = {s.series_id for s in body.samples}
    if sample_sids:
        found = set(
            (
                await db.execute(
                    select(Series.id).where(Series.study_id == study.id, Series.id.in_(sample_sids))
                )
            )
            .scalars()
            .all()
        )
        bad = sample_sids - found
        if bad:
            raise _problem(
                422,
                "invalid_samples",
                "samples reference series that are not in this study",
                extra={"foreign_series": [str(s) for s in bad]},
            )

    result = compute_washout(
        [PhaseHu(acquisition_phase=s.acquisition_phase, hu_mean=s.hu_mean) for s in body.samples],
        region=body.region,
    )
    author_kind, agent_token_id = _provenance(request)
    pes = PhaseEnhancementSet(
        patient_id=study.patient_id,
        study_id=study.id,
        label=body.label,
        roi_kind=body.roi_kind,
        roi=body.roi,
        samples=[s.model_dump(mode="json") for s in body.samples],
        washout=_washout_to_dict(result),
        apw=result.apw,
        rpw=result.rpw,
        enhanced_phase=result.enhanced_phase,
        delayed_phase=result.delayed_phase,
        author_subject_id=user.subject_id,
        author_kind=author_kind,
        agent_token_id=agent_token_id,
        etag=uuid.uuid4(),
    )

    if body.dry_run:
        # Not persisted: the server-default id/etag aren't applied, so stamp
        # placeholders to render the would-be record.
        pes.id = pes.id or uuid.uuid4()
        out = _out(pes)
        out.etag = "dry-run"
        return out

    db.add(pes)
    await db.commit()
    await db.refresh(pes)
    await audit.log(
        action="phase_enhancement_set_create",
        actor_subject_id=user.subject_id,
        resource_kind="phase_enhancement_set",
        resource_id=pes.id,
        metadata={"study_id": str(study.id), "apw": result.apw, "rpw": result.rpw},
    )
    return _out(pes)


@router.get("/studies/{study_id}/phase-enhancement-sets", response_model=list[PhaseEnhancementOut])
async def list_phase_enhancement_sets(
    request: Request,
    study_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(optional_user)],
    include_deleted: bool = Query(False),
) -> list[PhaseEnhancementOut]:
    from bvphoenix.middleware.problem_details import problem as _problem

    study = (
        await db.execute(select(ImagingStudy).where(ImagingStudy.id == study_id))
    ).scalar_one_or_none()
    if study is None:
        raise _problem(404, "not_found", "study not found")
    if not await can(db, user=user, action=READ_METADATA, study=study):
        raise _problem(404, "not_found", "study not found")
    enforce_agent_patient_scope(request, study.patient_id, scope="patient:images")

    stmt = select(PhaseEnhancementSet).where(PhaseEnhancementSet.study_id == study.id)
    if not include_deleted:
        stmt = stmt.where(PhaseEnhancementSet.deleted_at.is_(None))
    stmt = stmt.order_by(PhaseEnhancementSet.created_at.desc())
    rows = (await db.execute(stmt)).scalars().all()
    return [_out(p) for p in rows]


@router.get("/phase-enhancement-sets/{set_id}", response_model=PhaseEnhancementOut)
async def get_phase_enhancement_set(
    request: Request,
    set_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(optional_user)],
) -> PhaseEnhancementOut:
    from bvphoenix.middleware.problem_details import problem as _problem

    pes = (
        await db.execute(select(PhaseEnhancementSet).where(PhaseEnhancementSet.id == set_id))
    ).scalar_one_or_none()
    if pes is None:
        raise _problem(404, "not_found", "phase-enhancement-set not found")
    study = (
        await db.execute(select(ImagingStudy).where(ImagingStudy.id == pes.study_id))
    ).scalar_one_or_none()
    if study is None or not await can(db, user=user, action=READ_METADATA, study=study):
        raise _problem(404, "not_found", "phase-enhancement-set not found")
    enforce_agent_patient_scope(request, study.patient_id, scope="patient:images")
    return _out(pes)


@router.delete("/phase-enhancement-sets/{set_id}", response_model=PhaseEnhancementOut)
async def delete_phase_enhancement_set(
    request: Request,
    set_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    audit: AuditDep,
    reason: str | None = Query(None),
) -> PhaseEnhancementOut:
    pes, study = await _set_for_write(db, request, user, set_id)
    enforce_agent_scope(request, "findings:write")
    if pes.deleted_at is None:
        now = datetime.now(UTC)
        pes.deleted_at = now
        pes.purge_after = now + _PURGE_GRACE
        pes.delete_reason = reason
        pes.etag = uuid.uuid4()
        await db.commit()
        await db.refresh(pes)
        await audit.log(
            action="phase_enhancement_set_delete",
            actor_subject_id=user.subject_id,
            resource_kind="phase_enhancement_set",
            resource_id=pes.id,
            metadata={"study_id": str(study.id)},
        )
    return _out(pes)


@router.post("/phase-enhancement-sets/{set_id}/restore", response_model=PhaseEnhancementOut)
async def restore_phase_enhancement_set(
    request: Request,
    set_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    audit: AuditDep,
) -> PhaseEnhancementOut:
    pes, study = await _set_for_write(db, request, user, set_id, allow_deleted=True)
    enforce_agent_scope(request, "findings:write")
    if pes.deleted_at is not None:
        pes.deleted_at = None
        pes.purge_after = None
        pes.delete_reason = None
        pes.etag = uuid.uuid4()
        await db.commit()
        await db.refresh(pes)
        await audit.log(
            action="phase_enhancement_set_restore",
            actor_subject_id=user.subject_id,
            resource_kind="phase_enhancement_set",
            resource_id=pes.id,
            metadata={"study_id": str(study.id)},
        )
    return _out(pes)
