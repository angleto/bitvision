"""Encrypt / decrypt the per-contact webhook HMAC secret at rest.

The plaintext webhook secret lives only in two places:

* in transit, briefly, when the operator sets it via the
  ``configure-channel`` endpoint
* in memory, in the dispatcher, when computing the
  ``X-BV-Signature`` header on an outbound webhook POST

At rest in Postgres the bytes are wrapped by pgcrypto's
``pgp_sym_encrypt(plaintext, key)`` using ``BVP_WEBHOOK_ENCRYPTION_KEY``
as the symmetric passphrase. The DB stores the resulting bytea in
``patient_contacts.webhook_secret_encrypted``.

Dev convenience: when ``BVP_WEBHOOK_ENCRYPTION_KEY`` is empty, the
helpers fall back to a plaintext (no-op) representation prefixed
with a sentinel byte so we can tell a "raw" blob from an encrypted
one at decrypt time. The fallback is loud (logger warning); a
production setup MUST set the key.
"""

from __future__ import annotations

import logging
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.config import get_settings

logger = logging.getLogger(__name__)

# Magic byte sequence that prefixes a plaintext-stored secret. The
# dev fallback uses this; production-encrypted blobs start with the
# pgcrypto envelope (``-----BEGIN PGP MESSAGE-----`` ASCII or a
# binary header), so the sentinel cannot collide with a real
# encrypted value.
_PLAINTEXT_SENTINEL: Final[bytes] = b"\x00BVPLAIN:"


def _is_plaintext_blob(blob: bytes) -> bool:
    return blob.startswith(_PLAINTEXT_SENTINEL)


def _wrap_plaintext(secret: bytes) -> bytes:
    return _PLAINTEXT_SENTINEL + secret


def _unwrap_plaintext(blob: bytes) -> bytes:
    return blob[len(_PLAINTEXT_SENTINEL) :]


async def encrypt_secret(db: AsyncSession, secret: bytes) -> bytes:
    """Encrypt ``secret`` for at-rest storage. Returns the bytea blob
    that goes into ``webhook_secret_encrypted``.

    Production: calls pgcrypto's ``pgp_sym_encrypt`` server-side so
    the key never touches the Python process beyond the env var.
    Dev (empty key): wraps the plaintext with the sentinel prefix so
    ``decrypt_secret`` round-trips it.
    """
    settings = get_settings()
    key = settings.webhook_encryption_key
    if not key:
        logger.warning(
            "webhook_encryption_key is empty — storing webhook secret as plaintext "
            "(dev mode). Set BVP_WEBHOOK_ENCRYPTION_KEY in production."
        )
        return _wrap_plaintext(secret)
    # pgp_sym_encrypt accepts text input; we feed it the raw secret
    # via decode(latin-1) which is lossless for arbitrary bytes.
    row = (
        await db.execute(
            text("SELECT pgp_sym_encrypt(:secret, :key)"),
            {"secret": secret.decode("latin-1"), "key": key},
        )
    ).scalar_one()
    return bytes(row)


async def decrypt_secret(db: AsyncSession, blob: bytes | None) -> bytes | None:
    """Reverse of :func:`encrypt_secret`. Returns ``None`` when the
    blob is missing or undecryptable (e.g. the key rotated since the
    secret was stored — the dispatcher then skips signing rather
    than sending an unsigned payload that the receiver would reject).
    """
    if not blob:
        return None
    if _is_plaintext_blob(blob):
        return _unwrap_plaintext(blob)
    settings = get_settings()
    key = settings.webhook_encryption_key
    if not key:
        logger.warning(
            "encountered an encrypted webhook secret blob without "
            "BVP_WEBHOOK_ENCRYPTION_KEY — refusing to decrypt"
        )
        return None
    try:
        # ``::bytea`` style cast clashes with SQLAlchemy's named-param
        # syntax under asyncpg (it parses ``:blob::bytea`` as two
        # colons in a row). Use the SQL-standard ``CAST`` instead so
        # the parser unambiguously binds the parameter first.
        row = (
            await db.execute(
                text("SELECT pgp_sym_decrypt(CAST(:blob AS bytea), :key)"),
                {"blob": blob, "key": key},
            )
        ).scalar_one()
    except Exception:
        logger.exception("pgp_sym_decrypt failed for a webhook secret blob")
        return None
    if row is None:
        return None
    return row.encode("latin-1") if isinstance(row, str) else bytes(row)


__all__ = ["decrypt_secret", "encrypt_secret"]
