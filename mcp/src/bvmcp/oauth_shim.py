"""Minimal OAuth 2.1 shim for MCP clients that require it.

ADR 0019 ships per-assistant ``client_id`` + ``client_secret`` pairs
and assumes MCP clients can carry the secret as a static
``Authorization: Bearer …`` header. Claude.ai's custom-connector flow
disagrees: when the operator pastes ``Server URL`` + ``OAuth Client
ID`` + ``OAuth Client Secret`` into Settings → Connectors, Claude
performs a real OAuth 2.1 Authorization Code + PKCE handshake against
the MCP server before issuing tool calls. With the auth endpoints
absent the dance dead-ends at ``/authorize`` and the connector cannot
attach.

This module is the thin wrapper that makes Claude.ai (and every other
MCP host that follows the official Authorization spec) happy without
re-introducing Authentik:

* ``GET /.well-known/oauth-authorization-server`` (RFC 8414) and
  ``GET /.well-known/oauth-protected-resource[/<suffix>]`` (RFC 9728)
  advertise the auth + resource metadata. Both live on the MCP host
  because we are auth server *and* resource server.
* ``GET /authorize`` — accepts the standard authorization-code
  request, mints a code through the backend RPC
  ``/api/internal/oauth-code/mint`` (so any mcp-http replica can
  redeem it), and 302s back to ``redirect_uri?code=…&state=…``.
  There is no consent page: the client is a confidential agent,
  the human approval already happened in phoenix's *Settings → AI
  assistants* when the operator minted the credential.
* ``POST /token`` — calls ``/api/internal/oauth-code/consume`` to
  pop the bound challenge, runs PKCE verification locally, hashes
  the submitted ``client_secret``, and delegates to the existing
  ``/api/internal/agent-bearer/resolve`` RPC. On a hit it returns
  ``access_token = <client_secret>`` so downstream MCP requests
  reuse the same Bearer flow that :mod:`bvmcp.auth` already
  understands.

Persisting the code in phoenix's database (rather than a per-pod
dict) is the central design choice: with multiple ``mcp-http``
replicas a process-local store would route ``/authorize`` and
``/token`` to different pods and break the handshake.

The server is intentionally not a full OAuth provider: no refresh
tokens (the static client_secret is the long-lived credential), no
dynamic client registration (clients are pre-issued in phoenix), no
introspection endpoint. Anything beyond the Claude.ai flow falls
through to a 4xx with an OAuth-shaped error body.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response

from bvmcp.auth import (  # type: ignore[attr-defined]
    AuthError,
    _resolve_via_backend,
    get_auth_settings,
)

_log = logging.getLogger("bvmcp.oauth_shim")


# ----- backend-backed code store -------------------------------------------


@dataclass(slots=True)
class _AuthCode:
    client_id: str
    redirect_uri: str
    code_challenge: str
    code_challenge_method: str


async def _backend_post(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Call an internal phoenix backend endpoint with the shared key.

    Used by mint/consume of OAuth codes. Failures bubble up as
    :class:`AuthError`; the shim translates them into OAuth-shaped
    error responses for the caller.
    """
    settings = get_auth_settings()
    if not settings.backend_internal_key:
        raise AuthError(503, "MCP auth misconfigured (BVP_MCP_BACKEND_INTERNAL_KEY unset)")
    url = f"{settings.backend_base_url.rstrip('/')}{path}"
    headers = {
        "X-Internal-Key": settings.backend_internal_key,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=settings.bearer_resolve_timeout_seconds) as c:
            resp = await c.post(url, headers=headers, json=payload)
    except httpx.HTTPError as exc:
        raise AuthError(502, f"upstream {path} failed: {exc}") from exc
    if resp.status_code == 200:
        return resp.json()
    if resp.status_code == 404:
        raise AuthError(404, f"upstream {path} returned 404")
    if resp.status_code == 401:
        raise AuthError(503, "MCP auth misconfigured (internal key rejected)")
    raise AuthError(502, f"upstream {path} unexpected status {resp.status_code}")


async def _mint_code(
    *,
    client_id: str,
    redirect_uri: str,
    code_challenge: str,
    code_challenge_method: str,
) -> str:
    payload = await _backend_post(
        "/api/internal/oauth-code/mint",
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code_challenge": code_challenge,
            "code_challenge_method": code_challenge_method,
        },
    )
    return str(payload["code"])


