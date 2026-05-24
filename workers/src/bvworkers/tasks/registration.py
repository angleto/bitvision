"""Cross-modal series registration worker (Sprint 6, P3).

Implements two ``kind`` values:

* ``rigid``: ``SimpleITK.ImageRegistrationMethod`` with a Mattes
  mutual-information metric and a regular step gradient descent
  optimiser. Classic recipe for inter-modality (CT, MR, PET) alignment.

* ``demons``: rigid pre-init (same as above) followed by
  ``FastSymmetricForcesDemonsRegistrationFilter`` to capture
  non-rigid local deformation. Output is a CompositeTransform
  combining the rigid pre-step and the dense displacement field.
  Suitable when the moving series has organ shift / breathing-cycle
  deformation residual after the rigid step.

Output: a SimpleITK ``.tfm`` file uploaded to
``s3_bucket_versioning/registrations/<id>.tfm``. The downstream agent
fetches it via ``GET /api/registrations/:id`` (signed URL).
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from bvworkers.config import get_settings
from bvworkers.job_safety import mark_job_failed_raw, with_safety_net

log = logging.getLogger(__name__)


async def _series_volume(db: AsyncSession, *, series_id: uuid.UUID, settings) -> object | None:
    """Stack the series instances into a SimpleITK Image (in-memory).

    Returns ``None`` when the series has no pixel-bearing instances or
    they cannot be stacked (mismatched in-plane shape).
    """
    import io as _io

    import numpy as np
    import pydicom
    import SimpleITK as sitk  # noqa: N813 — community-standard alias
    from bvphoenix.db.models import Instance, Series
    from bvphoenix.services.thumbnails import is_image_sop_class
    from bvphoenix.storage import get_s3_storage

    series = (await db.execute(select(Series).where(Series.id == series_id))).scalar_one_or_none()
    if series is None:
        return None
    instances = (
        (
            await db.execute(
                select(Instance)
                .where(Instance.series_id == series.id)
                .order_by(Instance.instance_number.asc().nullslast())
            )
        )
        .scalars()
        .all()
    )
    if not instances:
        return None

    storage = get_s3_storage()
    datasets: list[pydicom.Dataset] = []
    for inst in instances:
        if not is_image_sop_class(getattr(inst, "sop_class_uid", None)):
            continue
        raw = storage.get_object_bytes(bucket=inst.s3_bucket, key=inst.s3_key)
        ds = pydicom.dcmread(_io.BytesIO(raw))
        if "PixelData" not in ds:
            continue
        datasets.append(ds)
    if len(datasets) < 2:
        return None

    rows = int(datasets[0].Rows)
    cols = int(datasets[0].Columns)
    pixel_spacing = list(getattr(datasets[0], "PixelSpacing", [1.0, 1.0]))
    row_mm = float(pixel_spacing[0])
    col_mm = float(pixel_spacing[1])
    slice_mm = float(
        getattr(datasets[0], "SpacingBetweenSlices", None)
        or getattr(datasets[0], "SliceThickness", None)
        or 1.0
    )

    arr = np.empty((len(datasets), rows, cols), dtype=np.float32)
    for i, ds in enumerate(datasets):
        if int(ds.Rows) != rows or int(ds.Columns) != cols:
            return None
        slope = float(getattr(ds, "RescaleSlope", 1.0) or 1.0)
        intercept = float(getattr(ds, "RescaleIntercept", 0.0) or 0.0)
        arr[i] = ds.pixel_array.astype(np.float32) * slope + intercept

    img = sitk.GetImageFromArray(arr)
    img.SetSpacing((col_mm, row_mm, slice_mm))
    return img


@with_safety_net("register_series")
async def register_series(
    ctx: dict,  # type: ignore[type-arg]
    registration_id: str,
) -> dict[str, Any]:
    """Run the registration pipeline.

    The job row is keyed on ``Registration.id`` (not ``Job.id``)
    because the pipeline output is conceptually attached to the
    registration record; ``Job`` carries progress + retries via the
    standard ``services.jobs`` plumbing.
    """
    try:
        rid = uuid.UUID(registration_id)
    except (TypeError, ValueError) as exc:
        log.error("invalid registration_id: %s", exc)
        return {"status": "error", "reason": f"invalid uuid: {exc}"}

    try:
        from bvphoenix.db.models import Registration
        from bvphoenix.db.session import SERVICE_SUBJECT, set_current_subject
        from bvphoenix.services import jobs as jobs_service
        from bvphoenix.storage import get_s3_storage
    except ImportError as exc:
        log.exception("bvphoenix import failed: %s", exc)
        await mark_job_failed_raw(registration_id, code="bvphoenix_import_failed", message=str(exc))
        return {"status": "error", "reason": f"import: {exc}"}

    import SimpleITK as sitk  # noqa: N813 — community-standard alias

    settings = get_settings()
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as db:
            await set_current_subject(db, SERVICE_SUBJECT)
            reg = (
                await db.execute(select(Registration).where(Registration.id == rid))
            ).scalar_one_or_none()
            if reg is None:
                return {"status": "error", "reason": "registration_not_found"}
            if reg.job_id is not None:
                await jobs_service.mark_running(db, reg.job_id)
            await db.execute(
                update(Registration).where(Registration.id == rid).values(status="running")
            )
            await db.commit()

            if reg.kind not in ("rigid", "demons"):
                msg = f"registration kind {reg.kind!r} not supported"
                await db.execute(
                    update(Registration)
                    .where(Registration.id == rid)
                    .values(
                        status="failed",
                        error=msg,
                        finished_at=datetime.now(UTC),
                    )
                )
                if reg.job_id is not None:
                    await jobs_service.mark_failed(
                        db, reg.job_id, error={"code": "unsupported_kind", "message": msg}
                    )
                await db.commit()
                return {"status": "error", "reason": "unsupported_kind"}

            fixed = await _series_volume(db, series_id=reg.fixed_series_id, settings=settings)
            moving = await _series_volume(db, series_id=reg.moving_series_id, settings=settings)
            if fixed is None or moving is None:
                msg = "fixed or moving series cannot be stacked"
                await db.execute(
                    update(Registration)
                    .where(Registration.id == rid)
                    .values(
                        status="failed",
                        error=msg,
                        finished_at=datetime.now(UTC),
                    )
                )
                if reg.job_id is not None:
                    await jobs_service.mark_failed(
                        db, reg.job_id, error={"code": "stack_failed", "message": msg}
                    )
                await db.commit()
                return {"status": "error", "reason": "stack_failed"}

            # Cast to a common float pixel type so the metric is
            # well-defined across CT (Hounsfield) and MR (signal).
            fixed_f = sitk.Cast(fixed, sitk.sitkFloat32)
            moving_f = sitk.Cast(moving, sitk.sitkFloat32)

            initial = sitk.CenteredTransformInitializer(
                fixed_f,
                moving_f,
                sitk.Euler3DTransform(),
                sitk.CenteredTransformInitializerFilter.GEOMETRY,
            )

            registration = sitk.ImageRegistrationMethod()
            registration.SetMetricAsMattesMutualInformation(numberOfHistogramBins=50)
            registration.SetMetricSamplingStrategy(registration.RANDOM)
            registration.SetMetricSamplingPercentage(0.10)
            registration.SetInterpolator(sitk.sitkLinear)
            registration.SetOptimizerAsRegularStepGradientDescent(
                learningRate=1.0,
                minStep=1e-4,
                numberOfIterations=100,
            )
            registration.SetInitialTransform(initial, inPlace=False)

            try:
                rigid_transform = registration.Execute(fixed_f, moving_f)
            except RuntimeError as exc:
                msg = f"SimpleITK rigid registration failed: {exc}"
                await db.execute(
                    update(Registration)
                    .where(Registration.id == rid)
                    .values(
                        status="failed",
                        error=msg,
                        finished_at=datetime.now(UTC),
                    )
                )
                if reg.job_id is not None:
                    await jobs_service.mark_failed(
                        db, reg.job_id, error={"code": "sitk_failed", "message": msg}
                    )
                await db.commit()
                return {"status": "error", "reason": "sitk_failed"}

            rigid_metric = registration.GetMetricValue()
            rigid_iter = registration.GetOptimizerIteration()
            result_meta: dict[str, Any] = {
                "kind": reg.kind,
                "rigid_metric": float(rigid_metric),
                "rigid_iterations": int(rigid_iter),
                "rigid_metric_name": "MattesMutualInformation",
            }
            final_transform: Any = rigid_transform

            if reg.kind == "demons":
                # Resample moving onto the fixed grid using the rigid
                # transform, so demons sees two volumes that already
                # share orientation/spacing/origin. Demons assumes the
                # input grids are aligned voxel-wise.
                moving_resampled = sitk.Resample(
                    moving_f,
                    fixed_f,
                    rigid_transform,
                    sitk.sitkLinear,
                    0.0,
                    moving_f.GetPixelID(),
                )

                demons = sitk.FastSymmetricForcesDemonsRegistrationFilter()
                demons.SetNumberOfIterations(50)
                demons.SetStandardDeviations(1.5)
                try:
                    displacement_field = demons.Execute(fixed_f, moving_resampled)
                except RuntimeError as exc:
                    msg = f"SimpleITK demons failed: {exc}"
                    await db.execute(
                        update(Registration)
                        .where(Registration.id == rid)
                        .values(
                            status="failed",
                            error=msg,
                            finished_at=datetime.now(UTC),
                        )
                    )
                    if reg.job_id is not None:
                        await jobs_service.mark_failed(
                            db, reg.job_id, error={"code": "demons_failed", "message": msg}
                        )
                    await db.commit()
                    return {"status": "error", "reason": "demons_failed"}

                demons_metric = float(demons.GetMetric())
                demons_iter = int(demons.GetElapsedIterations())
                displacement_transform = sitk.DisplacementFieldTransform(displacement_field)

                composite = sitk.CompositeTransform([rigid_transform, displacement_transform])
                final_transform = composite

                result_meta.update(
                    {
                        "demons_metric": demons_metric,
                        "demons_iterations": demons_iter,
                        "demons_metric_name": "MeanSquaredDifference",
                        "demons_filter": "FastSymmetricForcesDemons",
                        "demons_std_deviations": 1.5,
                    }
                )

            # Save the transform to a tempfile then upload.
            import tempfile

            with tempfile.NamedTemporaryFile(suffix=".tfm", delete=False) as tf:
                tfm_path = tf.name
            sitk.WriteTransform(final_transform, tfm_path)
            with open(tfm_path, "rb") as f:
                tfm_bytes = f.read()

            storage = get_s3_storage()
            bucket = settings.s3_bucket_versioning
            key = f"registrations/{rid}.tfm"
            storage.put_object(
                bucket=bucket,
                key=key,
                data=tfm_bytes,
                content_type="application/octet-stream",
            )

            await db.execute(
                update(Registration)
                .where(Registration.id == rid)
                .values(
                    status="succeeded",
                    s3_bucket=bucket,
                    s3_key=key,
                    finished_at=datetime.now(UTC),
                    result_meta=result_meta,
                )
            )
            if reg.job_id is not None:
                await jobs_service.mark_succeeded(db, reg.job_id, result_uri=f"s3://{bucket}/{key}")
            await db.commit()
            return {"status": "succeeded", **result_meta}
    finally:
        await engine.dispose()
