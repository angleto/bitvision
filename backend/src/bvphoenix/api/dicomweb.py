"""DICOMweb read surface (PS3.18): QIDO-RS query + WADO-RS retrieve/metadata
under ``/api/dicom``. Closes the inbound-only (STOW-RS) asymmetry and makes
bitvision a drop-in DICOMweb node for OHIF / 3D Slicer / any PACS.

Design invariants:

* **Patient scoping is the query base.** Every list goes through
  ``visible_studies_filter`` and every UID lookup resolves *inside* that
  filtered set, so an out-of-scope study UID is a 404 (indistinguishable
  from non-existent) — cross-patient access is inexpressible, not merely
  refused (``cross_patient_links_forbidden``).
* **Storage isolation.** Instance bytes stream through the backend via
  ``S3Storage.iter_object``; a bucket name / key / presigned URL never
  crosses the response boundary (``feedback_storage_isolation``).
* **De-identification is honoured on egress.** WADO retrieve / metadata
  reuse ``should_deidentify`` + ``deidentify_dicom_bytes`` — the exact
  policy of ``GET /instances/{id}/file`` — so a share-link recipient or a
  T3 study is scrubbed per PS3.15 before the bytes leave.

Scope (v1): QIDO studies/series/instances (hierarchical) + WADO retrieve
(study/series/instance, multipart/related streaming) + WADO metadata.
Frames / bulkdata / rendered / transfer-syntax transcoding are tracked
follow-ups; metadata emits no ``BulkDataURI`` so nothing dangles.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.auth import active_share_grant, optional_user, require_scope_if_agent
from bvphoenix.config import get_settings
from bvphoenix.db.models import Grant, ImagingStudy, Instance, Series, User
from bvphoenix.db.session import get_db
from bvphoenix.middleware.audit_dependency import AuditDep
from bvphoenix.services import dicomweb as dw
from bvphoenix.services.deidentify import deidentify_dicom_bytes, should_deidentify
from bvphoenix.services.permissions import (
    DOWNLOAD_DICOM,
    can,
    visible_studies_filter,
)
from bvphoenix.storage import get_s3_storage

router = APIRouter(prefix="/dicom", tags=["dicomweb"])

# Agent tokens need a read scope to query, and the images scope to pull bytes
# (mirrors GET /instances/{id}/file). No-op for human / anonymous sessions.
_AGENT_READ = Depends(require_scope_if_agent("imaging:read"))
_AGENT_IMAGES = Depends(require_scope_if_agent("patient:images"))

_QIDO_LIMIT_DEFAULT = 100
_QIDO_LIMIT_MAX = 1000


# ---- shared helpers --------------------------------------------------------


def _wado_base(request: Request) -> str:
    """External base URL of the DICOMweb surface, for RetrieveURL /
    Content-Location. A bitvision URL only — never a storage location."""
    return str(request.base_url).rstrip("/") + "/api/dicom"


def _accepts(request: Request, *media: str) -> bool:
    accept = request.headers.get("accept", "").lower()
    if not accept or "*/*" in accept:
        return True
    return any(m in accept for m in media)


async def _resolve_study(db: AsyncSession, user: User | None, study_uid: str) -> ImagingStudy:
    """Resolve a StudyInstanceUID within the caller's visible set. 404 when
    not visible — the out-of-scope case is indistinguishable from absent."""
    visible: Select = await visible_studies_filter(db, user)
    study = (
        (
            await db.execute(
                visible.where(ImagingStudy.study_instance_uid == study_uid).order_by(
                    ImagingStudy.created_at.asc()
                )
            )
        )
        .scalars()
        .first()
    )
    if study is None:
        raise HTTPException(status_code=404, detail="study not found")
    return study


async def _resolve_series(db: AsyncSession, study: ImagingStudy, series_uid: str) -> Series:
    series = (
        await db.execute(
            select(Series).where(
                Series.study_id == study.id,
                Series.series_instance_uid == series_uid,
            )
        )
    ).scalar_one_or_none()
    if series is None:
        raise HTTPException(status_code=404, detail="series not found")
    return series


def _qido_controls(request: Request) -> tuple[int, int]:
    params = request.query_params
    try:
        limit = int(params.get("limit", _QIDO_LIMIT_DEFAULT))
    except (TypeError, ValueError):
        limit = _QIDO_LIMIT_DEFAULT
    try:
        offset = int(params.get("offset", 0))
    except (TypeError, ValueError):
        offset = 0
    return max(1, min(limit, _QIDO_LIMIT_MAX)), max(0, offset)


_CONTROL_KEYS = {"limit", "offset", "includefield", "fuzzymatching"}


def _match_value(request: Request, keyword: str) -> str | None:
    """Read a QIDO match value by DICOM keyword OR its hex tag, case-tolerant."""
    params = request.query_params
    tag = dw.TAG.get(keyword)
    for key in params:
        kl = key.lower()
        if kl in _CONTROL_KEYS:
            continue
        if kl == keyword.lower() or (tag and key.upper() == tag):
            return params[key]
    return None


def _study_match_conditions(request: Request) -> list:
    """Translate the supported QIDO matching keys into SQL conditions. Keys we
    do not index are ignored (lenient QIDO), and advertised in the conformance
    statement rather than silently 400-ing a client."""
    conds = []
    if (uid := _match_value(request, "StudyInstanceUID")) is not None:
        conds.append(ImagingStudy.study_instance_uid == uid)
    if (pid := _match_value(request, "PatientID")) is not None:
        try:
            conds.append(ImagingStudy.patient_id == uuid.UUID(pid))
        except ValueError:
            conds.append(ImagingStudy.id.is_(None))  # unmatchable -> empty result
    if (modality := _match_value(request, "ModalitiesInStudy")) is not None:
        conds.append(ImagingStudy.modalities.contains([modality]))
    if (desc := _match_value(request, "StudyDescription")) is not None:
        conds.append(ImagingStudy.study_description.ilike(desc.replace("*", "%")))
    if (sd := _match_value(request, "StudyDate")) is not None:
        lo, _, hi = sd.partition("-")
        if not _:  # exact date "YYYYMMDD"
            conds.append(func.to_char(ImagingStudy.study_date, "YYYYMMDD") == sd)
        else:
            if lo:
                conds.append(func.to_char(ImagingStudy.study_date, "YYYYMMDD") >= lo)
            if hi:
                conds.append(func.to_char(ImagingStudy.study_date, "YYYYMMDD") <= hi)
    return conds


async def _study_counts(
    db: AsyncSession, study_ids: list[uuid.UUID]
) -> dict[uuid.UUID, tuple[int, int]]:
    """One grouped query: (#series, #instances) per study id for the page."""
    if not study_ids:
        return {}
    rows = (
        await db.execute(
            select(
                Series.study_id,
                func.count(func.distinct(Series.id)),
                func.count(Instance.id),
            )
            .select_from(Series)
            .outerjoin(Instance, Instance.series_id == Series.id)
            .where(Series.study_id.in_(study_ids))
            .group_by(Series.study_id)
        )
    ).all()
    return {sid: (int(ns), int(ni)) for sid, ns, ni in rows}


# ---- QIDO-RS ---------------------------------------------------------------


@router.get("/studies")
async def qido_studies(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(optional_user)],
    _scope: None = _AGENT_READ,
) -> Response:
    visible = await visible_studies_filter(db, user)
    limit, offset = _qido_controls(request)
    q = visible
    for cond in _study_match_conditions(request):
        q = q.where(cond)
    q = (
        q.order_by(ImagingStudy.study_date.desc().nullslast(), ImagingStudy.id)
        .limit(limit)
        .offset(offset)
    )
    studies = (await db.execute(q)).scalars().all()
    if not studies:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    counts = await _study_counts(db, [s.id for s in studies])
    base = _wado_base(request)
    items = [
        dw.study_to_json(
            study_instance_uid=s.study_instance_uid,
            study_date=s.study_date,
            study_description=s.study_description,
            modalities=list(s.modalities or []),
            patient_id=s.patient_id,
            num_series=counts.get(s.id, (0, 0))[0],
            num_instances=counts.get(s.id, (0, 0))[1],
            retrieve_url=f"{base}/studies/{s.study_instance_uid}",
        )
        for s in studies
    ]
    return JSONResponse(items, media_type=dw.DICOM_JSON_MEDIA_TYPE)


