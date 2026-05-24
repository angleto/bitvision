"""Patient Health Record (Fascicolo) export — async Job route.

``POST /api/patients/{id}/export`` (DESIGN.md §11) enqueues a Job and
returns ``{job_id, status}``. The worker writes the ZIP to S3 and
the frontend polls ``GET /api/jobs/{job_id}`` until ``succeeded``,
then fetches the presigned URL on ``result_download_url``.

The legacy synchronous ``GET`` route was removed once the frontend
migrated to the polling flow; the archive layout, manifest schema
and permission checks live in
:mod:`bvphoenix.services.patient_export` and are shared with the
worker task ``export_patient_zip``.
"""

from __future__ import annotations

import json
import uuid
from typing import Annotated, Any

from arq import create_pool
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.api.jobs import JobOut, cap_exceeded_to_http
from bvphoenix.auth import enforce_agent_patient_scope, require_user
from bvphoenix.config import get_settings
from bvphoenix.db.models import (
    Document,
    Folder,
    FolderItem,
    ImagingStudy,
    Patient,
    User,
)
from bvphoenix.db.session import get_db
from bvphoenix.services import jobs as jobs_service
from bvphoenix.services.arq_redis import redis_settings
from bvphoenix.services.patient_export import (
    ALLOWED_INCLUDES,
    DEFAULT_INCLUDES,
)
from bvphoenix.services.permissions import (
    READ_METADATA,
    READ_PIXELS,
    can,
    can_patient,
)

router = APIRouter(tags=["patient-export"])


JOB_KIND_FASCICOLO_EXPORT = "fascicolo_export"
JOB_KIND_STUDY_EXPORT = "study_export"

# Export artifacts (the ZIP on S3 + the row in ``jobs`` that points at
# it) live for 48 hours after creation, then the ``cleanup_jobs`` cron
# drops both. Rationale: the export typically gets consumed within
# minutes of finishing — the user clicks Download, the browser keeps
# the bytes locally. Holding the S3 object for days is just paying
# for storage that nobody reads. 48h gives the user a safety net
# (overnight + one day) for the "I forgot to download / my browser
# crashed" case without letting clinical-PHI ZIPs accumulate.
_EXPORT_ARTIFACT_TTL_HOURS = 48


def _format_study_label(study: ImagingStudy) -> str:
    """Doctor-friendly one-liner for a study export Job row.

    Prefers the StudyDescription tag (e.g. "TC torace senza mdc")
    over a synthetic "<MOD> <date>" stitch, since the description is
    what the radiologist already recognises. Falls back to
    "<modalities> <date>" then to the bare UUID prefix so the
    JobsTray never renders an empty cell.
    """
    desc = (study.study_description or "").strip()
    parts: list[str] = []
    if desc:
        parts.append(desc)
    mods = ", ".join(study.modalities or [])
    if mods:
        parts.append(mods)
    if study.study_date is not None:
        parts.append(str(study.study_date))
    if parts:
        return " · ".join(parts)
    return f"Studio {str(study.id)[:8]}"


def _format_patient_label(patient: Patient) -> str:
    """Label for a fascicolo_export Job — the patient's display name
    is the only piece a doctor needs to see in the JobsTray to know
    which fascicolo is being prepared."""
    return f"Fascicolo: {patient.display_name}"


def _format_folder_label(folder: Folder, patient: Patient) -> str:
    """Label for a folder_export Job. Folders share the
    ``fascicolo_export`` kind with patient-level exports, so the FE
    cannot differentiate them by ``kind`` alone — the label disambiguates
    ("Cartella Piedi · Mamma Bianchi" vs "Fascicolo: Mamma Bianchi")."""
    return f"Cartella {folder.name} · {patient.display_name}"


