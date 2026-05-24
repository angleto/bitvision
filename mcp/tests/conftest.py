"""Shared fixtures for MCP server tests.

Tests are hermetic: no real backend, no real network. We patch
``bvmcp.tools.client.httpx.AsyncClient`` so every ``api_get`` / ``api_post``
call goes through an ``httpx.MockTransport`` handler supplied per-test.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from bvmcp.config import get_settings

# Canonical values used across tests so assertions can compare against them.
TEST_TOKEN = "test-token-123"
TEST_BASE_URL = "http://test-backend"


@pytest.fixture(autouse=True)
def _configure_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force deterministic settings for every test.

    ``get_settings`` is lru-cached, so we mutate the cached instance in place
    rather than clearing and rebuilding (which would also re-read env files).

    Also pin the feature flags to "everything on" by default — tests that
    care about the BYO/embedded distinction can opt into the all-off
    snapshot via the ``llm_classifier_off`` fixture below. Without
    this autouse pin, tests would call the real backend probe (which
    fails in the hermetic env, defaults to all-off, then surprises
    every assertion expecting the LLM-gated tools to be visible).
    """
    settings = get_settings()
    monkeypatch.setattr(settings, "backend_base_url", TEST_BASE_URL)
    monkeypatch.setattr(settings, "user_token", TEST_TOKEN)

    from bvmcp import feature_flags

    monkeypatch.setattr(feature_flags, "_FLAGS", feature_flags.FeatureFlags(llm_classifier=True))


@pytest.fixture
def llm_classifier_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Opt-in fixture: simulate the BYO-mode backend (no LLM provider)
    so the propose / apply care-phase tools are filtered out of the
    MCP toolkit. Use in tests that exercise the dynamic filtering."""
    from bvmcp import feature_flags

    monkeypatch.setattr(feature_flags, "_FLAGS", feature_flags.FeatureFlags(llm_classifier=False))


class RequestRecorder:
    """Captures every request seen by the MockTransport."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    @property
    def last(self) -> httpx.Request:
        assert self.requests, "no requests were captured"
        return self.requests[-1]


@contextmanager
def mock_backend(
    handler: Callable[[httpx.Request], httpx.Response],
) -> Iterator[RequestRecorder]:
    """Patch httpx.AsyncClient in the tools.client module to use MockTransport.

    The tools construct ``httpx.AsyncClient(timeout=...)`` directly, so we
    wrap the class to inject a transport while preserving any other kwargs.
    """
    recorder = RequestRecorder()

    def recording_handler(request: httpx.Request) -> httpx.Response:
        recorder.requests.append(request)
        return handler(request)

    transport = httpx.MockTransport(recording_handler)

    real_client = httpx.AsyncClient

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    with patch("bvmcp.tools.client.httpx.AsyncClient", side_effect=factory):
        yield recorder
