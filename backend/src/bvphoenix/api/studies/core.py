# ruff: noqa: F405, B008
# Auto-split from api/studies.py on 2026-05-21.
# Section: ``core``.

from __future__ import annotations

from fastapi import APIRouter

from bvphoenix.api.studies import _shared
from bvphoenix.api.studies._shared import *  # noqa: F403

router = APIRouter()


@router.get("/studies", response_model=PaginatedStudies)
async def list_studies(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(optional_user)],
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    modality: str | None = Query(None, max_length=16),
    body_part: str | None = Query(None, max_length=64),
) -> PaginatedStudies:
    # Cap runtime for this whole transaction — the body_part ILIKE is
    # the expensive path here (no trigram index) and a hostile client
    # could otherwise keep a worker busy scanning the series table.
    # SET LOCAL is scoped to this transaction only; no global leakage.
    await db.execute(text("SET LOCAL statement_timeout = '3s'"))

    base = await visible_studies_filter(db, user)
    if modality:
        # modalities is a TEXT[]; ANY() returns rows where the array contains the value.
        base = base.where(ImagingStudy.modalities.any(modality.upper()))
    if body_part:
        base = base.join(Series, Series.study_id == ImagingStudy.id).where(
            Series.body_part_examined.ilike(f"%{body_part}%")
        )
    count_query = select(func.count()).select_from(base.distinct().subquery())
    total = (await db.execute(count_query)).scalar_one()

    rows = (
        (
            await db.execute(
                base.order_by(ImagingStudy.created_at.desc()).limit(limit).offset(offset)
            )
        )
        .scalars()
        .unique()
        .all()
    )

    return PaginatedStudies(
        items=[StudyOut.model_validate(r) for r in rows],
        total=int(total),
        limit=limit,
        offset=offset,
    )


@router.patch(
    "/studies/{study_id}/tier",
    response_model=TierChangeOut,
    summary="Change a study's contribution tier (T1-T4)",
)
async def change_study_tier(
    study_id: uuid.UUID,
    body: TierChangeIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    audit: AuditDep,
) -> TierChangeOut:
    """Update the contribution tier of a study after upload.

    Gating:
    - Only the owner (or an admin) can change the tier.
    - The new tier must be a valid T1-T4 code.
    - No-op when the tier is already what was requested.

    Side effects on upgrade (T1/T2 -> T3/T4):
    - An active Consent row for the tier-implied kinds is created (or
      the existing one has this study appended to its metadata).
    - A deidentify_reindex job is enqueued for the worker to scrub
      PHI from the DICOM instances and refresh any cached
      derivatives; the current request returns immediately without
      waiting for the worker.

    Side effects on downgrade (T3/T4 -> T1/T2):
    - The study leaves the training pool. The historic Consent row is
      *not* deleted (append-only audit trail), but this study's id is
      moved from ``metadata['study_ids']`` to
      ``metadata['revoked_study_ids']`` so the audit view of what the
      consent still covers stays accurate. Revoking the consent row
      entirely is a separate GDPR flow.

    This endpoint does not touch studies that are referenced by a
    training-license aggregate (F10); once that model exists a 409
    will be returned if the study is under a signed license.
    """
    if body.tier not in _VALID_TIERS:
        raise HTTPException(status_code=422, detail=f"invalid tier; must be one of {_VALID_TIERS}")

    study = (
        await db.execute(select(ImagingStudy).where(ImagingStudy.id == study_id))
    ).scalar_one_or_none()
    if study is None:
        raise HTTPException(status_code=404, detail="study not found")

    # Ownership gate. We deliberately refuse 404 to non-owners so the
    # existence of someone else's study is not leaked.
    is_owner = study.owner_subject_id == user.subject_id
    is_admin = bool(getattr(user, "is_admin", False))
    if not (is_owner or is_admin):
        raise HTTPException(status_code=404, detail="study not found")

    old_tier = study.contribution_tier
    if old_tier == body.tier:
        return TierChangeOut(
            study_id=str(study.id),
            old_tier=old_tier,
            new_tier=body.tier,
            reindex_enqueued=False,
        )

    study.contribution_tier = body.tier

    # Update is_public to match when moving to T4 (CC public) or off.
    # T3 does not imply public visibility.
    if body.tier == "t4":
        study.is_public = True
    elif body.tier in ("t1", "t2"):
        study.is_public = False

    reindex = False
    # Upgrade to the commons tiers: materialise consent + queue reindex.
    if body.tier in _COMMONS_TIERS and old_tier in _QUOTA_TIERS:
        await ensure_tier_consents(
            db,
            user_subject_id=study.owner_subject_id,
            tier=body.tier,
            study_ids=[study.id],
        )
        reindex = await _enqueue_tier_reindex(study.id)
    # Downgrade out of the commons tiers: record per-study revocation in
    # the existing consent row's metadata so the audit trail matches the
    # study's new state (still append-only: we never delete the row).
    elif body.tier in _QUOTA_TIERS and old_tier in _COMMONS_TIERS:
        await revoke_tier_consent_for_study(
            db,
            user_subject_id=study.owner_subject_id,
            study_id=study.id,
        )

    await db.commit()

    await audit.log(
        action="study.tier_changed",
        actor_subject_id=user.subject_id,
        resource_kind="study",
        resource_id=study.id,
        metadata={
            "old_tier": old_tier,
            "new_tier": body.tier,
            "reindex_enqueued": reindex,
        },
    )

    return TierChangeOut(
        study_id=str(study.id),
        old_tier=old_tier,
        new_tier=body.tier,
        reindex_enqueued=reindex,
    )