def _parse_includes(include: str | None) -> set[str]:
    if not include:
        return set(DEFAULT_INCLUDES)
    parts = {p.strip() for p in include.split(",") if p.strip()}
    bad = parts - ALLOWED_INCLUDES
    if bad:
        raise HTTPException(
            status_code=400,
            detail=f"invalid include values: {sorted(bad)}",
        )
    return parts


class ExportJobIn(BaseModel):
    """Body for ``POST /patients/{id}/export``. ``include`` is the
    same comma-joined section list the legacy GET accepted."""

    include: str | None = None


@router.post(
    "/patients/{patient_id}/export",
    response_model=JobOut,
    status_code=202,
)
async def export_patient_async(
    request: Request,
    patient_id: uuid.UUID,
    body: ExportJobIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
) -> JobOut:
    """Enqueue an export Job. Returns 202 with the job descriptor;
    poll ``GET /api/jobs/{id}`` for progress and the result URL."""
    patient = (
        await db.execute(select(Patient).where(Patient.id == patient_id))
    ).scalar_one_or_none()
    if patient is None:
        raise HTTPException(status_code=404, detail="patient not found")
    enforce_agent_patient_scope(request, patient.id, scope="patient:read")
    if not await can_patient(db, user=user, action=READ_METADATA, patient=patient):
        raise HTTPException(status_code=404, detail="patient not found")

    includes = _parse_includes(body.include)
    canonical_input: dict[str, Any] = {
        "includes": sorted(includes),
        "_display_label": _format_patient_label(patient),
    }

    try:
        result = await jobs_service.enqueue_or_get(
            db,
            kind=JOB_KIND_FASCICOLO_EXPORT,
            owner_subject_id=user.subject_id,
            canonical_input=canonical_input,
            scope_ids=(patient.id,),
            expires_in_hours=_EXPORT_ARTIFACT_TTL_HOURS,
            is_admin=bool(user.is_admin),
        )
    except jobs_service.JobCapExceededError as e:
        raise cap_exceeded_to_http(e) from e

    if not result.deduped:
        # Fresh row: hand off to the worker. Failure to enqueue is
        # non-recoverable for this Job — mark it failed so the user
        # sees a definite outcome instead of a row stuck in 'queued'.
        try:
            settings = get_settings()
            redis = await create_pool(redis_settings(settings.redis_url))
            arq_handle = await redis.enqueue_job(
                "export_patient_zip",
                str(result.job.id),
                str(patient.id),
                str(user.subject_id),
                json.dumps(canonical_input),
            )
            await redis.close()
            if arq_handle is not None:
                await jobs_service.set_arq_job_id(db, result.job.id, arq_handle.job_id)
        except Exception as exc:
            await jobs_service.mark_failed(
                db,
                result.job.id,
                error={
                    "code": "enqueue_failed",
                    "message": str(exc),
                },
            )
            await db.commit()
            raise HTTPException(
                status_code=503,
                detail={
                    "error": "enqueue_failed",
                    "hint": "worker queue unavailable; try again shortly",
                },
            ) from exc

    await db.commit()
    # set_arq_job_id (when fired) issues an UPDATE that triggers
    # ``updated_at``'s ``onupdate=func.now()`` server-side; SQLAlchemy
    # marks that column as expired until the next refresh. Without an
    # explicit refresh the pydantic ``model_validate`` below tries to
    # lazy-load the column from outside a greenlet and crashes with
    # MissingGreenlet, returning a 500 to the client even though the
    # Job was successfully enqueued.
    await db.refresh(result.job)
    return JobOut.model_validate(result.job)


# ---------------------------------------------------------------------------
# Study export — single ImagingStudy, dedicated worker task
# ---------------------------------------------------------------------------


class StudyExportIn(BaseModel):
    """Body for ``POST /api/studies/{id}/export``.

    Empty for now; reserved for future per-call options
    (de-identification toggle, optional ``include`` filters, ...) so
    the dedup hash has a stable place to grow.
    """


