"""Remote MCP HTTP transport.

Hosts the same tool registry as :mod:`bvmcp.server` (the local stdio
transport), exposed on an HTTP endpoint guarded by per-assistant
bearer secrets minted by the phoenix backend. Each AI client (Claude
custom connector, Cursor, …) uses one secret; revocation is a single
``is_active = false`` toggle on the phoenix-side ``AgentAssistant``
row.

Endpoints
---------
* ``POST/GET/DELETE /mcp`` — the MCP Streamable HTTP transport. Each
  request is authenticated via :func:`bvmcp.auth.validate_token`,
  which sha256s the bearer and asks the phoenix backend (in-cluster
  RPC, ``X-Internal-Key`` shared secret) to resolve it to a
  Principal. The Principal is stashed on a ContextVar so tool
  handlers can forward the bearer + assistant_id to the backend.
* ``GET /health`` — liveness probe used by Kubernetes / Traefik.
* OAuth 2.1 + PKCE shim (``/.well-known/oauth-authorization-server``,
  ``/.well-known/oauth-protected-resource``, ``/authorize``,
  ``/token``) — see :mod:`bvmcp.oauth_shim`. The shim wraps the
  per-assistant bearer model so MCP clients that follow the official
  Authorization spec (Claude.ai is the immediate consumer) can
  complete the standard handshake; the access token that comes out of
  ``/token`` is the assistant's static ``client_secret``, validated on
  every MCP call by :mod:`bvmcp.auth`.

Run with::

    uv run python -m bvmcp.server_http   # binds to 0.0.0.0:8080

Or via the Dockerfile in ``mcp/Dockerfile.http``.
"""

from __future__ import annotations

import contextlib
import logging
import os
import time
from collections import defaultdict
from collections.abc import AsyncIterator
from contextvars import ContextVar

import httpx
import uvicorn
from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.types import ImageContent, TextContent, Tool
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.types import Message, Receive, Scope, Send

from bvmcp import __version__, oauth_shim
from bvmcp._tool_registry import TOOL_MODULES
from bvmcp.auth import AuthError, Principal, has_scope, validate_token
from bvmcp.feature_flags import get_feature_flags, is_tool_available
from bvmcp.scopes import scope_for_tool
from bvmcp.tools.client import format_http_error

_log = logging.getLogger("bvmcp.server_http")


# Context variable holding the authenticated JWT for the in-flight
# request. Tool handlers use :func:`current_principal` to read it.
# ContextVar (vs. thread-local) is the correct primitive for asyncio
# concurrency.
_principal_ctx: ContextVar[Principal | None] = ContextVar("bvmcp_principal", default=None)


def current_principal() -> Principal | None:
    """Return the authenticated principal for the in-flight HTTP MCP
    call, or ``None`` outside an HTTP context (e.g. stdio transport)."""
    return _principal_ctx.get()


# ---- MCP server (same registry as the stdio variant) -----------------------
#
# The canonical tool registry lives in ``bvmcp._tool_registry`` and is
# shared with the stdio variant. Pre-2026-05-03 each transport kept
# its own copy of the tuple — ``care_phases`` made it onto stdio but
# was missed on HTTP, so the Claude.ai connector reported
# ``Tool 'BitVision:create_care_phase' not found`` even though the
# stdio process happily exposed it. The single registry rules out
# that drift by construction.

_TOOL_MODULES = TOOL_MODULES

ALL_TOOLS: list[Tool] = [tool for mod in _TOOL_MODULES for tool in mod.TOOLS]

_HANDLERS: dict[str, object] = {}
for _mod in _TOOL_MODULES:
    for _tool in _mod.TOOLS:
        _HANDLERS[_tool.name] = _mod.handle


server: Server = Server("bitvision-phoenix-http")


def _tools_for_principal(available: list[Tool], principal: Principal | None) -> list[Tool]:
    """Filter a (feature-flag-available) tool list down to what the
    principal can actually invoke.

    Pure function — no I/O, no ContextVar — so the scope-visibility
    rule is unit-testable without the MCP ``@server.list_tools()``
    decorator wrapping the call signature.

    Two wins over exposing the whole catalogue:
    (1) list_tools and call_tool stay in lockstep — no "visible but
        403" tools that mislead the agent's planner;
    (2) the schema payload (the dominant MCP token cost) shrinks for
        narrowly-scoped assistants — a read-only documents assistant
        sees a handful of tools instead of the whole ~160-tool set.

    ``principal is None`` (no authenticated context) returns the input
    unchanged: call_tool still enforces scope, so it is safe, just not
    minimised. A tool with no scope-catalog entry is fail-closed in
    call_tool, so it is omitted here too.
    """
    if principal is None:
        return available
    visible: list[Tool] = []
    for t in available:
        required = scope_for_tool(t.name)
        if required is not None and has_scope(principal, required):
            visible.append(t)
    return visible