@router.delete(
    "/studies/{study_id}/training-consent",
    response_model=ConsentRevokeOut,
    summary="Opt a single study back out of the training pool (T3/T4 -> T2)",
)
async def revoke_training_consent(
    study_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    audit: AuditDep,
    body: ConsentRevokeIn | None = None,
) -> ConsentRevokeOut:
    """Granular opt-out for a single study's training contribution.

    DESIGN.md §2 promises instant revocability for non-commercial grants;
    this endpoint is the concrete implementation for T3/T4 studies.
    Effects:

    * study.contribution_tier → ``t2`` (least-restrictive private),
      so future aggregate assembly excludes it;
    * study.is_public → False;
    * the active :class:`TrainingConsent` row is closed by setting
      ``revoked_at = now()``; if the caller passed a ``reason`` it is
      stored on the row for audit. The row itself is kept (append-only
      audit trail); a later re-grant inserts a fresh active row.

    Returns 404 to non-owners (same disclosure posture as PATCH /tier)
    and a no-op ``ConsentRevokeOut`` when the study was never in the
    commons tiers. Does not enqueue a reindex: downgrade from T3/T4 to
    T2 already restores tenant-only visibility, and derivatives can be
    regenerated lazily by the read path.
    """
    study = (
        await db.execute(select(ImagingStudy).where(ImagingStudy.id == study_id))
    ).scalar_one_or_none()
    if study is None:
        raise HTTPException(status_code=404, detail="study not found")

    is_owner = study.owner_subject_id == user.subject_id
    is_admin = bool(getattr(user, "is_admin", False))
    if not (is_owner or is_admin):
        raise HTTPException(status_code=404, detail="study not found")

    old_tier = study.contribution_tier
    if old_tier not in _COMMONS_TIERS:
        # Nothing to revoke: the study is already private.
        return ConsentRevokeOut(
            study_id=str(study.id),
            old_tier=old_tier,
            new_tier=old_tier,
            consent_rows_updated=0,
        )

    study.contribution_tier = "t2"
    study.is_public = False
    touched = await revoke_tier_consent_for_study(
        db,
        user_subject_id=study.owner_subject_id,
        study_id=study.id,
        reason=body.reason if body else None,
    )
    await db.commit()

    await audit.log(
        action="study.training_consent_revoked",
        actor_subject_id=user.subject_id,
        resource_kind="study",
        resource_id=study.id,
        metadata={
            "old_tier": old_tier,
            "new_tier": "t2",
            "consent_rows_updated": len(touched),
            "reason": body.reason if body else None,
        },
    )

    return ConsentRevokeOut(
        study_id=str(study.id),
        old_tier=old_tier,
        new_tier="t2",
        consent_rows_updated=len(touched),
    )


@router.get("/studies/{study_id}", response_model=StudyDetailOut)
async def get_study(
    study_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(optional_user)],
    audit: AuditDep,
) -> StudyDetailOut:
    study = (
        await db.execute(select(ImagingStudy).where(ImagingStudy.id == study_id))
    ).scalar_one_or_none()
    if study is None:
        raise HTTPException(status_code=404, detail="study not found")
    if not await can(db, user=user, action=READ_METADATA, study=study):
        # Same response as 404 — don't reveal the resource exists to
        # someone who can't see it.
        raise HTTPException(status_code=404, detail="study not found")
    await audit.log(
        action="study_view",
        actor_subject_id=user.subject_id if user else None,
        resource_kind="study",
        resource_id=study.id,
    )
    series = (
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
        series=[SeriesOut.model_validate(s) for s in series],
    )