async def _consume_code(code: str) -> _AuthCode | None:
    """Pop a previously-minted code. ``None`` when unknown or expired."""
    try:
        payload = await _backend_post("/api/internal/oauth-code/consume", {"code": code})
    except AuthError as exc:
        if exc.status_code == 404:
            return None
        raise
    return _AuthCode(
        client_id=str(payload["client_id"]),
        redirect_uri=str(payload["redirect_uri"]),
        code_challenge=str(payload["code_challenge"]),
        code_challenge_method=str(payload["code_challenge_method"]),
    )


# ----- helpers -------------------------------------------------------------


def _issuer(request: Request) -> str:
    """Return the externally-visible base URL.

    We trust ``X-Forwarded-Proto`` / ``X-Forwarded-Host`` (Traefik
    sets both); :func:`Request.url_for` already honours those when
    uvicorn runs with ``proxy_headers=True``. A static override via
    ``BVP_MCP_PUBLIC_ISSUER`` wins when set, for environments where
    the host header is unreliable."""
    import os

    override = os.getenv("BVP_MCP_PUBLIC_ISSUER", "").strip()
    if override:
        return override.rstrip("/")
    scheme = request.url.scheme
    host = request.headers.get("host", request.url.netloc)
    return f"{scheme}://{host}"


def _verify_pkce(code_verifier: str, code_challenge: str, method: str) -> bool:
    if method.upper() != "S256":
        return False
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return secrets.compare_digest(expected, code_challenge)


def _hash_secret(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def _oauth_error(
    *,
    status_code: int,
    error: str,
    description: str | None = None,
) -> JSONResponse:
    payload: dict[str, Any] = {"error": error}
    if description:
        payload["error_description"] = description
    _log.info(
        "oauth_shim error: status=%s error=%s description=%s", status_code, error, description
    )
    return JSONResponse(payload, status_code=status_code)


def _is_acceptable_redirect(uri: str) -> bool:
    """Accept https:// callbacks and localhost loopbacks. ``http://``
    on a non-loopback host is rejected — the client_secret travels
    through the redirect-URI host's TLS terminator in the auth-code
    response, so cleartext callbacks would leak it."""
    if uri.startswith("https://"):
        return True
    return uri.startswith("http://localhost") or uri.startswith("http://127.0.0.1")


# ----- endpoints -----------------------------------------------------------


async def authorization_server_metadata(request: Request) -> JSONResponse:
    """RFC 8414 metadata so MCP clients can discover the auth endpoints.

    Branding fields (``logo_uri``, ``op_logo_uri``, ``client_uri``,
    ``service_documentation``, ``op_policy_uri``, ``op_tos_uri``)
    are optional in RFC 8414 / OIDC Discovery and let clients like
    Claude.ai's Custom Connector dialog render the BitVision logo +
    name instead of a generic placeholder when the user authorises
    the integration. Both ``logo_uri`` and ``op_logo_uri`` are sent
    because different clients pick one or the other.
    """
    import os

    iss = _issuer(request)
    # The brand assets live on the public-facing frontend host; the
    # connector dialog fetches them client-side.
    brand_host = os.getenv("BVP_PUBLIC_BRAND_HOST", "https://bitvision.example")
    logo_uri = f"{brand_host}/brand/icon.png"
    return JSONResponse(
        {
            "issuer": iss,
            # Endpoints live under /mcp (see server_http route table) so a
            # single ingress path covers the whole flow. The issuer stays
            # at the host root so the RFC 8414 metadata document remains at
            # ``{host}/.well-known/oauth-authorization-server`` (no path
            # insertion) — OAuth does not require the endpoints to sit
            # under the issuer path.
            "authorization_endpoint": f"{iss}/mcp/authorize",
            "token_endpoint": f"{iss}/mcp/token",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": [
                "client_secret_post",
                "client_secret_basic",
            ],
            "scopes_supported": [],
            # --- branding (optional, picked up by Claude.ai etc.) ---
            "service_documentation": brand_host,
            "client_uri": brand_host,
            "logo_uri": logo_uri,
            "op_logo_uri": logo_uri,
            "op_policy_uri": f"{brand_host}/privacy",
            "op_tos_uri": f"{brand_host}/terms",
        }
    )


