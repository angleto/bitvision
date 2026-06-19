"""M6: content-safety screening seam, storage isolation + fail-closed to block.

Security-critical assertions: an image is never POSTed to a host outside the
allowlist, and a configured-but-broken screener fails CLOSED to ``block`` (it
withholds the contribution) rather than letting unscreened content reach a
reviewer. Absence of a provider, by contrast, passes but records ``null``.
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx

import bvphoenix.config as config_mod
from bvphoenix.services.content_safety import (
    HttpContentSafetyScreener,
    NullScreener,
    get_screener,
)

_ALLOWED = frozenset({"localhost", "bvphoenix-contentsafety-svc"})
_IMG = b"\x89PNG\r\n\x1a\n-not-a-real-image"


def _settings(**over):
    base = {
        "content_safety_provider": "",
        "content_safety_endpoint": "",
        "content_safety_allowed_hosts": "localhost,127.0.0.1,bvphoenix-contentsafety-svc",
        "content_safety_timeout": 0.2,
    }
    base.update(over)
    return SimpleNamespace(**base)


async def test_null_screener_passes_but_records_absence():
    res = await NullScreener().screen(_IMG)
    assert res.verdict == "pass"
    assert res.provider == "null"


async def test_disallowed_host_blocks_without_network(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("network must not be attempted for a disallowed host")

    monkeypatch.setattr(httpx, "AsyncClient", _boom)
    eng = HttpContentSafetyScreener(endpoint="http://evil.example.com", allowed_hosts=_ALLOWED)
    res = await eng.screen(_IMG)
    assert res.verdict == "block"
    assert "misconfigured" in res.categories


async def test_allowed_host_error_fails_closed_to_block():
    eng = HttpContentSafetyScreener(
        endpoint="http://localhost:1", allowed_hosts=_ALLOWED, timeout=0.2
    )
    res = await eng.screen(_IMG)
    assert res.verdict == "block"
    assert "screen_error" in res.categories


def _mock_async_client(monkeypatch, payload):
    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return payload

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)


async def test_allowed_host_pass_verdict(monkeypatch):
    _mock_async_client(monkeypatch, {"verdict": "pass"})
    eng = HttpContentSafetyScreener(
        endpoint="http://bvphoenix-contentsafety-svc", allowed_hosts=_ALLOWED
    )
    res = await eng.screen(_IMG)
    assert res.verdict == "pass"
    assert res.provider == "http"


async def test_allowed_host_block_verdict_with_categories(monkeypatch):
    _mock_async_client(monkeypatch, {"verdict": "block", "categories": ["csam"]})
    eng = HttpContentSafetyScreener(
        endpoint="http://bvphoenix-contentsafety-svc", allowed_hosts=_ALLOWED
    )
    res = await eng.screen(_IMG)
    assert res.verdict == "block"
    assert res.categories == ("csam",)


async def test_unknown_verdict_blocks(monkeypatch):
    _mock_async_client(monkeypatch, {"verdict": "maybe"})
    eng = HttpContentSafetyScreener(
        endpoint="http://bvphoenix-contentsafety-svc", allowed_hosts=_ALLOWED
    )
    res = await eng.screen(_IMG)
    assert res.verdict == "block"
    assert "bad_response" in res.categories


def test_get_screener_default_is_null(monkeypatch):
    monkeypatch.setattr(config_mod, "get_settings", lambda: _settings())
    assert isinstance(get_screener(), NullScreener)


def test_get_screener_http_resolves(monkeypatch):
    monkeypatch.setattr(
        config_mod,
        "get_settings",
        lambda: _settings(
            content_safety_provider="http",
            content_safety_endpoint="http://bvphoenix-contentsafety-svc",
        ),
    )
    eng = get_screener()
    assert isinstance(eng, HttpContentSafetyScreener)
    assert eng.allowed_hosts == frozenset({"localhost", "127.0.0.1", "bvphoenix-contentsafety-svc"})


async def test_get_screener_http_without_endpoint_blocks(monkeypatch):
    monkeypatch.setattr(
        config_mod,
        "get_settings",
        lambda: _settings(content_safety_provider="http", content_safety_endpoint=""),
    )
    res = await get_screener().screen(_IMG)
    assert res.verdict == "block"
