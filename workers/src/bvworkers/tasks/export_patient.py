"""Arq task: build and upload a Fascicolo (Health Record) ZIP export.

Wraps :func:`bvphoenix.services.patient_export.build_export_zip` so
the heavy work runs out of the HTTP request and the result survives
client disconnect (DESIGN.md §11.9 step 2).

Lifecycle:

1. Mark Job ``running``.
2. Resolve the user + patient inside the worker's session
   (the API has already authorized the caller, but we re-check
   READ_METADATA here so a revoked grant in the meantime does not
   leak data).
3. Build the ZIP.
4. Upload to S3 under the canonical export key.
5. Stamp ``Job.result_uri = "s3://<bucket>/<key>"``. The API layer
   turns this into a fresh presigned URL on every read.
6. Mark Job ``succeeded`` and append a ``patient_export`` audit row
   so the trail matches what the legacy sync route used to write.

On any unhandled exception the row is flipped to ``failed`` with a
structured error payload; nothing in this task is fatal to the
worker process.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from bvworkers.config import get_settings

log = logging.getLogger(__name__)


async def export_patient_zip(
    ctx: dict,  # type: ignore[type-arg]
    job_id: str,
    patient_id: str,
    owner_subject_id: str,
    canonical_input_json: str,
) -> dict[str, Any]:
    """Arq entry point.

    Args:
        ctx: arq context.
        job_id: Job row UUID (string).
        patient_id: Patient UUID (string).
        owner_subject_id: Subject UUID of the requesting user.
        canonical_input_json: JSON-encoded dict that includes
            ``"includes": [...]`` matching the original request.
    """
    try:
        jid = uuid.UUID(job_id)
        pid = uuid.UUID(patient_id)
        sid = uuid.UUID(owner_subject_id)
    except (TypeError, ValueError) as exc:
        log.error("invalid uuid argument: %s", exc)
        return {"status": "error", "reason": f"invalid uuid: {exc}"}

    try:
        canonical_input = json.loads(canonical_input_json or "{}")
    except json.JSONDecodeError as exc:
        return {"status": "error", "reason": f"bad input json: {exc}"}

    settings = get_settings()

    try:
        from bvphoenix.db.models import Patient, User
        from bvphoenix.db.session import SERVICE_SUBJECT, set_current_subject
        from bvphoenix.services import jobs as jobs_service
        from bvphoenix.services.audit import log_action
        from bvphoenix.services.patient_export import (
            stream_export_to_s3,
        )
        from bvphoenix.services.permissions import (
            READ_METADATA,
            can_patient,
        )
    except ImportError as exc:
        log.exception("bvphoenix not importable from worker: %s", exc)
        return {"status": "error", "reason": f"bvphoenix import: {exc}"}

    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as db:
            # SERVICE_SUBJECT lets the worker bypass RLS — the API has
            # already authorised the caller. We still re-check the
            # patient grant via ``can_patient`` below so a revocation
            # between enqueue and run-time is honoured.
            await set_current_subject(db, SERVICE_SUBJECT)
            await jobs_service.mark_running(db, jid)
            await db.commit()

            patient = (
                await db.execute(select(Patient).where(Patient.id == pid))
            ).scalar_one_or_none()
            if patient is None:
                await jobs_service.mark_failed(
                    db,
                    jid,
                    error={
                        "code": "patient_not_found",
                        "message": f"patient {pid} not found",
                    },
                )
                await db.commit()
                return {"status": "error", "reason": "patient not found"}

            user = (
                await db.execute(select(User).where(User.subject_id == sid))
            ).scalar_one_or_none()
            if user is None:
                await jobs_service.mark_failed(
                    db,
                    jid,
                    error={
                        "code": "user_not_found",
                        "message": f"user {sid} not found",
                    },
                )
                await db.commit()
                return {"status": "error", "reason": "user not found"}

            if not await can_patient(db, user=user, action=READ_METADATA, patient=patient):
                await jobs_service.mark_failed(
                    db,
                    jid,
                    error={
                        "code": "permission_denied",
                        "message": "READ on patient revoked",
                    },
                )
                await db.commit()
                return {"status": "error", "reason": "permission denied"}

            includes = set(canonical_input.get("includes") or [])
            await jobs_service.update_progress(db, jid, stage="building_zip")
            await db.commit()

            # Streaming pipeline: stream-zip iterator → S3 multipart
            # upload, with periodic progress ticks. Memory stays at
            # the multipart part ceiling (~8 MiB) instead of holding
            # the full archive in RAM (legacy build_export_zip path).
            #
            # The progress callback uses a SHORT-LIVED separate
            # session per tick: the main ``db`` is busy inside the
            # streaming pipeline (running queries in the metadata
            # pass and held by the ``async with`` block), and writing
            # to it from the ticker thread would interleave commits
            # unsafely.
            from sqlalchemy.ext.asyncio import (
                AsyncSession as _AsyncSession,  # local — not exported
            )

            async def _progress(done: int, total: int, stage: str) -> None:
                async with _AsyncSession(engine, expire_on_commit=False) as ps:
                    await set_current_subject(ps, SERVICE_SUBJECT)
                    await jobs_service.update_progress(
                        ps,
                        jid,
                        progress_done=done,
                        progress_total=total,
                        stage=stage,
                    )
                    await ps.commit()

            # Optional scope filter (folder / bulk download). Stored
            # as a list of UUID strings inside the canonical_input so
            # the dedup key remains stable across reruns. Absent =
            # whole-patient export.
            raw_studies = canonical_input.get("scope_study_ids") or None
            raw_docs = canonical_input.get("scope_document_ids") or None
            scope_study_ids = (
                {uuid.UUID(x) for x in raw_studies} if raw_studies is not None else None
            )
            scope_document_ids = {uuid.UUID(x) for x in raw_docs} if raw_docs is not None else None

            # ``tree`` mirrors the patient's curated Folder structure
            # with human-readable names; ``flat`` is the legacy
            # UUID-keyed layout. Stored in canonical_input so it is part
            # of the dedup hash (a flat and a tree export never collide).
            layout = canonical_input.get("layout") or "flat"

            try:
                bucket, key, manifest, size_bytes = await stream_export_to_s3(
                    db,
                    user,
                    patient,
                    includes,
                    job_id=jid,
                    on_progress=_progress,
                    scope_study_ids=scope_study_ids,
                    scope_document_ids=scope_document_ids,
                    layout=layout,
                )
            except Exception as exc:
                log.exception("zip build/upload failed for job %s", jid)
                await jobs_service.mark_failed(
                    db,
                    jid,
                    error={
                        "code": "build_failed",
                        "message": str(exc),
                    },
                )
                await db.commit()
                return {"status": "error", "reason": "build failed"}

            result_uri = f"s3://{bucket}/{key}"
            await jobs_service.mark_succeeded(db, jid, result_uri=result_uri)
            await db.commit()

        # Audit row goes through its own session inside log_action;
        # outside the engine block so a failure here cannot rollback
        # the success transition above.
        await log_action(
            actor_subject_id=sid,
            action="patient_export",
            resource_kind="patient",
            resource_id=pid,
            metadata={
                "job_id": str(jid),
                "includes": sorted(includes),
                "counts": manifest["counts"],
                "size_bytes": size_bytes,
                "s3_key": key,
            },
        )
        return {
            "status": "ok",
            "job_id": str(jid),
            "result_uri": result_uri,
            "size_bytes": size_bytes,
        }
    finally:
        await engine.dispose()
