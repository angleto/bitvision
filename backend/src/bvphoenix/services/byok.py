"""BYOK (bring-your-own-key) service (F7.1).

AES-GCM encryption for user-supplied LLM provider API keys. The master
key lives in ``BVP_BYOK_MASTER_KEY`` (32 random bytes as base64url);
every row in ``user_api_keys`` carries its own 12-byte nonce, so two
rows with identical plaintext (e.g. a user who rotated back to a
cancelled key) still differ on the wire.

Public surface:

* ``save_user_api_key(db, *, user_subject_id, provider, api_key)`` —
  revokes any existing active row for (user, provider) and creates a
  fresh one with a fresh nonce. Returns the new row.
* ``revoke_user_api_key(db, *, user_subject_id, provider)`` — sets
  ``revoked_at`` on the active row. Idempotent.
* ``list_user_api_keys(db, *, user_subject_id)`` — returns active
  metadata (provider + granted_at + last_used_at), never plaintext.
* ``get_active_api_key_plaintext(db, *, user_subject_id, provider)`` —
  decrypts the active row's ciphertext. Bumps ``last_used_at`` as a
  side-effect so an operator can see stale keys at a glance.
  Returns ``None`` when no active row exists.

The master key is decoded once per process and cached. A missing /
malformed master key is a runtime error **only** when a plaintext is
actually requested — the endpoints can still list / revoke rows
without the key, so a misconfigured deployment is diagnosable.
"""

from __future__ import annotations

import base64
import logging
import os
import uuid
from datetime import UTC, datetime
from functools import lru_cache

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.config import get_settings
from bvphoenix.db.models import UserAPIKey

logger = logging.getLogger(__name__)


class BYOKConfigError(RuntimeError):
    """Raised when the BYOK master key is missing or invalid."""


@lru_cache(maxsize=1)
def _master_key_bytes() -> bytes:
    """Decode and validate the master key once per process."""
    settings = get_settings()
    raw = settings.byok_master_key.strip()
    if not raw:
        raise BYOKConfigError(
            "BVP_BYOK_MASTER_KEY is empty. BYOK is disabled. Generate with: "
            "python -c 'import secrets,base64;"
            " print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())'"
        )
    try:
        key = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    except Exception as exc:
        raise BYOKConfigError(f"BVP_BYOK_MASTER_KEY is not valid base64: {exc}") from exc
    if len(key) != 32:
        raise BYOKConfigError(f"BVP_BYOK_MASTER_KEY must decode to 32 bytes (got {len(key)})")
    return key


def _encrypt(plaintext: str) -> tuple[bytes, bytes]:
    """Return (nonce, ciphertext+tag) for the supplied plaintext."""
    key = _master_key_bytes()
    nonce = os.urandom(12)
    aes = AESGCM(key)
    ct = aes.encrypt(nonce, plaintext.encode("utf-8"), associated_data=None)
    return nonce, ct


def _decrypt(nonce: bytes, ciphertext: bytes) -> str:
    """Return the plaintext or raise :class:`BYOKConfigError` on tag
    mismatch (master key has changed, row was tampered with)."""
    key = _master_key_bytes()
    aes = AESGCM(key)
    try:
        pt = aes.decrypt(nonce, ciphertext, associated_data=None)
    except InvalidTag as exc:
        raise BYOKConfigError(
            "BYOK ciphertext failed authentication. Either the master key rotated"
            " without re-encrypting existing rows, or the row was tampered with."
        ) from exc
    return pt.decode("utf-8")


async def _active_row(
    db: AsyncSession, *, user_subject_id: uuid.UUID, provider: str
) -> UserAPIKey | None:
    return (
        await db.execute(
            select(UserAPIKey).where(
                UserAPIKey.user_subject_id == user_subject_id,
                UserAPIKey.provider == provider,
                UserAPIKey.revoked_at.is_(None),
            )
        )
    ).scalar_one_or_none()


async def save_user_api_key(
    db: AsyncSession,
    *,
    user_subject_id: uuid.UUID,
    provider: str,
    api_key: str,
) -> UserAPIKey:
    """Persist ``api_key`` encrypted. Any existing active row for the
    same (user, provider) is revoked in the same flush so at most one
    row is active at any time (enforced by the partial unique index)."""
    if not api_key or api_key.isspace():
        raise ValueError("api_key cannot be empty")
    # Round-trip the master key to surface config errors now rather than
    # inside a DB transaction.
    nonce, ciphertext = _encrypt(api_key)

    existing = await _active_row(db, user_subject_id=user_subject_id, provider=provider)
    if existing is not None:
        existing.revoked_at = datetime.now(UTC)

    row = UserAPIKey(
        user_subject_id=user_subject_id,
        provider=provider,
        key_nonce=nonce,
        key_ciphertext=ciphertext,
    )
    db.add(row)
    await db.flush()
    return row


async def revoke_user_api_key(
    db: AsyncSession, *, user_subject_id: uuid.UUID, provider: str
) -> bool:
    """Revoke the active (user, provider) key if present. Returns True
    when a row was changed, False when nothing was active."""
    row = await _active_row(db, user_subject_id=user_subject_id, provider=provider)
    if row is None:
        return False
    row.revoked_at = datetime.now(UTC)
    await db.flush()
    return True


async def list_user_api_keys(db: AsyncSession, *, user_subject_id: uuid.UUID) -> list[UserAPIKey]:
    """Return all *active* rows for the user, ordered by provider.
    Callers serialise only metadata — never the ciphertext."""
    return (
        (
            await db.execute(
                select(UserAPIKey)
                .where(
                    UserAPIKey.user_subject_id == user_subject_id,
                    UserAPIKey.revoked_at.is_(None),
                )
                .order_by(UserAPIKey.provider.asc())
            )
        )
        .scalars()
        .all()
    )


async def get_active_api_key_plaintext(
    db: AsyncSession, *, user_subject_id: uuid.UUID, provider: str
) -> str | None:
    """Decrypt and return the active plaintext key. Touches
    ``last_used_at`` on the row so an operator can spot stale keys.
    Returns ``None`` when no active row exists."""
    row = await _active_row(db, user_subject_id=user_subject_id, provider=provider)
    if row is None:
        return None
    plaintext = _decrypt(row.key_nonce, row.key_ciphertext)
    row.last_used_at = datetime.now(UTC)
    await db.flush()
    return plaintext


__all__ = [
    "BYOKConfigError",
    "get_active_api_key_plaintext",
    "list_user_api_keys",
    "revoke_user_api_key",
    "save_user_api_key",
]
