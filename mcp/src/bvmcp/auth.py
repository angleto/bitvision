"""Bearer-token auth for the MCP HTTP transport.

Each AI assistant configured in bitvision phoenix carries its own
``client_secret`` (generated server-side at create / rotate time and
shown to the operator exactly once). The MCP gate trusts whichever
bearer matches an active assistant's secret hash:

1. ``Authorization: Bearer <secret>`` arrives on ``POST /mcp/``.
2. We sha256-hex the bearer.
3. We POST it to the phoenix backend's
   ``/api/internal/agent-bearer/resolve`` endpoint, authenticated
   with the in-cluster shared key ``BVP_INTERNAL_API_KEY``.
4. The backend returns ``{owner_subject_id, scope, patient_ids,
   …}`` on a hit, 404 on miss, 401 on internal-key mismatch.
5. We build a :class:`Principal` and let the request proceed.

Caching: the resolve call is cheap but not free (an in-cluster RPC
per inbound MCP request would burn latency). We keep a tiny TTL
cache (default 60s) keyed by the secret hash. Revocation
(``is_active=false``, rotate, delete) takes effect at most one
TTL after the change — acceptable for clinical agent flows.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


@dataclass(slots=True)
class Principal:
    """Identity + capabilities of the calling AI assistant."""

    assistant_id: str
    owner_subject_id: str
    owner_email: str
    scope: frozenset[str]
    patient_ids: frozenset[str]
    raw_jwt: str  # backwards-compat alias for the bearer; downstream
    # auditing keeps calling the field ``raw_jwt`` even
    # though it's now an opaque secret, not a JWT.


class AuthError(Exception):
    """Raised when the bearer is missing, malformed, or rejected by
    the backend. ``status_code`` is the HTTP code the gate should
    surface to the client (always 401 / 403 in practice)."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class AuthSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="BVP_MCP_",
        env_file=(".env", "../.env"),
        extra="ignore",
    )

    # URL of the phoenix backend (in-cluster Service typically). The
    # bearer-resolve RPC lives at ``<backend_base_url>/api/internal/
    # agent-bearer/resolve``.
    backend_base_url: str = Field(default="http://bvphoenix-backend:80")
    # Shared key sent in ``X-Internal-Key`` for the resolve call.
    # Empty disables auth entirely (test-only); production deploys
    # set it via the ``bvphoenix-internal`` Secret.
    backend_internal_key: str = Field(default="")
    # Seconds to cache a successful lookup. Trade-off: longer means
    # less RPC traffic but slower revocation.
    bearer_cache_ttl_seconds: int = Field(default=60)
    # Seconds to cache a *negative* lookup (404 / 401). Shorter so
    # an operator who rotated a secret sees the new one work
    # quickly, but non-zero so a brute-force attacker doesn't get
    # one DB query per attempt.
    bearer_cache_neg_ttl_seconds: int = Field(default=10)
    bearer_resolve_timeout_seconds: float = Field(default=3.0)
    # Set ``true`` only in test harnesses. When true the gate
    # accepts any bearer and synthesises a Principal with no scope
    # / no patients. NEVER set in production.
    auth_disabled: bool = Field(default=False)


_cached_settings: AuthSettings | None = None


def get_auth_settings() -> AuthSettings:
    global _cached_settings
    if _cached_settings is None:
        _cached_settings = AuthSettings()
    return _cached_settings


@dataclass(slots=True)
class _CacheEntry:
    principal: Principal | None  # None = negative cache
    expires_at: float


@dataclass(slots=True)
class _BearerCache:
    entries: dict[str, _CacheEntry] = field(default_factory=dict)

    def get(self, key: str) -> _CacheEntry | None:
        entry = self.entries.get(key)
        if entry is None:
            return None
        if time.time() >= entry.expires_at:
            self.entries.pop(key, None)
            return None
        return entry

    def put(self, key: str, principal: Principal | None, ttl: float) -> None:
        self.entries[key] = _CacheEntry(principal=principal, expires_at=time.time() + ttl)

    def invalidate(self, key: str) -> None:
        self.entries.pop(key, None)


_cache = _BearerCache()


