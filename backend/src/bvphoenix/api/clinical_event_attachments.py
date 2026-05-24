"""Binary attachments on ClinicalEvent — upload / list / download / delete / promote.

Distinct from the v3 Document drive: an attachment is scoped to the
event lifecycle and lives in object storage under
``s3_bucket_raw/clinical_event_attachments/{patient}/{event}/{att}/...``.
When the user decides an attachment is worth keeping beyond the event
(e.g. a referral letter that should also show up on the patient drive),
``POST /promote-to-document`` materialises it as a regular Document
and back-fills ``promoted_to_document_id``.

Endpoints:

- ``POST /api/clinical-events/{event_id}/attachments`` — multipart
  upload, single file per request. Returns the attachment row.
- ``GET /api/clinical-events/{event_id}/attachments`` — list active
  attachments (soft-deleted hidden).
- ``GET /api/clinical-events/{event_id}/attachments/{att_id}/download``
  — 302 to a presigned URL (15 min) so the user's browser pulls bytes
  directly from S3, no proxying through the backend.
- ``DELETE /api/clinical-events/{event_id}/attachments/{att_id}`` —
  soft delete (``deleted_at`` set; storage object stays so a future
  Restore endpoint can rehydrate; cleanup deferred to a separate
  lifecycle job).
- ``POST /api/clinical-events/{event_id}/attachments/{att_id}/promote-to-document``
  — placeholder for the documents-promote flow; for the first round we
  mark the attachment as "promoted" with a synthetic document_id and
  leave the actual Document creation to the next iteration so we ship
  the upload UX without blocking on the full documents pipeline.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Request,
    Response,
    UploadFile,
)
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.auth.deps import require_user
from bvphoenix.config import get_settings
from bvphoenix.db.models import (
    ClinicalEvent,
    ClinicalEventAttachment,
    Patient,
    User,
)
from bvphoenix.db.session import get_db
from bvphoenix.middleware.audit_dependency import AuditDep
from bvphoenix.services.permissions import (
    READ_METADATA,
    WRITE_REPORT,
    can_patient,
)
from bvphoenix.services.provenance_log import record_provenance
from bvphoenix.storage.s3 import get_s3_storage

router = APIRouter(tags=["clinical-event-attachments"])

# Hard cap so a single attachment doesn't blow the bucket. 25 MB is
# enough for a referral letter PDF or a high-res photo; full DICOM
# studies go through the dedicated DICOM ingest path, not here.
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024


class AttachmentOut(BaseModel):
    id: str
    event_id: str
    patient_id: str
    filename: str
    mime: str
    size_bytes: int
    uploaded_by_kind: str
    promoted_to_document_id: str | None
    created_at: str


def _to_out(a: ClinicalEventAttachment) -> AttachmentOut:
    return AttachmentOut(
        id=str(a.id),
        event_id=str(a.event_id),
        patient_id=str(a.patient_id),
        filename=a.filename,
        mime=a.mime,
        size_bytes=a.size_bytes,
        uploaded_by_kind=a.uploaded_by_kind,
        promoted_to_document_id=(
            str(a.promoted_to_document_id) if a.promoted_to_document_id else None
        ),
        created_at=a.created_at.isoformat(),
    )


async def _load_event_for_access(
    db: AsyncSession, *, event_id: uuid.UUID, user: User, action: str
) -> ClinicalEvent:
    ev = (
        await db.execute(select(ClinicalEvent).where(ClinicalEvent.id == event_id))
    ).scalar_one_or_none()
    if ev is None:
        raise HTTPException(status_code=404, detail="clinical event not found")
    patient = (
        await db.execute(select(Patient).where(Patient.id == ev.patient_id))
    ).scalar_one_or_none()
    if patient is None or not await can_patient(db, user=user, action=action, patient=patient):
        raise HTTPException(status_code=404, detail="clinical event not found")
    return ev


def _storage_key(
    patient_id: uuid.UUID, event_id: uuid.UUID, att_id: uuid.UUID, filename: str
) -> str:
    # Sanitise filename for the object key — keep the original name on
    # the row, but the key uses a safe slug so URL-encoding doesn't bite.
    safe = "".join(c if c.isalnum() or c in ".-_" else "_" for c in filename)[:120]
    return f"clinical_event_attachments/{patient_id}/{event_id}/{att_id}/{safe}"


@router.post(
    "/clinical-events/{event_id}/attachments",
    response_model=AttachmentOut,
    status_code=201,
)
async def upload_attachment(
    event_id: uuid.UUID,
    request: Request,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    audit: AuditDep,
    file: Annotated[UploadFile, File()],
) -> AttachmentOut:
    """Multipart upload of a single binary attachment.

    The file is streamed into S3 raw bucket under a key scoped to the
    event. The attachment row is created in the same request so the
    row exists only when the binary lands. Cap: 25 MB.
    """
    del audit
    ev = await _load_event_for_access(db, event_id=event_id, user=user, action=WRITE_REPORT)
    data = await file.read()
    if len(data) > MAX_ATTACHMENT_BYTES:
        raise HTTPException(
            status_code=413,
            detail=(f"attachment too large: {len(data)} bytes > {MAX_ATTACHMENT_BYTES} bytes cap"),
        )
    if not file.filename:
        raise HTTPException(status_code=422, detail="filename required")
    att_id = uuid.uuid4()
    storage_key = _storage_key(ev.patient_id, ev.id, att_id, file.filename)
    settings = get_settings()
    storage = get_s3_storage()
    storage.upload_bytes(data, bucket=settings.s3_bucket_raw, key=storage_key)
    author_kind = "agent" if getattr(request.state, "is_agent", False) else "human"
    att = ClinicalEventAttachment(
        id=att_id,
        event_id=ev.id,
        patient_id=ev.patient_id,
        filename=file.filename,
        mime=file.content_type or "application/octet-stream",
        size_bytes=len(data),
        storage_key=storage_key,
        uploaded_by_subject_id=user.subject_id,
        uploaded_by_kind=author_kind,
    )
    db.add(att)
    await db.flush()
    record_provenance(
        db,
        target_kind="clinical_event",
        target_id=ev.id,
        activity="attachment.upload",
        user=user,
        request=request,
        diff={"attachment_id": str(att_id), "filename": file.filename, "size": len(data)},
    )
    await db.commit()
    await db.refresh(att)
    return _to_out(att)


@router.get(
    "/clinical-events/{event_id}/attachments",
    response_model=list[AttachmentOut],
)
async def list_attachments(
    event_id: uuid.UUID,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    audit: AuditDep,
) -> list[AttachmentOut]:
    """List active (non-deleted) attachments on the event."""
    del audit
    ev = await _load_event_for_access(db, event_id=event_id, user=user, action=READ_METADATA)
    rows = (
        (
            await db.execute(
                select(ClinicalEventAttachment)
                .where(
                    ClinicalEventAttachment.event_id == ev.id,
                    ClinicalEventAttachment.deleted_at.is_(None),
                )
                .order_by(ClinicalEventAttachment.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return [_to_out(a) for a in rows]


@router.get(
    "/clinical-events/{event_id}/attachments/{att_id}/download",
)
async def download_attachment(
    event_id: uuid.UUID,
    att_id: uuid.UUID,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    audit: AuditDep,
) -> Response:
    """Stream the attachment bytes inline. We don't redirect to a
    presigned URL: the dev MinIO endpoint isn't reachable from the
    browser by default, and the bucket-isolation invariant means the
    URL should never leak to the client anyway."""
    del audit
    ev = await _load_event_for_access(db, event_id=event_id, user=user, action=READ_METADATA)
    att = (
        await db.execute(
            select(ClinicalEventAttachment).where(
                ClinicalEventAttachment.id == att_id,
                ClinicalEventAttachment.event_id == ev.id,
                ClinicalEventAttachment.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if att is None:
        raise HTTPException(status_code=404, detail="attachment not found")
    settings = get_settings()
    storage = get_s3_storage()
    data = storage.get_object_bytes(bucket=settings.s3_bucket_raw, key=att.storage_key)
    return Response(
        content=data,
        media_type=att.mime,
        headers={
            "Content-Disposition": f'inline; filename="{att.filename}"',
            "Cache-Control": "private, max-age=60",
        },
    )


@router.delete(
    "/clinical-events/{event_id}/attachments/{att_id}",
    status_code=204,
)
async def delete_attachment(
    event_id: uuid.UUID,
    att_id: uuid.UUID,
    request: Request,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    audit: AuditDep,
) -> Response:
    """Soft delete. The binary stays around so a future Restore can
    bring it back; a separate sweep job is responsible for hard
    deletion (out of scope here)."""
    del audit
    ev = await _load_event_for_access(db, event_id=event_id, user=user, action=WRITE_REPORT)
    att = (
        await db.execute(
            select(ClinicalEventAttachment).where(
                ClinicalEventAttachment.id == att_id,
                ClinicalEventAttachment.event_id == ev.id,
                ClinicalEventAttachment.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if att is None:
        raise HTTPException(status_code=404, detail="attachment not found")
    att.deleted_at = datetime.now(UTC)
    record_provenance(
        db,
        target_kind="clinical_event",
        target_id=ev.id,
        activity="attachment.delete",
        user=user,
        request=request,
        diff={"attachment_id": str(att_id), "filename": att.filename},
    )
    await db.commit()
    return Response(status_code=204)


@router.post(
    "/clinical-events/{event_id}/attachments/{att_id}/promote-to-document",
    response_model=AttachmentOut,
)
async def promote_attachment_to_document(
    event_id: uuid.UUID,
    att_id: uuid.UUID,
    request: Request,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    audit: AuditDep,
) -> AttachmentOut:
    """Move an attachment into the patient Documents drive.

    Implementation note: the v3 Documents ingest pipeline (incl. OCR /
    classifier / authority tagging) lives in the dedicated documents
    services. For the first iteration we record the intent
    (``promoted_to_document_id`` set to a freshly minted UUID) and
    queue the actual Documents materialisation as a follow-up — that
    way the UX ships now and the heavy ingest plumbing comes in a
    dedicated PR without blocking this round.
    """
    del audit
    ev = await _load_event_for_access(db, event_id=event_id, user=user, action=WRITE_REPORT)
    att = (
        await db.execute(
            select(ClinicalEventAttachment).where(
                ClinicalEventAttachment.id == att_id,
                ClinicalEventAttachment.event_id == ev.id,
                ClinicalEventAttachment.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if att is None:
        raise HTTPException(status_code=404, detail="attachment not found")
    if att.promoted_to_document_id is not None:
        # Idempotent: returning the existing promotion target lets
        # the UI render the "already promoted" state without a 409.
        return _to_out(att)
    promoted_id = uuid.uuid4()
    att.promoted_to_document_id = promoted_id
    record_provenance(
        db,
        target_kind="clinical_event",
        target_id=ev.id,
        activity="attachment.promote",
        user=user,
        request=request,
        diff={
            "attachment_id": str(att_id),
            "promoted_to_document_id": str(promoted_id),
        },
    )
    await db.commit()
    await db.refresh(att)
    return _to_out(att)
