"""Reusable single-document ingestion (binary/text → Document row).

Extracted from ``api/documents.py:ingest_document`` so the same code
path serves both callers:

* the HTTP/MCP route ``POST /documents/ingest`` (one-shot single-doc
  surface);
* the review-queue promotion hooks (``services/review_queue``), which
  ingest an accepted inbox item into the fascicolo outside any HTTP
  request.

The service owns the invariant-bearing part — S3 upload, the Document
row with its dedup anchors (``content_sha256`` /
``original_blob_hash``), the no-orphan folder placement and the
provenance row — and stays transport-free: identity arrives as a
:class:`ReviewActor`, validation errors raise
:class:`DocumentIngestError` for the caller to map (422 on the route,
check details in a promotion hook). The caller owns the transaction:
nothing here commits.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.config import get_settings
from bvphoenix.db.models import Document, Patient
from bvphoenix.db.models.folders import Folder, FolderItem
from bvphoenix.services.folders import get_or_create_root_folder
from bvphoenix.services.provenance_log import record_provenance_event
from bvphoenix.services.review_queue.actor import ReviewActor
from bvphoenix.storage import get_s3_storage


class DocumentIngestError(ValueError):
    """Caller-mappable validation failure (the route returns 422)."""


async def ingest_document_blob(
    db: AsyncSession,
    *,
    patient: Patient,
    actor: ReviewActor,
    uploaded_by_subject_id: uuid.UUID | None,
    filename: str,
    binary: bytes | None,
    text: str | None,
    content_type: str | None,
    kind_id: str,
    provenance_id: str,
    authority_id: str,
    title: str | None = None,
    document_date: date | None = None,
    folder_id: uuid.UUID | None = None,
    source_kind: str | None = None,
    source_id: uuid.UUID | None = None,
) -> Document:
    """Create one Document (binary and/or inline text) with folder
    placement and provenance; returns the flushed row.

    ``binary`` may be ``None`` for inline-text-only documents (then
    ``text`` must be set — the route validates the either/or before
    calling). ``source_kind``/``source_id`` let promotion hooks point
    the provenance chain back at the staged item the document came
    from.
    """
    if binary is None and not text:
        raise DocumentIngestError("either binary content or text must be provided")

    sha256: str | None = None
    s3_key: str | None = None
    doc_id = uuid.uuid4()

    if binary is not None:
        if not binary:
            raise DocumentIngestError("binary content is empty")
        sha256 = hashlib.sha256(binary).hexdigest()
        ext = filename.rsplit(".", 1)[-1] if "." in filename else "bin"
        s3_key = f"patient-docs/{patient.id}/{doc_id}.{ext}"
        settings = get_settings()
        storage = get_s3_storage()
        await asyncio.to_thread(
            storage.upload_bytes,
            binary,
            bucket=settings.s3_bucket_raw,
            key=s3_key,
        )

    doc = Document(
        id=doc_id,
        patient_id=patient.id,
        uploaded_by_subject_id=uploaded_by_subject_id,
        kind_id=kind_id,
        provenance_id=provenance_id,
        authority_id=authority_id,
        title=title or filename,
        text=text,
        file_s3_key=s3_key,
        file_content_type=content_type,
        document_date=document_date,
        content_sha256=sha256,
        original_blob_hash=sha256,
    )
    db.add(doc)
    await db.flush()

    # No-orphan invariant: every freshly created document is attached
    # to a folder in the same transaction. When the caller passes an
    # explicit ``folder_id`` we use it (after validating it belongs to
    # the same patient); otherwise the document goes to the patient's
    # root, materialised by ``services.folders.get_or_create_root_folder``.
    if folder_id is not None:
        target_folder = (
            await db.execute(
                select(Folder).where(
                    Folder.id == folder_id,
                    Folder.patient_id == patient.id,
                )
            )
        ).scalar_one_or_none()
        if target_folder is None:
            raise DocumentIngestError("folder_id must reference a folder of the same patient")
    else:
        target_folder = await get_or_create_root_folder(db, patient)

    db.add(
        FolderItem(
            folder_id=target_folder.id,
            resource_kind="document",
            resource_id=doc.id,
        )
    )
    await db.flush()

    record_provenance_event(
        db,
        target_kind="document",
        target_id=doc.id,
        activity="create",
        agent_kind=actor.kind,
        agent_subject_id=actor.subject_id,
        agent_token_id=actor.agent_token_id,
        agent_assistant_id=actor.agent_assistant_id,
        source_kind=source_kind,
        source_id=source_id,
        diff={
            "filename": filename,
            "kind_id": kind_id,
            "provenance_id": provenance_id,
            "authority_id": authority_id,
            "size_bytes": len(binary) if binary else 0,
        },
    )
    return doc


__all__ = ["DocumentIngestError", "ingest_document_blob"]
