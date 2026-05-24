"""Build LLM provider instances for an arbitrary model_id.

The tier resolver in ``ai_tiers.py`` covers the default flow (resolve
tier → pick provider+model from settings). When the caller wants to
override the model (model dropdown in PatientAskPanel, A/B test from
admin) we still need to know which provider library to instantiate
for that model id; we get it by looking up the rate card's
``provider`` column. This keeps "what provider does X live on" as a
single piece of state in the DB instead of a mapping replicated in
code.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.config import get_settings
from bvphoenix.db.models import LLMRateCard
from bvphoenix.services.llm import LLMProvider, StubLLM


class UnknownProviderError(RuntimeError):
    pass


async def provider_for_model(db: AsyncSession, model_id: str) -> LLMProvider:
    row = (
        await db.execute(select(LLMRateCard).where(LLMRateCard.model_id == model_id))
    ).scalar_one_or_none()
    if row is None:
        raise UnknownProviderError(f"unknown model_id: {model_id!r}")
    if not row.is_active:
        raise UnknownProviderError(f"model {model_id!r} is disabled")

    s = get_settings()
    kind = row.provider

    if kind == "stub":
        return StubLLM()
    if kind == "scaleway":
        if not s.scaleway_api_key:
            raise UnknownProviderError("BVP_SCALEWAY_API_KEY not configured")
        from bvphoenix.services.llm_scaleway import ScalewayLLM

        return ScalewayLLM(
            api_key=s.scaleway_api_key,
            model_id=model_id,
            base_url=s.scaleway_base_url,
        )
    if kind == "anthropic":
        if not s.anthropic_api_key:
            raise UnknownProviderError("BVP_ANTHROPIC_API_KEY not configured")
        from bvphoenix.services.llm import AnthropicLLM

        return AnthropicLLM(api_key=s.anthropic_api_key, model_id=model_id)
    if kind == "openai":
        if not s.openai_api_key:
            raise UnknownProviderError("BVP_OPENAI_API_KEY not configured")
        from bvphoenix.services.llm_openai import OpenAILLM

        return OpenAILLM(api_key=s.openai_api_key, model_id=model_id)
    if kind in ("ollama-local", "in-house"):
        from bvphoenix.services.llm_ollama import OllamaLLM

        return OllamaLLM(base_url=s.ollama_base_url, model_id=model_id)
    raise UnknownProviderError(f"unsupported provider: {kind!r}")


__all__ = ["UnknownProviderError", "provider_for_model"]
