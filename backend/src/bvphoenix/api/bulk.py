"""Bulk operations API.

The Drive-style fascicolo lets the user select many heterogeneous
items (folder + study + document + ...) and apply one action across
the lot. The frontend dispatches a single ``POST /api/bulk/delete``
request with ``{items: [{id, kind}, ...]}`` and gets back a per-item
result so partial failures (one missing study, one permission denied)
don't take the whole batch down.

Currently implemented: bulk delete. Move / share / download have
client-side helpers that already iterate per-kind endpoints; we'll
fold them into this router as they grow legs.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import delete as sql_delete
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.auth import enforce_agent_patient_scope, require_user
from bvphoenix.db.models import (
    Document,
    Folder,
    FolderItem,
    ImagingStudy,
    Patient,
    Series,
    User,
)
from bvphoenix.db.session import get_db
from bvphoenix.middleware.audit_dependency import AuditDep
from bvphoenix.services.permissions import DELETE, can_patient

router = APIRouter(tags=["bulk"])


_BulkKind = Literal["folder", "study", "series", "document", "report", "consultation"]


class BulkItemRef(BaseModel):
    id: uuid.UUID
    kind: _BulkKind


class BulkDeleteIn(BaseModel):
    items: list[BulkItemRef] = Field(min_length=1, max_length=500)


class BulkResult(BaseModel):
    succeeded: list[str]
    failed: list[dict]


async def _patient_for_resource(
    db: AsyncSession, kind: _BulkKind, resource_id: uuid.UUID
) -> Patient | None:
    """Resolve the patient that owns a given resource, so the caller's
    DELETE permission can be checked uniformly across kinds."""
    if kind == "folder":
        # Folders are not strictly patient-scoped — but the bulk
        # delete only operates on folders inside a patient tree, so
        # we look up the first ``FolderItem`` to find the owning
        # patient. Empty folders fall through to the owner check
        # below (Folder.owner_subject_id).
        link = (
            await db.execute(select(FolderItem).where(FolderItem.folder_id == resource_id).limit(1))
        ).scalar_one_or_none()
        if link is None:
            return None
        return await _patient_for_resource(db, link.resource_kind, link.resource_id)  # type: ignore[arg-type]
    if kind == "study":
        return (
            await db.execute(
                select(Patient)
                .join(ImagingStudy, ImagingStudy.patient_id == Patient.id)
                .where(ImagingStudy.id == resource_id)
            )
        ).scalar_one_or_none()
    if kind == "series":
        return (
            await db.execute(
                select(Patient)
                .join(ImagingStudy, ImagingStudy.patient_id == Patient.id)
                .join(Series, Series.study_id == ImagingStudy.id)
                .where(Series.id == resource_id)
            )
        ).scalar_one_or_none()
    if kind == "document":
        return (
            await db.execute(
                select(Patient)
                .join(Document, Document.patient_id == Patient.id)
                .where(Document.id == resource_id)
            )
        ).scalar_one_or_none()
    if kind in ("report", "consultation"):
        # v3: 'report' (Study report) and 'consultation' (BitVision
        # synthesis) are both report_contents now. Resolving the patient
        # crosses the clinical_event parent.
        from bvphoenix.db.models import ClinicalEvent, ReportContent

        return (
            await db.execute(
                select(Patient)
                .join(ClinicalEvent, ClinicalEvent.patient_id == Patient.id)
                .join(ReportContent, ReportContent.clinical_event_id == ClinicalEvent.id)
                .where(ReportContent.id == resource_id)
            )
        ).scalar_one_or_none()
    return None


async def _delete_one(
    db: AsyncSession,
    request: Request,
    user: User,
    item: BulkItemRef,
    audit: AuditDep,
) -> str | None:
    """Delete a single item. Returns ``None`` on success, otherwise a
    human-readable failure reason. Each call is its own commit so a
    later failure doesn't roll back earlier successes — the user
    asked for "delete what you can"."""
    patient = await _patient_for_resource(db, item.kind, item.id)
    if patient is None and item.kind != "folder":
        return "not found"
    if patient is not None:
        # Patient-rooted permission gate. Folders without items still
        # fall back to ``Folder.owner_subject_id`` below.
        enforce_agent_patient_scope(request, patient.id)
        if not await can_patient(db, user=user, action=DELETE, patient=patient):
            return "permission denied"

    if item.kind == "folder":
        folder = (await db.execute(select(Folder).where(Folder.id == item.id))).scalar_one_or_none()
        if folder is None:
            return "not found"
        if folder.owner_subject_id != user.subject_id and not user.is_admin:
            return "permission denied"
        await db.delete(folder)
    elif item.kind == "study":
        row = (
            await db.execute(select(ImagingStudy).where(ImagingStudy.id == item.id))
        ).scalar_one_or_none()
        if row is None:
            return "not found"
        await db.delete(row)
    elif item.kind == "series":
        row = (await db.execute(select(Series).where(Series.id == item.id))).scalar_one_or_none()
        if row is None:
            return "not found"
        await db.delete(row)
    elif item.kind == "document":
        row = (
            await db.execute(select(Document).where(Document.id == item.id))
        ).scalar_one_or_none()
        if row is None:
            return "not found"
        await db.delete(row)
    elif item.kind in ("report", "consultation"):
        # v3: both legacy kinds resolve to a report_content row.
        from bvphoenix.db.models import ReportContent

        row = (
            await db.execute(select(ReportContent).where(ReportContent.id == item.id))
        ).scalar_one_or_none()
        if row is None:
            return "not found"
        await db.delete(row)
    else:
        return f"unsupported kind {item.kind!r}"

    # Drop any FolderItem rows pointing at the deleted resource so
    # the patient-tree views don't keep zombie placements.
    await db.execute(
        sql_delete(FolderItem).where(
            FolderItem.resource_kind == item.kind,
            FolderItem.resource_id == item.id,
        )
    )

    await db.commit()
    await audit.log(
        action=f"bulk_{item.kind}_delete",
        actor_subject_id=user.subject_id,
        resource_kind=item.kind,
        resource_id=item.id,
    )
    return None


class BulkReassignIn(BaseModel):
    items: list[BulkItemRef] = Field(min_length=1, max_length=500)
    target_patient_id: uuid.UUID


_REASSIGNABLE_KINDS: frozenset[str] = frozenset({"study", "document"})


async def _reassign_one(
    db: AsyncSession,
    request: Request,
    user: User,
    item: BulkItemRef,
    target_patient: Patient,
    audit: AuditDep,
) -> str | None:
    """Move a single resource to a different patient. The caller must
    have DELETE on the *source* (they're moving data away from it) and
    DELETE on the *target* too (they're injecting data into it). The
    "delete" gate is the strictest patient permission and approximates
    "owner or admin" without introducing a new permission verb."""
    if item.kind not in _REASSIGNABLE_KINDS:
        return f"kind {item.kind!r} cannot be reassigned across patients"

    source_patient = await _patient_for_resource(db, item.kind, item.id)
    if source_patient is None:
        return "not found"
    if source_patient.id == target_patient.id:
        return "already on the target patient"

    enforce_agent_patient_scope(request, source_patient.id)
    if not await can_patient(db, user=user, action=DELETE, patient=source_patient):
        return "permission denied on source"
    if not await can_patient(db, user=user, action=DELETE, patient=target_patient):
        return "permission denied on target"

    if item.kind == "study":
        row = (
            await db.execute(select(ImagingStudy).where(ImagingStudy.id == item.id))
        ).scalar_one_or_none()
        if row is None:
            return "not found"
        row.patient_id = target_patient.id
    elif item.kind == "document":
        row = (
            await db.execute(select(Document).where(Document.id == item.id))
        ).scalar_one_or_none()
        if row is None:
            return "not found"
        row.patient_id = target_patient.id

    # Cross-patient move detaches the resource from any folder
    # placement on the source side (folders are owned by a single
    # patient tree). The target patient's tree owner can re-file
    # afterwards if they want.
    await db.execute(
        sql_delete(FolderItem).where(
            FolderItem.resource_kind == item.kind,
            FolderItem.resource_id == item.id,
        )
    )

    await db.commit()
    await audit.log(
        action=f"bulk_{item.kind}_reassign",
        actor_subject_id=user.subject_id,
        resource_kind=item.kind,
        resource_id=item.id,
        metadata={
            "from_patient_id": str(source_patient.id),
            "to_patient_id": str(target_patient.id),
        },
    )
    return None


@router.post("/bulk/reassign-patient", response_model=BulkResult)
async def bulk_reassign_patient(
    request: Request,
    body: BulkReassignIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    audit: AuditDep,
) -> BulkResult:
    """Move studies / documents to a different patient. The caller
    must have DELETE permission on both the source patients (one per
    item) and the target patient. This is irreversible from the API
    side — surface a strong confirmation in the UI before calling."""
    target = (
        await db.execute(select(Patient).where(Patient.id == body.target_patient_id))
    ).scalar_one_or_none()
    if target is None:
        raise HTTPException(status_code=404, detail="target patient not found")
    if not await can_patient(db, user=user, action=DELETE, patient=target):
        raise HTTPException(status_code=403, detail="permission denied on target patient")

    succeeded: list[str] = []
    failed: list[dict] = []
    for item in body.items:
        try:
            reason = await _reassign_one(db, request, user, item, target, audit)
        except Exception as exc:
            await db.rollback()
            failed.append({"id": str(item.id), "reason": str(exc)})
            continue
        if reason is None:
            succeeded.append(str(item.id))
        else:
            failed.append({"id": str(item.id), "reason": reason})
    return BulkResult(succeeded=succeeded, failed=failed)


@router.post("/bulk/delete", response_model=BulkResult)
async def bulk_delete(
    request: Request,
    body: BulkDeleteIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    audit: AuditDep,
) -> BulkResult:
    """Delete a heterogeneous set of items. Per-item permission /
    existence checks are independent: a missing study doesn't block
    the deletion of a perfectly-good folder in the same call."""
    succeeded: list[str] = []
    failed: list[dict] = []
    for item in body.items:
        try:
            reason = await _delete_one(db, request, user, item, audit)
        except Exception as exc:
            await db.rollback()
            failed.append({"id": str(item.id), "reason": str(exc)})
            continue
        if reason is None:
            succeeded.append(str(item.id))
        else:
            failed.append({"id": str(item.id), "reason": reason})
    return BulkResult(succeeded=succeeded, failed=failed)
