"""Arq task: run OCR on a single patient document file (Sprint 3).

The API endpoint can call ``services.ocr.run_ocr`` inline for small
files, but pages with many high-DPI scans may take 30+ seconds. The
``run_document_ocr`` task offloads such requests to the worker and
caches the result via the ``document_ocr`` table.

Job input shape::

    {"document_id": "<uuid>", "file_id": "<uuid>", "force": bool}

The task short-circuits when a cache hit already exists for the
``(file_id, sha256, ocr_engine_version)`` triple unless ``force`` is
true.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from bvworkers.config import get_settings
from bvworkers.job_safety import mark_job_failed_raw, with_safety_net

log = logging.getLogger(__name__)


async def _enqueue_chunk_embed(ctx: dict, doc_id: uuid.UUID) -> None:  # type: ignore[type-arg]
    """After OCR succeeds, chain document chunking + embedding so the new
    text becomes searchable (MiniLM + BGE-M3). Best-effort: a failed
    enqueue must not fail the OCR job, and ``chunk_and_embed_document`` is
    idempotent on the content hash so a re-OCR is cheap.
    """
    try:
        from bvphoenix.db.models.text_chunks import DEFAULT_CHUNKER_VERSION

        redis = ctx.get("redis") if isinstance(ctx, dict) else None
        if redis is not None:
            await redis.enqueue_job(
                "chunk_and_embed_document", str(doc_id), DEFAULT_CHUNKER_VERSION
            )
    except Exception:
        log.exception("failed to enqueue chunk_and_embed_document for %s", doc_id)


@with_safety_net("run_document_ocr")
async def run_document_ocr(
    ctx: dict,  # type: ignore[type-arg]
    job_id: str,
) -> dict[str, Any]:
    try:
        jid = uuid.UUID(job_id)
    except (TypeError, ValueError) as exc:
        log.error("invalid job_id: %s", exc)
        return {"status": "error", "reason": f"invalid uuid: {exc}"}

    try:
        from bvphoenix.db.models import (
            Document,
            DocumentFile,
            DocumentOCR,
        )
        from bvphoenix.db.session import SERVICE_SUBJECT, set_current_subject
        from bvphoenix.services import jobs as jobs_service
        from bvphoenix.services.ocr import run_ocr
        from bvphoenix.storage import get_s3_storage
    except ImportError as exc:
        log.exception("bvphoenix import failed: %s", exc)
        await mark_job_failed_raw(job_id, code="bvphoenix_import_failed", message=str(exc))
        return {"status": "error", "reason": f"import: {exc}"}

    settings = get_settings()
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as db:
            await set_current_subject(db, SERVICE_SUBJECT)
            await jobs_service.mark_running(db, jid)
            await db.commit()

            job = await jobs_service.get_job(db, jid)
            payload = job.input or {}
            doc_id = uuid.UUID(payload["document_id"])
            file_id_raw = payload.get("file_id")
            force = bool(payload.get("force", False))
            language = payload.get("language")

            doc = (
                await db.execute(select(Document).where(Document.id == doc_id))
            ).scalar_one_or_none()
            if doc is None:
                await jobs_service.mark_failed(
                    db,
                    jid,
                    error={"code": "doc_not_found", "message": str(doc_id)},
                )
                await db.commit()
                return {"status": "error", "reason": "doc_not_found"}

            target_file: DocumentFile | None = None
            target_key: str | None = None
            target_mime: str | None = None
            target_file_uuid: uuid.UUID | None = None

            if file_id_raw:
                target_file_uuid = uuid.UUID(file_id_raw)
                target_file = (
                    await db.execute(
                        select(DocumentFile).where(
                            DocumentFile.id == target_file_uuid,
                            DocumentFile.document_id == doc.id,
                        )
                    )
                ).scalar_one_or_none()
                if target_file is None:
                    await jobs_service.mark_failed(
                        db,
                        jid,
                        error={"code": "file_not_found", "message": str(file_id_raw)},
                    )
                    await db.commit()
                    return {"status": "error", "reason": "file_not_found"}
                target_key = target_file.file_s3_key
                target_mime = target_file.file_content_type
            else:
                target_key = doc.file_s3_key
                target_mime = doc.file_content_type

            if not target_key:
                await jobs_service.mark_failed(
                    db,
                    jid,
                    error={"code": "no_binary", "message": "document has no binary file"},
                )
                await db.commit()
                return {"status": "error", "reason": "no_binary"}

            storage = get_s3_storage()
            try:
                data = storage.get_object_bytes(bucket=settings.s3_bucket_raw, key=target_key)
            except Exception as exc:
                await jobs_service.mark_failed(
                    db,
                    jid,
                    error={"code": "s3_missing", "message": str(exc)},
                )
                await db.commit()
                return {"status": "error", "reason": "s3_missing"}

            result = run_ocr(data, mime=target_mime, language=language)

            if not force:
                cached = (
                    await db.execute(
                        select(DocumentOCR).where(
                            DocumentOCR.file_id == target_file_uuid,
                            DocumentOCR.content_sha256 == result.sha256,
                            DocumentOCR.ocr_engine_version == result.engine_version,
                        )
                    )
                ).scalar_one_or_none()
                if cached is not None:
                    await jobs_service.mark_succeeded(db, jid, result_uri=None)
                    await db.commit()
                    await _enqueue_chunk_embed(ctx, doc.id)
                    return {
                        "status": "succeeded",
                        "cached": True,
                        "ocr_id": str(cached.id),
                    }

            row = DocumentOCR(
                document_id=doc.id,
                file_id=target_file_uuid,
                content_sha256=result.sha256,
                ocr_engine=result.engine,
                ocr_engine_version=result.engine_version,
                text=result.text,
                page_count=result.page_count,
                bbox_words=result.bbox_words,
            )
            db.add(row)
            await db.flush()
            await jobs_service.mark_succeeded(db, jid, result_uri=None)
            await db.commit()
            await _enqueue_chunk_embed(ctx, doc.id)
            return {
                "status": "succeeded",
                "cached": False,
                "ocr_id": str(row.id),
                "engine": result.engine,
                "engine_version": result.engine_version,
                "n_chars": len(result.text),
            }
    finally:
        await engine.dispose()
