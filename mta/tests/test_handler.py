"""Unit tests for the SMTP→HTTP handler (hermetic, mocked backend)."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from bvmta.server import InboundHandler


def _client(handler_fn) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url="http://backend.test",
        headers={"X-Inbound-Key": "s3cret"},
        transport=httpx.MockTransport(handler_fn),
    )


def _envelope(rcpts: list[str] | None = None, content: bytes = b"raw mail") -> SimpleNamespace:
    return SimpleNamespace(rcpt_tos=rcpts or [], content=content, original_content=content)


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch):
    from bvmta import config

    config.get_settings.cache_clear()
    monkeypatch.setenv("BVP_INBOUND_INTERNAL_SECRET", "s3cret")
    yield
    config.get_settings.cache_clear()


async def test_rcpt_accepts_known_code() -> None:
    def backend(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/internal/inbound-email/validate-rcpt"
        assert request.headers["x-inbound-key"] == "s3cret"
        return httpx.Response(200, json={"accept": True})

    handler = InboundHandler(client=_client(backend))
    env = _envelope()
    reply = await handler.handle_RCPT(None, None, env, "code+patient@inbox.example", [])
    assert reply.startswith("250")
    assert env.rcpt_tos == ["code+patient@inbox.example"]


async def test_rcpt_refuses_unknown_code() -> None:
    handler = InboundHandler(
        client=_client(lambda req: httpx.Response(404, json={"detail": "unknown recipient"}))
    )
    env = _envelope()
    reply = await handler.handle_RCPT(None, None, env, "nope+patient@inbox.example", [])
    assert reply.startswith("550")
    assert env.rcpt_tos == []


async def test_rcpt_tempfails_on_backend_down() -> None:
    def backend(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    handler = InboundHandler(client=_client(backend))
    reply = await handler.handle_RCPT(None, None, _envelope(), "x+patient@inbox.example", [])
    assert reply.startswith("451")


async def test_data_forwards_raw_per_recipient() -> None:
    seen: list[str] = []

    def backend(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/internal/inbound-email"
        assert request.content == b"raw mail"
        seen.append(request.headers["x-envelope-rcpt"])
        return httpx.Response(201, json={"accepted": True})

    handler = InboundHandler(client=_client(backend))
    env = _envelope(rcpts=["a+patient@inbox.example", "b+patient@inbox.example"])
    reply = await handler.handle_DATA(None, None, env)
    assert reply.startswith("250")
    assert seen == ["a+patient@inbox.example", "b+patient@inbox.example"]


@pytest.mark.parametrize(
    ("status", "expected"),
    [(413, "552"), (429, "451"), (500, "451"), (401, "451")],
)
async def test_data_maps_backend_errors(status: int, expected: str) -> None:
    handler = InboundHandler(client=_client(lambda req: httpx.Response(status, json={})))
    env = _envelope(rcpts=["a+patient@inbox.example"])
    reply = await handler.handle_DATA(None, None, env)
    assert reply.startswith(expected)
