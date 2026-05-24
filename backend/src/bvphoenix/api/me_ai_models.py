"""User-facing list of LLM models the platform can serve.

Slimmer than ``/admin/llm-rates``: returns just enough for the
PatientAskPanel dropdown (display_name, provider, tier_hint,
in_house flag) plus the model_id the tier resolver picks for the
current user, so the dropdown can render "★ Default (Mistral
Small)".
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.auth import require_user
from bvphoenix.config import get_settings
from bvphoenix.db.models import LLMRateCard, User
from bvphoenix.db.session import get_db
from bvphoenix.services.ai_tiers import config_for_tier, resolve_tier_for_user

router = APIRouter(tags=["ai-models"])


class AvailableModelOut(BaseModel):
    model_id: str
    display_name: str
    provider: str
    tier_hint: str
    is_in_house: bool


class AiModelsBundleOut(BaseModel):
    available: list[AvailableModelOut]
    current_tier: str
    current_default_model_id: str


def _provider_configured(provider: str) -> bool:
    s = get_settings()
    if provider == "scaleway":
        return bool(s.scaleway_api_key)
    if provider == "anthropic":
        return bool(s.anthropic_api_key)
    if provider == "openai":
        return bool(s.openai_api_key)
    if provider == "stub":
        return True
    if provider in ("ollama-local", "in-house"):
        # Ollama-style in-cluster LLMs are gated by an explicit
        # ``BVP_OLLAMA_ENABLED`` flag because the default base URL
        # always resolves; without the flag we cannot tell "deployed"
        # from "placeholder env value", and the user dropdown would
        # leak in-house entries that 500 on selection.
        return bool(s.ollama_enabled)
    return False


@router.get("/me/ai-models", response_model=AiModelsBundleOut)
async def list_my_models(
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AiModelsBundleOut:
    rows = (
        (
            await db.execute(
                select(LLMRateCard)
                .where(LLMRateCard.is_active.is_(True))
                .order_by(LLMRateCard.tier_hint, LLMRateCard.provider, LLMRateCard.display_name)
            )
        )
        .scalars()
        .all()
    )
    available = [
        AvailableModelOut(
            model_id=r.model_id,
            display_name=r.display_name,
            provider=r.provider,
            tier_hint=r.tier_hint,
            is_in_house=r.is_in_house,
        )
        for r in rows
        if _provider_configured(r.provider)
    ]

    tier = await resolve_tier_for_user(db, user_subject_id=user.subject_id)
    cfg = config_for_tier(tier)
    return AiModelsBundleOut(
        available=available,
        current_tier=tier.value,
        current_default_model_id=cfg.llm_model_id,
    )


__all__ = ["router"]