@router.get("/studies/{study_uid}/series")
async def qido_series(
    study_uid: str,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(optional_user)],
    _scope: None = _AGENT_READ,
) -> Response:
    study = await _resolve_study(db, user, study_uid)
    limit, offset = _qido_controls(request)
    q = select(Series).where(Series.study_id == study.id)
    if (modality := _match_value(request, "Modality")) is not None:
        q = q.where(Series.modality == modality)
    if (suid := _match_value(request, "SeriesInstanceUID")) is not None:
        q = q.where(Series.series_instance_uid == suid)
    q = q.order_by(Series.series_number.asc().nullslast(), Series.id).limit(limit).offset(offset)
    series = (await db.execute(q)).scalars().all()
    if not series:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    counts = {
        sid: int(n)
        for sid, n in (
            await db.execute(
                select(Instance.series_id, func.count(Instance.id))
                .where(Instance.series_id.in_([s.id for s in series]))
                .group_by(Instance.series_id)
            )
        ).all()
    }
    base = _wado_base(request)
    items = [
        dw.series_to_json(
            study_instance_uid=study.study_instance_uid,
            series_instance_uid=s.series_instance_uid,
            modality=s.modality,
            series_number=s.series_number,
            series_description=s.series_description,
            body_part=s.body_part_examined,
            num_instances=int(counts.get(s.id, 0)),
            retrieve_url=f"{base}/studies/{study.study_instance_uid}/series/{s.series_instance_uid}",
        )
        for s in series
    ]
    return JSONResponse(items, media_type=dw.DICOM_JSON_MEDIA_TYPE)