@router.get("/studies/{study_id}/fusion-candidates", response_model=list[SeriesOut])
async def list_fusion_candidates(
    study_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(optional_user)],
    exclude_series_id: uuid.UUID | None = Query(
        None, description="Series to exclude (typically the primary viewer series)"
    ),
) -> list[SeriesOut]:
    """Sibling series inside the same study that can overlay the primary volume.

    Intended use: PET/CT (or CT/MR) fusion. We trust that series in the
    same DICOM study share a frame of reference so they can be blended
    without an explicit registration step — this holds for the vast
    majority of multi-modality acquisitions exported from clinical PACS.
    """
    study = (
        await db.execute(select(ImagingStudy).where(ImagingStudy.id == study_id))
    ).scalar_one_or_none()
    if study is None:
        raise HTTPException(status_code=404, detail="study not found")
    if not await can(db, user=user, action=READ_METADATA, study=study):
        raise HTTPException(status_code=404, detail="study not found")

    stmt = (
        select(Series)
        .where(Series.study_id == study.id)
        .where(Series.ingestion_complete.is_(True))
        .order_by(Series.series_number.asc().nullslast())
    )

    primary_modality: str | None = None
    if exclude_series_id is not None:
        stmt = stmt.where(Series.id != exclude_series_id)
        primary = (
            await db.execute(select(Series).where(Series.id == exclude_series_id))
        ).scalar_one_or_none()
        if primary is not None:
            primary_modality = primary.modality

    rows = (await db.execute(stmt)).scalars().all()

    # Prefer series with a different modality first (PET over CT, MR over CT, …).
    if primary_modality:
        rows = sorted(
            rows, key=lambda s: 0 if (s.modality and s.modality != primary_modality) else 1
        )
    return [SeriesOut.model_validate(s) for s in rows]


@router.get("/series/{series_id}", response_model=SeriesOut)
async def get_series(
    series_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(optional_user)],
) -> SeriesOut:
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
    if not await can(db, user=user, action=READ_METADATA, study=study):
        raise HTTPException(status_code=404, detail="series not found")

    out = SeriesOut.model_validate(series)

    # Best-effort: failures here (missing instances, S3 down, garbage
    # tags) must not break the endpoint — the frontend falls back to
    # ``computeAutoWL`` when these fields are null.
    try:
        instances = (
            await db.execute(
                select(Instance.s3_bucket, Instance.s3_key)
                .where(Instance.series_id == series.id)
                .order_by(Instance.instance_number.asc().nullslast())
            )
        ).all()
        if instances:
            mid_bucket, mid_key = instances[len(instances) // 2]
            storage = get_s3_storage()
            wcww = await asyncio.to_thread(
                _suggested_wl_from_middle_instance, storage, mid_bucket, mid_key
            )
            if wcww is not None:
                out.suggested_wc, out.suggested_ww = wcww
    except Exception:
        pass

    return out


@router.get("/series/{series_id}/instances", response_model=list[InstanceOut])
async def list_instances(
    series_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(optional_user)],
) -> list[InstanceOut]:
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
    if not await can(db, user=user, action=READ_METADATA, study=study):
        raise HTTPException(status_code=404, detail="series not found")
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
    return [InstanceOut.model_validate(i) for i in instances]


