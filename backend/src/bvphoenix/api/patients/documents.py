# ruff: noqa: F405, B008
# Auto-split from api/patients.py on 2026-05-21.
# Section: ``documents``. Decorators register against the
# local ``router`` below; the package __init__.py
# aggregates every child via include_router so main.py's
# wiring stays a single line.

from __future__ import annotations

from fastapi import APIRouter

from bvphoenix.api.patients import _shared  # for runtime access
from bvphoenix.api.patients._shared import *  # noqa: F403

router = APIRouter()


@router.get("/patients/{patient_id}/documents", response_model=list[PatientDocumentOut])
async def list_documents(
    request: Request,
    patient_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(optional_user)],
    type: str | None = Query(None, alias="type"),
    include_deleted: bool = Query(
        default=False,
        description=(
            "Include soft-deleted documents (Sprint 3, ADR 0006). "
            "Default false: tombstones are filtered out."
        ),
    ),
) -> list[PatientDocumentOut]:
    patient = await _get_patient_or_404(db, patient_id, user, request)
    q = select(Document).where(Document.patient_id == patient.id)
    if not include_deleted:
        q = q.where(Document.deleted_at.is_(None))
    if type:
        q = q.where(Document.kind_id == type)
    rows = (await db.execute(q.order_by(Document.created_at.desc()))).scalars().all()
    if not rows:
        return []
    # Single roundtrip for folder_count + is_in_root_only across the
    # whole patient's document set: GROUP BY on folder_items joined to
    # folders, restricted to this patient's docs. The map keys on
    # document_id; missing keys default to (0, False) which the
    # ``_doc_out`` defaults handle (post-0088 there should be none).
    counts_map: dict[uuid.UUID, tuple[int, bool]] = {}
    counts_rows = (
        await db.execute(
            select(
                FolderItem.resource_id,
                func.count(FolderItem.folder_id).label("folder_count"),
                func.bool_and(Folder.is_root).label("only_root"),
            )
            .join(Folder, Folder.id == FolderItem.folder_id)
            .where(
                FolderItem.resource_kind == "document",
                FolderItem.resource_id.in_([d.id for d in rows]),
            )
            .group_by(FolderItem.resource_id)
        )
    ).all()
    for resource_id, fc, only_root in counts_rows:
        counts_map[resource_id] = (int(fc), bool(only_root))
    out: list[PatientDocumentOut] = []
    for d in rows:
        fc, only_root = counts_map.get(d.id, (0, False))
        out.append(_doc_out(d, folder_count=fc, is_in_root_only=only_root))
    return out


@router.get(
    "/patients/{patient_id}/documents/{doc_id}",
    response_model=PatientDocumentOut,
)
async def get_document(
    request: Request,
    patient_id: uuid.UUID,
    doc_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(optional_user)],
) -> PatientDocumentOut:
    patient = await _get_patient_or_404(db, patient_id, user, request)
    doc = (
        await db.execute(
            select(Document).where(
                Document.id == doc_id,
                Document.patient_id == patient.id,
            )
        )
    ).scalar_one_or_none()
    if doc is None:
        raise HTTPException(status_code=404, detail="document not found")
    files = (
        (
            await db.execute(
                select(DocumentFile)
                .where(DocumentFile.document_id == doc.id)
                .order_by(DocumentFile.sequence.asc())
            )
        )
        .scalars()
        .all()
    )
    fc_row = (
        await db.execute(
            select(
                func.count(FolderItem.folder_id),
                func.bool_and(Folder.is_root),
            )
            .join(Folder, Folder.id == FolderItem.folder_id)
            .where(
                FolderItem.resource_kind == "document",
                FolderItem.resource_id == doc.id,
            )
        )
    ).one()
    folder_count = int(fc_row[0] or 0)
    is_in_root_only = bool(fc_row[1]) if folder_count else False
    return _doc_out(
        doc,
        files=files,
        folder_count=folder_count,
        is_in_root_only=is_in_root_only,
    )


_OCR_INGEST_MIMES = ("application/pdf",)


async def _auto_enqueue_ocr_on_ingest(
    db: AsyncSession,
    *,
    user: User,
    doc: Document,
    files: list[DocumentFile],
    settings: object,
) -> None:
    """Best-effort: auto-OCR an ingested document so its text becomes
    searchable (OCR -> chunk -> MiniLM + BGE-M3).

    Only OCR-able files (PDF / image); the ``run_document_ocr`` worker
    chains ``chunk_and_embed_document`` on success. A failed enqueue must
    NEVER fail the upload, so everything is wrapped and swallowed.
    """
    target = next(
        (
            f
            for f in files
            if (f.file_content_type or "").startswith("image/")
            or (f.file_content_type or "") in _OCR_INGEST_MIMES
        ),
        None,
    )
    if target is None:
        return
    try:
        from arq import create_pool

        from bvphoenix.services.arq_redis import redis_settings
        from bvphoenix.services.jobs import enqueue_or_get, set_arq_job_id

        result = await enqueue_or_get(
            db,
            kind="run_document_ocr",
            owner_subject_id=user.subject_id,
            scope_ids=[str(doc.id)],
            canonical_input={
                "document_id": str(doc.id),
                "file_id": str(target.id),
                "force": False,
                "language": None,
            },
        )
        await db.commit()
        if not result.deduped:
            redis = await create_pool(redis_settings(settings.redis_url))  # type: ignore[attr-defined]
            arq_handle = await redis.enqueue_job("run_document_ocr", str(result.job.id))
            await redis.close()
            if arq_handle is not None:
                await set_arq_job_id(db, result.job.id, arq_handle.job_id)
                await db.commit()
    except Exception:
        import logging

        logging.getLogger(__name__).exception("auto-OCR enqueue failed for document %s", doc.id)