async def _instances_json(
    request: Request,
    db: AsyncSession,
    study: ImagingStudy,
    *,
    series: Series | None,
) -> Response:
    limit, offset = _qido_controls(request)
    q = (
        select(Instance, Series.series_instance_uid)
        .join(Series, Series.id == Instance.series_id)
        .where(Series.study_id == study.id)
    )
    if series is not None:
        q = q.where(Instance.series_id == series.id)
    if (sop := _match_value(request, "SOPInstanceUID")) is not None:
        q = q.where(Instance.sop_instance_uid == sop)
    q = (
        q.order_by(Instance.instance_number.asc().nullslast(), Instance.id)
        .limit(limit)
        .offset(offset)
    )
    rows = (await db.execute(q)).all()
    if not rows:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    base = _wado_base(request)
    items = [
        dw.instance_to_json(
            study_instance_uid=study.study_instance_uid,
            series_instance_uid=series_uid,
            sop_instance_uid=inst.sop_instance_uid,
            sop_class_uid=inst.sop_class_uid,
            instance_number=inst.instance_number,
            retrieve_url=(
                f"{base}/studies/{study.study_instance_uid}"
                f"/series/{series_uid}/instances/{inst.sop_instance_uid}"
            ),
        )
        for inst, series_uid in rows
    ]
    return JSONResponse(items, media_type=dw.DICOM_JSON_MEDIA_TYPE)


@router.get("/studies/{study_uid}/series/{series_uid}/instances")
async def qido_series_instances(
    study_uid: str,
    series_uid: str,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(optional_user)],
    _scope: None = _AGENT_READ,
) -> Response:
    study = await _resolve_study(db, user, study_uid)
    series = await _resolve_series(db, study, series_uid)
    return await _instances_json(request, db, study, series=series)


@router.get("/studies/{study_uid}/instances")
async def qido_study_instances(
    study_uid: str,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(optional_user)],
    _scope: None = _AGENT_READ,
) -> Response:
    study = await _resolve_study(db, user, study_uid)
    return await _instances_json(request, db, study, series=None)


# ---- WADO-RS retrieve (multipart/related) ----------------------------------


async def _instance_rows(
    db: AsyncSession, study: ImagingStudy, *, series: Series | None, sop_uid: str | None
) -> list[tuple[Instance, str]]:
    q = (
        select(Instance, Series.series_instance_uid)
        .join(Series, Series.id == Instance.series_id)
        .where(Series.study_id == study.id)
    )
    if series is not None:
        q = q.where(Instance.series_id == series.id)
    if sop_uid is not None:
        q = q.where(Instance.sop_instance_uid == sop_uid)
    q = q.order_by(
        Series.series_number.asc().nullslast(), Instance.instance_number.asc().nullslast()
    )
    return [(inst, series_uid) for inst, series_uid in (await db.execute(q)).all()]


def _stream_dicom(
    request: Request, study: ImagingStudy, rows: list[tuple[Instance, str]], grant: Grant | None
) -> StreamingResponse:
    storage = get_s3_storage()
    deid = should_deidentify(grant, study)
    current = get_settings().deid_method_version
    already = study.deidentified_at is not None and study.deid_method_version == current
    scrub = deid and not already
    base = _wado_base(request)

    def _source(inst: Instance):
        def gen():
            if scrub:
                raw = storage.get_object_bytes(bucket=inst.s3_bucket, key=inst.s3_key)
                yield deidentify_dicom_bytes(raw)
            else:
                body_iter, _length, _ctype = storage.iter_object(
                    bucket=inst.s3_bucket, key=inst.s3_key
                )
                yield from body_iter

        return gen

    parts: list[dw.MultipartPart] = [
        (
            f"{base}/studies/{study.study_instance_uid}/series/{series_uid}"
            f"/instances/{inst.sop_instance_uid}",
            _source(inst),
        )
        for inst, series_uid in rows
    ]
    boundary = dw.new_boundary()
    return StreamingResponse(
        dw.iter_multipart(parts, boundary),
        media_type=dw.multipart_content_type(boundary),
        headers={"cache-control": "no-store" if scrub else "private, max-age=0"},
    )


