"""Tests for the remote MCP HTTP transport.

Hermetic: no real Authentik, no real network. We patch
``bvmcp.auth.validate_token`` so the auth gate accepts a synthetic
:class:`Principal` and exercise the transport's auth + rate-limit
fence with the Starlette test client.
"""

from __future__ import annotations

import base64
import hashlib
from typing import Any

import pytest
from starlette.testclient import TestClient

from bvmcp import auth as auth_module
from bvmcp import oauth_shim, server_http
from bvmcp.auth import AuthError, Principal, has_scope
from bvmcp.scopes import scope_for_tool


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Return a fresh app + reset rate limiter buckets per test."""
    server_http._token_limiter = server_http._make_token_limiter()
    server_http._ip_limiter = server_http._make_ip_limiter()
    return server_http.app


@pytest.fixture
def stub_principal(monkeypatch: pytest.MonkeyPatch) -> Principal:
    principal = Principal(
        assistant_id="00000000-0000-0000-0000-0000000000aa",
        owner_subject_id="00000000-0000-0000-0000-000000000001",
        owner_email="stub@example.com",
        scope=frozenset({"documents:read"}),
        patient_ids=frozenset(),
        raw_jwt="stub.jwt.token",
    )

    async def _fake_validate(token: str) -> Principal:
        if token == "stub.jwt.token":
            return principal
        if token == "invalid":
            raise AuthError(401, "invalid token")
        raise AuthError(401, "unknown")

    monkeypatch.setattr(auth_module, "validate_token", _fake_validate)
    monkeypatch.setattr(server_http, "validate_token", _fake_validate)
    return principal


def test_health_unauthenticated(app: Any) -> None:
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["service"] == "bvmcp-http"


def test_oauth_metadata_endpoints(app: Any) -> None:
    """RFC 8414 + RFC 9728 metadata are exposed so MCP clients can
    discover the auth + resource endpoints. The shim is a thin layer
    over per-assistant bearer secrets — see :mod:`bvmcp.oauth_shim`."""
    client = TestClient(app)

    auth_meta = client.get("/.well-known/oauth-authorization-server")
    assert auth_meta.status_code == 200
    body = auth_meta.json()
    assert body["authorization_endpoint"].endswith("/mcp/authorize")
    assert body["token_endpoint"].endswith("/mcp/token")
    assert "S256" in body["code_challenge_methods_supported"]
    assert "code" in body["response_types_supported"]
    assert "authorization_code" in body["grant_types_supported"]

    res_meta = client.get("/.well-known/oauth-protected-resource")
    assert res_meta.status_code == 200
    rbody = res_meta.json()
    assert rbody["resource"]
    assert rbody["authorization_servers"]
    assert "header" in rbody["bearer_methods_supported"]


def test_register_endpoint_remains_absent(app: Any) -> None:
    """We don't support dynamic client registration: assistants are
    pre-issued in phoenix's *Settings → AI assistants*."""
    client = TestClient(app)
    assert client.get("/register").status_code == 404


def _pkce_pair() -> tuple[str, str]:
    verifier = "verifier-" + "a" * 50
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )
    return verifier, challenge