@router.get("/series/{series_id}/thumbnail")
async def series_thumbnail(
    series_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(optional_user)],
    index: int | None = Query(None, description="0-based slice index; default = middle"),
    wc_delta: float = Query(0, description="Window center offset from DICOM default"),
    ww_delta: float = Query(0, description="Window width offset from DICOM default"),
    max_side: int = Query(512, ge=64, le=2048, description="Max pixels on longest side"),
    _scope: None = _AGENT_PATIENT_IMAGES,
) -> Response:
    """Return a single slice as JPEG.

    Used by both the series preview cards and the full 2D slice viewer.
    ``wc_delta`` / ``ww_delta`` shift the DICOM window interactively —
    drag left/right adjusts width, up/down adjusts center, like a
    clinical PACS viewer.
    """
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
        raise HTTPException(status_code=409, detail="series has no instances yet")

    # Mixed-SOP series (e.g. one CT image + one Structured Report on the
    # same series UID) used to render the "no pixel data" placeholder
    # whenever the default index landed on the SR. Pre-compute the
    # subset of instances whose SOP Class UID can actually carry pixel
    # data so we can default to (and fall back to) an image-like one.
    image_positions = [
        i for i, inst in enumerate(instances) if is_image_sop_class(inst.sop_class_uid)
    ]

    if index is None:
        # Default = middle image-like instance, or middle of the whole
        # series if the SOP filter found nothing (legacy data, missing
        # ``sop_class_uid``).
        idx = image_positions[len(image_positions) // 2] if image_positions else len(instances) // 2
    else:
        if index < 0 or index >= len(instances):
            raise HTTPException(
                status_code=400,
                detail=f"index out of range [0, {len(instances)})",
            )
        idx = index
        # If the explicitly requested instance has no pixel data, hop to
        # the closest image-like one. Without this fallback the default
        # ``floor(N/2)`` from the preview card shows a placeholder
        # whenever the middle slot happens to be an SR/PR.
        if not is_image_sop_class(instances[idx].sop_class_uid) and image_positions:
            idx = min(image_positions, key=lambda p: abs(p - idx))
    storage = get_s3_storage()

    # Try the chosen index, then fall back to any other instance with
    # pixel data. ``is_image_sop_class`` is only a hint — vendor-specific
    # SOP Class UIDs (or NULL on legacy ingests) can fool both the
    # whitelist and the per-instance pre-filter, so the only reliable
    # signal is "did dicom_to_jpeg actually decode?". Order: requested
    # idx first, then nearest image-like, then everything else. We
    # break out of the loop on the first success and surface the
    # served index in ``x-slice-index`` so the slider lands on it.
    candidate_order: list[int] = [idx]
    # Remaining image-like positions, ordered by proximity to the
    # requested index so the served slice stays close to what the
    # caller asked for.
    nearby_image = sorted(
        (p for p in image_positions if p != idx),
        key=lambda p: abs(p - idx),
    )
    candidate_order.extend(nearby_image)
    # Catch-all: anything not yet tried (the SR/PR ladder). Almost all
    # of these will 422 again, but we'd rather make one extra S3 GET
    # than render a "no pixel data" placeholder when a sibling
    # instance carries the actual scan.
    for pos in range(len(instances)):
        if pos not in candidate_order:
            candidate_order.append(pos)

    served_idx: int | None = None
    jpeg: bytes | None = None
    last_no_pixel: NoPixelDataError | None = None
    for pos in candidate_order:
        cand = instances[pos]
        try:
            dcm_bytes = await asyncio.to_thread(
                storage.get_object_bytes, bucket=cand.s3_bucket, key=cand.s3_key
            )
        except Exception:
            continue
        try:
            jpeg = await asyncio.to_thread(
                dicom_to_jpeg,
                dcm_bytes,
                wc_delta=wc_delta,
                ww_delta=ww_delta,
                max_side=max_side,
            )
        except NoPixelDataError as exc:
            last_no_pixel = exc
            continue
        served_idx = pos
        break

    if jpeg is None or served_idx is None:
        raise HTTPException(
            status_code=422,
            detail=(
                "no instance in this series carries pixel data "
                "(structured report / presentation state / key object only)"
            ),
        ) from last_no_pixel

    cache = "public, max-age=86400" if wc_delta == 0 and ww_delta == 0 else "no-store"
    return Response(
        content=jpeg,
        media_type="image/jpeg",
        headers={
            "cache-control": cache,
            "x-slice-index": str(served_idx),
            "x-slice-count": str(len(instances)),
        },
    )


@router.get("/series/{series_id}/document")
async def series_document(
    series_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(optional_user)],
    index: int | None = Query(
        None, description="0-based instance index; default = first non-image instance"
    ),
) -> Response:
    """Return the rendered content of a non-image DICOM instance.

    Companion to ``/series/{id}/thumbnail`` — the viewer falls back to
    this endpoint when ``thumbnail`` raises 422 (no pixel data) so the
    user can read Encapsulated PDFs and Structured Reports inline
    instead of seeing a blank "no pixel data" placeholder.

    Output media type depends on the SOP class:
      * Encapsulated PDF (``104.1``) → ``application/pdf``
      * Structured Report (``88.x``) → ``text/plain; charset=utf-8``
        with the ContentSequence flattened to an indented outline.

    Returns 415 for SOP classes we don't render yet (Presentation
    State, Key Object, Encapsulated CDA) and 404 / 409 for the same
    auth / no-instances reasons as the thumbnail endpoint.
    """
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
        raise HTTPException(status_code=409, detail="series has no instances yet")

    # Default = first non-image instance (the SR / PDF). If the series
    # only carries image-like instances, fall back to the explicit
    # index (or the first one) so the caller still gets *something*
    # back rather than a 415.
    non_image_positions = [
        i for i, inst in enumerate(instances) if not is_image_sop_class(inst.sop_class_uid)
    ]
    if index is None:
        idx = non_image_positions[0] if non_image_positions else 0
    else:
        if index < 0 or index >= len(instances):
            raise HTTPException(
                status_code=400,
                detail=f"index out of range [0, {len(instances)})",
            )
        idx = index
    inst = instances[idx]

    storage = get_s3_storage()
    dcm_bytes = await asyncio.to_thread(
        storage.get_object_bytes, bucket=inst.s3_bucket, key=inst.s3_key
    )
    try:
        doc = await asyncio.to_thread(read_dicom_document, dcm_bytes)
    except UnsupportedDocumentError as exc:
        raise HTTPException(
            status_code=415,
            detail={"error": "unsupported_document", "message": str(exc)},
        ) from exc

    headers = {
        "x-slice-index": str(idx),
        "x-slice-count": str(len(instances)),
        "x-document-kind": doc.kind,
    }
    if doc.title:
        # Title can carry non-ASCII (Italian punctuation, accented
        # characters in clinical descriptions). Encode latin-1-safe so
        # ASGI servers don't reject it.
        try:
            headers["x-document-title"] = doc.title.encode("latin-1").decode("latin-1")
        except UnicodeEncodeError:
            headers["x-document-title"] = doc.title.encode("ascii", "ignore").decode("ascii")
    return Response(
        content=doc.data,
        media_type=doc.mime_type,
        headers=headers,
    )


