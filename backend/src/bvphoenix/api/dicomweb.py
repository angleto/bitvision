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

Scope: QIDO studies/series/instances (hierarchical) + relational roots
(/series, /instances) + WADO retrieve (study/series/instance,
multipart/related streaming) + WADO metadata (with ``BulkDataURI`` wired to
the frames/bulkdata resources) + WADO frames (per-frame codec bitstream, no
transcoding) + WADO bulkdata (top-level binary element). Rendered
(JPEG/PNG) and transfer-syntax transcoding via Accept remain tracked
follow-ups; v1 serves the stored transfer syntax verbatim.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.auth import active_share_grant, require_scope_if_agent
from bvphoenix.auth.deps import public_user
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
    Content-Location. A bitvision URL only — never a storage location.

    Behind the TLS-terminating Traefik proxy ``request.base_url`` is
    ``http://``; honour ``X-Forwarded-Proto`` so the emitted URLs use the
    public scheme (https) and a client following a RetrieveURL from an https
    context doesn't hit mixed-content."""
    base = str(request.base_url).rstrip("/")
    fwd = request.headers.get("x-forwarded-proto", "").split(",")[0].strip()
    if fwd in ("http", "https") and "://" in base:
        base = f"{fwd}://{base.split('://', 1)[1]}"
    return base + "/api/dicom"


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


async def _resolve_instance(db: AsyncSession, series: Series, sop_uid: str) -> Instance:
    inst = (
        await db.execute(
            select(Instance).where(
                Instance.series_id == series.id,
                Instance.sop_instance_uid == sop_uid,
            )
        )
    ).scalar_one_or_none()
    if inst is None:
        raise HTTPException(status_code=404, detail="instance not found")
    return inst


def _parse_frame_list(frame_list: str) -> list[int]:
    """Parse the PS3.18 ``framelist`` path segment (comma-separated, 1-based)."""
    try:
        nums = [int(x) for x in frame_list.split(",") if x != ""]
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid frame list") from None
    if not nums or any(n < 1 for n in nums):
        raise HTTPException(status_code=400, detail="invalid frame list")
    return nums


def _single_byte_part(loc: str, data: bytes) -> dw.MultipartPart:
    """A multipart part whose body is one in-memory blob."""

    def _body() -> Iterator[bytes]:
        yield data

    return (loc, _body)


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
    user: Annotated[User | None, Depends(public_user)],
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
    user: Annotated[User | None, Depends(public_user)],
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
    user: Annotated[User | None, Depends(public_user)],
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
    user: Annotated[User | None, Depends(public_user)],
    _scope: None = _AGENT_READ,
) -> Response:
    study = await _resolve_study(db, user, study_uid)
    return await _instances_json(request, db, study, series=None)


# ---- QIDO-RS relational roots (across the caller's visible studies) --------
#
# The hierarchical forms above pin a study (and series) in the path; the
# relational roots /series and /instances search the whole visible set, scoped
# by the same ``visible_studies_filter`` subquery so an out-of-scope row is
# simply absent — cross-patient access stays inexpressible.


@router.get("/series")
async def qido_relational_series(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(public_user)],
    _scope: None = _AGENT_READ,
) -> Response:
    visible_ids = (await visible_studies_filter(db, user)).with_only_columns(ImagingStudy.id)
    limit, offset = _qido_controls(request)
    q = (
        select(Series, ImagingStudy.study_instance_uid)
        .join(ImagingStudy, ImagingStudy.id == Series.study_id)
        .where(ImagingStudy.id.in_(visible_ids))
    )
    if (modality := _match_value(request, "Modality")) is not None:
        q = q.where(Series.modality == modality)
    if (suid := _match_value(request, "SeriesInstanceUID")) is not None:
        q = q.where(Series.series_instance_uid == suid)
    if (stuid := _match_value(request, "StudyInstanceUID")) is not None:
        q = q.where(ImagingStudy.study_instance_uid == stuid)
    q = q.order_by(Series.series_number.asc().nullslast(), Series.id).limit(limit).offset(offset)
    rows = (await db.execute(q)).all()
    if not rows:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    counts = {
        sid: int(n)
        for sid, n in (
            await db.execute(
                select(Instance.series_id, func.count(Instance.id))
                .where(Instance.series_id.in_([s.id for s, _ in rows]))
                .group_by(Instance.series_id)
            )
        ).all()
    }
    base = _wado_base(request)
    items = [
        dw.series_to_json(
            study_instance_uid=study_uid,
            series_instance_uid=s.series_instance_uid,
            modality=s.modality,
            series_number=s.series_number,
            series_description=s.series_description,
            body_part=s.body_part_examined,
            num_instances=counts.get(s.id, 0),
            retrieve_url=f"{base}/studies/{study_uid}/series/{s.series_instance_uid}",
        )
        for s, study_uid in rows
    ]
    return JSONResponse(items, media_type=dw.DICOM_JSON_MEDIA_TYPE)


