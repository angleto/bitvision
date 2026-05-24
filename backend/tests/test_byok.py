"""F7.1: BYOK encrypt/decrypt round-trip + config errors.

We exercise the pure-crypto helpers plus the high-level
``save_user_api_key`` / ``get_active_api_key_plaintext`` path with a
stubbed session so the tests do not require Postgres.
"""

from __future__ import annotations

import base64
import secrets
import uuid
from typing import Any

import pytest

from bvphoenix.services import byok


@pytest.fixture(autouse=True)
def _with_master_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install a deterministic 32-byte key for the whole test run."""
    key = base64.urlsafe_b64encode(b"0123456789abcdef0123456789abcdef").decode()
    # Reach into the cached settings and the lru-cache: overwrite both.
    monkeypatch.setenv("BVP_BYOK_MASTER_KEY", key)
    # get_settings is lru-cached; rebuild it against the patched env.
    from bvphoenix.config import get_settings as _get_settings

    _get_settings.cache_clear()
    byok._master_key_bytes.cache_clear()


def test_encrypt_decrypt_roundtrip() -> None:
    nonce, ct = byok._encrypt("sk-test-12345")
    assert len(nonce) == 12
    assert ct != b"sk-test-12345"
    assert byok._decrypt(nonce, ct) == "sk-test-12345"


def test_decrypt_rejects_tampered_ciphertext() -> None:
    nonce, ct = byok._encrypt("sk-test")
    tampered = bytearray(ct)
    tampered[-1] ^= 0xFF
    with pytest.raises(byok.BYOKConfigError):
        byok._decrypt(nonce, bytes(tampered))


def test_empty_master_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BVP_BYOK_MASTER_KEY", "")
    from bvphoenix.config import get_settings as _get_settings

    _get_settings.cache_clear()
    byok._master_key_bytes.cache_clear()
    with pytest.raises(byok.BYOKConfigError):
        byok._master_key_bytes()


def test_bad_base64_master_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BVP_BYOK_MASTER_KEY", "not@@@valid!!!base64")
    from bvphoenix.config import get_settings as _get_settings

    _get_settings.cache_clear()
    byok._master_key_bytes.cache_clear()
    with pytest.raises(byok.BYOKConfigError):
        byok._master_key_bytes()


def test_wrong_length_master_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    too_short = base64.urlsafe_b64encode(b"only-16-bytes!!!").decode()
    monkeypatch.setenv("BVP_BYOK_MASTER_KEY", too_short)
    from bvphoenix.config import get_settings as _get_settings

    _get_settings.cache_clear()
    byok._master_key_bytes.cache_clear()
    with pytest.raises(byok.BYOKConfigError):
        byok._master_key_bytes()


# --- save/list/revoke happy-path via a stubbed AsyncSession ----------------


class _Scalar:
    def __init__(self, value: Any) -> None:
        self._v = value

    def scalar_one_or_none(self) -> Any:
        return self._v

    def scalars(self) -> _ScalarList:
        return _ScalarList([self._v] if self._v is not None else [])


class _ScalarList:
    def __init__(self, items: list[Any]) -> None:
        self._items = items

    def all(self) -> list[Any]:
        return self._items


class _Session:
    """Fake AsyncSession. We hand back configured rows on execute()
    and capture added rows via add()."""

    def __init__(self, rows: list[Any] | None = None) -> None:
        self.rows_queue = list(rows or [])
        self.added: list[Any] = []
        self.flushed = 0

    async def execute(self, _stmt: Any) -> _Scalar:
        nxt = self.rows_queue.pop(0) if self.rows_queue else None
        return _Scalar(nxt)

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        self.flushed += 1


@pytest.mark.asyncio
async def test_save_creates_new_row_when_none_active() -> None:
    db = _Session(rows=[None])  # no existing active row
    uid = uuid.uuid4()
    row = await byok.save_user_api_key(
        db, user_subject_id=uid, provider="anthropic", api_key="sk-secret-xyz"
    )
    assert row.user_subject_id == uid
    assert row.provider == "anthropic"
    assert len(row.key_nonce) == 12
    assert row.key_ciphertext != b"sk-secret-xyz"
    # Round-trips back to the original plaintext via the _decrypt helper.
    assert byok._decrypt(row.key_nonce, row.key_ciphertext) == "sk-secret-xyz"
    assert row in db.added


@pytest.mark.asyncio
async def test_save_revokes_previous_active_row() -> None:
    from bvphoenix.db.models.user_api_keys import UserAPIKey

    prev = UserAPIKey(
        user_subject_id=uuid.uuid4(),
        provider="anthropic",
        key_nonce=b"0" * 12,
        key_ciphertext=b"ciphertext",
    )
    db = _Session(rows=[prev])
    await byok.save_user_api_key(
        db,
        user_subject_id=prev.user_subject_id,
        provider="anthropic",
        api_key="sk-fresh",
    )
    assert prev.revoked_at is not None  # previous row closed


@pytest.mark.asyncio
async def test_save_rejects_empty_key() -> None:
    db = _Session(rows=[None])
    with pytest.raises(ValueError):
        await byok.save_user_api_key(
            db, user_subject_id=uuid.uuid4(), provider="anthropic", api_key="   "
        )


@pytest.mark.asyncio
async def test_get_active_returns_none_when_absent() -> None:
    db = _Session(rows=[None])
    out = await byok.get_active_api_key_plaintext(
        db, user_subject_id=uuid.uuid4(), provider="anthropic"
    )
    assert out is None


@pytest.mark.asyncio
async def test_revoke_returns_false_when_absent() -> None:
    db = _Session(rows=[None])
    changed = await byok.revoke_user_api_key(db, user_subject_id=uuid.uuid4(), provider="anthropic")
    assert changed is False


@pytest.mark.asyncio
async def test_decrypt_nonsense_uses_random_key_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Encrypted with one master key, decrypted with a different one =>
    InvalidTag, surfaced as BYOKConfigError."""
    nonce, ct = byok._encrypt("sk-test")
    # Rotate the master key.
    new_key = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
    monkeypatch.setenv("BVP_BYOK_MASTER_KEY", new_key)
    from bvphoenix.config import get_settings as _get_settings

    _get_settings.cache_clear()
    byok._master_key_bytes.cache_clear()
    with pytest.raises(byok.BYOKConfigError):
        byok._decrypt(nonce, ct)
