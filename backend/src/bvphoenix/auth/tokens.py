"""JWT issuance and decoding for the local-password auth path.

Two signing modes are supported:

- **HS256** (default): a single shared secret (``BVP_JWT_SECRET``) is
  used to both sign and verify. Simple, fine for single-node deployments.
- **RS256** (opt-in): set ``BVP_JWT_ALGORITHM=RS256`` and point
  ``BVP_JWT_PRIVATE_KEY_PATH`` / ``BVP_JWT_PUBLIC_KEY_PATH`` at PEM
  files. Asymmetric signing lets verifiers hold only the public key,
  which matters once tokens are consumed by downstream services.

Key material is cached in-process so we don't re-read the PEM on every
request; ``get_settings`` is itself LRU-cached.

Library: PyJWT
--------------
Until 2026-05-21 this module used the ``python-jose`` library. PyJWT
replaces it because (a) python-jose has known unmaintained-library
risk (CVE-2022-29217 algorithm confusion, no maintainer activity for
years) and (b) PyJWT enforces audience / issuer validation by default
when you pass ``audience=`` / ``issuer=`` to ``decode``, whereas the
jose API silently accepts mismatches unless an explicit options dict
overrides it.

Claims minted on every token (RFC 7519)
---------------------------------------
* ``iss`` — issuer, from ``BVP_JWT_ISSUER`` (default ``"bvphoenix"``)
* ``aud`` — audience, from ``BVP_JWT_AUDIENCE`` (default
  ``"bvphoenix-api"``); decode rejects tokens whose ``aud`` does not
  match this exact string.
* ``iat`` — issued-at; informational.
* ``nbf`` — not-before; equal to ``iat``. Defends against clock-skew
  attacks where a future-dated token slips past the ``exp`` check.
* ``exp`` — expiry.
* ``jti`` — unique id used for revocation lookups (ADR 0016).
* ``sub`` — subject id.
* ``typ`` — token type (``user`` / ``agent``).

Two token *types* share this module, distinguished by the ``typ`` claim
in the decoded payload:

- ``user`` (default, back-compat — a missing ``typ`` is treated as
  ``user``): minted by ``/auth/login`` etc., carries ``email`` and
  ``is_admin``.
- ``agent``: minted by ``/api/agent-tokens``. Carries ``owner_sub`` (the
  human who minted it) and ``scope`` (array of capability strings). The
  DB lookup key is ``sha256(raw_jwt)``, recomputed from the presented
  bearer at verify time. See ``api/agent_tokens.py``.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path

import jwt as pyjwt

from bvphoenix.config import get_settings


def hash_agent_token(raw_jwt: str) -> str:
    """SHA-256 hex digest of a signed agent JWT.

    Used both at mint time (to populate ``agent_tokens.token_hash``)
    and at verify time (to look the row up). Keeping this helper in
    one place means the two sides can't drift.
    """
    return hashlib.sha256(raw_jwt.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class TokenPayload:
    subject_id: uuid.UUID
    email: str
    is_admin: bool
    exp: int
    # Optional share-link grant id — set when the token was minted from
    # ``/shared/{token}/verify``. Downstream DICOM download uses it to
    # apply grant-scoped transforms (e.g. de-identification).
    grant_id: uuid.UUID | None = None
    # ``share_links.id`` for tokens minted from ``/shared/{token}/verify``.
    # Used by the versioning layer to attribute writes coming through an
    # anonymous share link with ``ActorContext.kind='link'``; the
    # revision-history UI then renders a "modality A" badge on the
    # commits that originated from this link.
    share_link_id: uuid.UUID | None = None
    # Token *type*. ``user`` (default) is the standard session token;
    # ``agent`` is a scoped credential owned by a human but wielded by
    # an LLM / MCP client.
    typ: str = "user"
    # For agent tokens: subject id of the *owner* (the human). For user
    # tokens this equals ``subject_id``.
    owner_subject_id: uuid.UUID | None = None
    # For agent tokens: capability strings (e.g. ``patient:<uuid>:read``,
    # ``consultation:write``). Empty for user tokens.
    scope: tuple[str, ...] = field(default_factory=tuple)
    # JWT ID (RFC 7519 §4.1.7) used for revocation lookup (ADR 0016).
    # ``None`` only when decoding a legacy token that pre-dates the
    # claim — those still verify cryptographically but cannot be
    # revoked individually.
    jti: uuid.UUID | None = None


@lru_cache(maxsize=4)
def _read_pem(path: str) -> str:
    """Read a PEM key from disk. Cached because ``jwt.encode`` / ``decode``
    re-parses it each call but we don't want re-read it each request."""
    return Path(path).read_text(encoding="utf-8")


def _signing_key() -> str:
    """Return the key material used to *sign* new tokens."""
    s = get_settings()
    if s.jwt_algorithm.upper().startswith(("RS", "ES")):
        if not s.jwt_private_key_path:
            raise RuntimeError(
                f"BVP_JWT_ALGORITHM={s.jwt_algorithm} requires BVP_JWT_PRIVATE_KEY_PATH."
            )
        return _read_pem(s.jwt_private_key_path)
    return s.jwt_secret