@router.post(
    "/studies/{study_id}/export",
    response_model=JobOut,
    status_code=202,
)
async def export_study_async(
    request: Request,
    study_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    body: StudyExportIn | None = None,
) -> JobOut:
    """Enqueue a Job that streams every DICOM in this study into a ZIP.

    Same JobsTray UX as the fascicolo export, but scoped to one study
    and gated by the stricter ``READ_PIXELS`` permission. Returns 202
    with the job descriptor; poll ``GET /api/jobs/{id}`` for progress
    and the result URL.
    """
    del body  # placeholder for future options
    study = (
        await db.execute(select(ImagingStudy).where(ImagingStudy.id == study_id))
    ).scalar_one_or_none()
    if study is None:
        raise HTTPException(status_code=404, detail="study not found")
    enforce_agent_patient_scope(request, study.patient_id, scope="patient:images")
    if not await can(db, user=user, action=READ_PIXELS, study=study):
        raise HTTPException(status_code=404, detail="study not found")

    # Canonical input is empty today; kept as a dict so future
    # toggles (de-identification, alternate manifest variants, ...)
    # land on a stable dedup hash without breaking older rows.
    # ``_display_label`` is excluded from dedup (underscore prefix);
    # surfaces in the JobsTray so two parallel study_export rows are
    # told apart instead of "Esporta studio DICOM" twice.
    canonical_input: dict[str, Any] = {
        "_display_label": _format_study_label(study),
    }

    try:
        result = await jobs_service.enqueue_or_get(
            db,
            kind=JOB_KIND_STUDY_EXPORT,
            owner_subject_id=user.subject_id,
            canonical_input=canonical_input,
            scope_ids=(study.id,),
            expires_in_hours=_EXPORT_ARTIFACT_TTL_HOURS,
            is_admin=bool(user.is_admin),
        )
    except jobs_service.JobCapExceededError as e:
        raise cap_exceeded_to_http(e) from e

    if not result.deduped:
        try:
            settings = get_settings()
            redis = await create_pool(redis_settings(settings.redis_url))
            arq_handle = await redis.enqueue_job(
                "export_study_zip",
                str(result.job.id),
                str(study.id),
                str(user.subject_id),
                json.dumps(canonical_input),
            )
            await redis.close()
            if arq_handle is not None:
                await jobs_service.set_arq_job_id(db, result.job.id, arq_handle.job_id)
        except Exception as exc:
            await jobs_service.mark_failed(
                db,
                result.job.id,
                error={"code": "enqueue_failed", "message": str(exc)},
            )
            await db.commit()
            raise HTTPException(
                status_code=503,
                detail={
                    "error": "enqueue_failed",
                    "hint": "worker queue unavailable; try again shortly",
                },
            ) from exc

    await db.commit()
    await db.refresh(result.job)
    return JobOut.model_validate(result.job)


# ---------------------------------------------------------------------------
# Folder export — same async-Job pipeline, scoped to a single folder
# ---------------------------------------------------------------------------


class FolderExportIn(BaseModel):
    """Body for ``POST /api/folders/{folder_id}/export``.

    The scope (which studies / documents to ZIP) defaults to every
    item under the folder, but the user can narrow it via the
    optional ``include_study_ids`` / ``include_document_ids`` lists.
    Use case the user reported: a folder containing 6 studies + 2
    big DICOM ISOs — they want to export the studies only, not the
    multi-GB ISOs they already have on a physical disc. Pass the
    set of resource ids they want IN; everything else is dropped.

    ``include`` (kept for backward compatibility) chooses which
    sections (studies / reports / documents / annotations / dicom)
    to bundle inside the ZIP, regardless of which specific items are
    in scope.
    """

    include: str | None = None
    include_study_ids: list[uuid.UUID] | None = None
    include_document_ids: list[uuid.UUID] | None = None


