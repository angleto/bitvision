# ruff: noqa: F405
# Auto-split from api/studies.py on 2026-05-21.
# Section: ``metadata``.

from __future__ import annotations

from fastapi import APIRouter

from bvphoenix.api.studies import _shared
from bvphoenix.api.studies._shared import *  # noqa: F403

router = APIRouter()


@router.patch(
    "/studies/{study_id}",
    response_model=StudyDetailOut,
)
async def patch_study_metadata(
    request: Request,
    study_id: uuid.UUID,
    body: StudyMetadataPatchIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    audit: AuditDep,
) -> StudyDetailOut:
    """Edit safe descriptive fields on a study (Sprint 3.5).

    Whitelist: ``study_description``. The DICOM authoritative fields
    (UID, modalities array, owner) are never editable here. Only the
    study owner (or admin) can patch.
    """
    enforce_agent_scope(request, "studies:write_metadata")
    from bvphoenix.middleware.problem_details import problem as _problem

    study = (
        await db.execute(select(ImagingStudy).where(ImagingStudy.id == study_id))
    ).scalar_one_or_none()
    if study is None:
        raise _problem(404, "not_found", "study not found")
    if not await can(db, user=user, action=READ_PIXELS, study=study):
        raise _problem(404, "not_found", "study not found")
    is_owner = study.owner_subject_id == user.subject_id or getattr(user, "is_admin", False)
    if not is_owner:
        raise _problem(403, "forbidden", "study metadata write requires ownership")

    fields = body.model_dump(exclude_unset=True)
    unknown = set(fields.keys()) - _STUDY_EDITABLE
    if unknown:
        raise _problem(
            422,
            "unprocessable_entity",
            "field is read-only on this endpoint",
            extra={"read_only": sorted(unknown), "editable": sorted(_STUDY_EDITABLE)},
        )

    for k, v in fields.items():
        setattr(study, k, v)
    await db.commit()
    await db.refresh(study)

    await audit.log(
        action="study_metadata_update",
        actor_subject_id=user.subject_id,
        resource_kind="study",
        resource_id=study.id,
        metadata={"fields": sorted(fields.keys())},
    )

    series_rows = (
        (
            await db.execute(
                select(Series)
                .where(Series.study_id == study.id)
                .order_by(Series.series_number.asc().nullslast())
            )
        )
        .scalars()
        .all()
    )
    return StudyDetailOut(
        **StudyOut.model_validate(study).model_dump(),
        series=[SeriesOut.model_validate(s) for s in series_rows],
    )


@router.patch(
    "/series/{series_id}",
    response_model=SeriesOut,
)
async def patch_series_metadata(
    request: Request,
    series_id: uuid.UUID,
    body: SeriesMetadataPatchIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    audit: AuditDep,
) -> SeriesOut:
    """Edit safe descriptive fields on a series (Sprint 3.5).

    Whitelist: ``series_description``, ``body_part_examined``,
    ``modality_corrected``. The latter is recorded as a tag in the
    ``modality_corrected`` namespace rather than overwriting the
    DICOM-authoritative ``modality`` column — search picks it up via
    the tag, the original DICOM value remains intact.
    """
    enforce_agent_scope(request, "series:write_metadata")
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
    is_owner = study.owner_subject_id == user.subject_id or getattr(user, "is_admin", False)
    if not is_owner:
        raise _problem(403, "forbidden", "series metadata write requires ownership")

    fields = body.model_dump(exclude_unset=True)
    unknown = set(fields.keys()) - _SERIES_EDITABLE
    if unknown:
        raise _problem(
            422,
            "unprocessable_entity",
            "field is read-only on this endpoint",
            extra={"read_only": sorted(unknown), "editable": sorted(_SERIES_EDITABLE)},
        )

    modality_override = fields.pop("modality_corrected", None)
    for k, v in fields.items():
        setattr(series, k, v)

    if modality_override is not None:
        from bvphoenix.db.models import Tag as _Tag

        existing_override = (
            await db.execute(
                select(_Tag).where(
                    _Tag.target_kind == "series",
                    _Tag.target_id == series.id,
                    _Tag.namespace == "modality_corrected",
                )
            )
        ).scalar_one_or_none()
        if existing_override is None:
            db.add(
                _Tag(
                    target_kind="series",
                    target_id=series.id,
                    namespace="modality_corrected",
                    value=modality_override,
                    source="manual",
                    created_by_subject_id=user.subject_id,
                )
            )
        elif existing_override.value != modality_override:
            existing_override.value = modality_override
            existing_override.source = "manual"
            existing_override.created_by_subject_id = user.subject_id

    await db.commit()
    await db.refresh(series)

    await audit.log(
        action="series_metadata_update",
        actor_subject_id=user.subject_id,
        resource_kind="series",
        resource_id=series.id,
        metadata={
            "fields": sorted(
                list(fields.keys()) + (["modality_corrected"] if modality_override else [])
            ),
        },
    )

    return SeriesOut.model_validate(series)


