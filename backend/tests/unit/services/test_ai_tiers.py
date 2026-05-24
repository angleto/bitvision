"""Unit tests for tier resolution from ``app_settings``."""

from __future__ import annotations

import uuid

import pytest

from bvphoenix.services.ai_tiers import (
    DEFAULT_TIER_ANONYMOUS,
    DEFAULT_TIER_AUTHENTICATED,
    KEY_ALLOW_USER_OVERRIDE,
    KEY_DEFAULT_TIER,
    KEY_USER_TIER_PREFIX,
    AiTier,
    config_for_tier,
    provider_for_tier,
    resolve_tier_for_user,
)
from bvphoenix.services.llm import StubLLM


class _StubDB:
    """Minimal AsyncSession stand-in for the resolver."""

    def __init__(self, settings_map: dict[str, object]) -> None:
        self.settings_map = settings_map

    async def execute(self, query):
        # We only support the single SELECT shape the resolver issues.
        # Pull the WHERE clause's bindparam to recover the requested key.
        # `query.compile().params` returns the values for bound params.
        compiled = query.compile()
        key = compiled.params.get("key_1") or compiled.params.get("param_1")
        if key is None:
            # Fallback: introspect the where clause directly.
            crit = query.whereclause
            key = next(iter(crit.right.value for _ in (0,)), None)
        return _Result(self.settings_map.get(str(key)))


class _Row:
    def __init__(self, value: object) -> None:
        self.value = value


class _Result:
    def __init__(self, value: object | None) -> None:
        self._value = value

    def scalar_one_or_none(self) -> _Row | None:
        if self._value is None:
            return None
        return _Row(self._value)


@pytest.mark.asyncio
async def test_anonymous_falls_back_to_anonymous_default() -> None:
    db = _StubDB({})
    tier = await resolve_tier_for_user(db, user_subject_id=None)
    assert tier == DEFAULT_TIER_ANONYMOUS == AiTier.FREE


@pytest.mark.asyncio
async def test_anonymous_uses_workspace_default_when_set() -> None:
    db = _StubDB({KEY_DEFAULT_TIER: "premium"})
    tier = await resolve_tier_for_user(db, user_subject_id=None)
    assert tier == AiTier.PREMIUM


@pytest.mark.asyncio
async def test_authenticated_falls_back_to_standard() -> None:
    db = _StubDB({})
    tier = await resolve_tier_for_user(db, user_subject_id=uuid.uuid4())
    assert tier == DEFAULT_TIER_AUTHENTICATED == AiTier.STANDARD


@pytest.mark.asyncio
async def test_per_user_override_wins_when_allowed() -> None:
    uid = uuid.uuid4()
    db = _StubDB(
        {
            KEY_DEFAULT_TIER: "standard",
            f"{KEY_USER_TIER_PREFIX}{uid}": "premium",
        }
    )
    tier = await resolve_tier_for_user(db, user_subject_id=uid)
    assert tier == AiTier.PREMIUM


@pytest.mark.asyncio
async def test_per_user_override_disabled_falls_through() -> None:
    uid = uuid.uuid4()
    db = _StubDB(
        {
            KEY_DEFAULT_TIER: "free",
            KEY_ALLOW_USER_OVERRIDE: False,
            f"{KEY_USER_TIER_PREFIX}{uid}": "premium",
        }
    )
    tier = await resolve_tier_for_user(db, user_subject_id=uid)
    assert tier == AiTier.FREE


@pytest.mark.asyncio
async def test_malformed_per_user_override_falls_through() -> None:
    uid = uuid.uuid4()
    db = _StubDB(
        {
            KEY_DEFAULT_TIER: "premium",
            f"{KEY_USER_TIER_PREFIX}{uid}": "ultra-extreme",  # not a valid enum
        }
    )
    tier = await resolve_tier_for_user(db, user_subject_id=uid)
    assert tier == AiTier.PREMIUM


def test_config_for_tier_free_uses_stub() -> None:
    cfg = config_for_tier(AiTier.FREE)
    assert cfg.llm_provider_kind == "stub"
    assert cfg.llm_model_id == "stub-v0"
    assert cfg.embedding_provider_kind == "local"
    assert cfg.embedding_model_id == "minilm-multi-v1"


def test_config_for_tier_standard_when_no_keys_falls_back_to_stub() -> None:
    # In the test env we have no Scaleway/Anthropic keys, so STANDARD
    # also lands on stub (graceful degradation rather than 500).
    cfg = config_for_tier(AiTier.STANDARD)
    assert cfg.llm_provider_kind == "stub"
    assert cfg.embedding_model_id == "minilm-multi-v1"


def test_provider_for_tier_returns_provider_instance() -> None:
    p = provider_for_tier(AiTier.FREE)
    assert isinstance(p, StubLLM)