async def _resolve_folder_scope(
    db: AsyncSession,
    folder_id: uuid.UUID,
    *,
    recursive: bool = True,
) -> tuple[Patient, set[uuid.UUID], set[uuid.UUID]]:
    """Resolve ``(patient, study_ids, document_ids)`` for a folder.

    Walks ``FolderItem`` rows for ``folder_id`` (and recursively for
    every ``subfolder`` child when ``recursive`` is True). The folder
    must contain at least one item that pins it to a single patient
    — empty folders are rejected with 422 because there's nothing to
    ZIP.
    """
    visited: set[uuid.UUID] = set()
    queue: list[uuid.UUID] = [folder_id]
    study_ids: set[uuid.UUID] = set()
    document_ids: set[uuid.UUID] = set()

    while queue:
        fid = queue.pop()
        if fid in visited:
            continue
        visited.add(fid)
        rows = (
            await db.execute(
                select(FolderItem.resource_kind, FolderItem.resource_id).where(
                    FolderItem.folder_id == fid
                )
            )
        ).all()
        for kind, rid in rows:
            if kind == "study":
                study_ids.add(rid)
            elif kind == "document":
                document_ids.add(rid)
            elif kind == "subfolder" and recursive:
                queue.append(rid)
            # series / annotation / report / consultation: skipped —
            # they're emitted as part of their parent study's branch.

    # Resolve the owning patient by sampling one in-scope item. We
    # require all items live under the same patient; the v3 schema
    # enforces this via composite FKs, so a folder with a study from
    # patient A and a document from patient B is unrepresentable —
    # the cross-patient invariant. Still, we sanity-check here.
    sample_patient_id: uuid.UUID | None = None
    if study_ids:
        sample_patient_id = (
            await db.execute(
                select(ImagingStudy.patient_id).where(ImagingStudy.id.in_(study_ids)).limit(1)
            )
        ).scalar_one_or_none()
    if sample_patient_id is None and document_ids:
        sample_patient_id = (
            await db.execute(
                select(Document.patient_id).where(Document.id.in_(document_ids)).limit(1)
            )
        ).scalar_one_or_none()
    if sample_patient_id is None:
        raise HTTPException(
            status_code=422,
            detail="folder has no exportable items",
        )
    patient = (
        await db.execute(select(Patient).where(Patient.id == sample_patient_id))
    ).scalar_one_or_none()
    if patient is None:
        raise HTTPException(status_code=404, detail="patient not found")
    return patient, study_ids, document_ids


