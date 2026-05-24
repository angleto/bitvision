"""Stateless HMAC token for the public iCal subscription feed.

A subscription feed has to be fetchable by an external calendar app
(Google / Apple Calendar "add by URL") with no BitVision login, so the
URL carries its own credential: a compact, URL-safe token that binds
``(subscription_id, patient_id)`` under an HMAC-SHA256 signature.

Design:

- The signing key is **derived** from ``BVP_JWT_SECRET`` via HMAC
  key-separation (label ``bvphoenix/calendar-subscription/v1``). A
  calendar token therefore can never be confused with — nor swapped
  for — an auth JWT signed with the raw secret, and the two purposes
  rotate independently in intent even though they share a root secret.
  Rotating ``BVP_JWT_SECRET`` invalidates every calendar token at once
  (the global kill switch); per-link revocation is the DB row's
  ``revoked_at``, checked by the feed endpoint *after* the signature
  verifies.
- The signature covers BOTH ids, so a recipient cannot edit the
  patient id in the URL to read another fascicolo: cross-patient is
  cryptographically inexpressible, not merely rejected (project-wide
  guardrail, memory ``cross_patient_links_forbidden``).
- The token is deterministic: ``sign`` of the same pair always yields
  the same string, so the feed URL is stable across listings and can
  be reconstructed without persisting it.
- Layout (65 bytes, base64url without padding, no ``.`` so it slots
  cleanly into a ``/calendar/feed/{token}.ics`` path)::

      byte  0       version (0x01)
      bytes 1..17    subscription_id (uuid, 16 bytes)
      bytes 17..33   patient_id      (uuid, 16 bytes)
      bytes 33..65   HMAC-SHA256(key, bytes[0:33])  (32 bytes)
"""

from __future__ import annotations

import base64
import binascii
import hmac
import uuid
from hashlib import sha256

from bvphoenix.config import get_settings

_VERSION = 1
_KEY_LABEL = b"bvphoenix/calendar-subscription/v1"
_BODY_LEN = 1 + 16 + 16  # version + sub_id + patient_id
_TOKEN_LEN = _BODY_LEN + 32  # + HMAC-SHA256 digest


def _derive_key() -> bytes:
    """HKDF-style key separation from the JWT secret.

    ``get_settings().jwt_secret`` is never empty: the config validator
    substitutes an obvious dev marker outside production and refuses to
    boot production without a real secret, so this is safe to call
    unconditionally."""
    secret = get_settings().jwt_secret.encode("utf-8")
    return hmac.new(secret, _KEY_LABEL, sha256).digest()


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    pad = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + pad)


def sign(subscription_id: uuid.UUID, patient_id: uuid.UUID) -> str:
    """Return the public token for ``(subscription_id, patient_id)``."""
    body = bytes([_VERSION]) + subscription_id.bytes + patient_id.bytes
    mac = hmac.new(_derive_key(), body, sha256).digest()
    return _b64url_encode(body + mac)


def verify(token: str) -> tuple[uuid.UUID, uuid.UUID] | None:
    """Validate ``token`` and return ``(subscription_id, patient_id)``.

    Returns ``None`` for any malformed / wrong-version / bad-signature
    input (constant-time MAC comparison). The caller still has to load
    the subscription row and enforce ``revoked_at IS NULL`` / expiry —
    the signature only proves the URL was minted by us and untampered.
    """
    try:
        raw = _b64url_decode(token)
    except (binascii.Error, ValueError):
        return None
    if len(raw) != _TOKEN_LEN or raw[0] != _VERSION:
        return None
    body, mac = raw[:_BODY_LEN], raw[_BODY_LEN:]
    expected = hmac.new(_derive_key(), body, sha256).digest()
    if not hmac.compare_digest(mac, expected):
        return None
    return uuid.UUID(bytes=body[1:17]), uuid.UUID(bytes=body[17:_BODY_LEN])


__all__ = ["sign", "verify"]