@server.list_tools()
async def _list_tools() -> list[Tool]:
    flags = await get_feature_flags()
    available = [t for t in ALL_TOOLS if is_tool_available(t.name, flags)]
    return _tools_for_principal(available, current_principal())


@server.call_tool()
async def _call_tool(name: str, arguments: dict | None) -> list[TextContent | ImageContent]:
    flags = await get_feature_flags()
    if not is_tool_available(name, flags):
        return [
            TextContent(
                type="text",
                text=(
                    f"Error: tool '{name}' is currently disabled — "
                    "the backend reports this feature is not configured. "
                    "BYO mode advice: classify in your own LLM and use "
                    "create_care_phase + assign_event_to_phase directly."
                ),
            )
        ]
    handler = _HANDLERS.get(name)
    if handler is None:
        return [TextContent(type="text", text=f"Error: unknown tool '{name}'")]

    # Scope enforcement (D6). Fail-closed: a tool that does not appear
    # in the catalog cannot be invoked, regardless of bearer scope.
    # The catalog lives in :mod:`bvmcp.scopes`; every new tool MUST
    # register there.
    required = scope_for_tool(name)
    if required is None:
        return [
            TextContent(
                type="text",
                text=f"Error: tool '{name}' has no scope catalog entry; "
                f"refusing invocation (server-side fail-closed).",
            )
        ]
    principal = current_principal()
    if principal is None:
        return [
            TextContent(
                type="text",
                text="Error: tool invocation outside an authenticated HTTP context",
            )
        ]
    # Use ``has_scope`` (not a raw ``required in principal.scope``) so
    # tokens granted under the legacy singular catalog
    # (``patient:read``, ``consultation:read``, ...) keep working
    # against tools that declare the MCP-canonical plural names
    # (``patients:read``, ``events:read``, ...). The alias map +
    # ``documents:download`` → implies ``documents:read`` hierarchy
    # live in ``bvmcp.auth._SCOPE_ALIASES``.
    if not has_scope(principal, required):
        return [
            TextContent(
                type="text",
                text=f"Error: missing required scope '{required}' "
                f"for tool '{name}'. Granted: {sorted(principal.scope)}.",
            )
        ]

    try:
        result = await handler(name, arguments or {})  # type: ignore[operator]
    except httpx.HTTPStatusError as exc:
        # Specialise on HTTPStatusError so the backend's structured
        # detail (Pydantic loc/msg/type, RFC 7807 problem,
        # ``required_scope``) reaches the agent intact. Without this
        # the generic ``f"Error: {exc}"`` below would collapse the
        # response body into the one-line repr of the exception, and
        # every write tool would need its own try/except to recover
        # the diagnostic information.
        return [TextContent(type="text", text=format_http_error(exc))]
    except Exception as exc:  # pragma: no cover - tool-level error fence
        return [TextContent(type="text", text=f"Error: {exc}")]
    if isinstance(result, str):
        return [TextContent(type="text", text=result)]
    return list(result)


# ---- HTTP wiring -----------------------------------------------------------


_session_manager = StreamableHTTPSessionManager(server, stateless=True)


# ---- Rate limit (ADR 0018 §"Rate limit per token + per IP") ---------------


class _SlidingWindowLimiter:
    """In-process sliding-window counter, keyed by an opaque string.

    Default budget matches ADR 0018: 50 req/s per token, 200 req/s per
    IP. The limiter is intentionally process-local — a Redis-backed
    variant is appropriate once the deployment scales past two
    replicas. For now, two replicas with a 50 req/s budget per token
    means an attacker who spreads load round-robin doubles the cap to
    100 req/s, which is acceptable for incident response horizon.
    """

    def __init__(self, *, max_hits: int, window_seconds: float) -> None:
        self._max = max_hits
        self._window = window_seconds
        self._buckets: dict[str, list[float]] = defaultdict(list)

    def check(self, key: str, now: float | None = None) -> bool:
        now = now or time.monotonic()
        floor = now - self._window
        bucket = self._buckets[key]
        # Drop hits older than the window.
        bucket[:] = [ts for ts in bucket if ts > floor]
        if len(bucket) >= self._max:
            return False
        bucket.append(now)
        return True


