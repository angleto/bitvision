"""Arq task: build and upload a single-study DICOM ZIP export.

Sibling of :mod:`export_patient`. Where the patient task walks the
whole fascicolo and accepts an ``includes`` set, the study task is
narrower: scope is one ``ImagingStudy``, the include set is
implicitly ``{"dicom"}``, and the permission gate is
``READ_PIXELS`` on the study (stricter than the patient
``READ_METADATA``).

Lifecycle mirrors :func:`export_patient_zip`:

1. Mark Job ``running``.
2. Resolve user + study + parent patient inside the worker session.
3. Re-check ``can(READ_PIXELS)`` on the study so a grant revoked
   between enqueue and execution is honoured.
4. Reuse :func:`stream_export_to_s3` with
   ``includes={"dicom"}`` and ``scope_study_ids={study.id}`` — the
   service layer already knows how to filter the export plan to a
   single study, so we get streaming + multipart + progress for
   free without duplicating zip-building logic.
5. Stamp ``Job.result_uri = "s3://<bucket>/<key>"`` and mark
   ``succeeded``.
6. Audit ``study_export``.

Failure path flips the row to ``failed`` with a structured error
payload; nothing here can crash the worker process.
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


async def export_study_zip(
    ctx: dict,  # type: ignore[type-arg]
    job_id: str,
    study_id: str,
    owner_subject_id: str,
    canonical_input_json: str,
) -> dict[str, Any]:
    """Arq entry point.

    Args:
        ctx: arq context.
        job_id: Job row UUID (string).
        study_id: ImagingStudy UUID (string).
        owner_subject_id: Subject UUID of the requesting user.
        canonical_input_json: JSON-encoded dict (currently unused
            beyond enabling future per-call options like
            de-identification toggles); kept in the signature for
            symmetry with :func:`export_patient_zip` and to keep
            the dedup hash extensible.
    """
    try:
        jid = uuid.UUID(job_id)
        sid_uuid = uuid.UUID(study_id)
        sub = uuid.UUID(owner_subject_id)
    except (TypeError, ValueError) as exc:
        log.error("invalid uuid argument: %s", exc)
        return {"status": "error", "reason": f"invalid uuid: {exc}"}

    try:
        canonical_input = json.loads(canonical_input_json or "{}")
    except json.JSONDecodeError as exc:
        return {"status": "error", "reason": f"bad input json: {exc}"}
    # Deidentify is the only canonical-input flag the study task
    # honours today. Branching on it here means the dedup hash
    # (computed from canonical_input by the API layer) keeps
    # identifying and pseudonymized archives at distinct ZIPs.
    deidentify_dicom = bool(canonical_input.get("deidentify"))
    # ``tree`` files the study under its curated Folder path with a
    # human-readable name; ``flat`` keeps the legacy UUID-keyed layout.
    layout = canonical_input.get("layout") or "flat"

    settings = get_settings()

    try:
        from bvphoenix.db.models import ImagingStudy, Patient, User
        from bvphoenix.db.session import SERVICE_SUBJECT, set_current_subject
        from bvphoenix.services import jobs as jobs_service
        from bvphoenix.services.audit import log_action
        from bvphoenix.services.patient_export import (
            ExportCancelledError,
            stream_export_to_s3,
        )
        from bvphoenix.services.permissions import READ_PIXELS, can
    except ImportError as exc:
        log.exception("bvphoenix not importable from worker: %s", exc)
        return {"status": "error", "reason": f"bvphoenix import: {exc}"}

    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as db:
            await set_current_subject(db, SERVICE_SUBJECT)
            await jobs_service.mark_running(db, jid)
            await db.commit()

            study = (
                await db.execute(select(ImagingStudy).where(ImagingStudy.id == sid_uuid))
            ).scalar_one_or_none()
            if study is None:
                await jobs_service.mark_failed(
                    db,
                    jid,
                    error={"code": "study_not_found", "message": f"study {sid_uuid} not found"},
                )
                await db.commit()
                return {"status": "error", "reason": "study not found"}

            patient = (
                await db.execute(select(Patient).where(Patient.id == study.patient_id))
            ).scalar_one_or_none()
            if patient is None:
                await jobs_service.mark_failed(
                    db,
                    jid,
                    error={
                        "code": "patient_not_found",
                        "message": f"patient {study.patient_id} not found",
                    },
                )
                await db.commit()
                return {"status": "error", "reason": "patient not found"}

            user = (
                await db.execute(select(User).where(User.subject_id == sub))
            ).scalar_one_or_none()
            if user is None:
                await jobs_service.mark_failed(
                    db,
                    jid,
                    error={"code": "user_not_found", "message": f"user {sub} not found"},
                )
                await db.commit()
                return {"status": "error", "reason": "user not found"}

            if not await can(db, user=user, action=READ_PIXELS, study=study):
                await jobs_service.mark_failed(
                    db,
                    jid,
                    error={"code": "permission_denied", "message": "READ_PIXELS revoked"},
                )
                await db.commit()
                return {"status": "error", "reason": "permission denied"}

            await jobs_service.update_progress(db, jid, stage="building_zip")
            await db.commit()

            # Same short-lived-session ticker pattern as
            # ``export_patient_zip``: the streaming pipeline holds
            # the main session, so progress writes go through a
            # separate session per tick to avoid interleaved commits.
            from sqlalchemy.ext.asyncio import (
                AsyncSession as _AsyncSession,  # local, not exported
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

            # Cooperative cancellation: the streaming pipeline runs an
            # async ticker that calls this every ~1s and propagates the
            # result to the sync member generator via a shared bool.
            # User clicks Cancel → DELETE /api/jobs/{id} flips Job.status
            # to 'cancelled' → next poll picks it up → ExportCancelledError
            # propagates → upload_iter aborts the multipart upload. No
            # orphan parts, no nested event loops here.
            from bvphoenix.db.models import Job as _JobModel
            from sqlalchemy import select as _select

            async def _should_cancel() -> bool:
                async with _AsyncSession(engine, expire_on_commit=False) as ps:
                    await set_current_subject(ps, SERVICE_SUBJECT)
                    row = (
                        await ps.execute(_select(_JobModel.status).where(_JobModel.id == jid))
                    ).scalar_one_or_none()
                    return row == "cancelled"

            # Land the artifact under ``exports/study/<job_id>/study-<uid>.zip``
            # so the browser's suggested filename reflects the scope
            # (the Content-Disposition header on /jobs/{id}/result_download
            # is derived from the S3 key's last path component).
            study_label = (study.study_description or study.study_instance_uid).replace(
                "/", "_"
            ).replace("\\", "_")[:80] or str(study.id)
            # Deidentified ZIPs are stored under a distinct sub-prefix
            # so a list-bucket eyeball can tell at a glance which
            # archive carries PHI and which doesn't, and so a future
            # ``deidentify`` toggle on an existing share never
            # silently re-uses an identifying artifact.
            scrub_marker = "deid-" if deidentify_dicom else ""
            study_filename = f"study-{scrub_marker}{study_label}-{study.id}.zip"
            study_key = f"exports/study/{jid}/{study_filename}"

            try:
                bucket, key, manifest, size_bytes = await stream_export_to_s3(
                    db,
                    user,
                    patient,
                    {"dicom"},
                    job_id=jid,
                    on_progress=_progress,
                    scope_study_ids={study.id},
                    s3_key_override=study_key,
                    should_cancel=_should_cancel,
                    deidentify_dicom=deidentify_dicom,
                    layout=layout,
                )
            except ExportCancelledError:
                # User clicked Cancel mid-stream. Job.status was already
                # flipped to 'cancelled' by DELETE /api/jobs/{id}; the
                # multipart upload is aborted by upload_iter's context
                # manager when the iterator stops, so no orphan parts
                # remain in the bucket. Just exit cleanly.
                log.info("study export cancelled by caller (job=%s)", jid)
                return {"status": "cancelled", "job_id": str(jid)}
            except Exception as exc:
                log.exception("zip build/upload failed for job %s", jid)
                await jobs_service.mark_failed(
                    db,
                    jid,
                    error={"code": "build_failed", "message": str(exc)},
                )
                await db.commit()
                return {"status": "error", "reason": "build failed"}

            result_uri = f"s3://{bucket}/{key}"
            await jobs_service.mark_succeeded(db, jid, result_uri=result_uri)
            await db.commit()

        await log_action(
            actor_subject_id=sub,
            action="study_export",
            resource_kind="study",
            resource_id=sid_uuid,
            metadata={
                "job_id": str(jid),
                "patient_id": str(patient.id),
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
