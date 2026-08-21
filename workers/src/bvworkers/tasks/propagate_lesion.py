"""Semi-automatic lesion propagation worker (compute half).

Brings a baseline lesion measurement forward onto a follow-up study so the
user sees whether the tumour grew, with the measurement computed on the
follow-up's REAL voxels (not the warped baseline). Mirrors the
``register_series`` worker pattern: direct DB + S3 writes under the service
subject, the ``Job`` row carries status/progress.

Pipeline (pure pieces are unit-tested on phantoms; persistence is
unit-tested on Postgres — see ``test_registration_core``,
``test_lesion_measure``, ``test_lesion_propagation``):

  baseline finding's mask  ──warp(registration)──▶  seed on follow-up grid
        │                                                   │
        │                                          refine on follow-up voxels
        ▼                                                   ▼
  register(follow-up, baseline)                       measure (volume / Feret / HU)
                                                            │
                                  Segmentation + Finding(system, candidate) + track point
"""

from __future__ import annotations

import logging
import tempfile
import uuid
from datetime import UTC, datetime
from typing import Any

from bvphoenix.db.engine import make_async_engine
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bvworkers.config import get_settings
from bvworkers.job_safety import mark_job_failed_raw, with_safety_net

log = logging.getLogger(__name__)


def _mask_image_from_bytes(raw: bytes, reference: Any) -> Any:
    """Reshape a raw uint8 mask blob (nz, ny, nx, x-fastest) to a SimpleITK
    image sharing ``reference``'s geometry."""
    import numpy as np
    import SimpleITK as sitk  # noqa: N813

    nx, ny, nz = reference.GetSize()
    arr = np.frombuffer(raw, dtype=np.uint8)
    if arr.size != nx * ny * nz:
        raise ValueError(f"mask size {arr.size} != volume {nx * ny * nz}")
    img = sitk.GetImageFromArray(arr.reshape(nz, ny, nx).copy())
    img.CopyInformation(reference)
    return img


async def _fail(db: AsyncSession, rid: str, job_id, code: str, msg: str) -> dict[str, Any]:
    from bvphoenix.services import jobs as jobs_service

    log.error("propagate_lesion %s: %s", rid, msg)
    if job_id is not None:
        await jobs_service.mark_failed(db, job_id, error={"code": code, "message": msg})
        await db.commit()
    return {"status": "error", "reason": code}


