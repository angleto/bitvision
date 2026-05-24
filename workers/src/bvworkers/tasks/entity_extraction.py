"""Arq task: extract clinical entities from a document's OCR text
(Sprint 4, ADR 0008).

Job input shape::

    {"document_id": "<uuid>", "force": bool}

The task reuses the most recent ``document_ocr`` row as input. If
none exists the job fails fast with code ``ocr_missing``; the agent
must populate the OCR cache first (POST .../text on the API).
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from bvworkers.config import get_settings
from bvworkers.job_safety import mark_job_failed_raw, with_safety_net

log = logging.getLogger(__name__)


@with_safety_net("extract_document_entities")
async def extract_document_entities(
    ctx: dict,  # type: ignore[type-arg]
    job_id: str,
) -> dict[str, Any]:
    try:
        jid = uuid.UUID(job_id)
    except (TypeError, ValueError) as exc:
        log.error("invalid job_id: %s", exc)
        return {"status": "error", "reason": f"invalid uuid: {exc}"}

    try:
        from bvphoenix.db.models import DocumentEntities, DocumentOCR
        from bvphoenix.db.session import SERVICE_SUBJECT, set_current_subject
        from bvphoenix.services import jobs as jobs_service
        from bvphoenix.services.clinical_entities import (
            EXTRACTOR_VERSION,
            extract_entities,
        )
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
            force = bool(payload.get("force", False))

            ocr_row = (
                await db.execute(
                    select(DocumentOCR)
                    .where(DocumentOCR.document_id == doc_id)
                    .order_by(DocumentOCR.created_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if ocr_row is None or not ocr_row.text:
                await jobs_service.mark_failed(
                    db,
                    jid,
                    error={
                        "code": "ocr_missing",
                        "message": "no OCR cache; populate via POST .../text first",
                    },
                )
                await db.commit()
                return {"status": "error", "reason": "ocr_missing"}

            text = ocr_row.text
            text_sha = hashlib.sha256(text.encode("utf-8")).hexdigest()

            if not force:
                cached = (
                    await db.execute(
                        select(DocumentEntities).where(
                            DocumentEntities.document_id == doc_id,
                            DocumentEntities.extractor_version == EXTRACTOR_VERSION,
                            DocumentEntities.content_sha256 == text_sha,
                        )
                    )
                ).scalar_one_or_none()
                if cached is not None:
                    await jobs_service.mark_succeeded(db, jid, result_uri=None)
                    await db.commit()
                    return {
                        "status": "succeeded",
                        "cached": True,
                        "entities_id": str(cached.id),
                    }

            result = extract_entities(text)
            row = DocumentEntities(
                document_id=doc_id,
                content_sha256=text_sha,
                extractor_version=result.extractor_version,
                entities_jsonb=result.to_payload(),
            )
            db.add(row)
            await db.flush()
            await jobs_service.mark_succeeded(db, jid, result_uri=None)
            await db.commit()
            return {
                "status": "succeeded",
                "cached": False,
                "entities_id": str(row.id),
                "extractor_version": result.extractor_version,
                "n_lab_values": len(result.entities_proposed.get("lab_values", [])),
                "n_measurements": len(result.entities_proposed.get("measurements", [])),
            }
    finally:
        await engine.dispose()