@router.get("/series/{series_id}/volume.raw")
async def get_series_volume(
    series_id: uuid.UUID,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(optional_user)],
    earl_fwhm_mm: float = Query(
        0.0,
        ge=0.0,
        le=20.0,
        description=(
            "Optional EARL Gaussian harmonization target FWHM (mm). "
            "0 disables; 7 mm = EARL1, 5 mm = EARL2."
        ),
    ),
    stack: int | None = Query(
        None,
        ge=0,
        description=(
            "Sub-stack index for a multi-stack series (Philips mDIXON "
            "Water/Fat/In-phase/Out-of-phase, multi-echo, DWI). None / 0 = "
            "the primary stack; 1.. select the extra contrasts. The "
            "available stacks + labels are listed in display-metadata."
        ),
    ),
    _scope: None = _AGENT_PATIENT_IMAGES,
) -> Response:
    """Return the packed Float32 volume for a series.

    Header layout is documented in ``services.volumes`` and the viewer
    parses it in one pass. Cached in the derivatives bucket on first
    request; requires ``read:pixels``.

    Transport optimizations:
    - ``Accept-Encoding: gzip`` on the request triggers level-1 gzip
      (balanced for speed vs. ratio — Float32 medical scans compress ~1.8x).
    - Payloads larger than 20 MB stream in 1 MB chunks so the server
      doesn't hold two copies of the blob in memory during the response.

    Optional EARL harmonization (Addendum C §7): when
    ``earl_fwhm_mm > 0``, the cached volume is post-filtered with a
    3D Gaussian to match a target effective PSF FWHM. Cached
    separately under a ``f32-earl-{fwhm}`` format so subsequent
    fetches at the same FWHM hit the cache.
    """
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

    settings = get_settings()
    storage = get_s3_storage()
    want_stack = stack or 0
    target_format = (
        DERIVATIVE_FORMAT
        if earl_fwhm_mm <= 0
        else f"{DERIVATIVE_FORMAT}-earl-{round(earl_fwhm_mm * 10)}"
    )
    derivative = (
        await db.execute(
            select(Derivative).where(
                Derivative.series_id == series.id,
                Derivative.kind == DERIVATIVE_KIND,
                Derivative.format == target_format,
                Derivative.stack_index == want_stack,
            )
        )
    ).scalar_one_or_none()
    if derivative is not None:
        # Revalidated cache: the ETag is the derivative row id, recreated on
        # every re-pack, so a stale client copy is invalidated automatically
        # (the mDIXON backfill foot-gun where stack 0 reuses volume.f32).
        etag = _derivative_etag(derivative.id)
        not_modified = _not_modified_response(request, etag)
        if not_modified is not None:
            return not_modified
        cached = await asyncio.to_thread(
            storage.get_object_bytes,
            bucket=derivative.s3_bucket,
            key=derivative.s3_key,
        )
        return _volume_response(
            cached,
            accept_gzip=_client_accepts_gzip(request),
            geometry=derivative.geometry,
            etag=etag,
        )

    # Pre-check: avoid an S3 round-trip for series that are obviously
    # non-volumetric (every instance is a Secondary Capture, Presentation
    # State, Structured Report, ...). Reading sop_class_uid from the
    # ``instances`` table is cheap and lets us reply 404 immediately so
    # the frontend falls through to the 2D viewer.
    sop_classes = (
        await db.execute(select(Instance.sop_class_uid).where(Instance.series_id == series.id))
    ).all()
    if sop_classes and all((sc[0] or "") in NON_VOLUMETRIC_SOP_CLASSES for sc in sop_classes):
        raise HTTPException(
            status_code=404,
            detail="series is non-volumetric (no 3D volume to serve)",
        )

    instances = (
        await db.execute(
            select(Instance.s3_bucket, Instance.s3_key)
            .where(Instance.series_id == series.id)
            .order_by(Instance.instance_number.asc().nullslast())
        )
    ).all()
    if not instances:
        raise HTTPException(status_code=409, detail="series has no instances yet")

    try:
        packed = await asyncio.to_thread(
            pack_series,
            storage=storage,
            instance_entries=[(b, k) for b, k in instances],
            stack_index=want_stack,
        )
    except NonVolumetricSeriesError as exc:
        # The packer detected something the SOP-class precheck missed,
        # most often inconsistent ImageOrientationPatient across
        # instances (CT scout AP + LAT). Translate to 404 so the
        # frontend uses ``<Series2DViewer>``.
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        # stack_index out of range (client asked for a sub-stack this
        # series doesn't have). 404 is the right "no such resource".
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if earl_fwhm_mm > 0:
        packed = await asyncio.to_thread(apply_earl_harmonization, packed, earl_fwhm_mm, 0.0)

    cache_key = (
        volume_stack_key(patient_id=study.patient_id, series_id=series.id, stack_index=want_stack)
        if earl_fwhm_mm <= 0
        else volume_stack_earl_key(
            patient_id=study.patient_id,
            series_id=series.id,
            stack_index=want_stack,
            earl_fwhm_mm=earl_fwhm_mm,
        )
    )
    await asyncio.to_thread(
        storage.upload_bytes,
        packed.bytes_,
        bucket=settings.s3_bucket_derivatives,
        key=cache_key,
    )
    deriv = Derivative(
        series_id=series.id,
        kind=DERIVATIVE_KIND,
        format=target_format,
        stack_index=want_stack,
        s3_bucket=settings.s3_bucket_derivatives,
        s3_key=cache_key,
        size_bytes=packed.size,
        generator_version=(
            "pack_series-v1" if earl_fwhm_mm <= 0 else f"pack_series-v1+earl-{earl_fwhm_mm:.1f}"
        ),
        geometry=packed.geometry,
    )
    db.add(deriv)
    await db.commit()
    # ``id`` is server-generated (gen_random_uuid); refresh to read it back
    # for the ETag so the freshly-packed blob is a revalidated cache entry.
    await db.refresh(deriv)

    return _volume_response(
        packed.bytes_,
        accept_gzip=_client_accepts_gzip(request),
        geometry=packed.geometry,
        etag=_derivative_etag(deriv.id),
    )


