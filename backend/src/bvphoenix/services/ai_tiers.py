"""AI tier resolution and provider routing.

The plan exposes three pricing tiers — ``free``, ``standard``,
``premium`` — and the user's monetisation decision was that:

* admins set the workspace default;
* every user can opt into a higher tier (constrained by wallet
  balance) or fall back to a lower one;
* a top-level admin switch (``ai.allow_user_override``) can lock the
  workspace to its default tier.

This module owns the resolution chain plus the mapping from a
resolved tier to an active :class:`LLMProvider` and pricing markup.
The Q&A orchestrator imports :func:`resolve_tier_for_user` to know
which tier the live caller belongs to and
:func:`provider_for_tier` to obtain the provider instance with the
right model and base url.

Configuration source — every key lives in the existing
``app_settings`` table so the admin UI can edit them without a
redeploy:

============================================  =====================
Key                                            Notes
============================================  =====================
``ai.default_tier``                             Workspace default; one
                                                of ``free`` /
                                                ``standard`` /
                                                ``premium``.
``ai.allow_user_override``                      Bool. When false the
                                                workspace default
                                                wins regardless of
                                                user override.
``ai.user_tier:<user_subject_id>``              Per-user override
                                                value. Same enum.
============================================  =====================

Hardcoded fallbacks are conservative: anonymous / unauthenticated
callers always resolve to ``free`` (which currently produces no LLM
output — only deterministic SQL retrieval).

Embedding tier note: until the Scaleway embedding integration ships
(see plan M14b / migration 0091), every tier currently uses the local
MiniLM embedding regardless. The :class:`AiTierConfig` carries the
embedding model id so that integration is a one-line change here when
the provisioning lands; until then the field stays
``minilm-multi-v1`` for all three tiers.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.config import get_settings
from bvphoenix.db.models import AppSetting
from bvphoenix.services.llm import LLMProvider, StubLLM

logger = logging.getLogger(__name__)


class AiTier(StrEnum):
    FREE = "free"
    STANDARD = "standard"
    PREMIUM = "premium"


# Setting keys.
KEY_DEFAULT_TIER = "ai.default_tier"
KEY_ALLOW_USER_OVERRIDE = "ai.allow_user_override"
KEY_USER_TIER_PREFIX = "ai.user_tier:"

# Hardcoded floor when no setting and no override is present. Logged-in
# users land on standard so the freemium / paid path is functional out
# of the box; unauthenticated callers always land on free.
DEFAULT_TIER_AUTHENTICATED = AiTier.STANDARD
DEFAULT_TIER_ANONYMOUS = AiTier.FREE


@dataclass(frozen=True, slots=True)
class AiTierConfig:
    """Runtime profile of a tier.

    The orchestrator reads ``llm_provider_kind`` to know how to build
    the provider, ``llm_model_id`` to seed it, and the embedding fields
    when premium embedding is enabled.
    """

    tier: AiTier
    llm_provider_kind: str  # 'stub' | 'scaleway' | 'anthropic' | 'openai' | 'ollama'
    llm_model_id: str
    embedding_provider_kind: str  # 'local' | 'scaleway' | 'openai' | 'ollama'
    embedding_model_id: str


def _coerce_tier(raw: object | None) -> AiTier | None:
    if raw is None:
        return None
    val = str(raw).strip().lower()
    try:
        return AiTier(val)
    except ValueError:
        return None


async def _read_setting(db: AsyncSession, key: str) -> object | None:
    row = (await db.execute(select(AppSetting).where(AppSetting.key == key))).scalar_one_or_none()
    if row is None:
        return None
    return row.value


async def resolve_tier_for_user(
    db: AsyncSession,
    *,
    user_subject_id: uuid.UUID | None,
) -> AiTier:
    """Compute the active tier for ``user_subject_id``.

    Order:
        1. ``ai.user_tier:<id>`` if set AND
           ``ai.allow_user_override`` is true (default true).
        2. ``ai.default_tier``.
        3. Hardcoded fallback (``standard`` for authenticated, ``free``
           otherwise).

    Unknown / malformed values are ignored at each step (logged) and
    the chain falls through to the next entry.
    """
    if user_subject_id is None:
        # Anonymous callers never get the override path; they pick up
        # the workspace default but capped at standard at most. This
        # matches the user's "freemium core" requirement.
        default = _coerce_tier(await _read_setting(db, KEY_DEFAULT_TIER))
        return default or DEFAULT_TIER_ANONYMOUS

    allow_override_raw = await _read_setting(db, KEY_ALLOW_USER_OVERRIDE)
    allow_override = True if allow_override_raw is None else bool(allow_override_raw)

    if allow_override:
        per_user = _coerce_tier(await _read_setting(db, f"{KEY_USER_TIER_PREFIX}{user_subject_id}"))
        if per_user is not None:
            return per_user

    default = _coerce_tier(await _read_setting(db, KEY_DEFAULT_TIER))
    if default is not None:
        return default

    return DEFAULT_TIER_AUTHENTICATED


def config_for_tier(tier: AiTier) -> AiTierConfig:
    """Map a resolved tier to its provider + model profile.

    Reads from :func:`get_settings` so the admin can adjust per-tier
    model picks via env (e.g. ``BVP_SCALEWAY_DEFAULT_MODEL``,
    ``BVP_SCALEWAY_PREMIUM_MODEL``). Embedding stays local across all
    tiers for now; switch to ``scaleway`` + ``qwen3-embedding-8b``
    once the dedicated 4096-d table lands.
    """
    s = get_settings()
    if tier is AiTier.FREE:
        return AiTierConfig(
            tier=AiTier.FREE,
            llm_provider_kind="stub",
            llm_model_id="stub-v0",
            embedding_provider_kind="local",
            embedding_model_id="minilm-multi-v1",
        )
    if tier is AiTier.STANDARD:
        return AiTierConfig(
            tier=AiTier.STANDARD,
            llm_provider_kind="scaleway"
            if s.scaleway_api_key
            else "anthropic"
            if s.anthropic_api_key
            else "stub",
            llm_model_id=s.scaleway_default_model
            if s.scaleway_api_key
            else (s.llm_default_model if s.anthropic_api_key else "stub-v0"),
            embedding_provider_kind="local",
            embedding_model_id="minilm-multi-v1",
        )
    # PREMIUM
    return AiTierConfig(
        tier=AiTier.PREMIUM,
        llm_provider_kind="scaleway"
        if s.scaleway_api_key
        else "anthropic"
        if s.anthropic_api_key
        else "stub",
        llm_model_id=s.scaleway_premium_model
        if s.scaleway_api_key
        else (s.llm_default_model if s.anthropic_api_key else "stub-v0"),
        embedding_provider_kind="local",
        embedding_model_id="minilm-multi-v1",
    )


def provider_for_tier(tier: AiTier) -> LLMProvider:
    """Build a fresh :class:`LLMProvider` configured for ``tier``.

    Not cached: the orchestrator calls this once per ``/ask`` request.
    Construction is sub-millisecond (SDK clients are cheap to init).
    """
    cfg = config_for_tier(tier)
    s = get_settings()

    if cfg.llm_provider_kind == "stub":
        return StubLLM()

    if cfg.llm_provider_kind == "scaleway":
        from bvphoenix.services.llm_scaleway import ScalewayLLM

        return ScalewayLLM(
            api_key=s.scaleway_api_key,
            model_id=cfg.llm_model_id,
            base_url=s.scaleway_base_url,
        )
    if cfg.llm_provider_kind == "anthropic":
        from bvphoenix.services.llm import AnthropicLLM

        return AnthropicLLM(api_key=s.anthropic_api_key, model_id=cfg.llm_model_id)
    if cfg.llm_provider_kind == "openai":
        from bvphoenix.services.llm_openai import OpenAILLM

        return OpenAILLM(api_key=s.openai_api_key, model_id=cfg.llm_model_id)
    if cfg.llm_provider_kind == "ollama":
        from bvphoenix.services.llm_ollama import OllamaLLM

        return OllamaLLM(model_id=cfg.llm_model_id, base_url=s.ollama_base_url)

    logger.warning(
        "unknown provider_kind=%s for tier=%s — falling back to stub", cfg.llm_provider_kind, tier
    )
    return StubLLM()


__all__ = [
    "DEFAULT_TIER_ANONYMOUS",
    "DEFAULT_TIER_AUTHENTICATED",
    "KEY_ALLOW_USER_OVERRIDE",
    "KEY_DEFAULT_TIER",
    "KEY_USER_TIER_PREFIX",
    "AiTier",
    "AiTierConfig",
    "config_for_tier",
    "provider_for_tier",
    "resolve_tier_for_user",
]