@router.get("/series/{series_id}/slice/{idx}")
async def get_series_slice_mpr(
    series_id: uuid.UUID,
    idx: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(optional_user)],
    audit: AuditDep,
    plane: str = Query(default="axial", description="axial | coronal | sagittal"),
    wc_delta: float = Query(0, description="Window center offset"),
    ww_delta: float = Query(0, description="Window width offset"),
    max_side: int = Query(512, ge=64, le=2048),
    _scope: None = _AGENT_PATIENT_IMAGES,
) -> Response:
    """Return one MPR slice as JPEG (Sprint 5b).

    ``axial`` is the DICOM-acquired plane and reuses the existing
    thumbnail single-instance pipeline (cheap). ``coronal`` /
    ``sagittal`` stack the whole series in memory and reslice via
    SimpleITK. Cache key includes
    ``(series_id, plane, idx, wc_delta, ww_delta, max_side, content_hash)``;
    a repeat call hits the LRU disk cache and emits ``X-Cache: hit``.
    """
    import hashlib as _hashlib

    from bvphoenix.middleware.problem_details import problem as _problem
    from bvphoenix.services.mpr import MPRUnavailableError, reslice_to_jpeg
    from bvphoenix.services.slice_cache import get_slice_cache

    if plane not in ("axial", "coronal", "sagittal"):
        raise _problem(
            422,
            "invalid_plane",
            f"plane must be one of axial / coronal / sagittal, got {plane!r}",
        )

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
            )
        )
        .scalars()
        .all()
    )
    if not instances:
        raise _problem(409, "conflict", "series has no instances yet")

    content_hash = _hashlib.sha256(
        "|".join(f"{i.s3_bucket}/{i.s3_key}" for i in instances).encode("utf-8")
    ).hexdigest()
    cache = get_slice_cache()
    key = cache.make_key(
        series_id=str(series.id),
        plane=plane,
        idx=idx,
        wc_delta=wc_delta,
        ww_delta=ww_delta,
        max_side=max_side,
        content_hash=content_hash,
    )
    cached = await cache.get(key)
    if cached is not None:
        await audit.log(
            action="series_mpr_slice_read",
            actor_subject_id=user.subject_id if user else None,
            resource_kind="series",
            resource_id=series.id,
            metadata={"plane": plane, "idx": idx, "cache": "hit"},
        )
        return Response(
            content=cached,
            media_type="image/jpeg",
            headers={
                "x-cache": "hit",
                "x-plane": plane,
                "x-slice-index": str(idx),
                "cache-control": "private, max-age=86400",
            },
        )

    storage = get_s3_storage()

    if plane == "axial":
        if idx < 0 or idx >= len(instances):
            raise _problem(
                422,
                "slice_index_out_of_range",
                f"axial idx {idx} out of range",
                extra={"slice_total": len(instances)},
            )
        inst = instances[idx]
        dcm_bytes = await asyncio.to_thread(
            storage.get_object_bytes, bucket=inst.s3_bucket, key=inst.s3_key
        )
        try:
            jpeg = await asyncio.to_thread(
                dicom_to_jpeg,
                dcm_bytes,
                wc_delta=wc_delta,
                ww_delta=ww_delta,
                max_side=max_side,
            )
        except NoPixelDataError as exc:
            raise _problem(
                422,
                "no_pixel_data",
                "axial idx points to an instance without pixel data",
            ) from exc
        await cache.put(key, jpeg)
        await audit.log(
            action="series_mpr_slice_read",
            actor_subject_id=user.subject_id if user else None,
            resource_kind="series",
            resource_id=series.id,
            metadata={"plane": "axial", "idx": idx, "cache": "miss"},
        )
        return Response(
            content=jpeg,
            media_type="image/jpeg",
            headers={
                "x-cache": "miss",
                "x-plane": "axial",
                "x-slice-index": str(idx),
                "cache-control": "private, max-age=86400",
            },
        )

    dcm_bytes_list = await asyncio.gather(
        *[
            asyncio.to_thread(storage.get_object_bytes, bucket=i.s3_bucket, key=i.s3_key)
            for i in instances
        ]
    )
    try:
        jpeg, info = await asyncio.to_thread(
            reslice_to_jpeg,
            list(dcm_bytes_list),
            plane=plane,
            idx=idx,
            wc_delta=wc_delta,
            ww_delta=ww_delta,
            max_side=max_side,
        )
    except MPRUnavailableError as exc:
        raise _problem(
            422,
            "mpr_unavailable",
            str(exc),
            extra={"plane": plane, "idx": idx},
        ) from exc

    await cache.put(key, jpeg)
    await audit.log(
        action="series_mpr_slice_read",
        actor_subject_id=user.subject_id if user else None,
        resource_kind="series",
        resource_id=series.id,
        metadata={"plane": plane, "idx": idx, "cache": "miss"},
    )
    return Response(
        content=jpeg,
        media_type="image/jpeg",
        headers={
            "x-cache": "miss",
            "x-plane": plane,
            "x-slice-index": str(idx),
            "x-volume-shape": f"{info['nx']}x{info['ny']}x{info['nz']}",
            "cache-control": "private, max-age=86400",
        },
    )