def _stub_backend_code_store(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace ``_mint_code`` / ``_consume_code`` with an in-test
    dictionary. Each test gets a private store, so collisions across
    tests are impossible."""
    store: dict[str, Any] = {}

    async def _fake_mint(
        *,
        client_id: str,
        redirect_uri: str,
        code_challenge: str,
        code_challenge_method: str,
    ) -> str:
        code = f"fake-code-{len(store)}"
        store[code] = oauth_shim._AuthCode(
            client_id=client_id,
            redirect_uri=redirect_uri,
            code_challenge=code_challenge,
            code_challenge_method=code_challenge_method,
        )
        return code

    async def _fake_consume(code: str) -> Any:
        return store.pop(code, None)

    monkeypatch.setattr(oauth_shim, "_mint_code", _fake_mint)
    monkeypatch.setattr(oauth_shim, "_consume_code", _fake_consume)
    return store


def test_authorize_redirects_with_code(app: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_backend_code_store(monkeypatch)
    client = TestClient(app)
    _, challenge = _pkce_pair()
    resp = client.get(
        "/mcp/authorize",
        params={
            "response_type": "code",
            "client_id": "bvp_agt_abc",
            "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "xyz",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302
    location = resp.headers["location"]
    assert location.startswith("https://claude.ai/api/mcp/auth_callback")
    assert "code=" in location
    assert "state=xyz" in location


def test_authorize_rejects_plain_method(app: Any) -> None:
    client = TestClient(app)
    resp = client.get(
        "/mcp/authorize",
        params={
            "response_type": "code",
            "client_id": "bvp_agt_abc",
            "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
            "code_challenge": "x" * 43,
            "code_challenge_method": "plain",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_request"


def test_authorize_rejects_http_redirect(app: Any) -> None:
    client = TestClient(app)
    _, challenge = _pkce_pair()
    resp = client.get(
        "/mcp/authorize",
        params={
            "response_type": "code",
            "client_id": "bvp_agt_abc",
            "redirect_uri": "http://evil.example.com/cb",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 400


def test_token_exchange_full_flow(app: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end: /authorize mints a code (stubbed backend), /token
    verifies PKCE and swaps it for the client_secret. The backend
    resolve hop is stubbed too so we don't need a real phoenix under
    the test."""
    _stub_backend_code_store(monkeypatch)
    seen_hashes: list[str] = []

    async def _fake_resolve(secret_hash: str) -> dict[str, Any] | None:
        seen_hashes.append(secret_hash)
        return {
            "assistant_id": "00000000-0000-0000-0000-0000000000aa",
            "owner_subject_id": "00000000-0000-0000-0000-000000000001",
            "owner_email": "owner@example.com",
            "scope": ["documents:read"],
            "patient_ids": [],
            "is_active": True,
        }

    monkeypatch.setattr(oauth_shim, "_resolve_via_backend", _fake_resolve)

    client = TestClient(app)
    verifier, challenge = _pkce_pair()
    auth_resp = client.get(
        "/mcp/authorize",
        params={
            "response_type": "code",
            "client_id": "bvp_agt_abc",
            "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "xyz",
        },
        follow_redirects=False,
    )
    location = auth_resp.headers["location"]
    code = location.split("code=")[1].split("&")[0]

    secret = "super-secret-client-credential-value"
    expected_hash = hashlib.sha256(secret.encode("utf-8")).hexdigest()

    token_resp = client.post(
        "/mcp/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
            "code_verifier": verifier,
            "client_id": "bvp_agt_abc",
            "client_secret": secret,
        },
    )
    assert token_resp.status_code == 200, token_resp.text
    body = token_resp.json()
    assert body["access_token"] == secret
    assert body["token_type"] == "Bearer"
    assert seen_hashes == [expected_hash]