def _verification_key() -> str:
    """Return the key material used to *verify* incoming tokens.

    For HS* we reuse the shared secret. For RS*/ES* we prefer the
    explicit public key; if unset we fall back to the private key (PEMs
    from OpenSSL include the public portion, so PyJWT can verify with
    it).
    """
    s = get_settings()
    if s.jwt_algorithm.upper().startswith(("RS", "ES")):
        path = s.jwt_public_key_path or s.jwt_private_key_path
        if not path:
            raise RuntimeError(
                f"BVP_JWT_ALGORITHM={s.jwt_algorithm} requires a public or private key path."
            )
        return _read_pem(path)
    return s.jwt_secret


def _base_claims(now: datetime, ttl_seconds: int) -> dict:
    """Return the set of standard claims we attach to every token we
    mint: ``iss``, ``aud``, ``iat``, ``nbf``, ``exp``. Caller adds the
    type-specific ones on top (``sub``, ``typ``, …)."""
    s = get_settings()
    iat = int(now.timestamp())
    return {
        "iss": s.jwt_issuer,
        "aud": s.jwt_audience,
        "iat": iat,
        "nbf": iat,
        "exp": int((now + timedelta(seconds=ttl_seconds)).timestamp()),
    }


def issue_access_token(
    *,
    subject_id: uuid.UUID,
    email: str,
    is_admin: bool,
    grant_id: uuid.UUID | None = None,
    share_link_id: uuid.UUID | None = None,
) -> str:
    s = get_settings()
    now = datetime.now(UTC)
    payload: dict = {
        **_base_claims(now, s.jwt_expires_seconds),
        "sub": str(subject_id),
        "email": email,
        "is_admin": is_admin,
        "typ": "user",
        "jti": str(uuid.uuid4()),
    }
    if grant_id is not None:
        payload["grant_id"] = str(grant_id)
    if share_link_id is not None:
        payload["share_link_id"] = str(share_link_id)
    return pyjwt.encode(payload, _signing_key(), algorithm=s.jwt_algorithm)


def issue_agent_token(
    *,
    agent_token_id: uuid.UUID,
    owner_subject_id: uuid.UUID,
    scope: list[str],
    ttl_seconds: int,
) -> tuple[str, str]:
    """Mint an agent-scoped JWT.

    Returns ``(jwt_raw, token_hash)`` where ``token_hash`` is
    ``sha256(jwt_raw)`` — the caller persists it as
    ``agent_tokens.token_hash`` and hands ``jwt_raw`` to the client
    exactly once. Verification recomputes the hash from the presented
    token and looks the row up by it, so no state travels in the JWT
    beyond what's needed for signature checking.
    """
    s = get_settings()
    now = datetime.now(UTC)
    payload: dict = {
        **_base_claims(now, ttl_seconds),
        "sub": str(agent_token_id),
        "owner_sub": str(owner_subject_id),
        "typ": "agent",
        "scope": list(scope),
        "jti": str(uuid.uuid4()),
    }
    raw = pyjwt.encode(payload, _signing_key(), algorithm=s.jwt_algorithm)
    return raw, hash_agent_token(raw)


def decode_token(token: str) -> TokenPayload | None:
    s = get_settings()
    try:
        data = pyjwt.decode(
            token,
            _verification_key(),
            algorithms=[s.jwt_algorithm],
            audience=s.jwt_audience,
            issuer=s.jwt_issuer,
            leeway=s.jwt_leeway_seconds,
            options={
                # Pin the require-list so a future PyJWT default change
                # cannot silently relax the validation surface.
                "require": ["exp", "iat", "nbf", "iss", "aud", "sub"],
                "verify_signature": True,
                "verify_exp": True,
                "verify_nbf": True,
                "verify_iat": True,
                "verify_aud": True,
                "verify_iss": True,
            },
        )
    except pyjwt.PyJWTError:
        return None
    try:
        jti_raw = data.get("jti")
        jti = uuid.UUID(jti_raw) if jti_raw else None
        typ = str(data.get("typ", "user"))
        if typ == "agent":
            # Agent tokens: ``sub`` is the agent_tokens row id, the
            # *principal* is ``owner_sub``. We expose both via the
            # dataclass so downstream deps can bind the owner as the
            # RLS subject while still being able to load the agent row
            # via ``jti`` / ``sub``.
            owner_raw = data.get("owner_sub")
            if not owner_raw:
                return None
            raw_scope = data.get("scope") or []
            if not isinstance(raw_scope, list):
                return None
            return TokenPayload(
                subject_id=uuid.UUID(data["sub"]),
                email="",
                is_admin=False,
                exp=int(data["exp"]),
                grant_id=None,
                typ="agent",
                owner_subject_id=uuid.UUID(owner_raw),
                scope=tuple(str(s) for s in raw_scope),
                jti=jti,
            )
        grant_raw = data.get("grant_id")
        grant_id = uuid.UUID(grant_raw) if grant_raw else None
        share_link_raw = data.get("share_link_id")
        share_link_id = uuid.UUID(share_link_raw) if share_link_raw else None
        return TokenPayload(
            subject_id=uuid.UUID(data["sub"]),
            email=data["email"],
            is_admin=bool(data.get("is_admin", False)),
            exp=int(data["exp"]),
            grant_id=grant_id,
            share_link_id=share_link_id,
            typ="user",
            jti=jti,
        )
    except (KeyError, ValueError):
        return None