async def protected_resource_metadata(request: Request) -> JSONResponse:
    """RFC 9728 metadata advertising this MCP server as a protected
    resource and pointing clients at the co-located auth server.

    The MCP Authorization spec (and RFC 9728 §3) constructs the
    well-known URL by inserting ``/.well-known/oauth-protected-resource``
    between the host and the resource path: a resource at
    ``https://host/mcp`` exposes its metadata at
    ``https://host/.well-known/oauth-protected-resource/mcp``. We
    serve both the path-suffixed variant and the bare one and let the
    captured suffix flow back into the ``resource`` field so clients
    that registered with ``…/mcp`` see a stable identifier.
    """
    import os

    iss = _issuer(request)
    suffix = request.path_params.get("suffix", "").strip("/")
    # Bare well-known defaults to the canonical MCP resource path
    # rather than the host root, since the MCP transport lives at
    # /mcp (and clients that paste only the host into Claude.ai's
    # connector dialog still need that as the resource id).
    resource_url = f"{iss}/{suffix}" if suffix else f"{iss}/mcp"
    brand_host = os.getenv("BVP_PUBLIC_BRAND_HOST", "https://bitvision.example")
    return JSONResponse(
        {
            "resource": resource_url,
            "authorization_servers": [iss],
            "bearer_methods_supported": ["header"],
            # Branding fields surfaced by some MCP clients (Claude.ai
            # Custom Connector picks them up to render the BitVision
            # logo + name instead of a generic placeholder).
            "resource_name": "BitVision",
            "resource_documentation": brand_host,
            "logo_uri": f"{brand_host}/brand/icon.png",
        }
    )


async def authorize_endpoint(request: Request) -> Response:
    """Authorization code request. We don't render a consent page —
    the operator already chose this assistant's scope in phoenix. We
    bind the PKCE challenge to a fresh code and bounce back."""
    params = request.query_params
    response_type = params.get("response_type", "")
    client_id = params.get("client_id", "").strip()
    redirect_uri = params.get("redirect_uri", "").strip()
    code_challenge = params.get("code_challenge", "").strip()
    code_challenge_method = params.get("code_challenge_method", "S256").strip()
    state = params.get("state", "")

    if response_type != "code":
        return _oauth_error(
            status_code=400,
            error="unsupported_response_type",
            description="only response_type=code is supported",
        )
    if not client_id or not client_id.startswith("bvp_agt_"):
        return _oauth_error(
            status_code=400,
            error="invalid_request",
            description="client_id missing or malformed",
        )
    if not redirect_uri or not _is_acceptable_redirect(redirect_uri):
        return _oauth_error(
            status_code=400,
            error="invalid_request",
            description="redirect_uri missing or not https",
        )
    if not code_challenge:
        return _oauth_error(
            status_code=400,
            error="invalid_request",
            description="code_challenge required (PKCE)",
        )
    if code_challenge_method.upper() != "S256":
        return _oauth_error(
            status_code=400,
            error="invalid_request",
            description="code_challenge_method must be S256",
        )

    try:
        code = await _mint_code(
            client_id=client_id,
            redirect_uri=redirect_uri,
            code_challenge=code_challenge,
            code_challenge_method=code_challenge_method,
        )
    except AuthError as exc:
        _log.warning("authorize endpoint backend mint failed: %s", exc.detail)
        return _oauth_error(
            status_code=exc.status_code, error="server_error", description=exc.detail
        )

    target_qs = {"code": code}
    if state:
        target_qs["state"] = state
    sep = "&" if "?" in redirect_uri else "?"
    target = f"{redirect_uri}{sep}{urlencode(target_qs)}"
    return RedirectResponse(target, status_code=302)


def _basic_auth_creds(authorization: str | None) -> tuple[str, str] | None:
    if not authorization or not authorization.lower().startswith("basic "):
        return None
    try:
        raw = base64.b64decode(authorization[6:].strip()).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    if ":" not in raw:
        return None
    cid, _, sec = raw.partition(":")
    return cid, sec


