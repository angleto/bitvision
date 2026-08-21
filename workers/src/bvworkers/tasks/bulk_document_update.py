"""Arq task: apply a bulk document metadata update job (Sprint 2, ADR 0003).

The API endpoint ``POST /api/patients/:pid/documents/bulk_update``
enqueues this task whenever the manifest has more than 50 items and
``atomic=False``. The task replays the same service entry point used
by the synchronous path (``apply_bulk_update``) so behaviour is
identical except for the transport.

Lifecycle:
1. Mark Job ``running``.
2. Reconstruct :class:`BulkUpdateItem` instances from ``Job.input``.
3. Call :func:`apply_bulk_update` with ``atomic=False, dry_run=False``.
4. Persist the per-item outcome inside ``Job.input['result']``.
5. Flip the row to ``succeeded`` (or ``failed`` on uncaught exception).
"""

from __future__ import annotations

import logging
import uuid
from datetime import date
from typing import Any

from bvphoenix.db.engine import make_async_engine
from sqlalchemy.ext.asyncio import AsyncSession

from bvworkers.config import get_settings
from bvworkers.job_safety import mark_job_failed_raw, with_safety_net

log = logging.getLogger(__name__)


@with_safety_net("bulk_document_update")
async def bulk_document_update(
    ctx: dict,  # type: ignore[type-arg]
    job_id: str,
) -> dict[str, Any]:
    try:
        jid = uuid.UUID(job_id)
    except (TypeError, ValueError) as exc:
        log.error("invalid job_id: %s", exc)
        return {"status": "error", "reason": f"invalid uuid: {exc}"}

    try:
        from bvphoenix.db.models import Patient, User
        from bvphoenix.db.models.jobs import Job
        from bvphoenix.db.session import SERVICE_SUBJECT, set_current_subject
        from bvphoenix.services import jobs as jobs_service
        from bvphoenix.services.document_bulk_update import (
            BulkUpdateItem,
            apply_bulk_update,
        )
    except ImportError as exc:
        log.exception("bvphoenix import failed: %s", exc)
        await mark_job_failed_raw(
            job_id,
            code="bvphoenix_import_failed",
            message=str(exc),
        )
        return {"status": "error", "reason": f"import: {exc}"}

    from sqlalchemy import select

    settings = get_settings()
    engine = make_async_engine(settings.database_url, pool_pre_ping=True)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as db:
            await set_current_subject(db, SERVICE_SUBJECT)
            await jobs_service.mark_running(db, jid)
            await db.commit()

            job = await jobs_service.get_job(db, jid)
            payload = job.input or {}
            patient_id = uuid.UUID(payload["patient_id"])
            raw_items = payload.get("items") or []

            patient = (
                await db.execute(select(Patient).where(Patient.id == patient_id))
            ).scalar_one_or_none()
            if patient is None:
                await jobs_service.mark_failed(
                    db,
                    jid,
                    error={"code": "patient_not_found", "message": str(patient_id)},
                )
                await db.commit()
                return {"status": "error", "reason": "patient_not_found"}

            owner = (
                await db.execute(select(User).where(User.subject_id == job.owner_subject_id))
            ).scalar_one_or_none()
            if owner is None:
                await jobs_service.mark_failed(
                    db,
                    jid,
                    error={"code": "owner_missing", "message": str(job.owner_subject_id)},
                )
                await db.commit()
                return {"status": "error", "reason": "owner_missing"}

            items: list[BulkUpdateItem] = []
            for raw in raw_items:
                fields_set: set[str] = set()
                for k in ("title", "document_type", "document_date", "text"):
                    if k in raw:
                        fields_set.add(k)
                items.append(
                    BulkUpdateItem(
                        document_id=uuid.UUID(raw["document_id"]),
                        title=raw.get("title"),
                        document_type=raw.get("document_type"),
                        document_date=(
                            date.fromisoformat(raw["document_date"])
                            if raw.get("document_date")
                            else None
                        ),
                        text=raw.get("text"),
                        etag=raw.get("etag"),
                        fields_set=frozenset(fields_set),
                    )
                )

            # The worker has no Request — pass a minimal stub object that
            # satisfies the few attributes the audit / versioning helpers
            # read (``state.agent_token``, ``state.share_link_id``, …).
            class _NullRequest:
                class _State:
                    agent_token = None
                    share_link_id = None
                    is_agent = False

                state = _State()
                client = None

                def __init__(self) -> None:
                    self.headers: dict[str, str] = {}

            result = await apply_bulk_update(
                db,
                patient=patient,
                user=owner,
                request=_NullRequest(),
                items=items,
                atomic=False,
                dry_run=False,
            )

            await db.commit()

            await jobs_service.mark_succeeded(
                db,
                jid,
                result_uri=None,
            )
            # Stash the outcome inside Job.input so the GET /api/jobs
            # endpoint can render it (no dedicated result column yet).
            payload_out = dict(payload)
            payload_out["result"] = result.to_jsonable()
            await db.execute(Job.__table__.update().where(Job.id == jid).values(input=payload_out))
            await db.commit()

            return {
                "status": "succeeded",
                "n_ok": result.n_ok,
                "n_error": result.n_error,
            }
    finally:
        await engine.dispose()