@with_safety_net("propagate_lesion")
async def propagate_lesion(
    ctx: dict,  # type: ignore[type-arg]
    track_id: str,
    followup_series_id: str,
    refine: bool = True,
    job_id: str | None = None,
) -> dict[str, Any]:
    """Propagate the baseline lesion of ``track_id`` onto
    ``followup_series_id`` and record the re-measured follow-up Finding +
    timepoint."""
    try:
        tid = uuid.UUID(track_id)
        fu_series_id = uuid.UUID(followup_series_id)
        jid = uuid.UUID(job_id) if job_id else None
    except (TypeError, ValueError) as exc:
        await mark_job_failed_raw(job_id or track_id, code="bad_uuid", message=str(exc))
        return {"status": "error", "reason": f"bad uuid: {exc}"}

    try:
        from bvphoenix.db.models import (
            Derivative,
            Finding,
            FindingGeometry,
            ImagingStudy,
            LesionTrack,
            LesionTrackPoint,
            Registration,
            Segmentation,
            Series,
        )
        from bvphoenix.db.session import SERVICE_SUBJECT, set_current_subject
        from bvphoenix.services import jobs as jobs_service
        from bvphoenix.services.lesion_propagation import (
            PROPAGATION_MODEL_ID,
            persist_propagated_finding,
        )
        from bvphoenix.services.volumes import DERIVATIVE_FORMAT, DERIVATIVE_KIND
        from bvphoenix.storage import get_s3_storage

        from bvworkers.lesion_measure import measure_mask, refine_mask_on_image
        from bvworkers.registration_core import register_pair, warp_mask
        from bvworkers.tasks.registration import _load_series_image
    except ImportError as exc:
        await mark_job_failed_raw(job_id or track_id, code="import_failed", message=str(exc))
        return {"status": "error", "reason": f"import: {exc}"}

    import numpy as np
    import SimpleITK as sitk  # noqa: N813

    settings = get_settings()
    engine = make_async_engine(settings.database_url, pool_pre_ping=True)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as db:
            await set_current_subject(db, SERVICE_SUBJECT)
            if jid is not None:
                await jobs_service.mark_running(db, jid)
                await db.commit()

            track = (
                await db.execute(select(LesionTrack).where(LesionTrack.id == tid))
            ).scalar_one_or_none()
            if track is None or track.deleted_at is not None:
                return await _fail(db, track_id, jid, "track_not_found", "lesion track not found")

            base_point = (
                await db.execute(
                    select(LesionTrackPoint).where(
                        LesionTrackPoint.lesion_track_id == tid,
                        LesionTrackPoint.is_baseline.is_(True),
                    )
                )
            ).scalar_one_or_none()
            if base_point is None:
                return await _fail(db, track_id, jid, "no_baseline", "track has no baseline point")

            baseline = (
                await db.execute(select(Finding).where(Finding.id == base_point.finding_id))
            ).scalar_one_or_none()
            if baseline is None or baseline.series_id is None:
                return await _fail(
                    db, track_id, jid, "no_baseline_finding", "baseline finding/series missing"
                )

            seg = (
                await db.execute(
                    select(Segmentation)
                    .join(FindingGeometry, FindingGeometry.segmentation_id == Segmentation.id)
                    .where(
                        FindingGeometry.finding_id == baseline.id,
                        FindingGeometry.role == "mask",
                    )
                )
            ).scalar_one_or_none()
            if seg is None:
                return await _fail(
                    db,
                    track_id,
                    jid,
                    "no_mask",
                    "baseline finding has no segmentation mask to propagate",
                )

            fu_series = (
                await db.execute(select(Series).where(Series.id == fu_series_id))
            ).scalar_one_or_none()
            if fu_series is None:
                return await _fail(db, track_id, jid, "no_series", "follow-up series not found")
            fu_study = (
                await db.execute(select(ImagingStudy).where(ImagingStudy.id == fu_series.study_id))
            ).scalar_one_or_none()
            if fu_study is None or fu_study.patient_id != track.patient_id:
                return await _fail(
                    db, track_id, jid, "cross_patient", "follow-up series is not this patient's"
                )

            # --- load images (true patient-space LPS) ---
            storage = get_s3_storage()
            base_img = await _load_series_image(db, series_id=baseline.series_id, settings=settings)
            fu_img = await _load_series_image(db, series_id=fu_series_id, settings=settings)
            if base_img is None or fu_img is None:
                return await _fail(
                    db, track_id, jid, "stack_failed", "baseline or follow-up volume unavailable"
                )

            mask_raw = storage.get_object_bytes(bucket=seg.s3_bucket, key=seg.s3_key)
            try:
                base_mask = _mask_image_from_bytes(mask_raw, base_img)
            except ValueError as exc:
                return await _fail(db, track_id, jid, "mask_shape", str(exc))

            # --- register, warp, refine, measure ---
            try:
                transform, reg_meta = register_pair(fu_img, base_img, "rigid")
            except RuntimeError as exc:
                return await _fail(db, track_id, jid, "sitk_failed", f"registration: {exc}")

            warped = warp_mask(base_mask, transform, fu_img)
            refined = refine_mask_on_image(warped, fu_img) if refine else warped
            m = measure_mask(refined)
            if m["n_voxels"] == 0:
                return await _fail(
                    db, track_id, jid, "empty_result", "re-segmentation produced an empty mask"
                )
            fu_arr = sitk.GetArrayFromImage(sitk.Cast(fu_img, sitk.sitkFloat32))
            mask_arr = sitk.GetArrayFromImage(sitk.Cast(refined > 0, sitk.sitkUInt8)).astype(bool)
            vals = fu_arr[mask_arr]
            m["hu_mean"] = float(vals.mean())
            m["hu_std"] = float(vals.std())

            # --- save transform + Registration row (provenance / reuse) ---
            with tempfile.NamedTemporaryFile(suffix=".tfm", delete=False) as tf:
                tfm_path = tf.name
            sitk.WriteTransform(transform, tfm_path)
            with open(tfm_path, "rb") as f:
                tfm_bytes = f.read()
            reg = Registration(
                fixed_series_id=fu_series_id,
                moving_series_id=baseline.series_id,
                kind="rigid",
                status="succeeded",
                result_meta=reg_meta,
                # System-authored: the actor is the worker, not a subject row.
                # ``SERVICE_SUBJECT`` is the RLS principal string, not a UUID;
                # the subject FK is nullable and provenance is carried by the
                # status/result_meta, so this stays null.
                requested_by_subject_id=None,
                finished_at=datetime.now(UTC),
            )
            db.add(reg)
            await db.flush()
            reg_key = f"registrations/{reg.id}.tfm"
            storage.upload_bytes(tfm_bytes, bucket=settings.s3_bucket_versioning, key=reg_key)
            await db.execute(
                update(Registration)
                .where(Registration.id == reg.id)
                .values(s3_bucket=settings.s3_bucket_versioning, s3_key=reg_key)
            )

            # --- persist the refined mask as a Segmentation on the follow-up ---
            label = f"lesion-track-{tid}"
            seg_key = f"segmentations/{fu_series_id}/{label}.bin"
            mask_bytes = (
                sitk.GetArrayFromImage(sitk.Cast(refined > 0, sitk.sitkUInt8))
                .astype(np.uint8)
                .tobytes()
            )
            storage.upload_bytes(mask_bytes, bucket=settings.s3_bucket_derivatives, key=seg_key)
            new_seg = Segmentation(
                series_id=fu_series_id,
                producer="propagated",
                producer_version=PROPAGATION_MODEL_ID,
                label=label,
                s3_bucket=settings.s3_bucket_derivatives,
                s3_key=seg_key,
                size_bytes=len(mask_bytes),
                nonzero_voxels=int(m["n_voxels"]),
                patient_id=track.patient_id,
                author_kind="system",
                model_id=PROPAGATION_MODEL_ID,
                # System-authored mask; ``author_kind`` carries provenance and
                # the subject FK is nullable (SERVICE_SUBJECT is not a UUID).
                created_by_subject_id=None,
            )
            db.add(new_seg)
            await db.flush()

            # --- the follow-up Finding + track timepoint (medical write) ---
            fu_geom = (
                await db.execute(
                    select(Derivative.geometry).where(
                        Derivative.series_id == fu_series_id,
                        Derivative.kind == DERIVATIVE_KIND,
                        Derivative.format == DERIVATIVE_FORMAT,
                        Derivative.stack_index == 0,
                    )
                )
            ).scalar_one_or_none()
            for_uid = (fu_geom or {}).get("frame_of_reference_uid") if fu_geom else None

            finding, point = await persist_propagated_finding(
                db,
                track=track,
                baseline_finding=baseline,
                followup_study_id=fu_study.id,
                followup_series_id=fu_series_id,
                frame_of_reference_uid=for_uid,
                measurements=m,
                segmentation_id=new_seg.id,
                registration_id=reg.id,
                timepoint_date=fu_study.study_date,
                # System-authored finding; persist_propagated_finding accepts
                # ``subject_id=None`` and stamps author_kind='system'.
                subject_id=None,
            )
            result = {
                "status": "succeeded",
                "finding_id": str(finding.id),
                "point_id": str(point.id),
                "registration_id": str(reg.id),
                "volume_ml": m["volume_ml"],
                "longest_diameter_mm": m["longest_diameter_mm"],
                "baseline_volume_ml": baseline.volume_ml,
            }
            if jid is not None:
                await jobs_service.mark_succeeded(db, jid, result_uri=f"finding:{finding.id}")
            await db.commit()
            return result
    finally:
        await engine.dispose()