@router.post(
    "/patients/{patient_id}/documents",
    response_model=PatientDocumentOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_document(
    request: Request,
    patient_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    audit: AuditDep,
    title: str = Form(...),
    document_type: str = Form(...),
    text: str = Form(""),
    document_date: date | None = Form(None),
    file: UploadFile | None = File(None),
    files: list[UploadFile] | None = File(None, alias="files[]"),
) -> PatientDocumentOut:
    """Create a patient document with optional inline text + 0..N files.

    Three shapes are supported:

      - text-only (``text`` non-empty, no files): pasted clinical note;
      - single file (``file`` populated): legacy single-attachment
        document — kept for backwards compat with old clients;
      - multi-file (``files[]`` populated): a paper report scanned as
        N JPEGs, a multi-page PDF rendered as N images, etc. — they
        all live under one document row, queried as a gallery.

    The two file slots are mutually exclusive at the API level: if both
    are sent we store ``files[]`` as the canonical collection and the
    legacy ``file`` is appended as the first entry. The frontend
    universal uploader sends only ``files[]``.
    """
    patient = await _get_patient_or_404(db, patient_id, user, request)
    perms = await effective_permissions_on_patient(db, user=user, patient=patient)
    if "write:report" not in perms and DELETE not in perms:
        raise HTTPException(status_code=403, detail="cannot upload documents")

    # Per-subject hard storage cap. Use the request's content-length
    # as an upper-bound estimate (the multipart envelope adds a few %
    # of overhead, which rounds in the user's favour). Without
    # content-length we still block "already over quota" calls and
    # rely on the per-file validate_size below for ad-hoc protection.
    from bvphoenix.services.storage_quota import check_storage_quota

    cl_header = request.headers.get("content-length")
    estimated_bytes = int(cl_header) if cl_header and cl_header.isdigit() else 0
    await check_storage_quota(
        db,
        subject_id=patient.managed_by_subject_id,
        additional_bytes=estimated_bytes,
    )

    incoming_files: list[UploadFile] = []
    if files:
        incoming_files.extend(f for f in files if f and f.filename)
    if file and file.filename:
        incoming_files.insert(0, file)

    settings = get_settings()
    storage = get_s3_storage()

    legacy_key: str | None = None
    legacy_ct: str | None = None
    file_records: list[DocumentFile] = []

    # Patient-document binaries are *raw* ingested artifacts (the PDF /
    # JPEG the clinician uploaded), not derivatives. Earlier code wrote
    # them to ``s3_bucket_derivatives`` while OCR + the binary-streaming
    # endpoints read from ``s3_bucket_raw``: every document was a "ghost"
    # for the OCR path. Canonical bucket is now ``s3_bucket_raw``
    # everywhere. Operators with legacy data in ``derivatives`` must run
    # the one-shot copy migration (see deploy/README) before deleting it.
    if len(incoming_files) == 1:
        # Single file — write to legacy slot. Saves a join on the
        # detail endpoint for the common case (one scan, one PDF).
        f = incoming_files[0]
        validate_mime(f.content_type)
        ext = f.filename.rsplit(".", 1)[-1] if "." in (f.filename or "") else "bin"
        doc_id = uuid.uuid4()
        legacy_key = f"patient-docs/{patient_id}/{doc_id}.{ext}"
        data = await f.read()
        validate_size(len(data))
        storage.upload_bytes(data, bucket=settings.s3_bucket_raw, key=legacy_key)
        legacy_ct = f.content_type
    elif len(incoming_files) > 1:
        # Multi-file — write each to its own row. Per-doc subfolder
        # keeps storage listing usable when debugging.
        doc_id = uuid.uuid4()
        for i, f in enumerate(incoming_files):
            validate_mime(f.content_type)
            ext = f.filename.rsplit(".", 1)[-1] if "." in (f.filename or "") else "bin"
            key = f"patient-docs/{patient_id}/{doc_id}/{i:03d}.{ext}"
            data = await f.read()
            validate_size(len(data))
            storage.upload_bytes(data, bucket=settings.s3_bucket_raw, key=key)
            file_records.append(
                DocumentFile(
                    sequence=i,
                    file_s3_key=key,
                    file_content_type=f.content_type,
                    original_filename=f.filename,
                    size_bytes=len(data),
                )
            )

    # v3: ``document_type`` (legacy enum) is mapped to ``kind_id`` on
    # the catalog; provenance for manual upload is ``manual_entry``
    # for free-text + scanned_paper / digital_native_pdf for files
    # (we cannot tell apart on this code path so we default to native).
    # Authority defaults to ``original``; the dedup pass demotes to
    # ``derived`` later when a similar blob is detected.
    if not (document_type or "").strip():
        document_type = "unclassified"
    provenance_id = "digital_native_pdf" if file_records or legacy_key else "manual_entry"
    doc = Document(
        patient_id=patient.id,
        uploaded_by_subject_id=user.subject_id,
        kind_id=document_type,
        provenance_id=provenance_id,
        authority_id="original",
        title=title,
        text=text or None,
        file_s3_key=legacy_key,
        file_content_type=legacy_ct,
        document_date=document_date,
    )
    db.add(doc)
    await db.flush()
    for fr in file_records:
        fr.document_id = doc.id
        db.add(fr)
    await db.flush()
    await db.refresh(doc)
    await record_versioned_change(
        db,
        patient=patient,
        user=user,
        request=request,
        entity_kind="patient_document",
        entity_id=doc.id,
        payload=_document_versioning_payload(doc, file_records),
        message=f"[document] add {document_type}",
    )
    await db.commit()
    await db.refresh(doc)

    await audit.log(
        action="document_upload",
        actor_subject_id=user.subject_id,
        resource_kind="patient_document",
        resource_id=doc.id,
        metadata={
            "patient_id": str(patient.id),
            "document_type": document_type,
            "n_files": len(incoming_files),
        },
    )
    # Index the document for search: OCR -> chunk -> embed (MiniLM +
    # BGE-M3). Best-effort, never fails the upload.
    await _auto_enqueue_ocr_on_ingest(db, user=user, doc=doc, files=file_records, settings=settings)
    return _doc_out(doc, files=file_records)


@router.patch(
    "/patients/{patient_id}/documents/{doc_id}",
    response_model=PatientDocumentOut,
)
async def update_document(
    request: Request,
    patient_id: uuid.UUID,
    doc_id: uuid.UUID,
    body: PatientDocumentUpdateIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    audit: AuditDep,
    idem: Annotated[IdempotencyContext, Depends(idempotent)],
    dry_run: Annotated[bool, Depends(dry_run_flag)] = False,
) -> PatientDocumentOut:
    """Edit document metadata in place.

    Sprint 2 mutating contract:

    * ``If-Match`` (optional): when supplied, must match the current
      ``etag`` of the document; otherwise 412 ``etag_mismatch``.
    * ``Idempotency-Key`` (optional): replay-safe for 24h. Same key
      with a different body returns 422 ``idempotency_conflict``.
    * ``?dry_run=true``: return the diff without committing or auditing.

    Files (multi-file gallery) are not touched by this endpoint;
    replace them via a new upload if needed.
    """
    if idem.replay is not None:
        return idem.replay  # type: ignore[return-value]

    patient = await _get_patient_or_404(db, patient_id, user, request)
    perms = await effective_permissions_on_patient(db, user=user, patient=patient)
    if "write:report" not in perms and DELETE not in perms:
        raise problem(
            403,
            "forbidden",
            "cannot edit documents",
            extra={"patient_id": str(patient.id)},
        )

    doc = (
        await db.execute(
            select(Document).where(
                Document.id == doc_id,
                Document.patient_id == patient.id,
            )
        )
    ).scalar_one_or_none()
    if doc is None:
        raise problem(404, "not_found", "document not found")

    fields = body.model_dump(exclude_unset=True)
    # Collapse the legacy ``document_type`` alias onto ``kind_id`` so
    # the apply loop has a single field to set. Pre-2026-05-03 the
    # alias was only resolved on the read side (``_doc_out`` returns
    # both); on the write side ``setattr(doc, 'document_type', x)``
    # silently no-op'd because the column was dropped in 0075. Now
    # the write side also collapses, so legacy callers actually see
    # their value persist. ``kind_id`` wins when both are set.
    if "document_type" in fields:
        legacy = fields.pop("document_type")
        if "kind_id" not in fields:
            fields["kind_id"] = legacy
    # v3: kind_id validation has two layers. (1) Pre-validate against
    # the active catalog so the agent / FE gets a structured 422 with
    # the slug ``invalid_kind_id`` when the value is not in
    # ``document_kinds`` (the original 500 we observed when the FE
    # dropdown listed phantom keys like ``imaging_report`` /
    # ``discharge_letter`` that never existed in the seed). (2) Trap
    # the IntegrityError on flush as defense-in-depth for races
    # (catalog row deactivated between read and write).
    if "kind_id" in fields and fields["kind_id"] is not None and not str(fields["kind_id"]).strip():
        raise problem(
            400,
            "invalid_kind_id",
            "kind_id (or its document_type alias) cannot be empty",
        )
    if "kind_id" in fields and fields["kind_id"] is not None:
        catalog = await load_active_catalog_ids(db)
        kind_err = validate_kind_id(str(fields["kind_id"]), catalog)
        if kind_err is not None:
            raise problem(
                422,
                "invalid_kind_id",
                kind_err,
                extra={
                    "field": "kind_id",
                    "rejected_value": fields["kind_id"],
                    "catalog_table": "document_kinds",
                },
            )

    # ETag / If-Match against the per-row ``documents.etag`` UUID — the
    # SAME value the GET responses (``list_patient_documents`` /
    # ``get_document``) advertise in the JSON ``etag`` field, and the
    # SAME granularity ``bulk_update_documents`` checks against. The
    # header is OPTIONAL: when present, a stale value yields 412; when
    # absent, the request goes through (caller is opting out of
    # optimistic concurrency, same semantics as the per-item ``etag``
    # field on the bulk endpoint). RFC 7232 wildcard (``If-Match: *``)
    # is honoured as "any current representation".
    #
    # Pre-2026-05-03 this gate raised 428 on missing header, which
    # made the bulk and single endpoints behave asymmetrically: the
    # agent's session report flagged it as #11 ("incentivises calling
    # bulk_update_documents with array length 1 to avoid the
    # concurrency check"). The fix aligns both paths on optional
    # If-Match with consistent 412 on stale.
    current_etag = str(doc.etag) if doc.etag is not None else None
    presented = parse_if_match(request.headers.get("if-match"))
    if presented is not None and presented != "*" and presented != current_etag:
        raise problem(
            412,
            "etag_mismatch",
            "If-Match does not match the current document etag",
            extra={"current_etag": current_etag},
        )

    diff = _document_diff(doc, fields)

    if dry_run:
        files_dry = (
            (
                await db.execute(
                    select(DocumentFile)
                    .where(DocumentFile.document_id == doc.id)
                    .order_by(DocumentFile.sequence.asc())
                )
            )
            .scalars()
            .all()
        )
        out_payload = _doc_out(doc, files=files_dry).model_dump()
        out_payload["etag"] = current_etag
        out_payload["diff"] = diff
        out_payload["dry_run"] = True
        return idem.capture(out_payload, status_code=200)  # type: ignore[return-value]

    for k, v in fields.items():
        if k == "text" and v == "":
            v = None
        setattr(doc, k, v)
    # Catalog FK violation (kind_id / provenance_id / authority_id not
    # in the seeded ``document_*`` tables) used to bubble up as a raw
    # 500. Roll the transaction back and translate into a structured
    # 422 so the agent / FE can recover. Other IntegrityErrors (unique
    # constraints, NOT NULL on title, ...) re-raise to keep their
    # existing semantics.
    try:
        await db.flush()
    except sqlalchemy_exc.IntegrityError as exc:
        await db.rollback()
        translated = translate_catalog_fk_violation(exc)
        if translated is not None:
            raise translated from exc
        raise
    files = (
        (
            await db.execute(
                select(DocumentFile)
                .where(DocumentFile.document_id == doc.id)
                .order_by(DocumentFile.sequence.asc())
            )
        )
        .scalars()
        .all()
    )
    commit = await record_versioned_change(
        db,
        patient=patient,
        user=user,
        request=request,
        entity_kind="patient_document",
        entity_id=doc.id,
        payload=_document_versioning_payload(doc, files),
        message=f"[document] edit ({', '.join(sorted(fields.keys())) or 'no-op'})",
    )
    await db.commit()
    await db.refresh(doc)

    new_etag = commit.commit_hash.hex()

    await audit.log(
        action="document_update",
        actor_subject_id=user.subject_id,
        resource_kind="patient_document",
        resource_id=doc.id,
        metadata={
            "patient_id": str(patient.id),
            "fields_updated": sorted(fields.keys()),
            "etag": new_etag,
        },
    )
    out = _doc_out(doc, files=files)
    out.etag = new_etag
    return idem.capture(  # type: ignore[return-value]
        out.model_dump(),
        status_code=200,
        extra_headers={"ETag": format_etag(new_etag)},
    )


@router.delete(
    "/patients/{patient_id}/documents/{doc_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_document(
    request: Request,
    patient_id: uuid.UUID,
    doc_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    audit: AuditDep,
    reason: str | None = Query(default=None, max_length=255),
    hard: bool = Query(
        default=False,
        description=(
            "Admin-only: skip the soft-delete tombstone and purge the "
            "row immediately. Otherwise the document is flagged "
            "deleted_at=now() with purge_after = now()+30d."
        ),
    ),
) -> Response:
    """Soft-delete by default (Sprint 3, ADR 0006 + git-like update 2026-05).

    The default flow flips ``deleted_at`` to a tombstone, leaves
    ``purge_after`` NULL (no automatic hard-delete; ``restore_document``
    is always available — see memory ``feedback_documents_no_orphans_git_like``),
    rimuove tutte le folder_items in stessa transazione (il trigger
    deferred verifica al COMMIT che lo stato finale sia coerente:
    deleted_at NOT NULL + zero folder_items), e emette un delete
    commit sul DAG.

    Reference guard: la cancellazione è bloccata 409 se esistono
    reference cliniche attive (``document_study_links`` non-mention,
    ``content_document_links``, ``report_content_citations``). Il
    payload elenca le reference così la UI può portare l'utente a
    rimuoverle prima.

    ``?hard=true`` richiede scope ``documents:purge`` (admin GDPR
    escape-hatch); bypassa il guard reference, lascia le reference
    puntare al tombstone, e procede con cascade FK. NON è il flusso
    utente.
    """
    patient = await _get_patient_or_404(db, patient_id, user, request, action=DELETE)

    if hard and not getattr(user, "is_admin", False):
        raise problem(
            403,
            "forbidden",
            "hard delete is admin-only (scope documents:purge); use the default soft-delete instead",
        )

    doc = (
        await db.execute(
            select(Document).where(
                Document.id == doc_id,
                Document.patient_id == patient.id,
            )
        )
    ).scalar_one_or_none()
    if doc is None:
        raise problem(404, "not_found", "document not found")

    if not hard:
        # Reference guard: rifiuta il soft-delete finché esistono
        # reference cliniche attive. La UI mostra ``BlockingReferencesModal``
        # con i link per andare a rimuoverle.
        from bvphoenix.services.documents.references import collect_blocking_references

        blocking = await collect_blocking_references(db, doc.id)
        if blocking:
            raise problem(
                409,
                "document_has_active_references",
                "cancellazione bloccata: rimuovi prima le reference attive",
                extra={"blocking_references": blocking},
            )

    if doc.deleted_at is not None and not hard:
        # Already a tombstone — return 204 idempotently.
        await audit.log(
            action="document_delete_noop",
            actor_subject_id=user.subject_id,
            resource_kind="patient_document",
            resource_id=doc.id,
            metadata={"patient_id": str(patient.id)},
        )
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    deleted_id = doc.id
    if hard:
        await db.delete(doc)
        await db.flush()
        await record_versioned_change(
            db,
            patient=patient,
            user=user,
            request=request,
            entity_kind="patient_document",
            entity_id=deleted_id,
            payload=None,
            message="[document] hard-delete",
        )
        action = "document_hard_delete"
    else:
        now = datetime.now(UTC)
        doc.deleted_at = now
        doc.purge_after = now + timedelta(days=_DEFAULT_PURGE_AFTER_DAYS)
        doc.delete_reason = reason
        await db.flush()
        files = (
            (
                await db.execute(
                    select(DocumentFile)
                    .where(DocumentFile.document_id == doc.id)
                    .order_by(DocumentFile.sequence.asc())
                )
            )
            .scalars()
            .all()
        )
        # The versioning payload still records the metadata snapshot;
        # the agent / UI can detect the tombstone via ``deleted_at``.
        payload = _document_versioning_payload(doc, files)
        payload["deleted_at"] = doc.deleted_at.isoformat()
        payload["purge_after"] = doc.purge_after.isoformat()
        if reason:
            payload["delete_reason"] = reason
        await record_versioned_change(
            db,
            patient=patient,
            user=user,
            request=request,
            entity_kind="patient_document",
            entity_id=deleted_id,
            payload=payload,
            message=(f"[document] soft-delete (purge_after={doc.purge_after.isoformat()})"),
        )
        action = "document_soft_delete"

    await db.commit()
    await audit.log(
        action=action,
        actor_subject_id=user.subject_id,
        resource_kind="patient_document",
        resource_id=deleted_id,
        metadata={
            "patient_id": str(patient.id),
            "reason": reason,
        },
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/patients/{patient_id}/documents/{primary_id}/merge",
    response_model=DocumentMergeOut,
    status_code=status.HTTP_200_OK,
)
async def merge_patient_documents(
    request: Request,
    patient_id: uuid.UUID,
    primary_id: uuid.UUID,
    body: DocumentMergeIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    audit: AuditDep,
    idem: Annotated[IdempotencyContext, Depends(idempotent)],
) -> DocumentMergeOut:
    """Merge ``duplicate_ids`` into ``primary_id`` (Sprint 3, ADR 0017).

    File ownership transfers from each duplicate to the primary; the
    duplicates are soft-deleted with a default 30-day retention window.
    """
    if idem.replay is not None:
        return idem.replay  # type: ignore[return-value]

    from bvphoenix.services.document_merge import (
        DocumentMergeError,
        merge_documents,
    )

    patient = await _get_patient_or_404(db, patient_id, user, request)
    perms = await effective_permissions_on_patient(db, user=user, patient=patient)
    if "write:report" not in perms and DELETE not in perms:
        raise problem(403, "forbidden", "cannot merge documents")

    primary = (
        await db.execute(
            select(Document).where(
                Document.id == primary_id,
                Document.patient_id == patient.id,
            )
        )
    ).scalar_one_or_none()
    if primary is None:
        raise problem(404, "not_found", "primary document not found")

    try:
        merge_result = await merge_documents(
            db,
            primary=primary,
            duplicate_ids=body.duplicate_ids,
            preserve_files_as_attachments=body.preserve_files_as_attachments,
            reason=body.reason,
            actor_subject_id=user.subject_id,
        )
    except DocumentMergeError as exc:
        raise problem(422, "merge_failed", str(exc)) from exc

    # Refresh the primary's file list and emit a single commit on its chain.
    files = (
        (
            await db.execute(
                select(DocumentFile)
                .where(DocumentFile.document_id == primary.id)
                .order_by(DocumentFile.sequence.asc())
            )
        )
        .scalars()
        .all()
    )
    payload = _document_versioning_payload(primary, files)
    payload["merge"] = merge_result.to_jsonable()
    commit = await record_versioned_change(
        db,
        patient=patient,
        user=user,
        request=request,
        entity_kind="patient_document",
        entity_id=primary.id,
        payload=payload,
        message=(f"[document] merge ({len(body.duplicate_ids)} dup -> {primary.id})"),
    )
    await db.commit()
    new_etag = commit.commit_hash.hex()

    await audit.log(
        action="document_merge",
        actor_subject_id=user.subject_id,
        resource_kind="patient_document",
        resource_id=primary.id,
        metadata={
            "patient_id": str(patient.id),
            "merge": merge_result.to_jsonable(),
            "etag": new_etag,
        },
    )

    return idem.capture(  # type: ignore[return-value]
        DocumentMergeOut(
            primary_id=str(primary.id),
            duplicate_ids=[str(d) for d in body.duplicate_ids],
            files_transferred=[
                {
                    "file_id": str(t.file_id),
                    "from_document_id": str(t.from_document_id),
                    "to_document_id": str(t.to_document_id),
                }
                for t in merge_result.files_transferred
            ],
            files_orphaned=[str(f) for f in merge_result.files_orphaned],
            etag=new_etag,
        ).model_dump(),
        status_code=200,
        extra_headers={"ETag": format_etag(new_etag)},
    )


@router.get(
    "/patients/{patient_id}/documents/{doc_id}/versions",
    response_model=DocumentVersionsOut,
)
async def list_document_versions(
    request: Request,
    patient_id: uuid.UUID,
    doc_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    limit: int = Query(default=50, ge=1, le=500),
) -> DocumentVersionsOut:
    """Return every commit on the patient DAG that touched this document.

    Sprint 3 (ADR 0001): the document version chain is reconstructed
    by joining ``manifest_entries`` to ``commits`` filtered by
    ``entity_kind='patient_document'`` AND ``entity_id=doc_id`` for
    the patient. ``is_delete`` is true on commits where the manifest
    entry was a tombstone (no entity_object recorded).
    """
    from sqlalchemy import text as _text

    patient = await _get_patient_or_404(db, patient_id, user, request)
    # Confirm the document belongs to the patient (live or tombstoned).
    exists = (
        await db.execute(
            select(Document.id).where(
                Document.id == doc_id,
                Document.patient_id == patient.id,
            )
        )
    ).first()
    if exists is None:
        raise problem(404, "not_found", "document not found")

    rows = (
        await db.execute(
            _text(
                """
                SELECT c.commit_hash, c.parent_hashes,
                       c.author_subject_id, c.author_kind,
                       c.model_id, c.provider,
                       c.agent_token_id, c.branch_at_creation,
                       c.message, c.created_at,
                       s.display_name AS author_display_name
                FROM commits c
                JOIN manifest_entries m
                  ON m.commit_hash = c.commit_hash
                LEFT JOIN subjects s
                  ON s.id = c.author_subject_id
                WHERE c.patient_id = :p
                  AND m.entity_kind = 'patient_document'
                  AND m.entity_id = :d
                ORDER BY c.created_at DESC
                LIMIT :lim
                """
            ),
            {"p": patient.id, "d": doc_id, "lim": limit},
        )
    ).all()

    head_etag = await etag_for_branch(db, patient_id=patient.id, ref_name="main")

    versions: list[DocumentVersionOut] = []
    for r in rows:
        msg = (r[8] or "").lower()
        is_delete = msg.startswith("[document] hard-delete") or msg.startswith(
            "[document] soft-delete"
        )
        versions.append(
            DocumentVersionOut(
                commit_hash=r[0].hex(),
                parent_hashes=[p.hex() for p in (r[1] or [])],
                author_subject_id=str(r[2]) if r[2] else None,
                author_kind=r[3],
                author_display_name=r[10],
                model_id=r[4],
                provider=r[5],
                agent_token_id=str(r[6]) if r[6] else None,
                branch_at_creation=r[7],
                message=r[8],
                created_at=r[9].isoformat(),
                is_delete=is_delete,
            )
        )

    return DocumentVersionsOut(
        document_id=str(doc_id),
        head_etag=head_etag,
        versions=versions,
    )


@router.post(
    "/patients/{patient_id}/documents/{doc_id}/restore",
    response_model=PatientDocumentOut,
)
async def restore_document(
    request: Request,
    patient_id: uuid.UUID,
    doc_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    audit: AuditDep,
) -> PatientDocumentOut:
    """Undo a soft-delete (Sprint 3, ADR 0006).

    Clears ``deleted_at``/``purge_after``/``delete_reason`` and emits a
    restore commit on the patient DAG. 404 if the document is already
    live. Document-ImagingStudy links survive the soft-delete, so no relink
    work is needed.
    """
    patient = await _get_patient_or_404(db, patient_id, user, request)
    perms = await effective_permissions_on_patient(db, user=user, patient=patient)
    if "write:report" not in perms and DELETE not in perms:
        raise problem(403, "forbidden", "cannot restore documents")

    doc = (
        await db.execute(
            select(Document).where(
                Document.id == doc_id,
                Document.patient_id == patient.id,
            )
        )
    ).scalar_one_or_none()
    if doc is None:
        raise problem(404, "not_found", "document not found")
    if doc.deleted_at is None:
        raise problem(409, "conflict", "document is already live")

    doc.deleted_at = None
    doc.purge_after = None
    doc.delete_reason = None
    # Riattacco-alla-root in stessa transazione: il trigger
    # ``trg_documents_restore_no_orphan`` (DEFERRABLE INITIALLY DEFERRED)
    # verifica al COMMIT che il documento abbia almeno una folder_items
    # row. Se le folder_items erano state perse (smart-delete via card
    # path), il documento riemerge nella root del paziente. Se erano
    # state preservate (delete via document detail page), il documento
    # torna esattamente nelle folder originarie e il riattacco è un
    # no-op (ON CONFLICT DO NOTHING).
    folder_count = (
        await db.execute(
            select(func.count(FolderItem.folder_id)).where(
                FolderItem.resource_kind == "document",
                FolderItem.resource_id == doc.id,
            )
        )
    ).scalar_one()
    if folder_count == 0:
        from bvphoenix.services.folders import get_or_create_root_folder

        root = await get_or_create_root_folder(db, patient)
        db.add(
            FolderItem(
                folder_id=root.id,
                resource_kind="document",
                resource_id=doc.id,
            )
        )
    await db.flush()
    files = (
        (
            await db.execute(
                select(DocumentFile)
                .where(DocumentFile.document_id == doc.id)
                .order_by(DocumentFile.sequence.asc())
            )
        )
        .scalars()
        .all()
    )
    commit = await record_versioned_change(
        db,
        patient=patient,
        user=user,
        request=request,
        entity_kind="patient_document",
        entity_id=doc.id,
        payload=_document_versioning_payload(doc, files),
        message="[document] restore",
    )
    await db.commit()
    await db.refresh(doc)

    new_etag = commit.commit_hash.hex()
    await audit.log(
        action="document_restore",
        actor_subject_id=user.subject_id,
        resource_kind="patient_document",
        resource_id=doc.id,
        metadata={"patient_id": str(patient.id), "etag": new_etag},
    )
    fc_row = (
        await db.execute(
            select(
                func.count(FolderItem.folder_id),
                func.bool_and(Folder.is_root),
            )
            .join(Folder, Folder.id == FolderItem.folder_id)
            .where(
                FolderItem.resource_kind == "document",
                FolderItem.resource_id == doc.id,
            )
        )
    ).one()
    folder_count = int(fc_row[0] or 0)
    is_in_root_only = bool(fc_row[1]) if folder_count else False
    out = _doc_out(
        doc,
        files=files,
        folder_count=folder_count,
        is_in_root_only=is_in_root_only,
    )
    out.etag = new_etag
    return out


@router.post(
    "/patients/{patient_id}/documents/bulk_update",
    response_model=BulkDocumentUpdateOut,
)
async def bulk_update_documents(
    request: Request,
    patient_id: uuid.UUID,
    body: BulkDocumentUpdateIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    audit: AuditDep,
    idem: Annotated[IdempotencyContext, Depends(idempotent)],
    dry_run: Annotated[bool, Depends(dry_run_flag)] = False,
) -> BulkDocumentUpdateOut:
    """Apply N metadata edits to N patient documents.

    Caps and routing:

    * Hard cap of 100 items per call (request body). 422 ``too_many_items``
      otherwise.
    * When ``atomic=False`` and the manifest carries more than 50 items,
      the request is enqueued as an Arq job (best-effort async path,
      ADR 0003 + ADR 0014). The response carries ``job_id`` and HTTP 202.
      Synchronous applies return 200 with the per-item array.
    * Atomic mode always runs synchronously: by definition all-or-nothing
      requires a single transaction.
    * ``dry_run=true`` always runs synchronously regardless of size: the
      pipeline is read-only, so the cost is bounded.
    """
    if idem.replay is not None:
        return idem.replay  # type: ignore[return-value]

    if len(body.items) == 0:
        raise problem(
            422,
            "bulk_empty",
            "items list must contain at least one entry",
        )
    if len(body.items) > _BULK_UPDATE_HARD_CAP:
        raise problem(
            422,
            "too_many_items",
            f"bulk_update accepts at most {_BULK_UPDATE_HARD_CAP} items",
            extra={"received": len(body.items), "cap": _BULK_UPDATE_HARD_CAP},
        )

    patient = await _get_patient_or_404(db, patient_id, user, request)
    perms = await effective_permissions_on_patient(db, user=user, patient=patient)
    if "write:report" not in perms and DELETE not in perms:
        raise problem(403, "forbidden", "cannot edit documents")

    # Async route: large best-effort manifests are offloaded to a worker.
    if not dry_run and not body.atomic and len(body.items) > _BULK_UPDATE_ASYNC_THRESHOLD:
        from arq import create_pool

        from bvphoenix.services.arq_redis import redis_settings
        from bvphoenix.services.jobs import (
            JobCapExceededError,
            enqueue_or_get,
            mark_failed,
            set_arq_job_id,
        )

        canonical_input = {
            "patient_id": str(patient.id),
            "items": [item.model_dump(mode="json") for item in body.items],
            "atomic": False,
        }
        try:
            result = await enqueue_or_get(
                db,
                kind="bulk_document_update",
                owner_subject_id=user.subject_id,
                canonical_input=canonical_input,
                scope_ids=[str(patient.id)],
            )
        except JobCapExceededError as exc:
            raise problem(
                429,
                "rate_limited",
                str(exc),
                extra={"retry_after_seconds": exc.retry_after_seconds},
            ) from exc

        await db.commit()

        if not result.deduped:
            settings = get_settings()
            try:
                redis = await create_pool(redis_settings(settings.redis_url))
                arq_handle = await redis.enqueue_job("bulk_document_update", str(result.job.id))
                await redis.close()
                if arq_handle is not None:
                    await set_arq_job_id(db, result.job.id, arq_handle.job_id)
                    await db.commit()
            except Exception as exc:
                await mark_failed(
                    db,
                    result.job.id,
                    error={"code": "enqueue_failed", "message": str(exc)},
                )
                await db.commit()
                raise problem(
                    503,
                    "service_unavailable",
                    "failed to enqueue bulk update worker job",
                ) from exc

        await audit.log(
            action="bulk_document_update_enqueued",
            actor_subject_id=user.subject_id,
            resource_kind="patient",
            resource_id=patient.id,
            metadata={
                "job_id": str(result.job.id),
                "n_items": len(body.items),
                "deduped": result.deduped,
            },
        )

        return idem.capture(  # type: ignore[return-value]
            BulkDocumentUpdateOut(
                items=[],
                n_ok=0,
                n_error=0,
                n_dry_run=0,
                head_etag=None,
                job_id=str(result.job.id),
            ).model_dump(),
            status_code=202,
            extra_headers={"X-Job-Id": str(result.job.id)},
        )

    from bvphoenix.services.document_bulk_update import (
        BulkUpdateItem,
        apply_bulk_update,
    )

    items_svc: list[BulkUpdateItem] = []
    for raw in body.items:
        provided = raw.model_fields_set
        items_svc.append(
            BulkUpdateItem(
                document_id=raw.document_id,
                title=raw.title,
                document_type=raw.document_type,
                # ``kind_id`` is the canonical 3-axis FK on the
                # documents table; ``document_type`` is its legacy
                # single-axis alias kept for back-compat. The service
                # collapses both onto ``kind_id`` before apply.
                kind_id=raw.kind_id,
                document_date=raw.document_date,
                text=raw.text,
                etag=raw.etag,
                fields_set=frozenset(
                    f
                    for f in (
                        "title",
                        "document_type",
                        "kind_id",
                        "document_date",
                        "text",
                    )
                    if f in provided
                ),
            )
        )

    result = await apply_bulk_update(
        db,
        patient=patient,
        user=user,
        request=request,
        items=items_svc,
        atomic=body.atomic,
        dry_run=dry_run,
    )

    if body.atomic and result.n_error > 0 and not dry_run:
        await db.rollback()
        # Re-emit the per-item outcomes with a 422 wrapper so the
        # caller still sees which item failed.
        raise problem(
            422,
            "bulk_failed",
            "atomic bulk update failed; no item committed",
            extra={
                "items": [
                    {
                        "document_id": str(i.document_id),
                        "status": i.status,
                        "error": i.error,
                    }
                    for i in result.items
                ]
            },
        )

    if not dry_run:
        await db.commit()

    out = BulkDocumentUpdateOut(
        items=[
            BulkDocumentUpdateItemOut(
                document_id=str(i.document_id),
                status=i.status,
                diff=i.diff,
                etag=i.etag,
                error=i.error,
            )
            for i in result.items
        ],
        n_ok=result.n_ok,
        n_error=result.n_error,
        n_dry_run=result.n_dry_run,
        head_etag=result.head_etag,
    )

    if not dry_run:
        await audit.log(
            action="bulk_document_update",
            actor_subject_id=user.subject_id,
            resource_kind="patient",
            resource_id=patient.id,
            metadata={
                "n_items": len(body.items),
                "n_ok": result.n_ok,
                "n_error": result.n_error,
                "atomic": body.atomic,
            },
        )

    headers: dict[str, str] = {}
    if result.head_etag:
        headers["ETag"] = format_etag(result.head_etag)

    return idem.capture(  # type: ignore[return-value]
        out.model_dump(),
        status_code=200,
        extra_headers=headers,
    )


@router.post(
    "/patients/{patient_id}/documents/{doc_id}/links",
    response_model=DocumentStudyLinkOut,
    status_code=status.HTTP_201_CREATED,
)
async def link_document_to_study(
    request: Request,
    patient_id: uuid.UUID,
    doc_id: uuid.UUID,
    body: DocumentStudyLinkIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    audit: AuditDep,
) -> DocumentStudyLinkOut:
    """Associate a document with an imaging study.

    Idempotent on ``(document_id, study_id, link_kind)``: re-posting
    the same triple returns the existing row. Both endpoints (study
    + document) must live under the same patient — cross-patient
    links are rejected with 422 ``cross_patient_link_forbidden``.
    """
    body.link_kind = coerce_link_kind(body.link_kind)
    if body.link_kind not in _DOCUMENT_STUDY_LINK_KINDS:
        raise problem(
            422,
            "invalid_link_kind",
            f"link_kind must be one of {sorted(_DOCUMENT_STUDY_LINK_KINDS)!r}",
        )
    patient = await _get_patient_or_404(db, patient_id, user, request)
    perms = await effective_permissions_on_patient(db, user=user, patient=patient)
    if "write:report" not in perms and DELETE not in perms:
        raise problem(403, "forbidden", "cannot link documents on this patient")

    doc = (
        await db.execute(
            select(Document).where(Document.id == doc_id, Document.patient_id == patient.id)
        )
    ).scalar_one_or_none()
    if doc is None:
        raise problem(404, "not_found", "document not found")
    study = (
        await db.execute(select(ImagingStudy).where(ImagingStudy.id == body.study_id))
    ).scalar_one_or_none()
    if study is None:
        raise problem(404, "not_found", "study not found")
    if study.patient_id != patient.id:
        # Cross-patient links are a PHI-leak risk and the MCP gate
        # already enforces the invariant elsewhere; we mirror it here
        # so a direct REST caller cannot bypass it.
        raise problem(
            422,
            "cross_patient_link_forbidden",
            "study and document belong to different patients",
        )

    existing = (
        await db.execute(
            select(DocumentStudyLink).where(
                DocumentStudyLink.document_id == doc.id,
                DocumentStudyLink.study_id == study.id,
                DocumentStudyLink.link_kind == body.link_kind,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return DocumentStudyLinkOut(
            id=str(existing.id),
            document_id=str(existing.document_id),
            study_id=str(existing.study_id),
            link_kind=existing.link_kind,
            created_at=existing.created_at.isoformat(),
        )

    link = DocumentStudyLink(
        document_id=doc.id,
        study_id=study.id,
        link_kind=body.link_kind,
        created_by_subject_id=user.subject_id,
    )
    db.add(link)
    try:
        await db.commit()
    except sqlalchemy_exc.IntegrityError as exc:
        await db.rollback()
        # Partial unique index ``uq_document_study_links_primary_per_study``
        # fires when the caller tries to attach a second ``primary_report``
        # to the same study. Surface a structured 409 with the existing
        # primary so the UI can route the user there to remove or
        # supersede it.
        if body.link_kind == "primary_report":
            existing_primary = (
                await db.execute(
                    select(DocumentStudyLink).where(
                        DocumentStudyLink.study_id == study.id,
                        DocumentStudyLink.link_kind == "primary_report",
                    )
                )
            ).scalar_one_or_none()
            raise problem(
                409,
                "primary_report_already_set",
                "study already has a primary_report; remove it first or use addendum",
                extra={
                    "existing_document_id": str(existing_primary.document_id)
                    if existing_primary
                    else None,
                },
            ) from exc
        raise
    await db.refresh(link)
    await audit.log(
        action="document_link_added",
        actor_subject_id=user.subject_id,
        resource_kind="patient_document",
        resource_id=doc.id,
        metadata={
            "patient_id": str(patient.id),
            "study_id": str(study.id),
            "link_kind": body.link_kind,
        },
    )
    return DocumentStudyLinkOut(
        id=str(link.id),
        document_id=str(link.document_id),
        study_id=str(link.study_id),
        link_kind=link.link_kind,
        created_at=link.created_at.isoformat(),
    )


@router.get(
    "/patients/{patient_id}/studies/{study_id}/document-links",
    response_model=list[StudyDocumentLinkOut],
)
async def list_study_document_links(
    request: Request,
    patient_id: uuid.UUID,
    study_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(optional_user)],
) -> list[StudyDocumentLinkOut]:
    """Forward inventory: every document attached to ``study_id`` via
    ``DocumentStudyLink``. Drives the "Documenti collegati" panel on
    the study detail page. Cross-patient is impossible by construction:
    both the study and the document must belong to ``patient_id``;
    rows that do not match are filtered out (an inserted-then-mutated
    cross-patient row would also be filtered, but the composite
    invariant on ``link_document_to_study`` already rejects writes).
    """
    patient = await _get_patient_or_404(db, patient_id, user, request)
    study = (
        await db.execute(
            select(ImagingStudy).where(
                ImagingStudy.id == study_id,
                ImagingStudy.patient_id == patient.id,
            )
        )
    ).scalar_one_or_none()
    if study is None:
        raise problem(404, "not_found", "study not found")

    rows = (
        await db.execute(
            select(DocumentStudyLink, Document)
            .join(Document, Document.id == DocumentStudyLink.document_id)
            .where(
                DocumentStudyLink.study_id == study.id,
                Document.patient_id == patient.id,
            )
            .order_by(DocumentStudyLink.created_at.asc())
        )
    ).all()

    out: list[StudyDocumentLinkOut] = []
    for link, doc in rows:
        text_preview: str | None = None
        if doc.text:
            stripped = doc.text.strip()
            text_preview = stripped[:200] + ("…" if len(stripped) > 200 else "")
        out.append(
            StudyDocumentLinkOut(
                document_id=str(doc.id),
                document_title=doc.title or "(senza titolo)",
                document_kind=doc.kind_id or doc.document_type or "document",
                document_date=doc.document_date.isoformat() if doc.document_date else None,
                document_text_preview=text_preview,
                has_attachment=bool(doc.file_s3_key),
                link_kind=link.link_kind,
                created_at=link.created_at.isoformat(),
                created_by_subject_id=str(link.created_by_subject_id)
                if link.created_by_subject_id
                else None,
            )
        )
    return out


@router.delete(
    "/patients/{patient_id}/documents/{doc_id}/links/{study_id}/{link_kind}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def unlink_document_from_study(
    request: Request,
    patient_id: uuid.UUID,
    doc_id: uuid.UUID,
    study_id: uuid.UUID,
    link_kind: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    audit: AuditDep,
) -> None:
    """Drop the (document, study, link_kind) row. Idempotent: a
    non-existent triple returns 204 without raising."""
    patient = await _get_patient_or_404(db, patient_id, user, request)
    perms = await effective_permissions_on_patient(db, user=user, patient=patient)
    if "write:report" not in perms and DELETE not in perms:
        raise problem(403, "forbidden", "cannot unlink documents on this patient")
    # Translate legacy ``report_of`` to ``primary_report`` so URLs
    # generated by older clients keep working through the deprecation
    # window.
    link_kind = coerce_link_kind(link_kind)
    row = (
        await db.execute(
            select(DocumentStudyLink).where(
                DocumentStudyLink.document_id == doc_id,
                DocumentStudyLink.study_id == study_id,
                DocumentStudyLink.link_kind == link_kind,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    await db.delete(row)
    await db.commit()
    await audit.log(
        action="document_link_removed",
        actor_subject_id=user.subject_id,
        resource_kind="patient_document",
        resource_id=doc_id,
        metadata={
            "patient_id": str(patient.id),
            "study_id": str(study_id),
            "link_kind": link_kind,
        },
    )
    return None


@router.get(
    "/patients/{patient_id}/documents/{doc_id}/references",
    response_model=DocumentReferencesOut,
)
async def get_document_references(
    request: Request,
    patient_id: uuid.UUID,
    doc_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(optional_user)],
) -> DocumentReferencesOut:
    """List the studies, report_contents, granular citations and
    folder containments that reference the document. Read-only;
    permission gated by patient read. Cross-patient guard via the
    ``patient_id`` URL segment + the patient_id check on each
    reference query (FK on ``documents.patient_id`` already enforces
    the invariant by construction)."""
    patient = await _get_patient_or_404(db, patient_id, user, request)

    # Document existence + same-patient assertion.
    doc = (
        await db.execute(
            select(Document).where(Document.id == doc_id, Document.patient_id == patient.id)
        )
    ).scalar_one_or_none()
    if doc is None:
        raise problem(404, "not_found", "document not found")

    study_links = (
        (await db.execute(select(DocumentStudyLink).where(DocumentStudyLink.document_id == doc_id)))
        .scalars()
        .all()
    )
    studies = [
        StudyRefOut(
            study_id=str(link.study_id),
            link_kind=link.link_kind,
            created_at=link.created_at.isoformat(),
            created_by_subject_id=str(link.created_by_subject_id)
            if link.created_by_subject_id
            else None,
        )
        for link in study_links
    ]

    from bvphoenix.db.models.report_contents import (
        ContentDocumentLink,
        ReportContent,
        ReportContentCitation,
    )

    content_rows = (
        await db.execute(
            select(ContentDocumentLink, ReportContent.clinical_event_id)
            .join(ReportContent, ReportContent.id == ContentDocumentLink.report_content_id)
            .where(ContentDocumentLink.document_id == doc_id)
        )
    ).all()
    report_contents = [
        ContentRefOut(
            report_content_id=str(cl.report_content_id),
            role=cl.role,
            excerpt=cl.excerpt,
            clinical_event_id=str(ev_id) if ev_id else None,
        )
        for cl, ev_id in content_rows
    ]

    citation_rows = (
        (
            await db.execute(
                select(ReportContentCitation).where(
                    ReportContentCitation.target_kind == "document",
                    ReportContentCitation.target_id == doc_id,
                )
            )
        )
        .scalars()
        .all()
    )
    citations = [
        CitationRefOut(
            report_content_id=str(c.report_content_id),
            citation_id=str(c.id),
            page=c.page,
            excerpt=c.excerpt,
        )
        for c in citation_rows
    ]

    folder_rows = (
        (
            await db.execute(
                select(Folder)
                .join(FolderItem, FolderItem.folder_id == Folder.id)
                .where(
                    FolderItem.resource_kind == "document",
                    FolderItem.resource_id == doc_id,
                )
                .order_by(Folder.is_root.desc(), Folder.name)
            )
        )
        .scalars()
        .all()
    )
    folders = [
        FolderMembershipOut(folder_id=str(f.id), name=f.name, is_root=f.is_root)
        for f in folder_rows
    ]

    return DocumentReferencesOut(
        studies=studies,
        report_contents=report_contents,
        citations=citations,
        folders=folders,
    )


@router.get("/patients/{patient_id}/documents/{doc_id}/content")
async def get_document_content(
    request: Request,
    patient_id: uuid.UUID,
    doc_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(optional_user)],
) -> Response:
    """Serve a patient document for inline preview.

    Streams the binary back through the backend (storage isolation —
    feedback_storage_isolation): the client never sees the storage host,
    bucket name, or key. For small text payloads the content is returned
    inline; for larger binaries the response is a streamed ``StreamingResponse``.
    """
    patient = await _get_patient_or_404(db, patient_id, user, request, action=READ_METADATA)
    doc = (
        await db.execute(
            select(Document).where(Document.id == doc_id, Document.patient_id == patient.id)
        )
    ).scalar_one_or_none()
    if doc is None:
        raise HTTPException(status_code=404, detail="document not found")

    # Text-only documents (no attached file) — the DB field *is* the body.
    if doc.file_s3_key is None:
        if doc.text is None:
            raise HTTPException(status_code=404, detail="no content")
        return Response(
            content=doc.text,
            media_type="text/plain; charset=utf-8",
            headers={"cache-control": "private, max-age=0"},
        )

    content_type = doc.file_content_type or "application/octet-stream"
    settings = get_settings()
    storage = get_s3_storage()
    filename = (doc.title or "document").replace('"', "")

    # Small text/markdown files inline straight from the API so the caller
    # can render them in a div without paying for a streaming response. A
    # ranged read caps the transfer at the ceiling + 1 byte: receiving
    # exactly ceiling+1 means the file is larger and we fall through to
    # the streamed path.
    is_textual = content_type.startswith("text/") or content_type in (
        "application/json",
        "application/xml",
    )
    if is_textual:
        data = await _read_s3_prefix(
            storage,
            settings.s3_bucket_raw,
            doc.file_s3_key,
            _INLINE_TEXT_MAX_BYTES + 1,
        )
        if data is not None and len(data) <= _INLINE_TEXT_MAX_BYTES:
            return Response(
                content=data,
                media_type=content_type,
                headers={"cache-control": "private, max-age=0"},
            )

    try:
        body_iter, length, _ = await asyncio.to_thread(
            storage.iter_object,
            bucket=settings.s3_bucket_raw,
            key=doc.file_s3_key,
        )
    except Exception as exc:
        raise problem(
            404,
            "binary_unavailable",
            "document binary unavailable",
        ) from exc
    headers: dict[str, str] = {
        "content-disposition": _content_disposition(filename, disposition="inline"),
        "cache-control": "private, max-age=0",
    }
    if length is not None:
        headers["content-length"] = str(length)
    return StreamingResponse(body_iter, media_type=content_type, headers=headers)


@router.get(
    "/patients/{patient_id}/labs",
    response_model=LabTimeseriesOut,
    tags=["labs"],
)
async def get_patient_lab_timeseries(
    request: Request,
    patient_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    audit: AuditDep,
    analyte: str = Query(..., min_length=2, max_length=64),
    since: datetime | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
) -> LabTimeseriesOut:
    """Aggregate lab values for a patient across cached extractor output.

    Sprint 4 (P2): the values come from ``document_entities`` rows
    (proposed namespace, ADR 0008) — only documents with a current
    extractor cache contribute. Trend direction reports ``unknown``
    when fewer than 3 points are available.
    """
    from bvphoenix.db.models import DocumentEntities

    patient = await _get_patient_or_404(db, patient_id, user, request, action=READ_METADATA)
    needle = _normalise_analyte(analyte)

    rows = (
        await db.execute(
            select(DocumentEntities, Document)
            .join(
                Document,
                Document.id == DocumentEntities.document_id,
            )
            .where(
                Document.patient_id == patient.id,
                Document.deleted_at.is_(None),
            )
            .order_by(DocumentEntities.created_at.desc())
        )
    ).all()

    points: list[LabPointOut] = []
    seen_unit: str | None = None
    for ent, doc in rows:
        payload = ent.entities_jsonb or {}
        labs = (payload.get("entities_proposed") or {}).get("lab_values") or []
        for lab in labs:
            text = str(lab.get("analyte") or "")
            if needle not in _normalise_analyte(text):
                continue
            unit = str(lab.get("unit") or "")
            value = lab.get("value")
            if value is None:
                continue
            if seen_unit is None:
                seen_unit = unit
            elif seen_unit != unit:
                # Skip mismatched units — comparing mg/dL with mmol/L
                # would be misleading.
                continue
            doc_date = doc.document_date.isoformat() if doc.document_date else None
            if since is not None and doc.document_date is not None:
                if doc.document_date < since.date():
                    continue
            points.append(
                LabPointOut(
                    document_id=str(doc.id),
                    document_date=doc_date,
                    text=str(lab.get("text") or text),
                    analyte=text,
                    value=float(value),
                    unit=unit,
                    confidence=float(lab.get("confidence") or 0.0),
                )
            )

    # Sort chronologically asc; missing dates go last.
    def _sort_key(p: LabPointOut) -> tuple[int, str]:
        if p.document_date:
            return (0, p.document_date)
        return (1, "")

    points.sort(key=_sort_key)
    points = points[:limit]

    trend = LabTrendOut(
        direction="unknown",
        delta=None,
        rel_delta_pct=None,
        earliest_iso=None,
        latest_iso=None,
    )
    if len(points) >= 3:
        first = points[0]
        last = points[-1]
        delta = round(last.value - first.value, 4)
        rel = None
        if first.value != 0:
            rel = round(100.0 * delta / first.value, 2)
        if abs(delta) < 1e-6:
            direction = "stable"
        elif delta > 0:
            direction = "up"
        else:
            direction = "down"
        trend = LabTrendOut(
            direction=direction,
            delta=delta,
            rel_delta_pct=rel,
            earliest_iso=first.document_date,
            latest_iso=last.document_date,
        )

    await audit.log(
        action="labs_timeseries_read",
        actor_subject_id=user.subject_id,
        resource_kind="patient",
        resource_id=patient.id,
        metadata={
            "analyte": analyte,
            "n_points": len(points),
        },
    )

    return LabTimeseriesOut(
        patient_id=str(patient.id),
        analyte=analyte,
        unit=seen_unit,
        points=points,
        trend=trend,
    )


@router.get(
    "/patients/{patient_id}/documents/{doc_id}/entities",
    response_model=DocumentEntitiesOut,
)
async def get_document_entities(
    request: Request,
    patient_id: uuid.UUID,
    doc_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    audit: AuditDep,
    extractor_version: str | None = Query(default=None),
) -> DocumentEntitiesOut:
    """Read the cached extractor output for a document (Sprint 4, ADR 0008).

    404 ``entities_cache_miss`` when no row exists; trigger extraction
    via POST .../entities. Use ``extractor_version`` to pin the
    response to a specific version when multiple are cached.
    """
    from bvphoenix.db.models import DocumentEntities

    patient = await _get_patient_or_404(db, patient_id, user, request, action=READ_METADATA)
    doc = (
        await db.execute(
            select(Document).where(
                Document.id == doc_id,
                Document.patient_id == patient.id,
            )
        )
    ).scalar_one_or_none()
    if doc is None:
        raise problem(404, "not_found", "document not found")

    q = select(DocumentEntities).where(DocumentEntities.document_id == doc.id)
    if extractor_version:
        q = q.where(DocumentEntities.extractor_version == extractor_version)
    row = (
        await db.execute(q.order_by(DocumentEntities.created_at.desc()).limit(1))
    ).scalar_one_or_none()
    if row is None:
        raise problem(
            404,
            "entities_cache_miss",
            "no entity-extraction cache; trigger via POST .../entities",
            extra={"document_id": str(doc.id)},
        )

    payload = dict(row.entities_jsonb or {})
    await audit.log(
        action="document_entities_read",
        actor_subject_id=user.subject_id,
        resource_kind="patient_document",
        resource_id=doc.id,
        metadata={
            "patient_id": str(patient.id),
            "extractor_version": row.extractor_version,
        },
    )
    return DocumentEntitiesOut(
        document_id=str(doc.id),
        extractor_version=row.extractor_version,
        extracted_at=payload.get("extracted_at"),
        entities_proposed=payload.get("entities_proposed") or {},
        entities_validated=payload.get("entities_validated") or {},
        cached=True,
    )


@router.post(
    "/patients/{patient_id}/documents/{doc_id}/entities",
    response_model=DocumentEntitiesOut,
)
async def run_document_entities(
    request: Request,
    patient_id: uuid.UUID,
    doc_id: uuid.UUID,
    body: DocumentEntitiesRunIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    audit: AuditDep,
    idem: Annotated[IdempotencyContext, Depends(idempotent)],
) -> DocumentEntitiesOut:
    """Trigger entity extraction over the document's OCR text."""
    if idem.replay is not None:
        return idem.replay  # type: ignore[return-value]

    from bvphoenix.db.models import DocumentEntities, DocumentOCR
    from bvphoenix.services.clinical_entities import (
        EXTRACTOR_VERSION,
        extract_entities,
    )

    patient = await _get_patient_or_404(db, patient_id, user, request, action=READ_METADATA)
    doc = (
        await db.execute(
            select(Document).where(
                Document.id == doc_id,
                Document.patient_id == patient.id,
            )
        )
    ).scalar_one_or_none()
    if doc is None:
        raise problem(404, "not_found", "document not found")

    ocr_row = (
        await db.execute(
            select(DocumentOCR)
            .where(DocumentOCR.document_id == doc.id)
            .order_by(DocumentOCR.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if ocr_row is None or not ocr_row.text:
        raise problem(
            422,
            "ocr_missing",
            "no OCR cache; populate via POST .../text first",
        )

    import hashlib

    text_sha = hashlib.sha256(ocr_row.text.encode("utf-8")).hexdigest()

    if not body.force:
        cached = (
            await db.execute(
                select(DocumentEntities).where(
                    DocumentEntities.document_id == doc.id,
                    DocumentEntities.extractor_version == EXTRACTOR_VERSION,
                    DocumentEntities.content_sha256 == text_sha,
                )
            )
        ).scalar_one_or_none()
        if cached is not None:
            payload = dict(cached.entities_jsonb or {})
            return idem.capture(  # type: ignore[return-value]
                DocumentEntitiesOut(
                    document_id=str(doc.id),
                    extractor_version=cached.extractor_version,
                    extracted_at=payload.get("extracted_at"),
                    entities_proposed=payload.get("entities_proposed") or {},
                    entities_validated=payload.get("entities_validated") or {},
                    cached=True,
                ).model_dump(),
                status_code=200,
            )

    if not body.inline:
        from arq import create_pool

        from bvphoenix.services.arq_redis import redis_settings
        from bvphoenix.services.jobs import (
            JobCapExceededError,
            enqueue_or_get,
            mark_failed,
            set_arq_job_id,
        )

        canonical_input = {
            "document_id": str(doc.id),
            "force": bool(body.force),
        }
        try:
            job_result = await enqueue_or_get(
                db,
                kind="extract_document_entities",
                owner_subject_id=user.subject_id,
                canonical_input=canonical_input,
                scope_ids=[str(doc.id)],
            )
        except JobCapExceededError as exc:
            raise problem(
                429,
                "rate_limited",
                str(exc),
                extra={"retry_after_seconds": exc.retry_after_seconds},
            ) from exc
        await db.commit()

        if not job_result.deduped:
            settings = get_settings()
            try:
                redis = await create_pool(redis_settings(settings.redis_url))
                arq_handle = await redis.enqueue_job(
                    "extract_document_entities", str(job_result.job.id)
                )
                await redis.close()
                if arq_handle is not None:
                    await set_arq_job_id(db, job_result.job.id, arq_handle.job_id)
                    await db.commit()
            except Exception as exc:
                await mark_failed(
                    db,
                    job_result.job.id,
                    error={"code": "enqueue_failed", "message": str(exc)},
                )
                await db.commit()
                raise problem(
                    503,
                    "service_unavailable",
                    "failed to enqueue entity-extraction worker job",
                ) from exc

        return idem.capture(  # type: ignore[return-value]
            DocumentEntitiesOut(
                document_id=str(doc.id),
                extractor_version="pending",
                extracted_at=None,
                entities_proposed={},
                entities_validated={},
                cached=False,
            ).model_dump(),
            status_code=202,
            extra_headers={"X-Job-Id": str(job_result.job.id)},
        )

    # Inline path.
    extraction = extract_entities(ocr_row.text)
    row = DocumentEntities(
        document_id=doc.id,
        content_sha256=text_sha,
        extractor_version=extraction.extractor_version,
        entities_jsonb=extraction.to_payload(),
    )
    db.add(row)
    await db.commit()

    await audit.log(
        action="document_entities_extracted",
        actor_subject_id=user.subject_id,
        resource_kind="patient_document",
        resource_id=doc.id,
        metadata={
            "patient_id": str(patient.id),
            "extractor_version": extraction.extractor_version,
            "n_lab_values": len(extraction.entities_proposed.get("lab_values", [])),
        },
    )

    payload = extraction.to_payload()
    return idem.capture(  # type: ignore[return-value]
        DocumentEntitiesOut(
            document_id=str(doc.id),
            extractor_version=extraction.extractor_version,
            extracted_at=payload.get("extracted_at"),
            entities_proposed=payload.get("entities_proposed") or {},
            entities_validated=payload.get("entities_validated") or {},
            cached=False,
        ).model_dump(),
        status_code=200,
    )


@router.get(
    "/patients/{patient_id}/documents/{doc_id}/text",
    response_model=DocumentTextOut,
)
async def get_document_text(
    request: Request,
    patient_id: uuid.UUID,
    doc_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    audit: AuditDep,
    file_id: uuid.UUID | None = Query(default=None),
    engine: str | None = Query(
        default=None,
        description=(
            "Optional ``ocr_engine`` filter (``pdfminer`` or ``tesseract``). "
            "When omitted the most recent cached entry wins."
        ),
    ),
) -> DocumentTextOut:
    """Read OCR text for a document file (Sprint 3, ADR 0007).

    Cache lookup only — does not run OCR inline. ``POST .../text`` is
    the entry point that triggers extraction (sync small payloads,
    async via worker for large ones).

    404 when the cache is empty: the agent should POST first to
    populate it.
    """
    from bvphoenix.db.models import DocumentOCR

    patient = await _get_patient_or_404(db, patient_id, user, request, action=READ_METADATA)
    doc = (
        await db.execute(
            select(Document).where(
                Document.id == doc_id,
                Document.patient_id == patient.id,
            )
        )
    ).scalar_one_or_none()
    if doc is None:
        raise problem(404, "not_found", "document not found")

    q = select(DocumentOCR).where(DocumentOCR.document_id == doc.id)
    if file_id is not None:
        q = q.where(DocumentOCR.file_id == file_id)
    if engine:
        q = q.where(DocumentOCR.ocr_engine == engine)
    row = (
        await db.execute(q.order_by(DocumentOCR.created_at.desc()).limit(1))
    ).scalar_one_or_none()
    if row is None:
        raise problem(
            404,
            "ocr_cache_miss",
            "no OCR cache entry; trigger extraction via POST .../text",
            extra={"document_id": str(doc.id)},
        )

    await audit.log(
        action="document_text_read",
        actor_subject_id=user.subject_id,
        resource_kind="patient_document",
        resource_id=doc.id,
        metadata={
            "patient_id": str(patient.id),
            "engine": row.ocr_engine,
            "engine_version": row.ocr_engine_version,
        },
    )

    return DocumentTextOut(
        document_id=str(doc.id),
        file_id=str(row.file_id) if row.file_id else None,
        text=row.text,
        engine=row.ocr_engine,
        engine_version=row.ocr_engine_version,
        sha256=row.content_sha256,
        page_count=row.page_count,
        bbox_words=row.bbox_words,
        cached=True,
    )


@router.post(
    "/patients/{patient_id}/documents/{doc_id}/text",
    response_model=DocumentTextOut,
)
async def run_document_text(
    request: Request,
    patient_id: uuid.UUID,
    doc_id: uuid.UUID,
    body: DocumentTextRunIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    audit: AuditDep,
    idem: Annotated[IdempotencyContext, Depends(idempotent)],
) -> DocumentTextOut:
    """Trigger OCR extraction for a document file.

    Inline path: the API runs the OCR pipeline synchronously and
    returns the extracted text. Async path (``inline=false`` or large
    file): an Arq job is enqueued and the response carries
    ``X-Job-Id`` + 202 — poll ``GET /api/jobs/:id`` for completion.

    Cache hits short-circuit: a fresh ``(file_id, sha256, engine_version)``
    entry skips both paths and is returned with ``cached=true``.
    """
    if idem.replay is not None:
        return idem.replay  # type: ignore[return-value]

    from bvphoenix.db.models import DocumentOCR
    from bvphoenix.services.ocr import (
        PDFMINER_ENGINE_VERSION,
        TESSERACT_ENGINE_VERSION,
        run_ocr,
    )

    patient = await _get_patient_or_404(db, patient_id, user, request, action=READ_METADATA)
    perms = await effective_permissions_on_patient(db, user=user, patient=patient)
    if "write:report" not in perms and DELETE not in perms and "read:report" not in perms:
        raise problem(403, "forbidden", "cannot run OCR on this patient")

    doc = (
        await db.execute(
            select(Document).where(
                Document.id == doc_id,
                Document.patient_id == patient.id,
            )
        )
    ).scalar_one_or_none()
    if doc is None:
        raise problem(404, "not_found", "document not found")

    target_key: str | None = None
    target_mime: str | None = None
    target_file_id: uuid.UUID | None = None

    if body.file_id is not None:
        f = (
            await db.execute(
                select(DocumentFile).where(
                    DocumentFile.id == body.file_id,
                    DocumentFile.document_id == doc.id,
                )
            )
        ).scalar_one_or_none()
        if f is None:
            raise problem(404, "not_found", "document file not found")
        target_key = f.file_s3_key
        target_mime = f.file_content_type
        target_file_id = f.id
    else:
        if doc.file_s3_key:
            target_key = doc.file_s3_key
            target_mime = doc.file_content_type
        else:
            f = (
                await db.execute(
                    select(DocumentFile)
                    .where(DocumentFile.document_id == doc.id)
                    .order_by(DocumentFile.sequence.asc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if f is None:
                raise problem(
                    422,
                    "no_binary_payload",
                    "document has no binary payload; nothing to OCR",
                )
            target_key = f.file_s3_key
            target_mime = f.file_content_type
            target_file_id = f.id

    if not target_key:
        raise problem(422, "no_binary_payload", "document has no binary file")

    settings = get_settings()
    storage = get_s3_storage()

    if not body.force:
        # Check the cache against the most likely engine version. We
        # cache by sha256, so we need the binary anyway only on a
        # cache miss; here we look up by ``(file_id, latest_engine)``
        # which is *enough* because a re-run with a different engine
        # is opted-in via ``force=True``.
        cached = (
            await db.execute(
                select(DocumentOCR)
                .where(
                    DocumentOCR.document_id == doc.id,
                    (DocumentOCR.file_id == target_file_id)
                    if target_file_id
                    else DocumentOCR.file_id.is_(None),
                    DocumentOCR.ocr_engine_version.in_(
                        [PDFMINER_ENGINE_VERSION, TESSERACT_ENGINE_VERSION]
                    ),
                )
                .order_by(DocumentOCR.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if cached is not None:
            return idem.capture(  # type: ignore[return-value]
                DocumentTextOut(
                    document_id=str(doc.id),
                    file_id=str(target_file_id) if target_file_id else None,
                    text=cached.text,
                    engine=cached.ocr_engine,
                    engine_version=cached.ocr_engine_version,
                    sha256=cached.content_sha256,
                    page_count=cached.page_count,
                    bbox_words=cached.bbox_words,
                    cached=True,
                ).model_dump(),
                status_code=200,
            )

    if not body.inline:
        return await _enqueue_ocr_async(
            db=db,
            user=user,
            doc=doc,
            target_file_id=target_file_id,
            body=body,
            idem=idem,
            settings=settings,
        )

    # Inline path: pull the binary, run the pipeline, store the result.
    # When the pipeline raises non-deterministic ``RuntimeError`` (Tesseract
    # binary missing, traineddata for the requested language unavailable,
    # PDF text-layer empty + raster fallback failure) the request degrades
    # to the async path so the worker safety-net + retry can take over.
    # Storage misses (S3 404) keep their explicit 404 — they are not
    # something a retry would fix.
    try:
        data = storage.get_object_bytes(bucket=settings.s3_bucket_raw, key=target_key)
    except Exception as exc:
        raise problem(
            404,
            "binary_unavailable",
            "document binary unavailable",
        ) from exc

    try:
        ocr_result = run_ocr(data, mime=target_mime, language=body.language)
    except RuntimeError as exc:
        _log.warning(
            "ocr_inline_fallback document_id=%s file_id=%s mime=%s language=%s reason=%s",
            doc.id,
            target_file_id,
            target_mime,
            body.language,
            exc,
        )
        return await _enqueue_ocr_async(
            db=db,
            user=user,
            doc=doc,
            target_file_id=target_file_id,
            body=body,
            idem=idem,
            settings=settings,
        )

    row = DocumentOCR(
        document_id=doc.id,
        file_id=target_file_id,
        content_sha256=ocr_result.sha256,
        ocr_engine=ocr_result.engine,
        ocr_engine_version=ocr_result.engine_version,
        text=ocr_result.text,
        page_count=ocr_result.page_count,
        bbox_words=ocr_result.bbox_words,
    )
    db.add(row)
    try:
        await db.commit()
        await db.refresh(row)
    except sqlalchemy_exc.IntegrityError:
        # The cache lookup above is best-effort: between the SELECT and
        # the INSERT another caller (or a previous run with the same
        # ``content_sha256`` + engine that the lookup missed because
        # the doc had no file_id) can land an OCR row. Postgres rejects
        # the second INSERT on the
        # ``(file_id, content_sha256, ocr_engine_version)`` unique
        # constraint and the request blew up with a generic 500. Recover
        # by reading the row that won the race and treating the call as
        # an idempotent cache hit. This was the symptom that surfaced
        # for every digital_native_pdf with a previously-extracted text
        # row pinned to NULL file_id.
        await db.rollback()
        existing = (
            await db.execute(
                select(DocumentOCR)
                .where(
                    DocumentOCR.document_id == doc.id,
                    (DocumentOCR.file_id == target_file_id)
                    if target_file_id
                    else DocumentOCR.file_id.is_(None),
                    DocumentOCR.content_sha256 == ocr_result.sha256,
                    DocumentOCR.ocr_engine_version == ocr_result.engine_version,
                )
                .order_by(DocumentOCR.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if existing is None:
            # Race lost but the row is no longer there — this is the
            # genuine integrity bug, not a cache race. Surface it.
            raise
        row = existing

    await audit.log(
        action="document_text_extracted",
        actor_subject_id=user.subject_id,
        resource_kind="patient_document",
        resource_id=doc.id,
        metadata={
            "patient_id": str(patient.id),
            "engine": ocr_result.engine,
            "engine_version": ocr_result.engine_version,
            "n_chars": len(ocr_result.text),
        },
    )

    return idem.capture(  # type: ignore[return-value]
        DocumentTextOut(
            document_id=str(doc.id),
            file_id=str(target_file_id) if target_file_id else None,
            text=ocr_result.text,
            engine=ocr_result.engine,
            engine_version=ocr_result.engine_version,
            sha256=ocr_result.sha256,
            page_count=ocr_result.page_count,
            bbox_words=ocr_result.bbox_words,
            cached=False,
        ).model_dump(),
        status_code=200,
    )


@router.get(
    "/patients/{patient_id}/documents/{doc_id}/binary_url",
    response_model=DocumentBinaryUrlOut,
)
async def get_document_binary_url(
    request: Request,
    patient_id: uuid.UUID,
    doc_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    audit: AuditDep,
    file_id: uuid.UUID | None = Query(
        default=None,
        description=(
            "Optional file id when the document is multi-file. When "
            "omitted the legacy single-file slot or the first file by "
            "sequence is selected."
        ),
    ),
) -> DocumentBinaryUrlOut:
    """Return a backend URL the caller can GET to stream the document.

    The returned ``url`` is a backend-relative path (``/api/…/binary``)
    served by this same FastAPI app. Storage details (bucket, region,
    key) never leave the backend pod. The caller authenticates against
    the streaming endpoint with the same session/token used here.
    """
    patient = await _get_patient_or_404(db, patient_id, user, request, action=READ_METADATA)
    doc = (
        await db.execute(
            select(Document).where(
                Document.id == doc_id,
                Document.patient_id == patient.id,
                Document.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if doc is None:
        raise problem(404, "not_found", "document not found")

    files = (
        (
            await db.execute(
                select(DocumentFile)
                .where(DocumentFile.document_id == doc.id)
                .order_by(DocumentFile.sequence.asc())
            )
        )
        .scalars()
        .all()
    )
    _, content_type, target_file_id, size = _document_binary_target(doc, file_id, list(files))

    backend_url = f"/api/patients/{patient.id}/documents/{doc.id}/binary"
    if target_file_id is not None:
        backend_url += f"?file_id={target_file_id}"

    await audit.log(
        action="document_binary_url_issued",
        actor_subject_id=user.subject_id,
        resource_kind="patient_document",
        resource_id=doc.id,
        metadata={
            "patient_id": str(patient.id),
            "file_id": str(target_file_id) if target_file_id else None,
        },
    )

    return DocumentBinaryUrlOut(
        document_id=str(doc.id),
        file_id=str(target_file_id) if target_file_id else None,
        url=backend_url,
        content_type=content_type or "application/octet-stream",
        size_bytes=size,
    )


@router.get("/patients/{patient_id}/documents/{doc_id}/binary")
async def stream_document_binary(
    request: Request,
    patient_id: uuid.UUID,
    doc_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    audit: AuditDep,
    file_id: uuid.UUID | None = Query(default=None),
) -> StreamingResponse:
    """Stream a document's binary payload through the backend.

    No redirect, no presigned URL, no storage host in the response.
    Auth uses the standard session/token; per-patient agent scope is
    enforced via ``_get_patient_or_404``. Agents additionally need the
    granular ``documents:download`` scope — humans always pass.
    Errors are mapped to neutral problem codes (``binary_unavailable``)
    so a missing object never leaks storage internals to the caller.
    """
    enforce_agent_scope(request, "documents:download")
    patient = await _get_patient_or_404(db, patient_id, user, request, action=READ_METADATA)
    doc = (
        await db.execute(
            select(Document).where(
                Document.id == doc_id,
                Document.patient_id == patient.id,
                Document.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if doc is None:
        raise problem(404, "not_found", "document not found")

    files = (
        (
            await db.execute(
                select(DocumentFile)
                .where(DocumentFile.document_id == doc.id)
                .order_by(DocumentFile.sequence.asc())
            )
        )
        .scalars()
        .all()
    )
    # ``size`` was used by the legacy hand-rolled streamer to set
    # an explicit Content-Length when the storage iter didn't
    # supply one. The migrated proxy_s3_object reads length from
    # the S3 response itself, so the unpacked value is unused.
    storage_key, content_type, target_file_id, _size = _document_binary_target(
        doc, file_id, list(files)
    )

    settings = get_settings()
    filename = (doc.title or "document").replace('"', "")

    await audit.log(
        action="document_binary_streamed",
        actor_subject_id=user.subject_id,
        resource_kind="patient_document",
        resource_id=doc.id,
        metadata={
            "patient_id": str(patient.id),
            "file_id": str(target_file_id) if target_file_id else None,
        },
    )

    # Migrated to the uniform proxy_s3_object helper so this path
    # gets Range/206/Accept-Ranges + RFC 6266 + storage isolation
    # for free, identical to /documents/{id}/download. The helper
    # raises 404 on storage errors with no bucket/key leakage; we
    # catch nothing extra here.
    return await proxy_s3_object(
        request=request,
        bucket=settings.s3_bucket_raw,
        key=storage_key,
        filename=filename,
        fallback_content_type=content_type or "application/octet-stream",
    )


@router.get("/patients/{patient_id}/documents/{doc_id}/thumbnail")
async def get_document_thumbnail(
    request: Request,
    patient_id: uuid.UUID,
    doc_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(optional_user)],
    max_side: int = Query(256, ge=64, le=1024),
) -> Response:
    """JPEG thumbnail for a document, used as the grid-card cover.

    Generated on the fly from the underlying file (PDF first page or
    raster image, see ``services.document_thumbnails``) and cached
    only via ``cache-control`` (1 day). Inline-text documents and
    unsupported MIME types return 404 so the frontend falls back to
    a kind-typed icon. Errors during rasterisation also return 404
    rather than 500: a missing thumbnail must never break the grid.
    """
    patient = await _get_patient_or_404(db, patient_id, user, request, action=READ_METADATA)
    doc = (
        await db.execute(
            select(Document).where(Document.id == doc_id, Document.patient_id == patient.id)
        )
    ).scalar_one_or_none()
    if doc is None:
        raise HTTPException(status_code=404, detail="document not found")
    if doc.file_s3_key is None:
        # Inline-text-only document: nothing to rasterise.
        raise HTTPException(status_code=404, detail="no thumbnail")
    if not is_supported_thumbnail_mime(doc.file_content_type, doc.title):
        raise HTTPException(status_code=404, detail="unsupported kind")

    settings = get_settings()
    storage = get_s3_storage()
    try:
        body = await asyncio.to_thread(
            storage.get_object_bytes,
            bucket=settings.s3_bucket_raw,
            key=doc.file_s3_key,
        )
    except Exception as exc:
        raise HTTPException(status_code=404, detail="object unavailable") from exc

    try:
        jpeg = await asyncio.to_thread(
            render_document_thumbnail,
            body,
            content_type=doc.file_content_type,
            filename=doc.title,
            max_side=max_side,
        )
    except UnsupportedThumbnailKindError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        # Treat malformed PDFs / corrupt images as "no thumbnail" so
        # the grid degrades gracefully. The original document remains
        # available via ``/content``.
        raise HTTPException(status_code=404, detail="render failed") from exc

    return Response(
        content=jpeg,
        media_type="image/jpeg",
        headers={"cache-control": "private, max-age=86400"},
    )


@router.get("/patients/{patient_id}/documents/{doc_id}/files/{file_id}/content")
async def get_document_file_content(
    request: Request,
    patient_id: uuid.UUID,
    doc_id: uuid.UUID,
    file_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(optional_user)],
) -> StreamingResponse:
    """Stream a single file from a multi-file document.

    Used by the gallery UI when a document holds multiple scans / pages.
    The bytes flow through the backend; the storage host never appears
    in the response.
    """
    patient = await _get_patient_or_404(db, patient_id, user, request, action=READ_METADATA)
    row = (
        await db.execute(
            select(DocumentFile)
            .join(
                Document,
                Document.id == DocumentFile.document_id,
            )
            .where(
                DocumentFile.id == file_id,
                DocumentFile.document_id == doc_id,
                Document.patient_id == patient.id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="file not found")

    settings = get_settings()
    storage = get_s3_storage()
    filename = (row.original_filename or f"file-{row.sequence:03d}.bin").replace('"', "")

    try:
        body_iter, length, _ = await asyncio.to_thread(
            storage.iter_object,
            bucket=settings.s3_bucket_raw,
            key=row.file_s3_key,
        )
    except Exception as exc:
        raise problem(
            404,
            "binary_unavailable",
            "file binary unavailable",
        ) from exc
    headers: dict[str, str] = {
        "content-disposition": _content_disposition(filename, disposition="inline"),
        "cache-control": "private, max-age=0",
    }
    if length is not None:
        headers["content-length"] = str(length)
    elif row.size_bytes is not None:
        headers["content-length"] = str(row.size_bytes)
    return StreamingResponse(
        body_iter,
        media_type=row.file_content_type or "application/octet-stream",
        headers=headers,
    )
