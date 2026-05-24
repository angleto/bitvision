"""F7.2: cost-module unit tests.

Pricing table lookup + markup semantics. Intentionally strict on the
numeric output: if somebody refreshes ``_MODEL_RATES`` the tests must
say so out loud.
"""

from __future__ import annotations

import pytest

from bvphoenix.services.llm import LLMUsage
from bvphoenix.services.llm_cost import (
    PLATFORM_MARKUP,
    UnknownModelError,
    billed_cents,
    billed_usd,
    wholesale_usd,
)


def test_wholesale_usd_sonnet_typical_call() -> None:
    # 2k input + 500 output tokens on Sonnet 4.6 = 2000*3 + 500*15 = 13500
    # per 1M tokens ⇒ 0.0135 USD.
    usage = LLMUsage(prompt=2000, completion=500)
    cost = wholesale_usd(usage, model_id="claude-sonnet-4-6")
    assert cost == pytest.approx(0.0135, rel=1e-6)


def test_wholesale_includes_cache_read_and_creation() -> None:
    usage = LLMUsage(
        prompt=1000,
        completion=200,
        cache_read_tokens=5000,
        cache_creation_tokens=1000,
    )
    # Sonnet: 1000*3 + 200*15 + 5000*0.30 + 1000*3.75 = 3000 + 3000 + 1500 + 3750 = 11250
    # per 1M tokens ⇒ 0.01125 USD.
    cost = wholesale_usd(usage, model_id="claude-sonnet-4-6")
    assert cost == pytest.approx(0.01125, rel=1e-6)


def test_opus_is_5x_sonnet() -> None:
    usage = LLMUsage(prompt=1000, completion=200)
    s = wholesale_usd(usage, model_id="claude-sonnet-4-6")
    o = wholesale_usd(usage, model_id="claude-opus-4-7")
    # 1000*3 + 200*15 = 6000 sonnet, 1000*15 + 200*75 = 30000 opus — 5x.
    assert o == pytest.approx(5 * s, rel=1e-9)


def test_haiku_cheapest() -> None:
    usage = LLMUsage(prompt=10_000, completion=1_000)
    opus = wholesale_usd(usage, model_id="claude-opus-4-7")
    sonnet = wholesale_usd(usage, model_id="claude-sonnet-4-6")
    haiku = wholesale_usd(usage, model_id="claude-haiku-4-5-20251001")
    assert haiku < sonnet < opus


def test_markup_applies_only_when_not_byok() -> None:
    usage = LLMUsage(prompt=1000, completion=200)
    raw = wholesale_usd(usage, model_id="claude-sonnet-4-6")
    platform = billed_usd(usage, model_id="claude-sonnet-4-6", is_byok=False)
    byok = billed_usd(usage, model_id="claude-sonnet-4-6", is_byok=True)
    assert platform == pytest.approx(raw * (1.0 + PLATFORM_MARKUP), rel=1e-9)
    assert byok == pytest.approx(raw, rel=1e-9)


def test_platform_markup_is_20_percent() -> None:
    assert PLATFORM_MARKUP == 0.20


def test_billed_cents_rounds_up() -> None:
    """Fractional charge must never become zero — we round up."""
    usage = LLMUsage(prompt=1, completion=1)  # ~0.000018 USD sonnet
    cents = billed_cents(usage, model_id="claude-sonnet-4-6", is_byok=False)
    assert cents == 1  # ceil(0.000022 → 1 cent)


def test_billed_cents_on_stub_is_zero() -> None:
    """Stub model has all-zero rates ⇒ zero cost ⇒ zero cents."""
    usage = LLMUsage(prompt=10_000, completion=10_000)
    assert billed_cents(usage, model_id="stub-v0", is_byok=False) == 0


def test_unknown_model_raises() -> None:
    usage = LLMUsage(prompt=1, completion=1)
    with pytest.raises(UnknownModelError):
        wholesale_usd(usage, model_id="claude-opus-99")
