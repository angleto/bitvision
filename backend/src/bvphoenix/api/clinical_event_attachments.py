"""Binary attachments on ClinicalEvent — upload / list / download / delete / promote.

Distinct from the v3 Document drive: an attachment is scoped to the
event lifecycle and lives in object storage under
``s3_bucket_raw/clinical_event_attachments/{patient}/{event}/{att}/...``.
When the user decides an attachment is worth keeping beyond the event
(e.g. a referral letter that should also show up on the patient drive),
``POST /promote-to-document`` reconciles it against the Drive — linking
to a byte-identical document when one exists, else materialising a new
one — and records a :class:`ClinicalEventDocument` link.

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
  — reconcile-or-ingest: link the raw upload to a byte-identical drive
  Document when one already exists, otherwise materialise one through
  the canonical ingest pipeline; either way an event↔document link is
  recorded so the event points at the curated drive document.

Sibling surface — references to *already curated* drive documents,
"attach from Drive" without re-uploading bytes:

- ``POST /api/clinical-events/{event_id}/documents`` — link an existing
  patient Document to the event.
- ``GET /api/clinical-events/{event_id}/documents`` — list linked
  curated documents.
- ``DELETE /api/clinical-events/{event_id}/documents/{link_id}`` —
  unlink (soft; the document stays in the Drive).
"""

from __future__ import annotations

import hashlib
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
    ClinicalEventDocument,
    Document,
    Patient,
    User,
)
from bvphoenix.db.session import get_db
from bvphoenix.middleware.audit_dependency import AuditDep
from bvphoenix.services.clinical_event_documents import (
    EventDocumentLinkError,
    get_or_create_event_document_link,
    promote_attachment,
    reconcile_attachment,
    soft_delete_event_document_link,
)
from bvphoenix.services.permissions import (
    READ_METADATA,
    WRITE_REPORT,
    can_patient,
)
from bvphoenix.services.provenance_log import record_provenance
from bvphoenix.services.review_queue.actor import ReviewActor
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
    created_at: str
    # The curated drive Document this raw upload is linked to, if any
    # (set when the bytes were reconciled against an existing document
    # or promoted into a new one). Drives the "Open in Drive" action.
    document_id: str | None = None
    # True when ``document_id`` matched an already-curated document
    # rather than a freshly ingested one. ``None`` when unknown (list).
    document_reconciled: bool | None = None


class EventDocumentOut(BaseModel):
    id: str  # link id
    event_id: str
    patient_id: str
    document_id: str
    document_title: str
    document_kind: str | None
    document_date: str | None
    source_attachment_id: str | None
    link_role: str
    created_by_kind: str
    created_at: str


class EventDocumentLinkIn(BaseModel):
    document_id: uuid.UUID


def _to_out(
    a: ClinicalEventAttachment,
    *,
    document_id: str | None = None,
    document_reconciled: bool | None = None,
) -> AttachmentOut:
    return AttachmentOut(
        id=str(a.id),
        event_id=str(a.event_id),
        patient_id=str(a.patient_id),
        filename=a.filename,
        mime=a.mime,
        size_bytes=a.size_bytes,
        uploaded_by_kind=a.uploaded_by_kind,
        created_at=a.created_at.isoformat(),
        document_id=document_id,
        document_reconciled=document_reconciled,
    )


def _event_doc_out(link: ClinicalEventDocument, doc: Document) -> EventDocumentOut:
    return EventDocumentOut(
        id=str(link.id),
        event_id=str(link.event_id),
        patient_id=str(link.patient_id),
        document_id=str(link.document_id),
        document_title=doc.title,
        document_kind=doc.kind_id,
        document_date=doc.document_date.isoformat() if doc.document_date else None,
        source_attachment_id=str(link.source_attachment_id) if link.source_attachment_id else None,
        link_role=link.link_role,
        created_by_kind=link.created_by_kind,
        created_at=link.created_at.isoformat(),
    )


