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


async def _load_series_image(db: AsyncSession, *, series_id: uuid.UUID, settings) -> object | None:
    """Load a series as a SimpleITK image in **true patient space (LPS)**.

    Prefers the packed ``volume_f32`` derivative + its stored geometry
    (origin / direction / spacing), so the registration transform is
    LPS→LPS and honours orientation, gantry tilt and slice ordering. Falls
    back to re-stacking DICOM in index space only when the series has not
    been packed yet (legacy / not-yet-warmed) — a degraded but working path.
    """
    from bvphoenix.db.models import Derivative
    from bvphoenix.services.volumes import DERIVATIVE_FORMAT, DERIVATIVE_KIND
    from bvphoenix.storage import get_s3_storage

    from bvworkers.registration_core import build_sitk_image_from_packed

    deriv = (
        await db.execute(
            select(Derivative).where(
                Derivative.series_id == series_id,
                Derivative.kind == DERIVATIVE_KIND,
                Derivative.format == DERIVATIVE_FORMAT,
                Derivative.stack_index == 0,
            )
        )
    ).scalar_one_or_none()
    if deriv is not None:
        storage = get_s3_storage()
        packed = storage.get_object_bytes(bucket=deriv.s3_bucket, key=deriv.s3_key)
        return build_sitk_image_from_packed(packed, deriv.geometry)
    return await _series_volume(db, series_id=series_id, settings=settings)


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

            fixed = await _load_series_image(db, series_id=reg.fixed_series_id, settings=settings)
            moving = await _load_series_image(db, series_id=reg.moving_series_id, settings=settings)
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

            # Rigid (+ optional demons) recipe, extracted to a pure module so
            # it is unit-testable and reusable by lesion propagation. Both
            # images are already in true patient space (LPS) when packed.
            from bvworkers.registration_core import register_pair

            try:
                final_transform, result_meta = register_pair(fixed, moving, reg.kind)
            except RuntimeError as exc:
                msg = f"SimpleITK registration failed: {exc}"
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