def _make_token_limiter() -> _SlidingWindowLimiter:
    cap = int(os.getenv("BVP_MCP_RATE_LIMIT_PER_TOKEN", "50"))
    window = float(os.getenv("BVP_MCP_RATE_LIMIT_WINDOW_SECONDS", "1"))
    return _SlidingWindowLimiter(max_hits=cap, window_seconds=window)


def _make_ip_limiter() -> _SlidingWindowLimiter:
    cap = int(os.getenv("BVP_MCP_RATE_LIMIT_PER_IP", "200"))
    window = float(os.getenv("BVP_MCP_RATE_LIMIT_WINDOW_SECONDS", "1"))
    return _SlidingWindowLimiter(max_hits=cap, window_seconds=window)


_token_limiter = _make_token_limiter()
_ip_limiter = _make_ip_limiter()


def _client_ip(scope: Scope) -> str:
    """Extract the originating IP, honouring trusted proxies' X-Forwarded-For."""
    for name, value in scope.get("headers", []):
        if name == b"x-forwarded-for":
            decoded = value.decode("latin-1", errors="replace")
            first = decoded.split(",")[0].strip()
            if first:
                return first
        if name == b"x-real-ip":
            return value.decode("latin-1", errors="replace").strip()
    client = scope.get("client")
    if client and isinstance(client, (tuple, list)) and client:
        return str(client[0])
    return "unknown"


def _bearer_token(scope: Scope) -> str | None:
    """Extract the bearer token from an ASGI scope's headers."""
    for name, value in scope.get("headers", []):
        if name == b"authorization":
            decoded = value.decode("latin-1", errors="replace")
            if decoded.lower().startswith("bearer "):
                return decoded[7:].strip()
    return None


async def _mcp_endpoint(scope: Scope, receive: Receive, send: Send) -> None:
    """Authenticate, then delegate to the MCP session manager.

    Auth failures short-circuit with a Problem-Details JSON body.
    """
    if scope["type"] != "http":
        await _session_manager.handle_request(scope, receive, send)
        return

    ip = _client_ip(scope)
    if not _ip_limiter.check(ip):
        await _send_rate_limit_error(send, scope_kind="ip")
        return

    token = _bearer_token(scope)
    if token is None:
        await _send_auth_error(send, 401, "Bearer token required")
        return

    if not _token_limiter.check(token):
        await _send_rate_limit_error(send, scope_kind="token")
        return

    try:
        principal = await validate_token(token)
    except AuthError as exc:
        await _send_auth_error(send, exc.status_code, exc.detail)
        return

    set_token = _principal_ctx.set(principal)
    status_holder: dict[str, int] = {"code": 0}

    async def _send_with_capture(message: Message) -> None:
        if message.get("type") == "http.response.start":
            status_holder["code"] = int(message.get("status", 0))
        await send(message)

    try:
        await _session_manager.handle_request(scope, receive, _send_with_capture)
    finally:
        _principal_ctx.reset(set_token)
        # Fire-and-forget audit. Failures are swallowed inside the
        # helper; we still want to record the IP / status / scope set
        # for forensic correlation.
        await _emit_audit(
            principal=principal,
            ip=ip,
            status_code=status_holder["code"],
        )


async def _emit_audit(
    *,
    principal: Principal,
    ip: str,
    status_code: int,
) -> None:
    """Fire-and-forget audit hop to the phoenix backend.

    The backend exposes ``POST /api/audit/mcp`` (see
    ``backend/src/bvphoenix/api/audit.py``) — it accepts a small JSON
    payload describing the request and writes a row to ``audit_log``
    with ``action='mcp_http_request'``.

    Failures are logged at warning level but never raised: a missing
    audit row should not break the user-visible call.
    """
    import httpx as _httpx

    from bvmcp.config import get_settings as _get_mcp_settings

    settings = _get_mcp_settings()
    base_url = settings.backend_base_url.rstrip("/")
    if not base_url:
        return
    payload = {
        "subject_id": principal.owner_subject_id,
        "scopes": sorted(principal.scope),
        "assistant_id": principal.assistant_id,
        "ip": ip,
        "status_code": status_code,
    }
    headers = {"Authorization": f"Bearer {principal.raw_jwt}"}
    try:
        async with _httpx.AsyncClient(timeout=2.0) as client:
            await client.post(
                f"{base_url}/api/audit/mcp",
                json=payload,
                headers=headers,
            )
    except Exception as exc:  # pragma: no cover - defensive
        _log.warning("mcp_http audit failed: %s", exc)


