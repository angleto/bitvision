"""Arq task: run the care-phase classifier off the request path.

Triggered by ``POST /api/patients/{patient_id}/care-phases:propose``
when the caller passes ``async=true``. The task opens its own DB
session (workers do not share the FastAPI engine), runs the
classifier (``bvphoenix.services.care_phase_classifier``), and
persists the proposal. Idempotency is enforced by the calling Job
row on Redis side; cache-hit by ``input_hash`` is enforced by the
classifier itself, so a duplicate enqueue with the same patient
events returns the cached proposal without a second LLM call.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from bvworkers.config import get_settings

log = logging.getLogger(__name__)


async def propose_care_phases(
    ctx: dict,  # type: ignore[type-arg]
    patient_id: str,
    actor_subject_id: str | None = None,
    lang: str = "it",
) -> dict:
    """Arq entry point.

    Args:
        ctx: arq context (unused for now; kept for parity with arq's
            task signature).
        patient_id: UUID string of the patient.
        actor_subject_id: subject id to attribute the proposal to in
            the audit log; ``None`` for unattended runs (e.g. nightly
            cron). The proposal row still records ``model_id``.
        lang: ``"it"`` or ``"en"``.

    Returns:
        ``{"status": "ok", "proposal_id": "...", "n_phases": N,
        "n_assignments": M, "cached": bool}`` on success.
        ``{"status": "error", "reason": "..."}`` on failure (the row in
        ``jobs`` will be marked failed by arq's lifecycle hook).
    """
    try:
        pid = uuid.UUID(patient_id)
    except (TypeError, ValueError):
        return {"status": "error", "reason": f"invalid patient_id: {patient_id!r}"}

    actor_uuid: uuid.UUID | None = None
    if actor_subject_id:
        try:
            actor_uuid = uuid.UUID(actor_subject_id)
        except (TypeError, ValueError):
            return {
                "status": "error",
                "reason": f"invalid actor_subject_id: {actor_subject_id!r}",
            }

    # Lazy import so the worker process can boot even when bvphoenix is
    # not on PYTHONPATH (kept as a safety net mirroring
    # ``generate_summary.py``).
    try:
        from bvphoenix.services.care_phase_classifier import propose_for_patient
    except ImportError as exc:  # pragma: no cover - deployment safety net
        log.warning(
            "care_phase_propose: backend not importable, skipping (err=%s)", exc
        )
        return {"status": "error", "reason": "backend not importable"}

    settings = get_settings()
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    try:
        async with Session() as session:
            out = await propose_for_patient(
                patient_id=pid,
                actor_id=actor_uuid,
                lang=lang,
                db=session,
            )
            return {
                "status": "ok",
                "proposal_id": str(out.proposal_id),
                "n_phases": len(out.payload.phases),
                "n_assignments": len(out.payload.assignments),
                "cached": out.cached,
                "model_id": out.model_id,
            }
    finally:
        await engine.dispose()