@router.post(
    "/folders/{folder_id}/export",
    response_model=JobOut,
    status_code=202,
)
async def export_folder_async(
    request: Request,
    folder_id: uuid.UUID,
    body: FolderExportIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
) -> JobOut:
    """Enqueue a Job that ZIPs only the items inside a folder.

    Reuses the patient-export worker (``export_patient_zip``) with a
    ``scope_study_ids`` / ``scope_document_ids`` filter populated from
    the folder's ``FolderItem`` rows (recursive). Reports / markers
    branches narrow to whatever links to the in-scope studies /
    documents.
    """
    folder = (await db.execute(select(Folder).where(Folder.id == folder_id))).scalar_one_or_none()
    if folder is None:
        raise HTTPException(status_code=404, detail="folder not found")
    if folder.owner_subject_id != user.subject_id and not user.is_admin:
        raise HTTPException(status_code=404, detail="folder not found")

    patient, study_ids, document_ids = await _resolve_folder_scope(db, folder_id)
    enforce_agent_patient_scope(request, patient.id, scope="patient:read")
    if not await can_patient(db, user=user, action=READ_METADATA, patient=patient):
        raise HTTPException(status_code=404, detail="patient not found")

    # Per-item filter: when the user deselected files in the dialog
    # (e.g. skipping the multi-GB DICOM ISOs they already have on a
    # physical disc), narrow the auto-resolved scope to the chosen
    # subset. Validate that every requested id was actually in the
    # folder so a tampered body can never broaden the scope to
    # resources outside this folder.
    if body.include_study_ids is not None:
        requested = set(body.include_study_ids)
        invalid = requested - study_ids
        if invalid:
            raise HTTPException(
                status_code=422,
                detail="include_study_ids contains ids not in this folder",
            )
        study_ids = study_ids & requested
    if body.include_document_ids is not None:
        requested = set(body.include_document_ids)
        invalid = requested - document_ids
        if invalid:
            raise HTTPException(
                status_code=422,
                detail="include_document_ids contains ids not in this folder",
            )
        document_ids = document_ids & requested
    if not study_ids and not document_ids:
        raise HTTPException(
            status_code=422,
            detail="folder has no exportable items after filter",
        )

    includes = _parse_includes(body.include)
    canonical_input: dict[str, Any] = {
        "includes": sorted(includes),
        "scope_study_ids": sorted(str(x) for x in study_ids),
        "scope_document_ids": sorted(str(x) for x in document_ids),
        "scope_kind": "folder",
        "scope_folder_id": str(folder_id),
        "_display_label": _format_folder_label(folder, patient),
    }

    try:
        result = await jobs_service.enqueue_or_get(
            db,
            kind=JOB_KIND_FASCICOLO_EXPORT,
            owner_subject_id=user.subject_id,
            canonical_input=canonical_input,
            scope_ids=(patient.id,),
            expires_in_hours=_EXPORT_ARTIFACT_TTL_HOURS,
            is_admin=bool(user.is_admin),
        )
    except jobs_service.JobCapExceededError as e:
        raise cap_exceeded_to_http(e) from e

    if not result.deduped:
        try:
            settings = get_settings()
            redis = await create_pool(redis_settings(settings.redis_url))
            arq_handle = await redis.enqueue_job(
                "export_patient_zip",
                str(result.job.id),
                str(patient.id),
                str(user.subject_id),
                json.dumps(canonical_input),
            )
            await redis.close()
            if arq_handle is not None:
                await jobs_service.set_arq_job_id(db, result.job.id, arq_handle.job_id)
        except Exception as exc:
            await jobs_service.mark_failed(
                db,
                result.job.id,
                error={"code": "enqueue_failed", "message": str(exc)},
            )
            await db.commit()
            raise HTTPException(
                status_code=503,
                detail={
                    "error": "enqueue_failed",
                    "hint": "worker queue unavailable; try again shortly",
                },
            ) from exc

    await db.commit()
    # set_arq_job_id (when fired) issues an UPDATE that triggers
    # ``updated_at``'s ``onupdate=func.now()`` server-side; SQLAlchemy
    # marks that column as expired until the next refresh. Without an
    # explicit refresh the pydantic ``model_validate`` below tries to
    # lazy-load the column from outside a greenlet and crashes with
    # MissingGreenlet, returning a 500 to the client even though the
    # Job was successfully enqueued.
    await db.refresh(result.job)
    return JobOut.model_validate(result.job)


# ---------------------------------------------------------------------------
# Bulk download — caller-supplied (kind, id) list → scoped Job
# ---------------------------------------------------------------------------


_BulkDownloadKind = str  # "study" | "document"


class BulkDownloadItem(BaseModel):
    id: uuid.UUID
    kind: _BulkDownloadKind


class BulkDownloadIn(BaseModel):
    """Body for ``POST /api/bulk/download``.

    The frontend's BatchActionBar passes the user's selection here;
    we resolve it into a scoped fascicolo_export Job. Only ``study``
    and ``document`` kinds are accepted as direct ZIP scope today —
    folders / series / reports are derived from their parent or
    handled by the dedicated endpoints.
    """

    items: list[BulkDownloadItem]
    include: str | None = None