async def _send_rate_limit_error(send: Send, *, scope_kind: str) -> None:
    detail = (
        "rate limit exceeded for this token; retry shortly"
        if scope_kind == "token"
        else "rate limit exceeded for this client; retry shortly"
    )
    payload = (
        b'{"type":"https://bitvision.example/errors/rate_limited",'
        b'"title":"Rate limit exceeded",'
        b'"status":429,'
        b'"detail":' + _quote_json_str(detail).encode() + b","
        b'"scope":' + _quote_json_str(scope_kind).encode() + b"}"
    )
    await send(
        {
            "type": "http.response.start",
            "status": 429,
            "headers": [
                (b"content-type", b"application/problem+json"),
                (b"content-length", str(len(payload)).encode()),
                (b"retry-after", b"1"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": payload})


async def _send_auth_error(send: Send, status_code: int, detail: str) -> None:
    """Emit a small Problem Details JSON body for an auth failure."""
    payload = (
        b'{"type":"https://bitvision.example/errors/unauthorized",'
        b'"title":"Authentication required",'
        b'"status":' + str(status_code).encode() + b","
        b'"detail":' + _quote_json_str(detail).encode() + b"}"
    )
    await send(
        {
            "type": "http.response.start",
            "status": status_code,
            "headers": [
                (b"content-type", b"application/problem+json"),
                (b"content-length", str(len(payload)).encode()),
                (
                    b"www-authenticate",
                    b'Bearer realm="mcp", error="invalid_token"',
                ),
            ],
        }
    )
    await send({"type": "http.response.body", "body": payload})


def _quote_json_str(value: str) -> str:
    import json

    return json.dumps(value)


# ---- Starlette app ---------------------------------------------------------


async def _health(_request: Request) -> JSONResponse:
    """Liveness probe — never touches the backend."""
    return JSONResponse(
        {
            "status": "ok",
            "service": "bvmcp-http",
            "version": __version__,
            "tools": len(ALL_TOOLS),
        }
    )


@contextlib.asynccontextmanager
async def _lifespan(_app: Starlette) -> AsyncIterator[None]:
    async with _session_manager.run():
        _log.info("bvmcp-http %s online with %d tools", __version__, len(ALL_TOOLS))
        yield
        _log.info("bvmcp-http shutting down")


def build_app() -> Starlette:
    """Construct the Starlette ASGI app for the MCP HTTP server.

    Kept as a factory so tests can stand up an isolated instance.
    """
    routes = [
        Route("/health", endpoint=_health, methods=["GET"]),
        Route(
            "/.well-known/oauth-authorization-server",
            endpoint=oauth_shim.authorization_server_metadata,
            methods=["GET"],
        ),
        Route(
            "/.well-known/oauth-protected-resource",
            endpoint=oauth_shim.protected_resource_metadata,
            methods=["GET"],
        ),
        Route(
            "/.well-known/oauth-protected-resource/{suffix:path}",
            endpoint=oauth_shim.protected_resource_metadata,
            methods=["GET"],
        ),
        # /authorize + /token are served UNDER /mcp (not the host root) so
        # that a single ingress path (/mcp) covers the entire OAuth dance
        # when the MCP server shares a hostname with the app (bitvision.xeno
        # .garden/mcp). Only the RFC-8414/9728 ``/.well-known`` documents
        # must stay at the host root — everything else lives under /mcp.
        # These exact routes MUST precede ``Mount("/mcp")`` so Starlette
        # matches them before the catch-all mount swallows /mcp/*.
        Route("/mcp/authorize", endpoint=oauth_shim.authorize_endpoint, methods=["GET"]),
        Route("/mcp/token", endpoint=oauth_shim.token_endpoint, methods=["POST"]),
        Mount("/mcp", app=_mcp_endpoint),
    ]
    return Starlette(routes=routes, lifespan=_lifespan)


app = build_app()


def main() -> None:
    """Entry point for ``python -m bvmcp.server_http``."""
    host = os.getenv("BVP_MCP_HTTP_HOST", "0.0.0.0")
    port = int(os.getenv("BVP_MCP_HTTP_PORT", "8080"))
    uvicorn.run(
        "bvmcp.server_http:app",
        host=host,
        port=port,
        log_level=os.getenv("BVP_MCP_HTTP_LOG_LEVEL", "info"),
        # Trust X-Forwarded-* headers from the in-cluster Traefik in
        # front of us. Without this Starlette generates redirects with
        # the internal scheme (``http://``) even when the original
        # request was HTTPS, breaking client redirect-following on
        # ``POST /mcp`` -> ``/mcp/``.
        proxy_headers=True,
        forwarded_allow_ips="*",
    )


if __name__ == "__main__":
    main()


# Compatibility shim: avoid an unused-import warning for ``Response``,
# which the Mount ASGI signature does not directly reference.
_ = Response