async def _live_links_by_attachment(
    db: AsyncSession, event_id: uuid.UUID
) -> dict[uuid.UUID, uuid.UUID]:
    """Map ``source_attachment_id -> document_id`` for the event's live
    attachment-derived links, so the list view can show each raw upload
    as "in the Drive" without an N+1."""
    rows = (
        await db.execute(
            select(
                ClinicalEventDocument.source_attachment_id,
                ClinicalEventDocument.document_id,
            )
            .join(Document, Document.id == ClinicalEventDocument.document_id)
            .where(
                ClinicalEventDocument.event_id == event_id,
                ClinicalEventDocument.source_attachment_id.is_not(None),
                ClinicalEventDocument.deleted_at.is_(None),
                Document.deleted_at.is_(None),
            )
        )
    ).all()
    return {att_id: doc_id for att_id, doc_id in rows if att_id is not None}


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
        content_sha256=hashlib.sha256(data).hexdigest(),
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
    # Auto-reconcile: if these exact bytes are already curated in the
    # patient's Drive, link the upload to that document (system-authored)
    # so the user immediately sees "already in the Drive". No new copy.
    link = await reconcile_attachment(db, event=ev, attachment=att)
    await db.commit()
    await db.refresh(att)
    return _to_out(
        att,
        document_id=str(link.document_id) if link else None,
        document_reconciled=True if link else None,
    )


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
    """List active (non-deleted) attachments on the event, each
    carrying ``document_id`` when it has been reconciled/promoted into
    the Drive."""
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
    links = await _live_links_by_attachment(db, ev.id)
    return [_to_out(a, document_id=str(links[a.id]) if a.id in links else None) for a in rows]


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

    Reconcile-or-ingest, idempotent:

    * if a byte-identical document already lives in the patient's Drive
      (``content_sha256`` / ``original_blob_hash`` match), the upload is
      linked to it — no second copy (``document_reconciled=True``);
    * otherwise the bytes are materialised into a new Document through
      the canonical ingest pipeline (folder placement + provenance) and
      linked (``document_reconciled=False``).

    Re-promoting returns the existing link without raising 409.
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
    actor = ReviewActor.from_request(user, request)
    try:
        link, reconciled = await promote_attachment(
            db,
            event=ev,
            attachment=att,
            actor=actor,
            uploaded_by_subject_id=user.subject_id,
        )
    except EventDocumentLinkError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    record_provenance(
        db,
        target_kind="clinical_event",
        target_id=ev.id,
        activity="attachment.promote",
        user=user,
        request=request,
        diff={
            "attachment_id": str(att_id),
            "document_id": str(link.document_id),
            "reconciled": reconciled,
        },
    )
    await db.commit()
    await db.refresh(att)
    return _to_out(att, document_id=str(link.document_id), document_reconciled=reconciled)


# ---------------------------------------------------------------------
# Event ↔ curated drive Document links ("attach from Drive").
# ---------------------------------------------------------------------


@router.post(
    "/clinical-events/{event_id}/documents",
    response_model=EventDocumentOut,
    status_code=201,
)
async def attach_document_from_drive(
    event_id: uuid.UUID,
    body: EventDocumentLinkIn,
    request: Request,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    audit: AuditDep,
) -> EventDocumentOut:
    """Link an already-curated patient Document to the event without
    re-uploading bytes. Idempotent on (event, document): a second call
    returns the existing link. The document must belong to the event's
    patient and be live, else 404 (cross-patient linking is
    unrepresentable)."""
    del audit
    ev = await _load_event_for_access(db, event_id=event_id, user=user, action=WRITE_REPORT)
    doc = (
        await db.execute(
            select(Document).where(
                Document.id == body.document_id,
                Document.patient_id == ev.patient_id,
                Document.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if doc is None:
        raise HTTPException(status_code=404, detail="document not found")
    actor = ReviewActor.from_request(user, request)
    try:
        link, _created = await get_or_create_event_document_link(
            db, event=ev, document=doc, actor=actor, link_role="reference"
        )
    except EventDocumentLinkError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await db.commit()
    await db.refresh(link)
    return _event_doc_out(link, doc)


@router.get(
    "/clinical-events/{event_id}/documents",
    response_model=list[EventDocumentOut],
)
async def list_event_documents(
    event_id: uuid.UUID,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    audit: AuditDep,
) -> list[EventDocumentOut]:
    """List curated drive documents linked to the event (live links to
    live documents), newest first."""
    del audit
    ev = await _load_event_for_access(db, event_id=event_id, user=user, action=READ_METADATA)
    rows = (
        await db.execute(
            select(ClinicalEventDocument, Document)
            .join(Document, Document.id == ClinicalEventDocument.document_id)
            .where(
                ClinicalEventDocument.event_id == ev.id,
                ClinicalEventDocument.deleted_at.is_(None),
                Document.deleted_at.is_(None),
            )
            .order_by(ClinicalEventDocument.created_at.desc())
        )
    ).all()
    return [_event_doc_out(link, doc) for link, doc in rows]


@router.delete(
    "/clinical-events/{event_id}/documents/{link_id}",
    status_code=204,
)
async def unlink_event_document(
    event_id: uuid.UUID,
    link_id: uuid.UUID,
    request: Request,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    audit: AuditDep,
) -> Response:
    """Soft-unlink a curated document from the event. The document
    stays in the Drive; only the event's reference is removed. 404 when
    the link does not exist."""
    del audit
    ev = await _load_event_for_access(db, event_id=event_id, user=user, action=WRITE_REPORT)
    actor = ReviewActor.from_request(user, request)
    ok = await soft_delete_event_document_link(db, event=ev, link_id=link_id, actor=actor)
    if not ok:
        raise HTTPException(status_code=404, detail="link not found")
    await db.commit()
    return Response(status_code=204)