async def _retrieve(
    request: Request,
    db: AsyncSession,
    user: User | None,
    grant: Grant | None,
    audit,
    *,
    study_uid: str,
    series_uid: str | None,
    sop_uid: str | None,
) -> StreamingResponse:
    if not _accepts(request, "multipart/related", dw.DICOM_MEDIA_TYPE):
        raise HTTPException(status_code=status.HTTP_406_NOT_ACCEPTABLE, detail="multipart/related")
    study = await _resolve_study(db, user, study_uid)
    if not await can(db, user=user, action=DOWNLOAD_DICOM, study=study):
        raise HTTPException(status_code=403, detail="retrieve not permitted")
    series = await _resolve_series(db, study, series_uid) if series_uid else None
    rows = await _instance_rows(db, study, series=series, sop_uid=sop_uid)
    if not rows:
        raise HTTPException(status_code=404, detail="no instances")
    await audit.log(
        action="dicomweb_retrieve",
        actor_subject_id=user.subject_id if user else None,
        resource_kind="study",
        resource_id=study.id,
        metadata={"series_uid": series_uid, "sop_uid": sop_uid, "instances": len(rows)},
    )
    return _stream_dicom(request, study, rows, grant)


@router.get("/studies/{study_uid}")
async def wado_study(
    study_uid: str,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(optional_user)],
    audit: AuditDep,
    grant: Annotated[Grant | None, Depends(active_share_grant)] = None,
    _scope: None = _AGENT_IMAGES,
) -> StreamingResponse:
    return await _retrieve(
        request, db, user, grant, audit, study_uid=study_uid, series_uid=None, sop_uid=None
    )


@router.get("/studies/{study_uid}/series/{series_uid}")
async def wado_series(
    study_uid: str,
    series_uid: str,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(optional_user)],
    audit: AuditDep,
    grant: Annotated[Grant | None, Depends(active_share_grant)] = None,
    _scope: None = _AGENT_IMAGES,
) -> StreamingResponse:
    return await _retrieve(
        request, db, user, grant, audit, study_uid=study_uid, series_uid=series_uid, sop_uid=None
    )


@router.get("/studies/{study_uid}/series/{series_uid}/instances/{sop_uid}")
async def wado_instance(
    study_uid: str,
    series_uid: str,
    sop_uid: str,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(optional_user)],
    audit: AuditDep,
    grant: Annotated[Grant | None, Depends(active_share_grant)] = None,
    _scope: None = _AGENT_IMAGES,
) -> StreamingResponse:
    return await _retrieve(
        request, db, user, grant, audit, study_uid=study_uid, series_uid=series_uid, sop_uid=sop_uid
    )


# ---- WADO-RS metadata (application/dicom+json) -----------------------------


async def _metadata(
    db: AsyncSession,
    user: User | None,
    grant: Grant | None,
    *,
    study_uid: str,
    series_uid: str | None,
    sop_uid: str | None,
) -> Response:
    study = await _resolve_study(db, user, study_uid)
    series = await _resolve_series(db, study, series_uid) if series_uid else None
    rows = await _instance_rows(db, study, series=series, sop_uid=sop_uid)
    if not rows:
        raise HTTPException(status_code=404, detail="no instances")
    storage = get_s3_storage()
    deid = should_deidentify(grant, study)
    current = get_settings().deid_method_version
    already = study.deidentified_at is not None and study.deid_method_version == current
    scrub = deid and not already

    def _read_header(inst: Instance) -> dict:
        raw = storage.get_object_bytes(bucket=inst.s3_bucket, key=inst.s3_key)
        if scrub:
            raw = deidentify_dicom_bytes(raw)
        return dw.header_to_json(raw)

    items = [await asyncio.to_thread(_read_header, inst) for inst, _series_uid in rows]
    return JSONResponse(items, media_type=dw.DICOM_JSON_MEDIA_TYPE)


@router.get("/studies/{study_uid}/metadata")
async def wado_study_metadata(
    study_uid: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(optional_user)],
    grant: Annotated[Grant | None, Depends(active_share_grant)] = None,
    _scope: None = _AGENT_READ,
) -> Response:
    return await _metadata(db, user, grant, study_uid=study_uid, series_uid=None, sop_uid=None)


@router.get("/studies/{study_uid}/series/{series_uid}/metadata")
async def wado_series_metadata(
    study_uid: str,
    series_uid: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(optional_user)],
    grant: Annotated[Grant | None, Depends(active_share_grant)] = None,
    _scope: None = _AGENT_READ,
) -> Response:
    return await _metadata(
        db, user, grant, study_uid=study_uid, series_uid=series_uid, sop_uid=None
    )


@router.get("/studies/{study_uid}/series/{series_uid}/instances/{sop_uid}/metadata")
async def wado_instance_metadata(
    study_uid: str,
    series_uid: str,
    sop_uid: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(optional_user)],
    grant: Annotated[Grant | None, Depends(active_share_grant)] = None,
    _scope: None = _AGENT_READ,
) -> Response:
    return await _metadata(
        db, user, grant, study_uid=study_uid, series_uid=series_uid, sop_uid=sop_uid
    )