@router.post(
    "/bulk/download",
    response_model=JobOut,
    status_code=202,
)
async def bulk_download_async(
    request: Request,
    body: BulkDownloadIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
) -> JobOut:
    """Enqueue a Job that ZIPs the caller's heterogeneous selection.

    The selection must point at items belonging to a single patient
    — the cross-patient invariant (memory ``cross_patient_links_forbidden``)
    forbids mixing patients in one archive. 422 if violated.
    """
    if not body.items:
        raise HTTPException(status_code=422, detail="empty selection")

    study_ids: set[uuid.UUID] = set()
    document_ids: set[uuid.UUID] = set()
    for it in body.items:
        if it.kind == "study":
            study_ids.add(it.id)
        elif it.kind == "document":
            document_ids.add(it.id)
        else:
            raise HTTPException(
                status_code=422,
                detail=f"kind {it.kind!r} cannot be bulk-downloaded directly",
            )

    patient_ids: set[uuid.UUID] = set()
    if study_ids:
        rows = (
            await db.execute(select(ImagingStudy.patient_id).where(ImagingStudy.id.in_(study_ids)))
        ).all()
        patient_ids.update(pid for (pid,) in rows)
    if document_ids:
        rows = (
            await db.execute(select(Document.patient_id).where(Document.id.in_(document_ids)))
        ).all()
        patient_ids.update(pid for (pid,) in rows)
    if len(patient_ids) != 1:
        raise HTTPException(
            status_code=422,
            detail="bulk download accepts items from a single patient only",
        )
    (patient_id,) = patient_ids

    patient = (
        await db.execute(select(Patient).where(Patient.id == patient_id))
    ).scalar_one_or_none()
    if patient is None:
        raise HTTPException(status_code=404, detail="patient not found")
    enforce_agent_patient_scope(request, patient.id, scope="patient:read")
    if not await can_patient(db, user=user, action=READ_METADATA, patient=patient):
        raise HTTPException(status_code=404, detail="patient not found")

    includes = _parse_includes(body.include)
    canonical_input: dict[str, Any] = {
        "includes": sorted(includes),
        "scope_study_ids": sorted(str(x) for x in study_ids),
        "scope_document_ids": sorted(str(x) for x in document_ids),
        "scope_kind": "bulk",
    }

    try:
        result = await jobs_service.enqueue_or_get(
            db,
            kind=JOB_KIND_FASCICOLO_EXPORT,
            owner_subject_id=user.subject_id,
            canonical_input=canonical_input,
            scope_ids=(patient.id,),
            expires_in_hours=_EXPORT_ARTIFACT_TTL_HOURS,
            is_admin=bool(user.is_admin),
        )
    except jobs_service.JobCapExceededError as e:
        raise cap_exceeded_to_http(e) from e

    if not result.deduped:
        try:
            settings = get_settings()
            redis = await create_pool(redis_settings(settings.redis_url))
            arq_handle = await redis.enqueue_job(
                "export_patient_zip",
                str(result.job.id),
                str(patient.id),
                str(user.subject_id),
                json.dumps(canonical_input),
            )
            await redis.close()
            if arq_handle is not None:
                await jobs_service.set_arq_job_id(db, result.job.id, arq_handle.job_id)
        except Exception as exc:
            await jobs_service.mark_failed(
                db,
                result.job.id,
                error={"code": "enqueue_failed", "message": str(exc)},
            )
            await db.commit()
            raise HTTPException(
                status_code=503,
                detail={
                    "error": "enqueue_failed",
                    "hint": "worker queue unavailable; try again shortly",
                },
            ) from exc

    await db.commit()
    # set_arq_job_id (when fired) issues an UPDATE that triggers
    # ``updated_at``'s ``onupdate=func.now()`` server-side; SQLAlchemy
    # marks that column as expired until the next refresh. Without an
    # explicit refresh the pydantic ``model_validate`` below tries to
    # lazy-load the column from outside a greenlet and crashes with
    # MissingGreenlet, returning a 500 to the client even though the
    # Job was successfully enqueued.
    await db.refresh(result.job)
    return JobOut.model_validate(result.job)
