"""Reconcile clinical-event attachments with curated drive Documents.

The spine of "from an event, point at the curated document in the
Drive". Three moves, all transport-free (the caller owns the
transaction and the commit; identity arrives as a
:class:`~bvphoenix.services.review_queue.actor.ReviewActor`):

* :func:`find_patient_document_by_hash` — the reconciliation lookup:
  does a byte-identical document already live in this patient's drive?
  Matches on ``content_sha256`` *or* ``original_blob_hash`` so an OCR
  re-encoding of the exact same original still reconciles.
* :func:`get_or_create_event_document_link` — idempotent upsert of the
  event↔document link, recording a ``link`` provenance row only on
  first creation. Same-patient is enforced by construction.
* :func:`reconcile_attachment` / :func:`promote_attachment` — wire a
  raw event upload to the drive: auto-link when an identical document
  already exists, otherwise (promote only) materialise one through the
  canonical ingest path and link that.

Cross-patient linking is unrepresentable: the link table carries a
composite FK on ``(patient_id, event_id)`` and every helper refuses a
document whose ``patient_id`` differs from the event's.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from datetime import UTC, datetime

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.config import get_settings
from bvphoenix.db.models import (
    ClinicalEvent,
    ClinicalEventAttachment,
    ClinicalEventDocument,
    Document,
    Patient,
)
from bvphoenix.services.documents.ingest_blob import ingest_document_blob
from bvphoenix.services.provenance_log import record_provenance_event
from bvphoenix.services.review_queue.actor import SYSTEM_ACTOR, ReviewActor
from bvphoenix.storage import get_s3_storage

# Provenance ``source_kind`` stamped on documents materialised out of a
# raw event upload, so the lineage backchains to the attachment.
_SOURCE_KIND = "clinical_event_attachment"

# Catalog defaults for a freshly materialised document. We intentionally
# do NOT guess the clinical ``kind`` or the ``provenance`` (paper scan vs
# digital PDF) — the user curates that in the Drive. These three ids are
# the controlled-vocabulary defaults guaranteed to exist (they are the
# column ``server_default``s on ``documents``).
_DEFAULT_KIND = "unclassified"
_DEFAULT_PROVENANCE = "manual_entry"
_DEFAULT_AUTHORITY = "original"


class EventDocumentLinkError(ValueError):
    """Caller-mappable validation failure (route maps to 4xx)."""


async def _fetch_attachment_bytes(attachment: ClinicalEventAttachment) -> bytes:
    settings = get_settings()
    storage = get_s3_storage()
    return await asyncio.to_thread(
        storage.get_object_bytes,
        bucket=settings.s3_bucket_raw,
        key=attachment.storage_key,
    )


async def _load_patient(db: AsyncSession, patient_id: uuid.UUID) -> Patient:
    patient = (
        await db.execute(select(Patient).where(Patient.id == patient_id))
    ).scalar_one_or_none()
    if patient is None:  # pragma: no cover - FK guarantees presence
        raise EventDocumentLinkError("patient not found")
    return patient


async def find_patient_document_by_hash(
    db: AsyncSession, *, patient_id: uuid.UUID, sha256: str
) -> Document | None:
    """Oldest live document of this patient whose stored bytes (or
    originating artefact) hash to ``sha256``. ``None`` when nothing
    matches. Oldest-first so the canonical original wins over later
    re-uploads."""
    return (
        await db.execute(
            select(Document)
            .where(
                Document.patient_id == patient_id,
                Document.deleted_at.is_(None),
                or_(
                    Document.content_sha256 == sha256,
                    Document.original_blob_hash == sha256,
                ),
            )
            .order_by(Document.created_at.asc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def get_or_create_event_document_link(
    db: AsyncSession,
    *,
    event: ClinicalEvent,
    document: Document,
    actor: ReviewActor,
    source_attachment_id: uuid.UUID | None = None,
    link_role: str = "reference",
) -> tuple[ClinicalEventDocument, bool]:
    """Idempotent upsert of the (event, document) link. Returns
    ``(row, created)``. A pre-existing pure reference is enriched with
    ``source_attachment_id`` when a later reconcile discovers the raw
    upload behind it, but no duplicate provenance row is written."""
    if document.patient_id != event.patient_id:
        raise EventDocumentLinkError("document belongs to a different patient")
    if document.deleted_at is not None:
        raise EventDocumentLinkError("document is deleted")

    existing = (
        await db.execute(
            select(ClinicalEventDocument).where(
                ClinicalEventDocument.event_id == event.id,
                ClinicalEventDocument.document_id == document.id,
                ClinicalEventDocument.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        if source_attachment_id is not None and existing.source_attachment_id is None:
            existing.source_attachment_id = source_attachment_id
            existing.link_role = link_role
        return existing, False

    row = ClinicalEventDocument(
        event_id=event.id,
        patient_id=event.patient_id,
        document_id=document.id,
        source_attachment_id=source_attachment_id,
        link_role=link_role,
        created_by_subject_id=actor.subject_id,
        created_by_kind=actor.kind,
    )
    db.add(row)
    await db.flush()
    record_provenance_event(
        db,
        target_kind="clinical_event",
        target_id=event.id,
        activity="link",
        agent_kind=actor.kind,
        agent_subject_id=actor.subject_id,
        agent_token_id=actor.agent_token_id,
        agent_assistant_id=actor.agent_assistant_id,
        diff={
            "link_id": str(row.id),
            "document_id": str(document.id),
            "source_attachment_id": str(source_attachment_id) if source_attachment_id else None,
            "link_role": link_role,
        },
    )
    return row, True


async def soft_delete_event_document_link(
    db: AsyncSession, *, event: ClinicalEvent, link_id: uuid.UUID, actor: ReviewActor
) -> bool:
    """Soft-delete one event↔document link. The curated document itself
    is untouched (it stays in the Drive). Returns ``False`` when the
    link doesn't exist / is already gone so the route can 404."""
    row = (
        await db.execute(
            select(ClinicalEventDocument).where(
                ClinicalEventDocument.id == link_id,
                ClinicalEventDocument.event_id == event.id,
                ClinicalEventDocument.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return False
    row.deleted_at = datetime.now(UTC)
    record_provenance_event(
        db,
        target_kind="clinical_event",
        target_id=event.id,
        activity="unlink",
        agent_kind=actor.kind,
        agent_subject_id=actor.subject_id,
        agent_token_id=actor.agent_token_id,
        agent_assistant_id=actor.agent_assistant_id,
        diff={"link_id": str(row.id), "document_id": str(row.document_id)},
    )
    return True


async def reconcile_attachment(
    db: AsyncSession,
    *,
    event: ClinicalEvent,
    attachment: ClinicalEventAttachment,
    actor: ReviewActor = SYSTEM_ACTOR,
) -> ClinicalEventDocument | None:
    """Auto-link a raw attachment to an already-curated drive document
    when their bytes are identical. No new document is ever created
    here (that is :func:`promote_attachment`); returns the link or
    ``None`` when there is no match / no hash yet."""
    if not attachment.content_sha256:
        return None
    doc = await find_patient_document_by_hash(
        db, patient_id=attachment.patient_id, sha256=attachment.content_sha256
    )
    if doc is None:
        return None
    link, _ = await get_or_create_event_document_link(
        db,
        event=event,
        document=doc,
        actor=actor,
        source_attachment_id=attachment.id,
        link_role="attachment",
    )
    return link


async def promote_attachment(
    db: AsyncSession,
    *,
    event: ClinicalEvent,
    attachment: ClinicalEventAttachment,
    actor: ReviewActor,
    uploaded_by_subject_id: uuid.UUID | None,
) -> tuple[ClinicalEventDocument, bool]:
    """Move a raw attachment into the Drive. Reconcile-or-ingest:

    * if a byte-identical document already exists for the patient, link
      to it (no second copy) — returns ``(link, reconciled=True)``;
    * otherwise materialise a new Document through the canonical ingest
      pipeline (folder placement + provenance) and link that — returns
      ``(link, reconciled=False)``.

    Backfills ``attachment.content_sha256`` when missing so future
    reconciles are hash-only.
    """
    data: bytes | None = None
    if not attachment.content_sha256:
        data = await _fetch_attachment_bytes(attachment)
        attachment.content_sha256 = hashlib.sha256(data).hexdigest()

    existing = await find_patient_document_by_hash(
        db, patient_id=attachment.patient_id, sha256=attachment.content_sha256
    )
    if existing is not None:
        link, _ = await get_or_create_event_document_link(
            db,
            event=event,
            document=existing,
            actor=actor,
            source_attachment_id=attachment.id,
            link_role="attachment",
        )
        return link, True

    if data is None:
        data = await _fetch_attachment_bytes(attachment)
    patient = await _load_patient(db, attachment.patient_id)
    doc = await ingest_document_blob(
        db,
        patient=patient,
        actor=actor,
        uploaded_by_subject_id=uploaded_by_subject_id,
        filename=attachment.filename,
        binary=data,
        text=None,
        content_type=attachment.mime,
        kind_id=_DEFAULT_KIND,
        provenance_id=_DEFAULT_PROVENANCE,
        authority_id=_DEFAULT_AUTHORITY,
        source_kind=_SOURCE_KIND,
        source_id=attachment.id,
    )
    link, _ = await get_or_create_event_document_link(
        db,
        event=event,
        document=doc,
        actor=actor,
        source_attachment_id=attachment.id,
        link_role="attachment",
    )
    return link, False


__all__ = [
    "EventDocumentLinkError",
    "find_patient_document_by_hash",
    "get_or_create_event_document_link",
    "promote_attachment",
    "reconcile_attachment",
    "soft_delete_event_document_link",
]
