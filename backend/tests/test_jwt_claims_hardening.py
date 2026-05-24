"""Hardening tests for the local JWT issuer.

Until 2026-05-21 the local JWT path omitted the ``aud`` (audience),
``iss`` (issuer) and ``nbf`` (not-before) claims and accepted any
token whose signature matched the shared HS256 secret. A token leaked
from staging would replay against production verbatim if the secrets
were ever rotated to the same value (or shared across environments,
which used to happen during a botched config migration).

These tests pin the post-migration invariants:
  * every minted token carries ``iss``, ``aud``, ``iat``, ``nbf``,
    ``exp``, ``jti``;
  * decoding rejects tokens whose ``aud`` or ``iss`` does not match
    the local configuration;
  * decoding rejects tokens whose ``nbf`` is in the future (beyond
    the configured leeway).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import jwt as pyjwt
import pytest

from bvphoenix.auth.tokens import (
    decode_token,
    issue_access_token,
)
from bvphoenix.config import get_settings


def _mint_user_token() -> str:
    return issue_access_token(
        subject_id=uuid.uuid4(),
        email="test@example.com",
        is_admin=False,
    )


def test_minted_token_carries_all_standard_claims() -> None:
    raw = _mint_user_token()
    s = get_settings()
    # Inspect raw claims without going through ``decode_token`` so we
    # see exactly what got minted.
    data = pyjwt.decode(
        raw,
        s.jwt_secret,
        algorithms=[s.jwt_algorithm],
        audience=s.jwt_audience,
        issuer=s.jwt_issuer,
    )
    for required in ("iss", "aud", "iat", "nbf", "exp", "jti", "sub", "typ"):
        assert required in data, f"claim {required!r} missing from minted token"
    assert data["iss"] == s.jwt_issuer
    assert data["aud"] == s.jwt_audience
    assert data["nbf"] == data["iat"]


def test_decode_accepts_well_formed_token() -> None:
    raw = _mint_user_token()
    payload = decode_token(raw)
    assert payload is not None
    assert payload.email == "test@example.com"


def test_decode_rejects_wrong_audience() -> None:
    """A token minted for a different audience must NOT verify even if
    the signature is valid (same HS256 secret)."""
    s = get_settings()
    now = datetime.now(UTC)
    bad = pyjwt.encode(
        {
            "iss": s.jwt_issuer,
            "aud": "some-other-service",  # wrong audience
            "iat": int(now.timestamp()),
            "nbf": int(now.timestamp()),
            "exp": int((now + timedelta(seconds=600)).timestamp()),
            "sub": str(uuid.uuid4()),
            "jti": str(uuid.uuid4()),
            "typ": "user",
            "email": "test@example.com",
            "is_admin": False,
        },
        s.jwt_secret,
        algorithm=s.jwt_algorithm,
    )
    assert decode_token(bad) is None


def test_decode_rejects_wrong_issuer() -> None:
    s = get_settings()
    now = datetime.now(UTC)
    bad = pyjwt.encode(
        {
            "iss": "attacker-issuer",  # wrong issuer
            "aud": s.jwt_audience,
            "iat": int(now.timestamp()),
            "nbf": int(now.timestamp()),
            "exp": int((now + timedelta(seconds=600)).timestamp()),
            "sub": str(uuid.uuid4()),
            "jti": str(uuid.uuid4()),
            "typ": "user",
            "email": "test@example.com",
            "is_admin": False,
        },
        s.jwt_secret,
        algorithm=s.jwt_algorithm,
    )
    assert decode_token(bad) is None


def test_decode_rejects_future_nbf() -> None:
    """A token whose ``nbf`` lies past the configured leeway must not
    validate, even if its signature is fine and ``exp`` is way in the
    future."""
    s = get_settings()
    now = datetime.now(UTC)
    future = now + timedelta(seconds=s.jwt_leeway_seconds + 120)
    bad = pyjwt.encode(
        {
            "iss": s.jwt_issuer,
            "aud": s.jwt_audience,
            "iat": int(future.timestamp()),
            "nbf": int(future.timestamp()),
            "exp": int((future + timedelta(seconds=600)).timestamp()),
            "sub": str(uuid.uuid4()),
            "jti": str(uuid.uuid4()),
            "typ": "user",
            "email": "test@example.com",
            "is_admin": False,
        },
        s.jwt_secret,
        algorithm=s.jwt_algorithm,
    )
    assert decode_token(bad) is None


def test_decode_rejects_expired_token() -> None:
    s = get_settings()
    past = datetime.now(UTC) - timedelta(seconds=s.jwt_leeway_seconds + 120)
    bad = pyjwt.encode(
        {
            "iss": s.jwt_issuer,
            "aud": s.jwt_audience,
            "iat": int(past.timestamp()),
            "nbf": int(past.timestamp()),
            "exp": int((past + timedelta(seconds=10)).timestamp()),
            "sub": str(uuid.uuid4()),
            "jti": str(uuid.uuid4()),
            "typ": "user",
            "email": "test@example.com",
            "is_admin": False,
        },
        s.jwt_secret,
        algorithm=s.jwt_algorithm,
    )
    assert decode_token(bad) is None


def test_decode_rejects_missing_required_claims() -> None:
    """A token that omits ``aud`` (a phenotype we explicitly require
    via ``options.require``) must not validate, even if the signature
    is valid and the other claims are fine."""
    s = get_settings()
    now = datetime.now(UTC)
    bad = pyjwt.encode(
        {
            # no ``aud``
            "iss": s.jwt_issuer,
            "iat": int(now.timestamp()),
            "nbf": int(now.timestamp()),
            "exp": int((now + timedelta(seconds=600)).timestamp()),
            "sub": str(uuid.uuid4()),
            "jti": str(uuid.uuid4()),
            "typ": "user",
            "email": "test@example.com",
            "is_admin": False,
        },
        s.jwt_secret,
        algorithm=s.jwt_algorithm,
    )
    assert decode_token(bad) is None


@pytest.mark.parametrize(
    "broken_token",
    [
        "",
        "not-a-jwt",
        "aaa.bbb.ccc",
    ],
)
def test_decode_rejects_garbage(broken_token: str) -> None:
    assert decode_token(broken_token) is None