@router.get("/instances")
async def qido_relational_instances(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(public_user)],
    _scope: None = _AGENT_READ,
) -> Response:
    visible_ids = (await visible_studies_filter(db, user)).with_only_columns(ImagingStudy.id)
    limit, offset = _qido_controls(request)
    q = (
        select(Instance, Series.series_instance_uid, ImagingStudy.study_instance_uid)
        .join(Series, Series.id == Instance.series_id)
        .join(ImagingStudy, ImagingStudy.id == Series.study_id)
        .where(ImagingStudy.id.in_(visible_ids))
    )
    if (sop := _match_value(request, "SOPInstanceUID")) is not None:
        q = q.where(Instance.sop_instance_uid == sop)
    if (suid := _match_value(request, "SeriesInstanceUID")) is not None:
        q = q.where(Series.series_instance_uid == suid)
    if (stuid := _match_value(request, "StudyInstanceUID")) is not None:
        q = q.where(ImagingStudy.study_instance_uid == stuid)
    if (modality := _match_value(request, "Modality")) is not None:
        q = q.where(Series.modality == modality)
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
            study_instance_uid=study_uid,
            series_instance_uid=series_uid,
            sop_instance_uid=inst.sop_instance_uid,
            sop_class_uid=inst.sop_class_uid,
            instance_number=inst.instance_number,
            retrieve_url=(
                f"{base}/studies/{study_uid}/series/{series_uid}/instances/{inst.sop_instance_uid}"
            ),
        )
        for inst, series_uid, study_uid in rows
    ]
    return JSONResponse(items, media_type=dw.DICOM_JSON_MEDIA_TYPE)


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
    user: Annotated[User | None, Depends(public_user)],
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
    user: Annotated[User | None, Depends(public_user)],
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
    user: Annotated[User | None, Depends(public_user)],
    audit: AuditDep,
    grant: Annotated[Grant | None, Depends(active_share_grant)] = None,
    _scope: None = _AGENT_IMAGES,
) -> StreamingResponse:
    return await _retrieve(
        request, db, user, grant, audit, study_uid=study_uid, series_uid=series_uid, sop_uid=sop_uid
    )


# ---- WADO-RS frames + bulkdata ---------------------------------------------
#
# Frames serve the stored codec bitstream per frame (no transcoding in v1) so
# OHIF's default pixel-streaming path works. Same DOWNLOAD_DICOM gate, storage
# isolation, and PS3.15 de-id-on-egress as the full-instance retrieve.


async def _load_instance_bytes_scrubbed(inst: Instance, *, scrub: bool) -> bytes:
    storage = get_s3_storage()

    def _read() -> bytes:
        raw = storage.get_object_bytes(bucket=inst.s3_bucket, key=inst.s3_key)
        return deidentify_dicom_bytes(raw) if scrub else raw

    return await asyncio.to_thread(_read)


def _scrub_needed(grant: Grant | None, study: ImagingStudy) -> bool:
    deid = should_deidentify(grant, study)
    current = get_settings().deid_method_version
    already = study.deidentified_at is not None and study.deid_method_version == current
    return deid and not already


@router.get("/studies/{study_uid}/series/{series_uid}/instances/{sop_uid}/frames/{frame_list}")
async def wado_frames(
    study_uid: str,
    series_uid: str,
    sop_uid: str,
    frame_list: str,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(public_user)],
    audit: AuditDep,
    grant: Annotated[Grant | None, Depends(active_share_grant)] = None,
    _scope: None = _AGENT_IMAGES,
) -> StreamingResponse:
    if not _accepts(request, "multipart/related"):
        raise HTTPException(status_code=status.HTTP_406_NOT_ACCEPTABLE, detail="multipart/related")
    frames_req = _parse_frame_list(frame_list)
    study = await _resolve_study(db, user, study_uid)
    if not await can(db, user=user, action=DOWNLOAD_DICOM, study=study):
        raise HTTPException(status_code=403, detail="retrieve not permitted")
    series = await _resolve_series(db, study, series_uid)
    inst = await _resolve_instance(db, series, sop_uid)
    scrub = _scrub_needed(grant, study)
    raw = await _load_instance_bytes_scrubbed(inst, scrub=scrub)
    try:
        transfer_syntax, frames = await asyncio.to_thread(dw.extract_frames, raw, frames_req)
    except dw.FrameError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    await audit.log(
        action="dicomweb_retrieve_frames",
        actor_subject_id=user.subject_id if user else None,
        resource_kind="study",
        resource_id=study.id,
        metadata={"sop_uid": sop_uid, "frames": frames_req},
    )
    media = dw.frame_media_type(transfer_syntax)
    base = _wado_base(request)
    inst_url = f"{base}/studies/{study.study_instance_uid}/series/{series_uid}/instances/{sop_uid}"
    parts = [
        _single_byte_part(f"{inst_url}/frames/{n}", frames[i]) for i, n in enumerate(frames_req)
    ]
    boundary = dw.new_boundary()
    return StreamingResponse(
        dw.iter_multipart(
            parts,
            boundary,
            part_type=media,
            part_content_type=f"{media}; transfer-syntax={transfer_syntax}",
        ),
        media_type=dw.multipart_content_type(boundary, part_type=media),
        headers={"cache-control": "no-store" if scrub else "private, max-age=0"},
    )


