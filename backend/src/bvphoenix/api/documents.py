"""Document operations — merge / split aliases + download.

The data model recognises that the same clinical content can land in
the system as multiple Documents (a PDF original from the hospital,
a smartphone photo of the same paper, an OCR'd scan of a fotocopia
of that scan). The Manifestation layer keeps each upload as its own
row — that preserves provenance — but the ``original_blob_hash``
column lets us declare "all these rows are copies of the same
underlying artefact" without losing the individual histories.

Endpoints (v3 phase 3g):

  POST /api/documents/merge
    Mark N documents as aliases of one canonical. All N rows get the
    canonical's ``original_blob_hash`` (typically its
    ``content_sha256``); the canonical row is unchanged. Cross-patient
    sets are refused. The merge is idempotent: re-running with the
    same set is a no-op.

  POST /api/documents/{id}/split
    Undo a previous merge by resetting ``original_blob_hash`` back
    to ``content_sha256`` on this document. Other documents that were
    in the same alias group are not affected — split removes only
    the addressed document from the group.

  GET /api/documents/{id}/download
    Proxy-stream the document's underlying blob through the backend
    (storage isolation: bucket / key / S3 endpoint never leak to
    the client). Replaces the legacy
    ``GET /api/patients/{pid}/documents/{did}/files/{fid}`` paths.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.api._http import proxy_s3_object
from bvphoenix.auth.deps import enforce_agent_patient_scope, public_user, require_user
from bvphoenix.config import get_settings
from bvphoenix.db.models import Document, DocumentFile, Patient, User
from bvphoenix.db.session import get_db
from bvphoenix.middleware.audit_dependency import AuditDep
from bvphoenix.services.documents.ingest_blob import (
    DocumentIngestError,
    ingest_document_blob,
)
from bvphoenix.services.download_tokens import resolve_download_user
from bvphoenix.services.permissions import (
    DOWNLOAD_DERIVATIVE,
    READ_METADATA,
    WRITE_REPORT,
    can_patient,
)
from bvphoenix.services.provenance_log import record_provenance
from bvphoenix.services.review_queue.actor import ReviewActor

router = APIRouter(tags=["documents-v3"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class MergeAliasesIn(BaseModel):
    """Mark N documents as aliases of one canonical.

    ``canonical_id`` defaults to the first id in ``document_ids`` when
    omitted. The canonical's ``content_sha256`` becomes the shared
    ``original_blob_hash`` for the whole set."""

    document_ids: list[uuid.UUID] = Field(..., min_length=2, max_length=50)
    canonical_id: uuid.UUID | None = None
    reason: str | None = Field(default=None, max_length=2000)


class MergeAliasesOut(BaseModel):
    canonical_id: str
    original_blob_hash: str
    affected_ids: list[str]


class SplitAliasIn(BaseModel):
    reason: str | None = Field(default=None, max_length=2000)


class IngestDocumentIn(BaseModel):
    """Single-document upload over JSON (base64 binary).

    The bulk ingestion path stays the canonical one for multi-file
    uploads; this endpoint is the agent-friendly variant: a single
    JSON POST with the binary inline, suited to one-shot ingestion
    from an MCP tool. Free-text-only documents (``content_base64``
    omitted) are also accepted; in that case the document carries
    only the inline ``text`` body."""

    patient_id: uuid.UUID
    filename: str = Field(..., min_length=1, max_length=255)
    content_base64: str | None = Field(
        default=None,
        description="Base64-encoded binary; omit for inline-text-only documents",
    )
    content_type: str | None = Field(default=None, max_length=128)
    text: str | None = Field(
        default=None,
        description="Inline text body (clinical note pasted by the agent)",
    )
    kind_id: str = Field(default="unclassified", max_length=64)
    provenance_id: str = Field(default="manual_entry", max_length=64)
    authority_id: str = Field(default="original", max_length=64)
    title: str | None = Field(default=None, max_length=255)
    document_date: str | None = Field(
        default=None,
        description="ISO date YYYY-MM-DD; the date the document refers to (not the ingestion date)",
    )
    folder_id: uuid.UUID | None = Field(
        default=None,
        description=(
            "Optional target folder. When omitted, the document is "
            "attached to the patient's root folder so the no-orphan "
            "invariant holds (every live document has at least one "
            "folder containment row)."
        ),
    )


# ---------------------------------------------------------------------------
# Endpoints — merge / split
# ---------------------------------------------------------------------------


@router.post(
    "/documents/merge",
    response_model=MergeAliasesOut,
)
async def merge_aliases(
    body: MergeAliasesIn,
    request: Request,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    audit: AuditDep,
) -> MergeAliasesOut:
    """Set ``original_blob_hash`` on every document in the set to
    the canonical's ``content_sha256``.

    Validates: all ids must exist, all must belong to the same
    patient, none in soft-deleted state, the canonical must have
    ``content_sha256`` populated (so an inline-text-only document
    cannot be the canonical). The merge is idempotent."""
    canonical_id = body.canonical_id or body.document_ids[0]
    if canonical_id not in body.document_ids:
        raise HTTPException(
            status_code=422,
            detail="canonical_id must be present in document_ids",
        )

    rows = (
        (await db.execute(select(Document).where(Document.id.in_(body.document_ids))))
        .scalars()
        .all()
    )
    if len(rows) != len(set(body.document_ids)):
        missing = set(body.document_ids) - {r.id for r in rows}
        raise HTTPException(
            status_code=404,
            detail=f"documents not found: {sorted(str(m) for m in missing)}",
        )

    patient_ids = {r.patient_id for r in rows}
    if len(patient_ids) > 1:
        raise HTTPException(
            status_code=409,
            detail="cross-patient merge forbidden",
        )
    patient = (
        await db.execute(select(Patient).where(Patient.id == next(iter(patient_ids))))
    ).scalar_one_or_none()
    if patient is None or not await can_patient(
        db, user=user, action=WRITE_REPORT, patient=patient
    ):
        raise HTTPException(status_code=404, detail="patient not visible")
    enforce_agent_patient_scope(request, patient.id)

    canonical = next((r for r in rows if r.id == canonical_id), None)
    if canonical is None or canonical.content_sha256 is None:
        raise HTTPException(
            status_code=409,
            detail="canonical document must have a content_sha256 (no inline-text-only)",
        )

    target_hash = canonical.content_sha256
    affected: list[str] = []
    for r in rows:
        if r.deleted_at is not None:
            raise HTTPException(
                status_code=409,
                detail=f"document {r.id} is soft-deleted; restore before merging",
            )
        if r.original_blob_hash != target_hash:
            r.original_blob_hash = target_hash
            r.etag = uuid.uuid4()
            affected.append(str(r.id))

    if affected:
        record_provenance(
            db,
            target_kind="document",
            target_id=canonical.id,
            activity="merge",
            user=user,
            request=request,
            diff={
                "merged_ids": [str(i) for i in body.document_ids],
                "shared_hash": target_hash,
                "reason": body.reason,
            },
        )
    await db.commit()
    return MergeAliasesOut(
        canonical_id=str(canonical.id),
        original_blob_hash=target_hash,
        affected_ids=affected,
    )


@router.post(
    "/documents/{document_id}/split",
    response_model=dict,
)
async def split_alias(
    document_id: uuid.UUID,
    body: SplitAliasIn,
    request: Request,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    audit: AuditDep,
) -> dict:
    """Reset this document's ``original_blob_hash`` to its own
    ``content_sha256``, removing it from any alias group it was in.
    Other documents in the group are untouched."""
    doc = (
        await db.execute(select(Document).where(Document.id == document_id))
    ).scalar_one_or_none()
    if doc is None:
        raise HTTPException(status_code=404, detail="document not found")
    patient = (
        await db.execute(select(Patient).where(Patient.id == doc.patient_id))
    ).scalar_one_or_none()
    if patient is None or not await can_patient(
        db, user=user, action=WRITE_REPORT, patient=patient
    ):
        raise HTTPException(status_code=404, detail="document not found")
    enforce_agent_patient_scope(request, patient.id)

    previous_hash = doc.original_blob_hash
    doc.original_blob_hash = doc.content_sha256
    doc.etag = uuid.uuid4()

    record_provenance(
        db,
        target_kind="document",
        target_id=doc.id,
        activity="split",
        user=user,
        request=request,
        diff={
            "previous_original_blob_hash": previous_hash,
            "reset_to": doc.content_sha256,
            "reason": body.reason,
        },
    )
    await db.commit()
    return {
        "document_id": str(doc.id),
        "original_blob_hash": doc.content_sha256,
    }


# ---------------------------------------------------------------------------
# Endpoint — generic download (storage isolation)
# ---------------------------------------------------------------------------


@router.get(
    "/documents/{document_id}/download",
)
async def download_document(
    document_id: uuid.UUID,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    audit: AuditDep,
    user_or_none: Annotated[User | None, Depends(public_user)] = None,
    dt: str | None = None,
) -> StreamingResponse:
    """Proxy-stream the document blob through the backend.

    Storage isolation invariant (memoria ``feedback_storage_isolation``):
    the bucket name, S3 endpoint, and key are never exposed to the
    client. The response is a ``StreamingResponse`` with
    ``Content-Disposition: attachment`` so the browser saves the file
    with the original filename when present.

    Auth: standard ``Authorization: Bearer`` header *or* a single-use
    ``?dt=<token>`` query string from ``POST /auth/download-token``.
    The token path lets a plain ``<a href>`` click stream the file
    natively to disk — no fetch+Blob in the browser, no 2 GB cap, no
    RAM pressure on multi-GB DVD ISOs. The persistent JWT never
    appears in URLs.
    """
    user = await resolve_download_user(
        user=user_or_none,
        dt=dt,
        db=db,
        resource_kind="document",
        resource_id=document_id,
    )
    doc = (
        await db.execute(select(Document).where(Document.id == document_id))
    ).scalar_one_or_none()
    if doc is None or doc.deleted_at is not None:
        raise HTTPException(status_code=404, detail="document not found")
    patient = (
        await db.execute(select(Patient).where(Patient.id == doc.patient_id))
    ).scalar_one_or_none()
    if patient is None or (
        not await can_patient(db, user=user, action=DOWNLOAD_DERIVATIVE, patient=patient)
        and not await can_patient(db, user=user, action=READ_METADATA, patient=patient)
    ):
        raise HTTPException(status_code=404, detail="document not found")
    if doc.file_s3_key is None:
        raise HTTPException(
            status_code=404,
            detail="document has no binary attachment (inline text only)",
        )

    settings = get_settings()
    return await proxy_s3_object(
        request=request,
        bucket=settings.s3_bucket_raw,
        key=doc.file_s3_key,
        filename=doc.title or "document",
        fallback_content_type=doc.file_content_type or "application/octet-stream",
    )


# ---------------------------------------------------------------------------
# Endpoint — download a single child file of a multi-file document
# ---------------------------------------------------------------------------


@router.get(
    "/documents/{document_id}/files/{file_id}/download",
)
async def download_document_file(
    document_id: uuid.UUID,
    file_id: uuid.UUID,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    audit: AuditDep,
    user_or_none: Annotated[User | None, Depends(public_user)] = None,
    dt: str | None = None,
) -> StreamingResponse:
    """Proxy-stream a single ``DocumentFile`` child blob.

    Sibling of ``/documents/{id}/download`` for documents that hold
    N attached files (e.g. a paper report scanned into 5 JPEGs, or
    an ISO archive that landed as a child file of a folder bundle).
    Same storage-isolation contract: bucket / key / S3 host never
    appear in the response. Same hybrid auth: Bearer header *or*
    ``?dt=`` token. Differs from the legacy
    ``/patients/.../documents/.../files/.../content`` endpoint by
    setting ``Content-Disposition: attachment`` so the browser
    downloads instead of rendering inline.
    """
    user = await resolve_download_user(
        user=user_or_none,
        dt=dt,
        db=db,
        resource_kind="document_file",
        resource_id=document_id,
        child_id=file_id,
    )
    row = (
        await db.execute(
            select(DocumentFile)
            .join(Document, Document.id == DocumentFile.document_id)
            .where(
                DocumentFile.id == file_id,
                DocumentFile.document_id == document_id,
                Document.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="file not found")
    doc = (
        await db.execute(select(Document).where(Document.id == row.document_id))
    ).scalar_one_or_none()
    if doc is None:
        raise HTTPException(status_code=404, detail="file not found")
    patient = (
        await db.execute(select(Patient).where(Patient.id == doc.patient_id))
    ).scalar_one_or_none()
    if patient is None or (
        not await can_patient(db, user=user, action=DOWNLOAD_DERIVATIVE, patient=patient)
        and not await can_patient(db, user=user, action=READ_METADATA, patient=patient)
    ):
        raise HTTPException(status_code=404, detail="file not found")

    settings = get_settings()
    filename = row.original_filename or f"file-{row.sequence:03d}.bin"
    return await proxy_s3_object(
        request=request,
        bucket=settings.s3_bucket_raw,
        key=row.file_s3_key,
        filename=filename,
        fallback_content_type=row.file_content_type or "application/octet-stream",
    )


# ---------------------------------------------------------------------------
# Endpoint — single-document ingestion (agent-friendly)
# ---------------------------------------------------------------------------


@router.post(
    "/documents/ingest",
    response_model=dict,
    status_code=status.HTTP_201_CREATED,
)
async def ingest_document(
    body: IngestDocumentIn,
    request: Request,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    audit: AuditDep,
) -> dict:
    """Ingest one document into a patient's fascicolo from a JSON
    payload (base64 binary). The bulk async pipeline remains the
    canonical path for multi-file uploads; this endpoint is the
    one-shot single-doc surface that the MCP ``ingest_document`` tool
    targets.

    Either ``content_base64`` or ``text`` must be provided. The
    classifier hints (kind/provenance/authority) default to safe
    catch-all values when the agent does not specify them; the FK
    constraints reject unknown ids.

    Transport layer only: decode/validate inputs, authorize, then
    delegate to ``services.documents.ingest_blob.ingest_document_blob``
    — the same service the review-queue promotion hooks call."""
    import base64
    from datetime import date as _date

    if not body.content_base64 and not body.text:
        raise HTTPException(
            status_code=422,
            detail="either content_base64 or text must be provided",
        )

    patient = (
        await db.execute(select(Patient).where(Patient.id == body.patient_id))
    ).scalar_one_or_none()
    if patient is None or not await can_patient(
        db, user=user, action=WRITE_REPORT, patient=patient
    ):
        raise HTTPException(status_code=404, detail="patient not found")
    enforce_agent_patient_scope(request, patient.id)

    parsed_date: _date | None = None
    if body.document_date:
        try:
            parsed_date = _date.fromisoformat(body.document_date)
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail=f"document_date must be ISO YYYY-MM-DD: {exc}",
            ) from exc

    binary: bytes | None = None
    if body.content_base64:
        try:
            binary = base64.b64decode(body.content_base64, validate=True)
        except Exception as exc:
            raise HTTPException(
                status_code=422,
                detail=f"content_base64 is not valid base64: {exc}",
            ) from exc
        if not binary:
            raise HTTPException(status_code=422, detail="content_base64 decoded empty")

    try:
        doc = await ingest_document_blob(
            db,
            patient=patient,
            actor=ReviewActor.from_request(user, request),
            uploaded_by_subject_id=user.subject_id,
            filename=body.filename,
            binary=binary,
            text=body.text,
            content_type=body.content_type,
            kind_id=body.kind_id,
            provenance_id=body.provenance_id,
            authority_id=body.authority_id,
            title=body.title,
            document_date=parsed_date,
            folder_id=body.folder_id,
        )
    except DocumentIngestError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await db.commit()
    return {
        "document_id": str(doc.id),
        "kind_id": doc.kind_id,
        "provenance_id": doc.provenance_id,
        "authority_id": doc.authority_id,
        "content_sha256": doc.content_sha256,
    }


__all__ = ["router"]
