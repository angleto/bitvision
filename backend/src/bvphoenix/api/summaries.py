"""Summaries API — cascading LLM summaries at series / study / patient scope.

Endpoints:

* ``POST /api/summaries/generate`` — mint (or reuse) a summary for a
  given ``(target_kind, target_id, lang)``. The service hashes the
  current inputs first: if a cached row matches we return it without
  calling the LLM. ``force_refresh=true`` skips that shortcut.
* ``GET  /api/summaries/{target_kind}/{target_id}`` — fetch the latest
  cached summary for a target (no LLM call). Returns 404 when none
  exists yet.

Generation dispatch:

* ``series`` runs synchronously — cheap enough that the viewer can
  block on it.
* ``study`` and ``patient`` are enqueued to the Arq worker by default
  so a long cascade doesn't tie up an HTTP connection. Callers can
  still force sync execution via ``mode="sync"`` when they need the
  result immediately (e.g. MCP tools).

Permissions: ``RUN_LLM`` on the relevant study (for series / study
scope) or ``WRITE_REPORT`` on the patient (for patient scope). Read
access follows ``READ_METADATA`` / ``READ_ANNOTATIONS``.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Literal

from arq import create_pool
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.auth import enforce_agent_patient_scope, optional_user, require_user
from bvphoenix.config import get_settings
from bvphoenix.db.models import ImagingStudy, Patient, Series, Summary, User
from bvphoenix.db.session import get_db
from bvphoenix.middleware.audit_dependency import AuditDep
from bvphoenix.services.arq_redis import redis_settings
from bvphoenix.services.permissions import (
    READ_METADATA,
    RUN_LLM,
    WRITE_REPORT,
    can,
    can_patient,
)
from bvphoenix.services.rate_limit import LLM_LIMIT, limiter
from bvphoenix.services.summarizer import (
    compute_source_hash,
    get_cached_summary,
    summarize_patient,
    summarize_series,
    summarize_study,
)

router = APIRouter(tags=["summaries"])


TargetKind = Literal["series", "study", "patient"]


# ---- schemas --------------------------------------------------------------


class GenerateSummaryIn(BaseModel):
    target_kind: TargetKind
    target_id: uuid.UUID
    lang: str = Field(default="en", min_length=2, max_length=8)
    force_refresh: bool = False
    # ``sync`` blocks the request until the summary is ready (series
    # default). ``async`` enqueues a worker job and returns 202 with the
    # arq job id. ``auto`` keeps the documented default: sync for series,
    # async for study / patient.
    mode: Literal["auto", "sync", "async"] = "auto"


class SummaryOut(BaseModel):
    id: str
    target_kind: str
    target_id: str
    lang: str
    text: str
    model_id: str | None
    provider: str | None
    token_usage: dict | None
    source_version_hash: str
    created_at: str
    updated_at: str


class SummaryEnqueuedOut(BaseModel):
    status: Literal["enqueued"]
    job_id: str | None
    target_kind: str
    target_id: str
    lang: str


class SummaryResponse(BaseModel):
    """Union response — ``summary`` is set on sync / cache hit,
    ``enqueued`` is set when the job was handed to the worker."""

    summary: SummaryOut | None = None
    enqueued: SummaryEnqueuedOut | None = None
    cached: bool = False


def _summary_out(s: Summary) -> SummaryOut:
    return SummaryOut(
        id=str(s.id),
        target_kind=s.target_kind,
        target_id=str(s.target_id),
        lang=s.lang,
        text=s.text,
        model_id=s.model_id,
        provider=s.provider,
        token_usage=s.token_usage,
        source_version_hash=s.source_version_hash,
        created_at=s.created_at.isoformat(),
        updated_at=s.updated_at.isoformat(),
    )


# ---- permission helpers ---------------------------------------------------


async def _check_generate_permission(
    db: AsyncSession,
    *,
    user: User,
    target_kind: str,
    target_id: uuid.UUID,
    request: Request,
) -> None:
    """Authorise a write-ish summarize call. Anyone who can ``run:llm``
    on the underlying study is allowed to mint series/study summaries;
    patient summaries additionally require ``write:report`` on the
    patient (they land in the fascicolo)."""
    if target_kind == "series":
        row = (
            await db.execute(
                select(Series, ImagingStudy)
                .join(ImagingStudy, ImagingStudy.id == Series.study_id)
                .where(Series.id == target_id)
            )
        ).first()
        if row is None:
            raise HTTPException(status_code=404, detail="series not found")
        _series, study = row
        enforce_agent_patient_scope(request, study.patient_id, scope="patient:images")
        if not await can(db, user=user, action=RUN_LLM, study=study):
            raise HTTPException(status_code=403, detail="run:llm not permitted")
        return
    if target_kind == "study":
        study = (
            await db.execute(select(ImagingStudy).where(ImagingStudy.id == target_id))
        ).scalar_one_or_none()
        if study is None:
            raise HTTPException(status_code=404, detail="study not found")
        enforce_agent_patient_scope(request, study.patient_id, scope="patient:images")
        if not await can(db, user=user, action=RUN_LLM, study=study):
            raise HTTPException(status_code=403, detail="run:llm not permitted")
        return
    if target_kind == "patient":
        patient = (
            await db.execute(select(Patient).where(Patient.id == target_id))
        ).scalar_one_or_none()
        if patient is None:
            raise HTTPException(status_code=404, detail="patient not found")
        enforce_agent_patient_scope(request, patient.id, scope="patient:read")
        if not await can_patient(db, user=user, action=WRITE_REPORT, patient=patient):
            raise HTTPException(status_code=403, detail="cannot summarise this patient")
        return
    raise HTTPException(status_code=400, detail="invalid target_kind")


async def _check_read_permission(
    db: AsyncSession,
    *,
    user: User | None,
    target_kind: str,
    target_id: uuid.UUID,
    request: Request,
) -> None:
    if target_kind == "series":
        row = (
            await db.execute(
                select(Series, ImagingStudy)
                .join(ImagingStudy, ImagingStudy.id == Series.study_id)
                .where(Series.id == target_id)
            )
        ).first()
        if row is None:
            raise HTTPException(status_code=404, detail="series not found")
        _series, study = row
        enforce_agent_patient_scope(request, study.patient_id, scope="patient:read")
        if not await can(db, user=user, action=READ_METADATA, study=study):
            raise HTTPException(status_code=404, detail="series not found")
        return
    if target_kind == "study":
        study = (
            await db.execute(select(ImagingStudy).where(ImagingStudy.id == target_id))
        ).scalar_one_or_none()
        if study is None:
            raise HTTPException(status_code=404, detail="study not found")
        enforce_agent_patient_scope(request, study.patient_id, scope="patient:read")
        if not await can(db, user=user, action=READ_METADATA, study=study):
            raise HTTPException(status_code=404, detail="study not found")
        return
    if target_kind == "patient":
        patient = (
            await db.execute(select(Patient).where(Patient.id == target_id))
        ).scalar_one_or_none()
        if patient is None:
            raise HTTPException(status_code=404, detail="patient not found")
        enforce_agent_patient_scope(request, patient.id, scope="patient:read")
        if not await can_patient(db, user=user, action=READ_METADATA, patient=patient):
            raise HTTPException(status_code=404, detail="patient not found")
        return
    raise HTTPException(status_code=400, detail="invalid target_kind")


# ---- endpoints ------------------------------------------------------------


@router.post(
    "/summaries/generate",
    response_model=SummaryResponse,
)
@limiter.limit(LLM_LIMIT)
async def generate_summary(
    request: Request,
    body: GenerateSummaryIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    audit: AuditDep,
) -> SummaryResponse:
    """Generate (or reuse) a summary at the requested scope.

    Series-level requests get an explicit hash-and-lookup fast path here
    so we can answer from cache without even opening a worker connection.
    ImagingStudy / patient requests delegate to the summarizer service, which
    owns the cascade (and its own per-level cache checks) — the service
    is authoritative because the hash of a study or patient summary
    depends on the freshness of its child summaries.
    """
    await _check_generate_permission(
        db,
        user=user,
        target_kind=body.target_kind,
        target_id=body.target_id,
        request=request,
    )

    if body.target_kind == "series" and not body.force_refresh:
        try:
            source_hash = await compute_source_hash(
                db, target_kind="series", target_id=body.target_id
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        cached = await get_cached_summary(
            db,
            target_kind="series",
            target_id=body.target_id,
            lang=body.lang,
            source_version_hash=source_hash,
        )
        if cached is not None:
            await audit.log(
                action="summary_cache_hit",
                actor_subject_id=user.subject_id,
                resource_kind="series",
                resource_id=body.target_id,
                metadata={"lang": body.lang},
            )
            return SummaryResponse(summary=_summary_out(cached), cached=True)

    sync = body.mode == "sync" or (body.mode == "auto" and body.target_kind == "series")

    if sync:
        try:
            if body.target_kind == "series":
                row = await summarize_series(
                    db,
                    body.target_id,
                    body.lang,
                    force_refresh=body.force_refresh,
                    user_subject_id=user.subject_id,
                )
            elif body.target_kind == "study":
                row = await summarize_study(
                    db,
                    body.target_id,
                    body.lang,
                    force_refresh=body.force_refresh,
                    user_subject_id=user.subject_id,
                )
            else:
                row = await summarize_patient(
                    db,
                    body.target_id,
                    body.lang,
                    force_refresh=body.force_refresh,
                    user_subject_id=user.subject_id,
                )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        await audit.log(
            action="summary_generate",
            actor_subject_id=user.subject_id,
            resource_kind=body.target_kind,
            resource_id=body.target_id,
            metadata={
                "lang": body.lang,
                "mode": "sync",
                "force_refresh": body.force_refresh,
                "model_id": row.model_id,
            },
        )
        return SummaryResponse(summary=_summary_out(row), cached=False)

    # Async path: hand off to the worker and return 202-style payload.
    # ``user_subject_id`` travels with the job so the worker can bill
    # the wallet of whoever originally requested the summary.
    settings = get_settings()
    redis = await create_pool(redis_settings(settings.redis_url))
    try:
        job = await redis.enqueue_job(
            "generate_summary",
            body.target_kind,
            str(body.target_id),
            body.lang,
            body.force_refresh,
            str(user.subject_id),
        )
    finally:
        await redis.close()

    await audit.log(
        action="summary_enqueue",
        actor_subject_id=user.subject_id,
        resource_kind=body.target_kind,
        resource_id=body.target_id,
        metadata={"lang": body.lang, "force_refresh": body.force_refresh},
    )

    return SummaryResponse(
        enqueued=SummaryEnqueuedOut(
            status="enqueued",
            job_id=job.job_id if job is not None else None,
            target_kind=body.target_kind,
            target_id=str(body.target_id),
            lang=body.lang,
        ),
        cached=False,
    )


@router.get(
    "/summaries/{target_kind}/{target_id}",
    response_model=SummaryOut,
)
async def get_latest_summary(
    request: Request,
    target_kind: TargetKind,
    target_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(optional_user)],
    lang: str = "en",
) -> SummaryOut:
    """Return the most recently generated summary for a target, without
    triggering any LLM work. 404 when no cached row exists yet."""
    await _check_read_permission(
        db,
        user=user,
        target_kind=target_kind,
        target_id=target_id,
        request=request,
    )
    row = (
        await db.execute(
            select(Summary)
            .where(
                Summary.target_kind == target_kind,
                Summary.target_id == target_id,
                Summary.lang == lang,
            )
            .order_by(Summary.updated_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="no summary cached")
    return _summary_out(row)


@router.delete(
    "/summaries/{target_kind}/{target_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_summaries(
    request: Request,
    target_kind: TargetKind,
    target_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    audit: AuditDep,
    lang: str | None = None,
) -> None:
    """Drop every cached summary at this target (optionally scoped by
    ``lang``). Used by UI "re-run summary" actions and by admin tools
    when the underlying fascicolo is recomposed out-of-band.
    """
    await _check_generate_permission(
        db,
        user=user,
        target_kind=target_kind,
        target_id=target_id,
        request=request,
    )
    stmt = select(Summary).where(Summary.target_kind == target_kind, Summary.target_id == target_id)
    if lang is not None:
        stmt = stmt.where(Summary.lang == lang)
    rows = (await db.execute(stmt)).scalars().all()
    for r in rows:
        await db.delete(r)
    await db.commit()
    await audit.log(
        action="summary_delete",
        actor_subject_id=user.subject_id,
        resource_kind=target_kind,
        resource_id=target_id,
        metadata={"lang": lang, "deleted": len(rows)},
    )