@router.get("/series/{series_id}/volume-preview.raw")
async def get_series_volume_preview(
    series_id: uuid.UUID,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(optional_user)],
    _scope: None = _AGENT_PATIENT_IMAGES,
) -> Response:
    """Return a 1/8-resolution preview of the packed Float32 volume.

    Progressive-load strategy: the viewer fetches this blob first (it's
    ~1/8th the size of the full one at factor=2, so ~1.6 MB for a typical
    512x512x50 CT) and renders a coarse volume while the full-res
    ``volume.raw`` is still on the wire. Same 32-byte header format — the
    frontend parser needs no changes.

    Cached in the derivatives bucket on first request; honors
    ``Accept-Encoding: gzip`` like the full-res endpoint.
    """
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

    settings = get_settings()
    storage = get_s3_storage()

    # Fast path: preview already cached.
    preview_deriv = (
        await db.execute(
            select(Derivative).where(
                Derivative.series_id == series.id,
                Derivative.kind == DERIVATIVE_KIND_PREVIEW,
                Derivative.format == DERIVATIVE_FORMAT,
                Derivative.stack_index == 0,  # preview is of the primary stack
            )
        )
    ).scalar_one_or_none()
    if preview_deriv is not None:
        etag = _derivative_etag(preview_deriv.id)
        not_modified = _not_modified_response(request, etag)
        if not_modified is not None:
            return not_modified
        cached = await asyncio.to_thread(
            storage.get_object_bytes,
            bucket=preview_deriv.s3_bucket,
            key=preview_deriv.s3_key,
        )
        return _volume_response(cached, accept_gzip=_client_accepts_gzip(request), etag=etag)

    # Cheap precheck: skip non-volumetric SOP classes before paying for
    # any packing work. Same shortcut as the full-res endpoint.
    sop_classes = (
        await db.execute(select(Instance.sop_class_uid).where(Instance.series_id == series.id))
    ).all()
    if sop_classes and all((sc[0] or "") in NON_VOLUMETRIC_SOP_CLASSES for sc in sop_classes):
        raise HTTPException(
            status_code=404,
            detail="series is non-volumetric (no 3D volume to serve)",
        )

    # Otherwise we need a full PackedVolume to downsample. Prefer the
    # cached full-res blob; fall back to packing from instances.
    full_deriv = (
        await db.execute(
            select(Derivative).where(
                Derivative.series_id == series.id,
                Derivative.kind == DERIVATIVE_KIND,
                Derivative.format == DERIVATIVE_FORMAT,
                Derivative.stack_index == 0,  # preview downsamples the primary stack
            )
        )
    ).scalar_one_or_none()

    if full_deriv is not None:
        full_bytes = await asyncio.to_thread(
            storage.get_object_bytes,
            bucket=full_deriv.s3_bucket,
            key=full_deriv.s3_key,
        )
        # Reconstitute a PackedVolume shell so pack_low_res can shape it.
        # Header fields are decoded from the first 32 bytes.
        nx, ny, nz, sx, sy, sz, vmin, vmax = HEADER_STRUCT.unpack_from(full_bytes)
        packed = PackedVolume(
            bytes_=full_bytes,
            nx=nx,
            ny=ny,
            nz=nz,
            spacing=(sx, sy, sz),
            value_range=(vmin, vmax),
        )
    else:
        instances = (
            await db.execute(
                select(Instance.s3_bucket, Instance.s3_key)
                .where(Instance.series_id == series.id)
                .order_by(Instance.instance_number.asc().nullslast())
            )
        ).all()
        if not instances:
            raise HTTPException(status_code=409, detail="series has no instances yet")
        try:
            packed = await asyncio.to_thread(
                pack_series,
                storage=storage,
                instance_entries=[(b, k) for b, k in instances],
            )
        except NonVolumetricSeriesError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        # We just paid to pack the full-res blob; cache it too so the
        # next /volume.raw hit doesn't redo the work.
        full_key = volume_key(patient_id=study.patient_id, series_id=series.id)
        await asyncio.to_thread(
            storage.upload_bytes,
            packed.bytes_,
            bucket=settings.s3_bucket_derivatives,
            key=full_key,
        )
        db.add(
            Derivative(
                series_id=series.id,
                kind=DERIVATIVE_KIND,
                format=DERIVATIVE_FORMAT,
                stack_index=0,  # primary stack
                s3_bucket=settings.s3_bucket_derivatives,
                s3_key=full_key,
                size_bytes=packed.size,
                generator_version="pack_series-v1",
                geometry=packed.geometry,
            )
        )

    preview = await asyncio.to_thread(pack_low_res, packed, 2)

    cache_key = volume_preview_key(patient_id=study.patient_id, series_id=series.id)
    await asyncio.to_thread(
        storage.upload_bytes,
        preview.bytes_,
        bucket=settings.s3_bucket_derivatives,
        key=cache_key,
    )
    preview_deriv = Derivative(
        series_id=series.id,
        kind=DERIVATIVE_KIND_PREVIEW,
        format=DERIVATIVE_FORMAT,
        stack_index=0,  # preview is of the primary stack
        s3_bucket=settings.s3_bucket_derivatives,
        s3_key=cache_key,
        size_bytes=preview.size,
        generator_version="pack_low_res-v1",
    )
    db.add(preview_deriv)
    await db.commit()
    await db.refresh(preview_deriv)

    return _volume_response(
        preview.bytes_,
        accept_gzip=_client_accepts_gzip(request),
        etag=_derivative_etag(preview_deriv.id),
    )


