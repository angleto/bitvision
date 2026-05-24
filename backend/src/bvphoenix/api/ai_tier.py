"""User-facing tier preference endpoint.

The patient Q&A orchestrator picks the active LLM tier from the
``ai_tiers`` resolver: a per-user override stored in
``app_settings`` under ``ai.user_tier:<subject_id>``, falling back
to ``ai.default_tier`` and then a hardcoded ``standard``. The admin
can lock the workspace by flipping ``ai.allow_user_override`` to
false; in that case the per-user override is ignored even when set.

This module exposes two endpoints:

* ``GET /api/me/ai-tier`` — return the effective tier for the
  current user, the raw per-user override (if any), the workspace
  default, and the override-allowed flag. Read by the
  ``/settings/ai-models`` page so it can render the right radio
  choice and grey out the picker when override is disabled.
* ``PUT /api/me/ai-tier`` — set or clear the caller's per-user
  override. Refuses with 403 when the workspace has disabled
  override; refuses with 400 on an unknown tier value.

The endpoint never inspects the wallet — the auto-downgrade decision
lives in :mod:`api.qna`. A user can express a preference for premium
even if their balance is low; the orchestrator will downgrade at
call time and surface the warning to the FE.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.auth import require_user
from bvphoenix.db.models import AppSetting, User
from bvphoenix.db.session import get_db
from bvphoenix.services.ai_tiers import (
    KEY_ALLOW_USER_OVERRIDE,
    KEY_DEFAULT_TIER,
    KEY_USER_TIER_PREFIX,
    AiTier,
    resolve_tier_for_user,
)

router = APIRouter(tags=["ai-tier"])

TierLiteral = Literal["free", "standard", "premium"]


class AiTierOut(BaseModel):
    effective_tier: TierLiteral
    user_override: TierLiteral | None
    workspace_default: TierLiteral
    allow_user_override: bool


class AiTierPutIn(BaseModel):
    tier: TierLiteral | None  # ``None`` clears the override


def _coerce_tier_literal(raw: object | None, default: AiTier) -> AiTier:
    if raw is None:
        return default
    val = str(raw).strip().lower()
    try:
        return AiTier(val)
    except ValueError:
        return default


async def _read_setting(db: AsyncSession, key: str) -> object | None:
    row = (await db.execute(select(AppSetting).where(AppSetting.key == key))).scalar_one_or_none()
    return row.value if row else None


@router.get("/me/ai-tier", response_model=AiTierOut)
async def get_my_ai_tier(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
) -> AiTierOut:
    effective = await resolve_tier_for_user(db, user_subject_id=user.subject_id)
    workspace_default = _coerce_tier_literal(
        await _read_setting(db, KEY_DEFAULT_TIER), AiTier.STANDARD
    )
    allow_override_raw = await _read_setting(db, KEY_ALLOW_USER_OVERRIDE)
    allow_override = True if allow_override_raw is None else bool(allow_override_raw)

    user_override_raw = await _read_setting(db, f"{KEY_USER_TIER_PREFIX}{user.subject_id}")
    user_override: AiTier | None = None
    if user_override_raw is not None:
        try:
            user_override = AiTier(str(user_override_raw).strip().lower())
        except ValueError:
            user_override = None

    return AiTierOut(
        effective_tier=effective.value,  # type: ignore[arg-type]
        user_override=(user_override.value if user_override else None),  # type: ignore[arg-type]
        workspace_default=workspace_default.value,  # type: ignore[arg-type]
        allow_user_override=allow_override,
    )


@router.put("/me/ai-tier", response_model=AiTierOut)
async def set_my_ai_tier(
    body: AiTierPutIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
) -> AiTierOut:
    """Upsert (or delete) the caller's per-user tier override.

    A null ``tier`` deletes the override entry, falling back to the
    workspace default. Refuses with 403 when the admin has set
    ``ai.allow_user_override = false``.
    """
    allow_override_raw = await _read_setting(db, KEY_ALLOW_USER_OVERRIDE)
    allow_override = True if allow_override_raw is None else bool(allow_override_raw)
    if not allow_override:
        raise HTTPException(status_code=403, detail="user override disabled by admin")

    key = f"{KEY_USER_TIER_PREFIX}{user.subject_id}"
    now = datetime.now(UTC)

    if body.tier is None:
        await db.execute(delete(AppSetting).where(AppSetting.key == key))
        await db.commit()
        return await get_my_ai_tier(db=db, user=user)

    s = (await db.execute(select(AppSetting).where(AppSetting.key == key))).scalar_one_or_none()
    if s is None:
        s = AppSetting(
            key=key,
            value=body.tier,
            scope="admin",
            description=f"Per-user AI tier preference for {user.subject_id}",
            updated_at=now,
            updated_by_subject_id=user.subject_id,
        )
        db.add(s)
    else:
        s.value = body.tier
        s.updated_at = now
        s.updated_by_subject_id = user.subject_id
    await db.commit()
    return await get_my_ai_tier(db=db, user=user)


__all__ = ["router"]