@router.get("/studies/{study_uid}/series/{series_uid}/instances/{sop_uid}/bulkdata/{tag}")
async def wado_bulkdata(
    study_uid: str,
    series_uid: str,
    sop_uid: str,
    tag: str,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(public_user)],
    audit: AuditDep,
    grant: Annotated[Grant | None, Depends(active_share_grant)] = None,
    _scope: None = _AGENT_IMAGES,
) -> StreamingResponse:
    if not _accepts(request, "multipart/related"):
        raise HTTPException(status_code=status.HTTP_406_NOT_ACCEPTABLE, detail="multipart/related")
    try:
        tag_int = int(tag, 16)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid bulkdata tag") from None
    study = await _resolve_study(db, user, study_uid)
    if not await can(db, user=user, action=DOWNLOAD_DICOM, study=study):
        raise HTTPException(status_code=403, detail="retrieve not permitted")
    series = await _resolve_series(db, study, series_uid)
    inst = await _resolve_instance(db, series, sop_uid)
    scrub = _scrub_needed(grant, study)
    raw = await _load_instance_bytes_scrubbed(inst, scrub=scrub)
    data = await asyncio.to_thread(dw.extract_bulkdata, raw, tag_int)
    if data is None:
        raise HTTPException(status_code=404, detail="bulkdata not found")
    base = _wado_base(request)
    loc = (
        f"{base}/studies/{study.study_instance_uid}/series/{series_uid}"
        f"/instances/{sop_uid}/bulkdata/{tag.upper()}"
    )
    boundary = dw.new_boundary()
    return StreamingResponse(
        dw.iter_multipart(
            [_single_byte_part(loc, data)], boundary, part_type=dw.OCTET_STREAM_MEDIA_TYPE
        ),
        media_type=dw.multipart_content_type(boundary, part_type=dw.OCTET_STREAM_MEDIA_TYPE),
        headers={"cache-control": "no-store" if scrub else "private, max-age=0"},
    )


# ---- WADO-RS metadata (application/dicom+json) -----------------------------


async def _metadata(
    request: Request,
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
    scrub = _scrub_needed(grant, study)
    base = _wado_base(request)

    def _read_header(inst: Instance, series_uid: str) -> dict:
        raw = storage.get_object_bytes(bucket=inst.s3_bucket, key=inst.s3_key)
        if scrub:
            raw = deidentify_dicom_bytes(raw)
        inst_url = (
            f"{base}/studies/{study.study_instance_uid}"
            f"/series/{series_uid}/instances/{inst.sop_instance_uid}"
        )
        return dw.header_to_json(raw, instance_url=inst_url)

    items = [await asyncio.to_thread(_read_header, inst, su) for inst, su in rows]
    return JSONResponse(items, media_type=dw.DICOM_JSON_MEDIA_TYPE)


@router.get("/studies/{study_uid}/metadata")
async def wado_study_metadata(
    study_uid: str,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(public_user)],
    grant: Annotated[Grant | None, Depends(active_share_grant)] = None,
    _scope: None = _AGENT_READ,
) -> Response:
    return await _metadata(
        request, db, user, grant, study_uid=study_uid, series_uid=None, sop_uid=None
    )


@router.get("/studies/{study_uid}/series/{series_uid}/metadata")
async def wado_series_metadata(
    study_uid: str,
    series_uid: str,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(public_user)],
    grant: Annotated[Grant | None, Depends(active_share_grant)] = None,
    _scope: None = _AGENT_READ,
) -> Response:
    return await _metadata(
        request, db, user, grant, study_uid=study_uid, series_uid=series_uid, sop_uid=None
    )


@router.get("/studies/{study_uid}/series/{series_uid}/instances/{sop_uid}/metadata")
async def wado_instance_metadata(
    study_uid: str,
    series_uid: str,
    sop_uid: str,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(public_user)],
    grant: Annotated[Grant | None, Depends(active_share_grant)] = None,
    _scope: None = _AGENT_READ,
) -> Response:
    return await _metadata(
        request, db, user, grant, study_uid=study_uid, series_uid=series_uid, sop_uid=sop_uid
    )