async def token_endpoint(request: Request) -> Response:
    """Exchange a code for an access token.

    The "access token" we mint is literally the assistant's
    client_secret — :mod:`bvmcp.auth` resolves it via the existing
    backend RPC on every MCP call. That keeps the bearer life-cycle
    (rotate, revoke, scope changes) centred on the
    ``AgentAssistant`` row and avoids minting a second short-lived
    token whose revocation we'd have to track separately."""
    content_type = request.headers.get("content-type", "")
    body: dict[str, Any] = {}
    if "application/json" in content_type:
        try:
            body = await request.json()
            if not isinstance(body, dict):
                body = {}
        except Exception:
            body = {}
    else:
        try:
            form = await request.form()
            body = dict(form.items())
        except Exception:
            return _oauth_error(
                status_code=400, error="invalid_request", description="form body required"
            )

    grant_type = str(body.get("grant_type", ""))
    code = str(body.get("code", "")).strip()
    redirect_uri = str(body.get("redirect_uri", "")).strip()
    code_verifier = str(body.get("code_verifier", "")).strip()
    form_client_id = str(body.get("client_id", "")).strip()
    form_client_secret = str(body.get("client_secret", "")).strip()

    basic = _basic_auth_creds(request.headers.get("authorization"))
    if basic is not None:
        if not form_client_id:
            form_client_id = basic[0]
        if not form_client_secret:
            form_client_secret = basic[1]

    _log.info(
        "token request: ct=%s keys=%s grant=%s code=%s redirect=%s cid=%s cs=%s cv=%s basic=%s",
        content_type,
        sorted(body.keys()),
        grant_type,
        f"{code[:6]}…" if code else "<empty>",
        redirect_uri,
        form_client_id,
        f"{form_client_secret[:4]}…({len(form_client_secret)})"
        if form_client_secret
        else "<empty>",
        f"{code_verifier[:6]}…({len(code_verifier)})" if code_verifier else "<empty>",
        "yes" if basic else "no",
    )

    if grant_type != "authorization_code":
        return _oauth_error(
            status_code=400,
            error="unsupported_grant_type",
            description="only authorization_code is supported",
        )
    if not code or not code_verifier or not form_client_id or not form_client_secret:
        return _oauth_error(
            status_code=400,
            error="invalid_request",
            description="code, code_verifier, client_id and client_secret are required",
        )

    try:
        entry = await _consume_code(code)
    except AuthError as exc:
        _log.warning("token endpoint backend consume failed: %s", exc.detail)
        return _oauth_error(
            status_code=exc.status_code, error="server_error", description=exc.detail
        )
    if entry is None:
        return _oauth_error(
            status_code=400, error="invalid_grant", description="code unknown or expired"
        )
    if not secrets.compare_digest(entry.client_id, form_client_id):
        return _oauth_error(
            status_code=400, error="invalid_grant", description="client_id mismatch"
        )
    if redirect_uri and not secrets.compare_digest(entry.redirect_uri, redirect_uri):
        return _oauth_error(
            status_code=400, error="invalid_grant", description="redirect_uri mismatch"
        )
    if not _verify_pkce(code_verifier, entry.code_challenge, entry.code_challenge_method):
        return _oauth_error(
            status_code=400, error="invalid_grant", description="PKCE verification failed"
        )

    secret_hash = _hash_secret(form_client_secret)
    try:
        payload = await _resolve_via_backend(secret_hash)
    except AuthError as exc:
        _log.warning("token endpoint upstream error: %s", exc.detail)
        return _oauth_error(status_code=503, error="server_error", description=exc.detail)
    if payload is None:
        return _oauth_error(
            status_code=401,
            error="invalid_client",
            description="client_secret rejected",
        )

    return JSONResponse(
        {
            "access_token": form_client_secret,
            "token_type": "Bearer",
            # The bearer is a static long-lived secret. We surface a
            # nominal expiry so well-behaved clients refresh through
            # the connector if it ever becomes a JWT, but in practice
            # the secret is valid until the operator rotates or
            # revokes it.
            "expires_in": 31536000,
            "scope": " ".join(sorted(payload.get("scope", []) or [])),
        },
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


__all__ = [
    "authorization_server_metadata",
    "authorize_endpoint",
    "protected_resource_metadata",
    "token_endpoint",
]
