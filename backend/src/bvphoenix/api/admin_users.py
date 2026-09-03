"""Admin dashboard endpoints — manage users, quotas, locks.

Surface area:

* ``GET    /api/admin/users`` — paginated list with search.
* ``GET    /api/admin/users/{subject_id}`` — single-user detail with
  current storage usage + active job count.
* ``PATCH  /api/admin/users/{subject_id}`` — edit overrides
  (storage_quota_bytes, max_concurrent_jobs, is_active,
  blocked_reason, is_admin).
* ``DELETE /api/admin/users/{subject_id}`` — hard-delete the user
  row. CASCADE on ``subjects.id`` cleans up grants and owned
  resources; this is destructive and reserved for true cleanups
  (test accounts, GDPR closures). Day-to-day deactivation should use
  ``PATCH … is_active=false`` instead.

All endpoints require ``require_admin`` so non-admins get 403.
Audited via :class:`AuditDep`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Query,
    Request,
    status,
)
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.auth.deps import require_admin
from bvphoenix.config import Settings, get_settings
from bvphoenix.db.models import Job, User
from bvphoenix.db.session import get_db
from bvphoenix.middleware.audit_dependency import AuditDep
from bvphoenix.middleware.problem_details import problem
from bvphoenix.services.account_provisioning import start_email_verification
from bvphoenix.services.credits import get_balance_cents
from bvphoenix.services.email_delivery import (
    run_in_background as run_email_delivery,
)
from bvphoenix.services.quota import (
    STORAGE_FREE_TIER_BYTES,
    get_user_storage_usage,
)

router = APIRouter(tags=["admin"])


class AdminUserOut(BaseModel):
    subject_id: str
    email: str
    is_admin: bool
    is_active: bool
    blocked_at: str | None
    blocked_reason: str | None
    email_verified_at: str | None
    mfa_enabled_at: str | None
    storage_quota_bytes: int | None = Field(
        description=(
            "NULL means the user inherits the platform default "
            "``STORAGE_FREE_TIER_BYTES``. The dashboard renders "
            "``effective_storage_quota_bytes`` for the actual value."
        ),
    )
    effective_storage_quota_bytes: int
    storage_used_bytes: int
    max_concurrent_jobs: int | None
    active_job_count: int
    wallet_balance_cents: int = Field(
        description=(
            "Current LLM wallet balance for this user (cents). Surfaces "
            "alongside storage so the admin can see both quotas at once."
        ),
    )
    created_at: str


class AdminUserListOut(BaseModel):
    items: list[AdminUserOut]
    total: int
    limit: int
    offset: int


class AdminUserUpdateIn(BaseModel):
    """All fields optional — only the keys present are applied. Pass
    ``storage_quota_bytes=null`` to clear the override and revert to
    the platform default."""

    storage_quota_bytes: int | None = Field(
        default=None,
        ge=0,
        description=(
            "Per-user T1+T2 cap in bytes. Set to ``null`` to clear "
            "the override and fall back to the platform default."
        ),
    )
    max_concurrent_jobs: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Per-user queued/running job cap. Set to ``null`` to "
            "fall back to ``BVP_JOB_MAX_PER_OWNER``."
        ),
    )
    is_active: bool | None = Field(
        default=None,
        description=(
            "Soft account lock. ``False`` blocks the user from "
            "logging in or making any new write. The lock can be "
            "lifted later by sending ``true``."
        ),
    )
    blocked_reason: str | None = Field(
        default=None,
        max_length=255,
        description="Operator note shown to the blocked user on next login.",
    )
    is_admin: bool | None = Field(
        default=None,
        description=(
            "Promote / demote admin role. The acting admin cannot "
            "demote themselves to avoid lockouts."
        ),
    )
    # Whether to clear the override fields explicitly. The bare
    # ``storage_quota_bytes=null`` syntax already does this; the flag
    # exists so a frontend that always sends the full payload can be
    # explicit instead of guessing default-vs-null.
    clear_storage_quota: bool = Field(default=False)
    clear_max_concurrent_jobs: bool = Field(default=False)


async def _resolve_user_or_404(db: AsyncSession, subject_id: uuid.UUID) -> User:
    user = (
        await db.execute(select(User).where(User.subject_id == subject_id))
    ).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")
    return user


async def _active_job_count(db: AsyncSession, subject_id: uuid.UUID) -> int:
    return int(
        (
            await db.execute(
                select(func.count(Job.id)).where(
                    Job.owner_subject_id == subject_id,
                    Job.status.in_(("queued", "running")),
                )
            )
        ).scalar_one()
        or 0
    )


async def _to_out(db: AsyncSession, user: User) -> AdminUserOut:
    usage = await get_user_storage_usage(db, user.subject_id)
    active_jobs = await _active_job_count(db, user.subject_id)
    balance = await get_balance_cents(db, user_subject_id=user.subject_id)
    return AdminUserOut(
        subject_id=str(user.subject_id),
        email=user.email,
        is_admin=user.is_admin,
        is_active=user.is_active,
        blocked_at=user.blocked_at.isoformat() if user.blocked_at else None,
        blocked_reason=user.blocked_reason,
        email_verified_at=(user.email_verified_at.isoformat() if user.email_verified_at else None),
        mfa_enabled_at=(user.mfa_enabled_at.isoformat() if user.mfa_enabled_at else None),
        storage_quota_bytes=user.storage_quota_bytes,
        effective_storage_quota_bytes=usage.quota_bytes,
        storage_used_bytes=usage.used_bytes,
        max_concurrent_jobs=user.max_concurrent_jobs,
        active_job_count=active_jobs,
        wallet_balance_cents=balance,
        created_at=user.created_at.isoformat(),
    )


@router.get("/admin/users", response_model=AdminUserListOut)
async def list_users(
    db: Annotated[AsyncSession, Depends(get_db)],
    admin: Annotated[User, Depends(require_admin)],
    audit: AuditDep,
    q: str | None = Query(None, max_length=120, description="Email substring filter"),
    blocked: bool | None = Query(None, description="True = only blocked users"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> AdminUserListOut:
    base = select(User)
    if q:
        like = f"%{q.lower()}%"
        base = base.where(or_(func.lower(User.email).like(like)))
    if blocked is True:
        base = base.where(User.is_active == False)  # noqa: E712 — SQL comparison
    if blocked is False:
        base = base.where(User.is_active == True)  # noqa: E712

    total = int(
        (
            await db.execute(select(func.count()).select_from(base.distinct().subquery()))
        ).scalar_one()
    )
    rows = (
        (await db.execute(base.order_by(User.created_at.desc()).limit(limit).offset(offset)))
        .scalars()
        .all()
    )
    items: list[AdminUserOut] = []
    for u in rows:
        items.append(await _to_out(db, u))
    await audit.log(
        action="admin_user_list",
        actor_subject_id=admin.subject_id,
        resource_kind="user",
        metadata={"q": q, "blocked": blocked, "count": len(items)},
    )
    return AdminUserListOut(items=items, total=total, limit=limit, offset=offset)


@router.get("/admin/users/{subject_id}", response_model=AdminUserOut)
async def get_user(
    subject_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    admin: Annotated[User, Depends(require_admin)],
    audit: AuditDep,
) -> AdminUserOut:
    user = await _resolve_user_or_404(db, subject_id)
    out = await _to_out(db, user)
    await audit.log(
        action="admin_user_view",
        actor_subject_id=admin.subject_id,
        resource_kind="user",
        resource_id=user.subject_id,
    )
    return out


class VerificationEmailOut(BaseModel):
    """Result of re-sending a verification email to one account."""

    subject_id: str
    email: str
    delivery_id: str


@router.post(
    "/admin/users/{subject_id}/verification-email",
    response_model=VerificationEmailOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def resend_user_verification_email(
    subject_id: uuid.UUID,
    background_tasks: BackgroundTasks,
    db: Annotated[AsyncSession, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    admin: Annotated[User, Depends(require_admin)],
    audit: AuditDep,
) -> VerificationEmailOut:
    """Re-send the verification email for one account, by subject id.

    ``POST /api/auth/resend-verification`` already does this and stays
    the self-service path, but it is unauthenticated and therefore
    deliberately blind: it answers 202 whether or not the address exists,
    so an operator helping somebody who is locked out learns nothing
    about whether anything happened. This one is admin-only, addresses
    the account by id rather than by a typed address, and returns the
    ledger row so the outcome can be followed
    (``SELECT status, error_code FROM email_deliveries WHERE id = ...``).

    It does **not** mark the address verified. Only the recipient
    clicking the link does that, which is the whole point of the
    verification: an operator attesting on somebody else's behalf would
    be attesting to control of a mailbox they do not hold.
    """
    user = await _resolve_user_or_404(db, subject_id)
    pending = await start_email_verification(db, user=user, settings=settings)
    if pending is None:
        raise problem(
            409,
            "email_already_verified",
            "this address is already verified; nothing to send",
        )
    await db.commit()
    background_tasks.add_task(run_email_delivery, pending.delivery_id, pending.message)
    await audit.log(
        action="admin_user_verification_email",
        actor_subject_id=admin.subject_id,
        resource_kind="user",
        resource_id=user.subject_id,
        metadata={"delivery_id": str(pending.delivery_id)},
    )
    return VerificationEmailOut(
        subject_id=str(user.subject_id),
        email=user.email,
        delivery_id=str(pending.delivery_id),
    )


@router.patch("/admin/users/{subject_id}", response_model=AdminUserOut)
async def update_user(
    subject_id: uuid.UUID,
    body: AdminUserUpdateIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    admin: Annotated[User, Depends(require_admin)],
    audit: AuditDep,
) -> AdminUserOut:
    user = await _resolve_user_or_404(db, subject_id)

    payload = body.model_dump(exclude_unset=True)
    changed: dict = {}

    # Quota override: ``clear_storage_quota`` wins, then explicit value.
    if body.clear_storage_quota:
        if user.storage_quota_bytes is not None:
            user.storage_quota_bytes = None
            changed["storage_quota_bytes"] = None
    elif "storage_quota_bytes" in payload:
        if user.storage_quota_bytes != body.storage_quota_bytes:
            user.storage_quota_bytes = body.storage_quota_bytes
            changed["storage_quota_bytes"] = body.storage_quota_bytes

    if body.clear_max_concurrent_jobs:
        if user.max_concurrent_jobs is not None:
            user.max_concurrent_jobs = None
            changed["max_concurrent_jobs"] = None
    elif "max_concurrent_jobs" in payload:
        if user.max_concurrent_jobs != body.max_concurrent_jobs:
            user.max_concurrent_jobs = body.max_concurrent_jobs
            changed["max_concurrent_jobs"] = body.max_concurrent_jobs

    if "is_active" in payload and body.is_active is not None:
        if body.is_active != user.is_active:
            user.is_active = body.is_active
            user.blocked_at = None if body.is_active else datetime.now(UTC)
            changed["is_active"] = body.is_active

    if "blocked_reason" in payload and user.blocked_reason != body.blocked_reason:
        user.blocked_reason = body.blocked_reason
        changed["blocked_reason"] = body.blocked_reason

    if "is_admin" in payload and body.is_admin is not None:
        # Guardrail: an admin cannot demote themselves. They can be
        # demoted by another admin.
        if user.subject_id == admin.subject_id and body.is_admin is False:
            raise HTTPException(
                status_code=400,
                detail="cannot demote your own admin role; ask another admin",
            )
        if body.is_admin != user.is_admin:
            user.is_admin = body.is_admin
            changed["is_admin"] = body.is_admin

    if not changed:
        # No-op: return the current state without touching the audit log.
        return await _to_out(db, user)

    await db.commit()
    await db.refresh(user)

    await audit.log(
        action="admin_user_update",
        actor_subject_id=admin.subject_id,
        resource_kind="user",
        resource_id=user.subject_id,
        metadata={"changed": list(changed.keys()), "values": changed},
    )
    return await _to_out(db, user)


@router.delete("/admin/users/{subject_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(
    request: Request,
    subject_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    admin: Annotated[User, Depends(require_admin)],
    audit: AuditDep,
) -> None:
    """Hard-delete a user row. Reserved for cleanups (test accounts,
    GDPR closures); use ``is_active=false`` for routine deactivation.

    The CASCADE on ``subjects.id → users.subject_id`` removes grants
    and any owned resources whose foreign key was declared with
    CASCADE. Anything declared SET NULL (e.g. authored consultations)
    survives as anonymised history.
    """
    if subject_id == admin.subject_id:
        raise HTTPException(status_code=400, detail="cannot delete your own user account")
    user = await _resolve_user_or_404(db, subject_id)
    email = user.email
    await db.delete(user)
    await db.commit()

    await audit.log(
        action="admin_user_delete",
        actor_subject_id=admin.subject_id,
        resource_kind="user",
        resource_id=subject_id,
        metadata={"email": email},
    )


# Convenience: surface the platform default so the dashboard can show
# a "platform default = X GiB" hint next to the per-user override.
class PlatformDefaultsOut(BaseModel):
    storage_free_tier_bytes: int


@router.get("/admin/platform-defaults", response_model=PlatformDefaultsOut)
async def platform_defaults(
    admin: Annotated[User, Depends(require_admin)],
) -> PlatformDefaultsOut:
    return PlatformDefaultsOut(storage_free_tier_bytes=STORAGE_FREE_TIER_BYTES)