@router.post("/series/{series_id}/pack-volume", status_code=202)
async def enqueue_pack_volume(
    series_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
) -> dict:
    """Enqueue a background job to pre-pack this series' volume.

    The worker downloads all DICOM instances, stacks them into a Float32
    blob, and uploads to the derivatives bucket. Once done, subsequent
    requests to ``volume.raw`` serve the cached result instantly.
    """
    row = (
        await db.execute(
            select(Series, ImagingStudy)
            .join(ImagingStudy, ImagingStudy.id == Series.study_id)
            .where(Series.id == series_id)
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="series not found")
    _series, study = row
    if not await can(db, user=user, action=READ_PIXELS, study=study):
        raise HTTPException(status_code=404, detail="series not found")

    settings = get_settings()
    redis = await create_pool(redis_settings(settings.redis_url))
    await redis.enqueue_job("pack_volume", str(series_id))
    await redis.close()
    return {"status": "enqueued", "series_id": str(series_id)}


@router.post("/series/{series_id}/embed", status_code=202)
async def enqueue_embed_series(
    series_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
) -> dict:
    """Enqueue a background job to generate a BiomedCLIP embedding.

    Once done, the series appears in similarity search results and can be
    used as a query target for ``/similar-to/{series_id}``.
    """
    row = (
        await db.execute(
            select(Series, ImagingStudy)
            .join(ImagingStudy, ImagingStudy.id == Series.study_id)
            .where(Series.id == series_id)
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="series not found")
    _series, study = row
    if not await can(db, user=user, action=READ_PIXELS, study=study):
        raise HTTPException(status_code=404, detail="series not found")

    # Non-image series (SR / PR / SEG, ...) cannot be embedded by BiomedCLIP —
    # skip without enqueuing rather than handing the worker a job that can
    # only no-op. The MCP ``embed_series`` tool calls this endpoint, so this
    # also keeps the agent path from churning. Source: services.embeddable.
    if not is_embeddable_modality(_series.modality):
        return {
            "status": "skipped",
            "reason": "non_image_modality",
            "series_id": str(series_id),
            "modality": _series.modality,
        }

    settings = get_settings()
    redis = await create_pool(redis_settings(settings.redis_url))
    await redis.enqueue_job("embed_series", str(series_id))
    await redis.close()
    return {"status": "enqueued", "series_id": str(series_id)}


@router.get("/instances/{instance_id}/file")
async def download_instance(
    instance_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(optional_user)],
    audit: AuditDep,
    grant: Annotated[Grant | None, Depends(active_share_grant)] = None,
    _scope: None = _AGENT_PATIENT_IMAGES,
) -> Response:
    """Return the DICOM instance — bytes streamed through the backend
    (storage isolation, ``feedback_storage_isolation``); de-identified
    in-memory when the active share grant requires it.

    Requires ``download:dicom`` — this is the gate the NFFA-style
    "researcher buys a dataset" workflow checks against. When the
    caller authenticated via a share link whose grant has
    ``deidentify=true``, the file is scrubbed per DICOM PS3.15 Basic
    Profile before hand-off (``services.deidentify``).
    """
    row = (
        await db.execute(
            select(Instance, Series, ImagingStudy)
            .join(Series, Series.id == Instance.series_id)
            .join(ImagingStudy, ImagingStudy.id == Series.study_id)
            .where(Instance.id == instance_id)
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="instance not found")
    instance, _series, study = row
    if not await can(db, user=user, action=DOWNLOAD_DICOM, study=study):
        raise HTTPException(status_code=403, detail="download not permitted")

    storage = get_s3_storage()

    if should_deidentify(grant, study):
        # Fast path: when the study was already de-identified at rest by the
        # CURRENT engine version (stamped by deidentify_reindex), the stored
        # bytes are already scrubbed — serve them directly, no per-download
        # re-scrub. A version mismatch (engine upgraded, or never stamped) falls
        # through to the on-the-fly scrub so the upgrade is always honoured.
        from bvphoenix.config import get_settings

        current_version = get_settings().deid_method_version
        raw = await asyncio.to_thread(
            storage.get_object_bytes, bucket=instance.s3_bucket, key=instance.s3_key
        )
        if study.deidentified_at is not None and study.deid_method_version == current_version:
            served, deid_header = raw, "stored"
        else:
            served = await asyncio.to_thread(deidentify_dicom_bytes, raw)
            deid_header = "true"
        filename = f"{instance.sop_instance_uid}.dcm"
        return Response(
            content=served,
            media_type="application/dicom",
            headers={
                "content-disposition": _content_disposition(filename, disposition="attachment"),
                "x-deidentified": deid_header,
                "cache-control": "no-store",
            },
        )

    # Storage isolation (feedback_storage_isolation): stream the DICOM
    # bytes through the backend instead of redirecting to a presigned
    # storage URL. The instance's bucket/key never crosses the response
    # boundary; the caller authenticates with their normal session.
    try:
        body_iter, length, _ = await asyncio.to_thread(
            storage.iter_object,
            bucket=instance.s3_bucket,
            key=instance.s3_key,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=404,
            detail="instance binary unavailable",
        ) from exc
    await audit.log(
        action="instance_download",
        actor_subject_id=user.subject_id if user else None,
        resource_kind="instance",
        resource_id=instance.id,
        metadata={"study_id": str(study.id), "series_id": str(instance.series_id)},
    )
    filename = f"{instance.sop_instance_uid}.dcm"
    headers: dict[str, str] = {
        "content-disposition": _content_disposition(filename, disposition="attachment"),
        "cache-control": "private, max-age=0",
    }
    if length is not None:
        headers["content-length"] = str(length)
    return StreamingResponse(
        body_iter,
        media_type="application/dicom",
        headers=headers,
    )