def _hash_bearer(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


async def _resolve_via_backend(secret_hash: str) -> dict[str, Any] | None:
    """POST the secret hash to the backend. Returns the principal
    payload on success; None on 404; raises AuthError on any other
    failure path."""
    settings = get_auth_settings()
    if not settings.backend_internal_key:
        # Fail closed — never call the backend without a key (an
        # unconfigured deploy would otherwise run open).
        raise AuthError(503, "MCP auth misconfigured (BVP_MCP_BACKEND_INTERNAL_KEY unset)")
    url = f"{settings.backend_base_url.rstrip('/')}/api/internal/agent-bearer/resolve"
    headers = {
        "X-Internal-Key": settings.backend_internal_key,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    body = {"secret_hash": secret_hash}
    try:
        async with httpx.AsyncClient(timeout=settings.bearer_resolve_timeout_seconds) as c:
            resp = await c.post(url, headers=headers, json=body)
    except httpx.HTTPError as exc:
        raise AuthError(502, f"upstream resolve failed: {exc}") from exc
    if resp.status_code == 200:
        return resp.json()
    if resp.status_code == 404:
        return None
    if resp.status_code == 401:
        raise AuthError(503, "MCP auth misconfigured (internal key rejected)")
    raise AuthError(502, f"upstream resolve unexpected status {resp.status_code}")


async def validate_token(token: str) -> Principal:
    """Validate a bearer secret and return a :class:`Principal`.

    Raises :class:`AuthError` on any validation failure.
    """
    settings = get_auth_settings()

    if settings.auth_disabled:
        return Principal(
            assistant_id="test",
            owner_subject_id="00000000-0000-0000-0000-000000000000",
            owner_email="test@local",
            scope=frozenset(),
            patient_ids=frozenset(),
            raw_jwt=token,
        )

    candidate = token.strip()
    if not candidate or len(candidate) < 16:
        raise AuthError(401, "bearer token missing or malformed")

    secret_hash = _hash_bearer(candidate)

    cached = _cache.get(secret_hash)
    if cached is not None:
        if cached.principal is None:
            raise AuthError(401, "invalid bearer token")
        return Principal(
            assistant_id=cached.principal.assistant_id,
            owner_subject_id=cached.principal.owner_subject_id,
            owner_email=cached.principal.owner_email,
            scope=cached.principal.scope,
            patient_ids=cached.principal.patient_ids,
            raw_jwt=token,
        )

    payload = await _resolve_via_backend(secret_hash)
    if payload is None:
        _cache.put(secret_hash, None, settings.bearer_cache_neg_ttl_seconds)
        raise AuthError(401, "invalid bearer token")

    principal = Principal(
        assistant_id=str(payload.get("assistant_id", "")),
        owner_subject_id=str(payload.get("owner_subject_id", "")),
        owner_email=str(payload.get("owner_email", "")),
        scope=frozenset(payload.get("scope", []) or []),
        patient_ids=frozenset(payload.get("patient_ids", []) or []),
        raw_jwt=token,
    )
    _cache.put(secret_hash, principal, settings.bearer_cache_ttl_seconds)
    return principal


_SCOPE_ALIASES: dict[str, frozenset[str]] = {
    # Two scope catalogs evolved independently: the *backend*
    # ``ai_assistants`` UI hands out singular legacy names ("patient:read",
    # "consultation:read", "patient:images") while the MCP catalog
    # (mcp/src/bvmcp/scopes.py) and the new tool routing expect the
    # plural / domain-aligned names ("patients:read", "events:read",
    # "imaging:read"). Bridging them here keeps already-issued tokens
    # working without a forced re-grant; the canonical fix is to align
    # both catalogs and migrate the token rows in a follow-up.
    # ``documents:download`` (binary access, sensitive) is a strict
    # superset of ``documents:read`` (metadata + OCR text), so anyone
    # cleared to fetch the bytes is implicitly cleared to read the text.
    "patient:read": frozenset({"patients:read"}),
    "patient:write": frozenset({"patients:write"}),
    "patient:images": frozenset({"imaging:read"}),
    "consultation:read": frozenset({"events:read", "reports:read"}),
    "consultation:write": frozenset({"events:write", "reports:write"}),
    "documents:download": frozenset({"documents:read"}),
}


def expand_scopes(scopes: frozenset[str]) -> frozenset[str]:
    """Resolve legacy / singular scope names into their MCP-canonical
    equivalents and add hierarchical implications so callers can
    compare against a stable surface. Public so the HTTP dispatcher
    in ``server_http`` can reuse the same logic without reimporting
    the alias map."""
    expanded = set(scopes)
    for s in scopes:
        if s in _SCOPE_ALIASES:
            expanded.update(_SCOPE_ALIASES[s])
    return frozenset(expanded)


def has_scope(principal: Principal, scope: str) -> bool:
    """Return True if ``principal`` carries ``scope`` directly or via
    an alias / hierarchy expansion. Use this anywhere the dispatch
    layer would otherwise do a raw ``scope in principal.scope`` check
    — the raw form misses tokens granted under the legacy singular
    catalog."""
    if scope in principal.scope:
        return True
    return scope in expand_scopes(principal.scope)


def require_scope(principal: Principal, scope: str) -> None:
    """Raise AuthError(403) when ``principal`` does not carry
    ``scope``. Tools that mutate state call this before performing
    their backend call. Honours legacy singular scope names + the
    download-implies-read hierarchy via ``_SCOPE_ALIASES``."""
    if has_scope(principal, scope):
        return
    raise AuthError(403, f"missing required scope: {scope}")


__all__ = [
    "AuthError",
    "AuthSettings",
    "Principal",
    "expand_scopes",
    "get_auth_settings",
    "has_scope",
    "require_scope",
    "validate_token",
]
