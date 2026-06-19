# ruff: noqa: F405
# Auto-split from api/studies.py on 2026-05-21.
# Section: ``registrations``.

from __future__ import annotations

from fastapi import APIRouter

from bvphoenix.api.studies import _shared
from bvphoenix.api.studies._shared import *  # noqa: F403

router = APIRouter()


@router.post(
    "/registrations",
    response_model=RegistrationOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_registration(
    body: RegistrationCreateIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    audit: AuditDep,
) -> RegistrationOut:
    """Enqueue a cross-modal registration job (Sprint 6, P3).

    Permission gate: the caller must have ``READ_PIXELS`` on both
    series (the moving series gets resampled, the fixed one is the
    target — neither is mutated, but pixel access is needed to load
    them into the worker).
    """
    from arq import create_pool as _create_pool

    from bvphoenix.db.models import REGISTRATION_KINDS, Registration
    from bvphoenix.middleware.problem_details import problem as _problem
    from bvphoenix.services import jobs as jobs_service
    from bvphoenix.services.arq_redis import redis_settings as _redis_settings

    if body.kind not in REGISTRATION_KINDS:
        raise _problem(
            422,
            "invalid_kind",
            f"kind must be one of {REGISTRATION_KINDS}, got {body.kind!r}",
        )

    fixed_row = (
        await db.execute(
            select(Series, ImagingStudy)
            .join(ImagingStudy, ImagingStudy.id == Series.study_id)
            .where(Series.id == body.fixed_series_id)
        )
    ).first()
    moving_row = (
        await db.execute(
            select(Series, ImagingStudy)
            .join(ImagingStudy, ImagingStudy.id == Series.study_id)
            .where(Series.id == body.moving_series_id)
        )
    ).first()
    if fixed_row is None or moving_row is None:
        raise _problem(404, "not_found", "fixed or moving series not found")
    fixed_series, fixed_study = fixed_row
    moving_series, moving_study = moving_row
    if not await can(db, user=user, action=READ_PIXELS, study=fixed_study):
        raise _problem(403, "forbidden", "READ_PIXELS denied on fixed series")
    if not await can(db, user=user, action=READ_PIXELS, study=moving_study):
        raise _problem(403, "forbidden", "READ_PIXELS denied on moving series")

    # Same-patient guard. Cross-STUDY registration of one patient's
    # baseline vs follow-up is the whole point; registering two DIFFERENT
    # patients' series is forbidden — a cross-patient spatial transform
    # is exactly where a measurement could be mis-attributed. (Mirrors the
    # platform's cross-patient-impossible invariant; CI security test in
    # tests/test_registration_same_patient.py.)
    if fixed_study.patient_id is None or moving_study.patient_id is None:
        raise _problem(
            422,
            "no_patient",
            "both series must belong to a patient to be registered",
        )
    if fixed_study.patient_id != moving_study.patient_id:
        raise _problem(
            422,
            "cross_patient",
            "cross-patient registration is not allowed: the fixed and moving "
            "series belong to different patients",
        )

    # Pre-pack guarantee: the worker registers fastest from packed volume_f32
    # derivatives (and the viewer reuses them). If a series isn't packed yet,
    # enqueue pack_volume so the registration hits the fast path instead of
    # re-stacking raw DICOM — the slow path that can blow the FE poll window.
    packed_sids = set(
        (
            await db.execute(
                select(Derivative.series_id).where(
                    Derivative.series_id.in_([fixed_series.id, moving_series.id]),
                    Derivative.kind == DERIVATIVE_KIND,
                    Derivative.format == DERIVATIVE_FORMAT,
                    Derivative.stack_index == 0,
                )
            )
        )
        .scalars()
        .all()
    )
    series_needing_pack = [
        sid for sid in (fixed_series.id, moving_series.id) if sid not in packed_sids
    ]

    reg = Registration(
        fixed_series_id=fixed_series.id,
        moving_series_id=moving_series.id,
        kind=body.kind,
        status="queued",
        requested_by_subject_id=user.subject_id,
    )
    db.add(reg)
    await db.flush()

    job_result = await jobs_service.enqueue_or_get(
        db,
        kind="register_series",
        owner_subject_id=user.subject_id,
        canonical_input={
            "registration_id": str(reg.id),
            "fixed_series_id": str(fixed_series.id),
            "moving_series_id": str(moving_series.id),
            "kind": body.kind,
        },
        scope_ids=[str(reg.id)],
    )
    reg.job_id = job_result.job.id
    await db.commit()
    await db.refresh(reg)

    if not job_result.deduped:
        try:
            settings = get_settings()
            redis = await _create_pool(_redis_settings(settings.redis_url))
            for sid in series_needing_pack:
                await redis.enqueue_job("pack_volume", str(sid))
            arq_handle = await redis.enqueue_job("register_series", str(reg.id))
            await redis.close()
            if arq_handle is not None:
                await jobs_service.set_arq_job_id(db, job_result.job.id, arq_handle.job_id)
                await db.commit()
        except Exception as exc:  # pragma: no cover - defensive
            await jobs_service.mark_failed(
                db,
                job_result.job.id,
                error={"code": "enqueue_failed", "message": str(exc)},
            )
            await db.commit()
            raise _problem(503, "service_unavailable", "registration enqueue failed") from exc

    await audit.log(
        action="registration_create",
        actor_subject_id=user.subject_id,
        resource_kind="registration",
        resource_id=reg.id,
        metadata={
            "kind": body.kind,
            "fixed_series_id": str(fixed_series.id),
            "moving_series_id": str(moving_series.id),
        },
    )
    return _registration_to_out(reg)


@router.get(
    "/registrations/{registration_id}",
    response_model=RegistrationOut,
)
async def get_registration(
    registration_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
) -> RegistrationOut:
    """Read a registration row + (when succeeded) a backend URL that
    streams the saved transform / warp field through this process. Mirrors
    the linked Job's stage/progress so the viewer can show live status."""
    from bvphoenix.db.models import Job, Registration
    from bvphoenix.middleware.problem_details import problem as _problem

    reg = (
        await db.execute(select(Registration).where(Registration.id == registration_id))
    ).scalar_one_or_none()
    if reg is None:
        raise _problem(404, "not_found", "registration not found")
    if reg.requested_by_subject_id != user.subject_id and not getattr(user, "is_admin", False):
        raise _problem(403, "forbidden", "not your registration")

    stage: str | None = None
    progress_done: int | None = None
    progress_total: int | None = None
    if reg.job_id is not None:
        job = (await db.execute(select(Job).where(Job.id == reg.job_id))).scalar_one_or_none()
        if job is not None:
            stage = job.stage
            progress_done = job.progress_done
            progress_total = job.progress_total

    download_url: str | None = None
    if reg.s3_bucket and reg.s3_key:
        download_url = f"/api/registrations/{reg.id}/file"
    return _registration_to_out(
        reg,
        download_url=download_url,
        stage=stage,
        progress_done=progress_done,
        progress_total=progress_total,
    )


@router.post("/registrations/{registration_id}/cancel", response_model=RegistrationOut)
async def cancel_registration(
    registration_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    audit: AuditDep,
) -> RegistrationOut:
    """Cancel a queued/running registration. The viewer calls this when the
    user dismisses a long-running alignment. Terminal rows are left as-is."""
    from datetime import UTC, datetime

    from bvphoenix.db.models import Registration
    from bvphoenix.middleware.problem_details import problem as _problem
    from bvphoenix.services import jobs as jobs_service

    reg = (
        await db.execute(select(Registration).where(Registration.id == registration_id))
    ).scalar_one_or_none()
    if reg is None:
        raise _problem(404, "not_found", "registration not found")
    if reg.requested_by_subject_id != user.subject_id and not getattr(user, "is_admin", False):
        raise _problem(403, "forbidden", "not your registration")

    if reg.status in ("queued", "running"):
        reg.status = "cancelled"
        reg.finished_at = datetime.now(UTC)
        if reg.job_id is not None:
            # Best-effort: also cancel the linked Job (no-op if it already
            # reached a terminal state in the meantime).
            from bvphoenix.services.jobs import (
                JobAlreadyTerminalError,
                JobNotFoundError,
            )

            try:
                await jobs_service.request_cancellation(
                    db,
                    reg.job_id,
                    user.subject_id,
                    is_admin=getattr(user, "is_admin", False),
                )
            except (JobNotFoundError, JobAlreadyTerminalError):
                pass
        await db.commit()
        await db.refresh(reg)
        await audit.log(
            action="registration_cancel",
            actor_subject_id=user.subject_id,
            resource_kind="registration",
            resource_id=reg.id,
        )
    return _registration_to_out(reg)


@router.get("/registrations/{registration_id}/file")
async def download_registration(
    registration_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    audit: AuditDep,
) -> StreamingResponse:
    """Stream a registration transform through the backend.

    Storage isolation: the registration's bucket/key never appear in
    the response. Same ownership gate as ``get_registration``.
    """
    from bvphoenix.db.models import Registration
    from bvphoenix.middleware.problem_details import problem as _problem

    reg = (
        await db.execute(select(Registration).where(Registration.id == registration_id))
    ).scalar_one_or_none()
    if reg is None:
        raise _problem(404, "not_found", "registration not found")
    if reg.requested_by_subject_id != user.subject_id and not getattr(user, "is_admin", False):
        raise _problem(403, "forbidden", "not your registration")
    if not reg.s3_bucket or not reg.s3_key:
        raise _problem(404, "not_found", "registration has no payload")

    storage = get_s3_storage()
    try:
        body_iter, length, _ = await asyncio.to_thread(
            storage.iter_object,
            bucket=reg.s3_bucket,
            key=reg.s3_key,
        )
    except Exception as exc:
        raise _problem(
            404,
            "binary_unavailable",
            "registration binary unavailable",
        ) from exc

    await audit.log(
        action="registration_download",
        actor_subject_id=user.subject_id,
        resource_kind="registration",
        resource_id=reg.id,
    )

    headers: dict[str, str] = {
        "content-disposition": _content_disposition(f"{reg.id}.tfm", disposition="attachment"),
        "cache-control": "private, max-age=0",
    }
    if length is not None:
        headers["content-length"] = str(length)
    return StreamingResponse(
        body_iter,
        media_type="application/octet-stream",
        headers=headers,
    )
