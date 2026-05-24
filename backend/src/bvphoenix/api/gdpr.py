"""GDPR endpoints — consents, data export, right to erasure.

See docs/security-gdpr.md for the full regulatory mapping. Endpoints:

* ``POST /api/gdpr/consent``        — record a consent change.
* ``GET  /api/gdpr/consents``       — list the caller's active consents.
* ``POST /api/gdpr/erasure-request``— open an Art. 17 erasure request.
* ``POST /api/gdpr/export``         — enqueue a Job that builds and
  uploads a ZIP of all user data (Art. 20 data portability). Poll
  ``GET /api/jobs/{id}`` for progress and the presigned download URL.

All endpoints require authentication — the user is always the subject of
their own GDPR workflow. Admin-scoped review of erasure requests lives
on dedicated admin endpoints (out of scope for the user-facing surface
delivered here).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from arq import create_pool
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.api.jobs import JobOut, cap_exceeded_to_http
from bvphoenix.auth import require_user
from bvphoenix.config import get_settings
from bvphoenix.db.models import (
    AuditLog,
    Consent,
    DataErasureRequest,
    User,
)
from bvphoenix.db.models.gdpr import (
    CONSENT_KINDS,
    ERASURE_SCOPES,
    REQUIRED_CONSENT_KINDS,
)
from bvphoenix.db.session import get_db
from bvphoenix.services import jobs as jobs_service
from bvphoenix.services.arq_redis import redis_settings
from bvphoenix.services.erasure import _user_has_legal_hold, execute_erasure

router = APIRouter(prefix="/gdpr", tags=["gdpr"])


JOB_KIND_GDPR_EXPORT = "gdpr_export"


# ---- Pydantic schemas ----


class ConsentChangeIn(BaseModel):
    kind: str = Field(..., min_length=1, max_length=64)
    granted: bool


class ConsentOut(BaseModel):
    kind: str
    granted: bool
    granted_at: str | None
    revoked_at: str | None


class ErasureRequestIn(BaseModel):
    scope: str = Field(default="self")
    reason: str | None = Field(default=None, max_length=4000)


class ErasureRequestOut(BaseModel):
    id: str
    scope: str
    status: str
    reason: str | None
    requested_at: str
    completed_at: str | None


# ---- Consent endpoints ----


@router.post("/consent", response_model=ConsentOut, status_code=status.HTTP_201_CREATED)
async def record_consent(
    body: ConsentChangeIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
) -> ConsentOut:
    if body.kind not in CONSENT_KINDS:
        raise HTTPException(
            status_code=422,
            detail=f"unknown consent kind; must be one of {list(CONSENT_KINDS)}",
        )

    now = datetime.now(UTC)

    if body.granted:
        # Close any still-open prior record so at most one (user, kind) is active.
        existing = (
            (
                await db.execute(
                    select(Consent)
                    .where(
                        Consent.user_subject_id == user.subject_id,
                        Consent.kind == body.kind,
                        Consent.revoked_at.is_(None),
                    )
                    .order_by(Consent.granted_at.desc())
                )
            )
            .scalars()
            .first()
        )
        if existing is not None:
            return ConsentOut(
                kind=existing.kind,
                granted=True,
                granted_at=existing.granted_at.isoformat(),
                revoked_at=None,
            )
        row = Consent(user_subject_id=user.subject_id, kind=body.kind)
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return ConsentOut(
            kind=row.kind,
            granted=True,
            granted_at=row.granted_at.isoformat(),
            revoked_at=None,
        )

    # Revoke — flip the active row(s).
    rows = (
        (
            await db.execute(
                select(Consent).where(
                    Consent.user_subject_id == user.subject_id,
                    Consent.kind == body.kind,
                    Consent.revoked_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        row.revoked_at = now
    await db.commit()
    return ConsentOut(
        kind=body.kind,
        granted=False,
        granted_at=None,
        revoked_at=now.isoformat(),
    )


@router.get("/consents", response_model=list[ConsentOut])
async def list_consents(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
) -> list[ConsentOut]:
    rows = (
        (
            await db.execute(
                select(Consent)
                .where(Consent.user_subject_id == user.subject_id)
                .order_by(Consent.kind, Consent.granted_at.desc())
            )
        )
        .scalars()
        .all()
    )

    # Collapse history → current state per kind.
    latest: dict[str, Consent] = {}
    for row in rows:
        if row.kind not in latest:
            latest[row.kind] = row

    out: list[ConsentOut] = []
    for kind in CONSENT_KINDS:
        row = latest.get(kind)
        if row is None:
            # Required consents are accepted implicitly at account
            # creation (the signup flow gates on them). Surface the
            # implicit grant so the UI checkbox reflects reality and
            # the user is not nudged to "accept the ToS" while
            # already using the platform. Explicit rows are persisted
            # for new signups; for users predating that, the
            # synthesised ``granted=true`` is the honest answer.
            granted_default = kind in REQUIRED_CONSENT_KINDS
            out.append(
                ConsentOut(kind=kind, granted=granted_default, granted_at=None, revoked_at=None)
            )
        else:
            granted = row.revoked_at is None
            out.append(
                ConsentOut(
                    kind=kind,
                    granted=granted,
                    granted_at=row.granted_at.isoformat() if granted else None,
                    revoked_at=row.revoked_at.isoformat() if row.revoked_at else None,
                )
            )
    return out


# ---- Erasure request endpoint ----


@router.post(
    "/erasure-request",
    response_model=ErasureRequestOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_erasure_request(
    body: ErasureRequestIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
) -> ErasureRequestOut:
    if body.scope not in ERASURE_SCOPES:
        raise HTTPException(
            status_code=422,
            detail=f"invalid scope; must be one of {list(ERASURE_SCOPES)}",
        )

    req = DataErasureRequest(
        user_subject_id=user.subject_id,
        scope=body.scope,
        reason=body.reason,
    )
    db.add(req)
    await db.flush()

    # Auto-execute when the user is erasing their own account and there
    # is no legal hold. Admin review remains available for non-self
    # scopes and for deployments that want to keep a human in the loop
    # (flip a config knob in the future; left explicit here for clarity).
    if body.scope == "self" and not await _user_has_legal_hold(db, user.subject_id):
        await execute_erasure(db, request=req)

    # Audit trail entry — keep the actor populated *before* the user is
    # anonymised so the action is traceable.
    db.add(
        AuditLog(
            actor_subject_id=user.subject_id if req.status != "completed" else None,
            action="gdpr.erasure_requested",
            resource_kind="user",
            resource_id=user.subject_id,
            metadata_={
                "request_id": str(req.id),
                "scope": req.scope,
                "auto_executed": req.status == "completed",
            },
        )
    )

    await db.commit()
    await db.refresh(req)
    return ErasureRequestOut(
        id=str(req.id),
        scope=req.scope,
        status=req.status,
        reason=req.reason,
        requested_at=req.requested_at.isoformat(),
        completed_at=req.completed_at.isoformat() if req.completed_at else None,
    )


# ---- Data export (Art. 20 portability) ----


@router.post("/export", response_model=JobOut, status_code=status.HTTP_202_ACCEPTED)
async def export_user_data_async(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
) -> JobOut:
    """Enqueue a GDPR Art. 20 data export Job. Returns 202 with the
    job descriptor; poll ``GET /api/jobs/{id}`` for progress and the
    presigned download URL.

    Idempotency: a single user is the entire scope, so concurrent
    retries dedup to one in-flight job. After the job lands a
    terminal state, a fresh request creates a new job (e.g. to
    refresh data added since the previous export)."""
    canonical_input: dict[str, Any] = {"version": 1}

    try:
        result = await jobs_service.enqueue_or_get(
            db,
            kind=JOB_KIND_GDPR_EXPORT,
            owner_subject_id=user.subject_id,
            canonical_input=canonical_input,
            scope_ids=(user.subject_id,),
            is_admin=bool(user.is_admin),
        )
    except jobs_service.JobCapExceededError as e:
        raise cap_exceeded_to_http(e) from e

    if not result.deduped:
        try:
            settings = get_settings()
            redis = await create_pool(redis_settings(settings.redis_url))
            arq_handle = await redis.enqueue_job(
                "export_gdpr_zip",
                str(result.job.id),
                str(user.subject_id),
            )
            await redis.close()
            if arq_handle is not None:
                await jobs_service.set_arq_job_id(db, result.job.id, arq_handle.job_id)
        except Exception as exc:
            await jobs_service.mark_failed(
                db,
                result.job.id,
                error={"code": "enqueue_failed", "message": str(exc)},
            )
            await db.commit()
            raise HTTPException(
                status_code=503,
                detail={
                    "error": "enqueue_failed",
                    "hint": "worker queue unavailable; try again shortly",
                },
            ) from exc

    await db.commit()
    return JobOut.model_validate(result.job)