def test_token_rejects_bad_pkce(app: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_backend_code_store(monkeypatch)

    async def _fake_resolve(_h: str) -> dict[str, Any] | None:
        return None

    monkeypatch.setattr(oauth_shim, "_resolve_via_backend", _fake_resolve)

    client = TestClient(app)
    _, challenge = _pkce_pair()
    location = client.get(
        "/mcp/authorize",
        params={
            "response_type": "code",
            "client_id": "bvp_agt_abc",
            "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        },
        follow_redirects=False,
    ).headers["location"]
    code = location.split("code=")[1].split("&")[0]

    resp = client.post(
        "/mcp/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
            "code_verifier": "wrong-verifier-" + "a" * 40,
            "client_id": "bvp_agt_abc",
            "client_secret": "anything",
        },
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_grant"


def test_token_rejects_unknown_secret(app: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_backend_code_store(monkeypatch)

    async def _fake_resolve(_h: str) -> dict[str, Any] | None:
        return None  # backend says "no such bearer"

    monkeypatch.setattr(oauth_shim, "_resolve_via_backend", _fake_resolve)

    client = TestClient(app)
    verifier, challenge = _pkce_pair()
    location = client.get(
        "/mcp/authorize",
        params={
            "response_type": "code",
            "client_id": "bvp_agt_abc",
            "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        },
        follow_redirects=False,
    ).headers["location"]
    code = location.split("code=")[1].split("&")[0]

    resp = client.post(
        "/mcp/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
            "code_verifier": verifier,
            "client_id": "bvp_agt_abc",
            "client_secret": "stolen-but-revoked",
        },
    )
    assert resp.status_code == 401
    assert resp.json()["error"] == "invalid_client"


def test_mcp_requires_bearer(app: Any) -> None:
    client = TestClient(app)
    resp = client.post("/mcp/", json={})
    assert resp.status_code == 401
    body = resp.json()
    assert body["status"] == 401
    assert body["type"].endswith("/unauthorized")


def test_mcp_rejects_invalid_token(app: Any, stub_principal: Principal) -> None:
    client = TestClient(app)
    resp = client.post(
        "/mcp/",
        headers={"Authorization": "Bearer invalid"},
        json={},
    )
    assert resp.status_code == 401


def _stub_session_manager(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the MCP session manager's handle_request with a no-op
    that returns 204. The transport's auth + rate-limit fences run
    *before* this hop, so we still exercise them faithfully."""

    async def _stub(scope: Any, receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    monkeypatch.setattr(server_http._session_manager, "handle_request", _stub)


def test_rate_limit_per_ip(
    app: Any, stub_principal: Principal, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_session_manager(monkeypatch)
    server_http._ip_limiter = server_http._SlidingWindowLimiter(max_hits=2, window_seconds=10)

    async def _no_audit(**_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(server_http, "_emit_audit", _no_audit)

    client = TestClient(app)
    headers = {"Authorization": "Bearer stub.jwt.token"}
    for _ in range(2):
        resp = client.post("/mcp/", headers=headers, json={})
        assert resp.status_code != 429
    resp = client.post("/mcp/", headers=headers, json={})
    assert resp.status_code == 429
    assert resp.json()["scope"] == "ip"


def test_rate_limit_per_token(
    app: Any, stub_principal: Principal, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_session_manager(monkeypatch)
    server_http._token_limiter = server_http._SlidingWindowLimiter(max_hits=1, window_seconds=10)
    server_http._ip_limiter = server_http._SlidingWindowLimiter(max_hits=100, window_seconds=10)

    async def _no_audit(**_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(server_http, "_emit_audit", _no_audit)

    client = TestClient(app)
    headers = {"Authorization": "Bearer stub.jwt.token"}
    first = client.post("/mcp/", headers=headers, json={})
    assert first.status_code != 429
    second = client.post("/mcp/", headers=headers, json={})
    assert second.status_code == 429
    assert second.json()["scope"] == "token"


def test_list_tools_filtered_by_scope() -> None:
    """A bearer scoped to ``documents:read`` only sees the tools it can
    actually invoke — not the whole ~160-tool catalogue. This is the
    token-saving win + keeps list_tools and call_tool in lockstep."""
    principal = Principal(
        assistant_id="00000000-0000-0000-0000-0000000000aa",
        owner_subject_id="00000000-0000-0000-0000-000000000001",
        owner_email="stub@example.com",
        scope=frozenset({"documents:read"}),
        patient_ids=frozenset(),
        raw_jwt="stub.jwt.token",
    )
    tools = server_http._tools_for_principal(server_http.ALL_TOOLS, principal)

    names = {t.name for t in tools}
    # Strictly smaller than the full catalogue.
    assert 0 < len(names) < len(server_http.ALL_TOOLS)
    # A documents:read tool is visible; a patients:write tool is not.
    assert "list_patient_documents" in names
    assert "update_patient" not in names
    # Every surfaced tool is genuinely invocable with this scope set
    # (no "visible but 403" entries).
    for n in names:
        required = scope_for_tool(n)
        assert required is not None and has_scope(principal, required)


def test_list_tools_no_principal_falls_back_to_all() -> None:
    """Outside an authenticated context the list is not minimised
    (call_tool still enforces scope, so this is safe)."""
    tools = server_http._tools_for_principal(server_http.ALL_TOOLS, None)
    assert len(tools) == len(server_http.ALL_TOOLS)
