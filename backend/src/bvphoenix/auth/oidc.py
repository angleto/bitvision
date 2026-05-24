"""OIDC login flow (optional external SSO).

When ``oidc_issuer`` is configured, browsers can authenticate via any
OIDC-compliant issuer (Keycloak, Auth0, Authentik, Google, …). The
local-password routes in ``api.auth`` are the default and remain in
place; OIDC is opt-in.

Flow:
1. Browser hits ``GET /api/auth/oidc/login`` → we redirect to the
   issuer's authorize URL with a signed state cookie.
2. Issuer redirects back to ``GET /api/auth/oidc/callback`` with
   ``code`` + ``state``; we verify state, exchange the code for an
   id_token (authlib handles JWKS verification), find or create the
   matching ``users`` row, and issue our own local JWT so the rest of
   the API sees a uniform authenticated principal.

Notes:
- We intentionally do NOT use Authlib's Starlette OAuth integration
  because it couples tightly to ``request.session`` (SessionMiddleware)
  which the app doesn't otherwise need. Instead we drive the flow
  manually with ``httpx`` + a short-lived signed state cookie and use
  Authlib only for id_token signature verification (``authlib.jose``).
- All routes no-op (503) when ``oidc_issuer`` is empty — deployments
  without OIDC configured must not expose half-wired endpoints.
"""

from __future__ import annotations

import secrets
from typing import Annotated, Any
from urllib.parse import urlencode

import httpx
from authlib.jose import JsonWebKey, JsonWebToken
from authlib.jose.errors import JoseError
from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.auth.tokens import issue_access_token
from bvphoenix.config import get_settings
from bvphoenix.db.models import Subject, User
from bvphoenix.db.session import get_db

router = APIRouter(prefix="/auth/oidc", tags=["auth"])

_STATE_COOKIE = "bvp_oidc_state"
_STATE_TTL_SECONDS = 600  # 10 minutes; the authorize round-trip is usually seconds

# Split httpx timeouts. The default
# ``httpx.AsyncClient(timeout=10.0)`` collapses connect / read / write
# / pool into one number; a slow-but-eventually-responsive issuer can
# burn 30 s of TCP read while the connect phase finishes in 50 ms,
# leaving the request thread blocked well past the user's patience.
# Splitting them keeps connect short (a slow connect is almost always
# DNS or routing, and is unrecoverable) while giving the read phase
# room for an issuer cold-cache fetch.
_OIDC_HTTP_TIMEOUT = httpx.Timeout(connect=5.0, read=15.0, write=5.0, pool=5.0)

# Tiny in-process cache of discovery + JWKS documents. The issuer
# barely ever rotates these, and refetching on every login adds 2
# round-trips to what should be a fast redirect. One process per worker
# is fine — the cache rebuilds itself if the issuer URL changes.
_discovery_cache: dict[str, dict[str, Any]] = {}
_jwks_cache: dict[str, Any] = {}


async def _fetch_discovery(issuer: str) -> dict[str, Any]:
    if issuer in _discovery_cache:
        return _discovery_cache[issuer]
    url = issuer.rstrip("/") + "/.well-known/openid-configuration"
    async with httpx.AsyncClient(timeout=_OIDC_HTTP_TIMEOUT) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()
    _discovery_cache[issuer] = data
    return data


async def _fetch_jwks(jwks_uri: str) -> Any:
    if jwks_uri in _jwks_cache:
        return _jwks_cache[jwks_uri]
    async with httpx.AsyncClient(timeout=_OIDC_HTTP_TIMEOUT) as client:
        resp = await client.get(jwks_uri)
        resp.raise_for_status()
        jwks = JsonWebKey.import_key_set(resp.json())
    _jwks_cache[jwks_uri] = jwks
    return jwks


def _require_configured() -> None:
    s = get_settings()
    if not (s.oidc_issuer and s.oidc_client_id and s.oidc_client_secret and s.oidc_redirect_uri):
        # 503 not 404 — the endpoint exists by design, it's just disabled.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="oidc not configured",
        )


@router.get("/login")
async def oidc_login(request: Request) -> RedirectResponse:
    """Kick off the OIDC authorization-code flow.

    Builds the issuer's authorize URL with a fresh ``state`` nonce and
    stores the same nonce in a short-lived HttpOnly cookie. The callback
    will refuse any code that doesn't echo this exact state → CSRF-safe.
    """
    _require_configured()
    s = get_settings()
    discovery = await _fetch_discovery(s.oidc_issuer)
    authorize_url = discovery["authorization_endpoint"]

    state = secrets.token_urlsafe(32)
    params = {
        "response_type": "code",
        "client_id": s.oidc_client_id,
        "redirect_uri": s.oidc_redirect_uri,
        "scope": "openid email profile",
        "state": state,
    }
    url = f"{authorize_url}?{urlencode(params)}"
    resp = RedirectResponse(url=url, status_code=status.HTTP_302_FOUND)
    # HttpOnly so JS can't read it, SameSite=lax to survive the issuer's
    # redirect back, Secure only when we can infer HTTPS from the
    # request (dev uses plain HTTP on localhost).
    resp.set_cookie(
        _STATE_COOKIE,
        state,
        max_age=_STATE_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        path="/api/auth/oidc",
    )
    return resp


