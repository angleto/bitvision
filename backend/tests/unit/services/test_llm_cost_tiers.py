"""Smoke tests for the tier-aware billing extensions in llm_cost +
embedding_cost. The pre-existing wholesale/billed flow stays covered
by tests/test_billing.py; these focus on the round 2 multi-tier
addition and Scaleway entries."""

from __future__ import annotations

import pytest

from bvphoenix.services.embedding_cost import (
    billed_embedding_cents,
    billed_embedding_usd,
    estimate_input_tokens,
    wholesale_embedding_usd,
)
from bvphoenix.services.llm import LLMUsage
from bvphoenix.services.llm_cost import (
    EUR_TO_USD,
    MARKUP_BY_TIER,
    ModelTier,
    UnknownModelError,
    billed_cents,
    billed_usd,
    markup_for_tier,
    tier_for_model,
    wholesale_usd,
)


def _usage(prompt: int = 0, completion: int = 0) -> LLMUsage:
    return LLMUsage(prompt=prompt, completion=completion)


# ---------------------------------------------------------------------------
# Tier classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "model_id, expected",
    [
        ("mistral-small-3.2-24b-instruct-2506", ModelTier.STANDARD),
        ("qwen3-235b-a22b-instruct-2507", ModelTier.PREMIUM),
        ("qwen3.5-397b-a17b", ModelTier.PREMIUM),
        ("claude-opus-4-7", ModelTier.PREMIUM),
        ("claude-sonnet-4-6", ModelTier.STANDARD),
        ("medgemma:27b", ModelTier.FREE),
        ("stub-v0", ModelTier.FREE),
        # Unknown models default to standard so legacy paths keep working.
        ("acme-llm-9000", ModelTier.STANDARD),
    ],
)
def test_tier_for_model(model_id: str, expected: ModelTier) -> None:
    assert tier_for_model(model_id) == expected


def test_markup_by_tier_matches_user_decision() -> None:
    assert MARKUP_BY_TIER[ModelTier.FREE] == 0.0
    assert MARKUP_BY_TIER[ModelTier.STANDARD] == 0.20
    assert MARKUP_BY_TIER[ModelTier.PREMIUM] == 0.30


def test_markup_for_tier_helper() -> None:
    assert markup_for_tier(ModelTier.STANDARD) == 0.20
    assert markup_for_tier(ModelTier.PREMIUM) == 0.30


# ---------------------------------------------------------------------------
# Wholesale + billed for Scaleway entries
# ---------------------------------------------------------------------------


def test_scaleway_mistral_small_input_price_matches_eur_pricing() -> None:
    # 1M input tokens of mistral-small at €0.15/M → 0.15 * EUR_TO_USD.
    rate = wholesale_usd(_usage(prompt=1_000_000), model_id="mistral-small-3.2-24b-instruct-2506")
    assert rate == pytest.approx(0.15 * EUR_TO_USD, rel=1e-9)


def test_billed_premium_uses_30_percent_markup() -> None:
    # 1M input tokens of qwen3-235b → wholesale + 30% markup.
    raw = wholesale_usd(_usage(prompt=1_000_000), model_id="qwen3-235b-a22b-instruct-2507")
    billed = billed_usd(
        _usage(prompt=1_000_000),
        model_id="qwen3-235b-a22b-instruct-2507",
        is_byok=False,
    )
    assert billed == pytest.approx(raw * 1.30, rel=1e-9)


def test_billed_standard_uses_20_percent_markup() -> None:
    raw = wholesale_usd(_usage(prompt=1_000_000), model_id="mistral-small-3.2-24b-instruct-2506")
    billed = billed_usd(
        _usage(prompt=1_000_000),
        model_id="mistral-small-3.2-24b-instruct-2506",
        is_byok=False,
    )
    assert billed == pytest.approx(raw * 1.20, rel=1e-9)


def test_billed_byok_strips_markup() -> None:
    raw = wholesale_usd(_usage(prompt=1_000_000), model_id="qwen3-235b-a22b-instruct-2507")
    billed = billed_usd(
        _usage(prompt=1_000_000),
        model_id="qwen3-235b-a22b-instruct-2507",
        is_byok=True,
    )
    assert billed == pytest.approx(raw, rel=1e-9)


def test_billed_cents_rounds_up() -> None:
    # Tiny call: pico-cents → 1 cent floor (round-up to whole cents).
    cents = billed_cents(
        _usage(prompt=10, completion=5),
        model_id="mistral-small-3.2-24b-instruct-2506",
        is_byok=False,
    )
    assert cents >= 1


def test_unknown_model_raises() -> None:
    with pytest.raises(UnknownModelError):
        wholesale_usd(_usage(prompt=10), model_id="totally-made-up-v9")


# ---------------------------------------------------------------------------
# Embedding cost
# ---------------------------------------------------------------------------


def test_estimate_input_tokens_rounds_up() -> None:
    assert estimate_input_tokens("") == 0
    # 1 char → 0.25 → ceil → 1 token.
    assert estimate_input_tokens("a") == 1
    # 16 chars → 4 tokens.
    assert estimate_input_tokens("a" * 16) == 4
    # 17 chars → 4.25 → 5 tokens.
    assert estimate_input_tokens("a" * 17) == 5


def test_local_minilm_costs_zero() -> None:
    assert wholesale_embedding_usd(1_000_000, model_id="minilm-multi-v1") == 0.0
    assert billed_embedding_usd(1_000_000, model_id="minilm-multi-v1", is_byok=False) == 0.0
    assert billed_embedding_cents(1_000_000, model_id="minilm-multi-v1", is_byok=False) == 0


def test_scaleway_embedding_uses_premium_markup() -> None:
    raw = wholesale_embedding_usd(1_000_000, model_id="qwen3-embedding-8b")
    billed = billed_embedding_usd(1_000_000, model_id="qwen3-embedding-8b", is_byok=False)
    assert raw == pytest.approx(0.10 * EUR_TO_USD, rel=1e-9)
    assert billed == pytest.approx(raw * 1.30, rel=1e-9)


def test_byok_embedding_strips_markup() -> None:
    raw = wholesale_embedding_usd(1_000_000, model_id="qwen3-embedding-8b")
    billed = billed_embedding_usd(1_000_000, model_id="qwen3-embedding-8b", is_byok=True)
    assert billed == pytest.approx(raw, rel=1e-9)


def test_unknown_embedding_model_raises() -> None:
    with pytest.raises(UnknownModelError):
        wholesale_embedding_usd(100, model_id="not-a-real-embedder")
