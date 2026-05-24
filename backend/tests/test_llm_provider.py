"""Unit tests for the LLM provider stub.

The Anthropic provider is not exercised here — it would require real
network access and an API key. Keep that for an integration suite.
"""

from __future__ import annotations

import pytest

from bvphoenix.services.llm import StubProvider, get_llm_provider


@pytest.mark.asyncio
async def test_stub_describe_includes_modality_and_body_part() -> None:
    provider = StubProvider()
    out = await provider.describe_series(modality="MR", body_part="HEAD", hint=None)
    assert "MR" in out.text
    assert "head" in out.text.lower()
    assert out.model_id == "stub-v0"


@pytest.mark.asyncio
async def test_stub_describe_includes_hint_when_present() -> None:
    out = await StubProvider().describe_series(
        modality="CT", body_part="CHEST", hint="rule out pneumonia"
    )
    assert "pneumonia" in out.text


def test_default_provider_is_stub_without_api_key() -> None:
    assert isinstance(get_llm_provider(), StubProvider)


def test_anthropic_without_key_falls_back_to_stub(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty API key must NOT crash the provider factory: the platform
    stays available, and LLM-dependent endpoints (e.g. care-phase
    classifier) surface a graceful 503 ``feature temporarily
    unavailable`` instead of a backend boot failure.
    """
    from bvphoenix.config import get_settings
    from bvphoenix.services import llm as llm_mod

    monkeypatch.setenv("BVP_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("BVP_ANTHROPIC_API_KEY", "")
    get_settings.cache_clear()
    llm_mod.get_llm_provider.cache_clear()
    try:
        assert isinstance(llm_mod.get_llm_provider(), StubProvider)
    finally:
        get_settings.cache_clear()
        llm_mod.get_llm_provider.cache_clear()