@router.get("/callback")
async def oidc_callback(
    db: Annotated[AsyncSession, Depends(get_db)],
    code: str,
    state: str,
    oidc_state_cookie: Annotated[str | None, Cookie(alias=_STATE_COOKIE)] = None,
) -> RedirectResponse:
    """Exchange the authorization code and mint a local JWT."""
    _require_configured()
    if not oidc_state_cookie or not secrets.compare_digest(oidc_state_cookie, state):
        raise HTTPException(status_code=400, detail="invalid oidc state")

    s = get_settings()
    discovery = await _fetch_discovery(s.oidc_issuer)
    token_endpoint = discovery["token_endpoint"]
    jwks_uri = discovery["jwks_uri"]
    id_token_signing_algs = discovery.get("id_token_signing_alg_values_supported") or ["RS256"]

    # Exchange authorization code for tokens.
    async with httpx.AsyncClient(timeout=_OIDC_HTTP_TIMEOUT) as client:
        token_resp = await client.post(
            token_endpoint,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": s.oidc_redirect_uri,
                "client_id": s.oidc_client_id,
                "client_secret": s.oidc_client_secret,
            },
            headers={"Accept": "application/json"},
        )
        if token_resp.status_code != 200:
            raise HTTPException(status_code=400, detail="oidc token exchange failed")
        tokens = token_resp.json()

    id_token = tokens.get("id_token")
    if not id_token:
        raise HTTPException(status_code=400, detail="oidc response missing id_token")

    # Verify id_token signature + claims against the issuer's JWKS.
    jwks = await _fetch_jwks(jwks_uri)
    try:
        jwt = JsonWebToken(id_token_signing_algs)
        claims = jwt.decode(
            id_token,
            jwks,
            claims_options={
                "iss": {"essential": True, "value": s.oidc_issuer},
                "aud": {"essential": True, "value": s.oidc_client_id},
                "exp": {"essential": True},
            },
        )
        claims.validate()
    except JoseError as exc:
        raise HTTPException(status_code=401, detail="invalid id_token") from exc

    oidc_sub = str(claims.get("sub") or "")
    email = str(claims.get("email") or "").lower()
    display_name = str(claims.get("name") or claims.get("preferred_username") or email or oidc_sub)
    if not oidc_sub:
        raise HTTPException(status_code=401, detail="id_token missing sub")

    # Link existing users by oidc_subject first, then fall back to email
    # to catch the common "admin created a local account, now logs in
    # via OIDC for the first time" case. Creating fresh users when
    # neither matches keeps first-login frictionless.
    user = (
        await db.execute(select(User).where(User.oidc_subject == oidc_sub))
    ).scalar_one_or_none()
    if user is None and email:
        user = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
        # Only link if the row isn't already bound to a different sub.
        # Overwriting a populated oidc_subject would let a second IdP
        # account hijack a local account that shares the same email.
        if user is not None and user.oidc_subject is None:
            user.oidc_subject = oidc_sub
        elif user is not None and user.oidc_subject != oidc_sub:
            raise HTTPException(
                status_code=409,
                detail="email already linked to a different oidc identity",
            )

    if user is None:
        if not email:
            raise HTTPException(status_code=400, detail="id_token missing email for new user")
        subject = Subject(kind="user", display_name=display_name)
        db.add(subject)
        await db.flush()
        user = User(
            subject_id=subject.id,
            email=email,
            oidc_subject=oidc_sub,
            password_hash=None,
        )
        db.add(user)

    await db.commit()

    local_token = issue_access_token(
        subject_id=user.subject_id,
        email=user.email,
        is_admin=user.is_admin,
    )
    # Hand the token back to the SPA via fragment — avoids leaking into
    # server access logs / Referer headers. Frontend reads it from
    # ``window.location.hash`` on the ``/auth/callback`` page.
    redirect_to = f"/auth/callback#access_token={local_token}&token_type=bearer"
    resp = RedirectResponse(url=redirect_to, status_code=status.HTTP_302_FOUND)
    resp.delete_cookie(_STATE_COOKIE, path="/api/auth/oidc")
    return resp
