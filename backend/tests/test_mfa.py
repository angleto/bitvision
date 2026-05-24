"""Unit tests for the MFA primitives (pure, no DB).

Integration of the activate/login-mfa flow is exercised separately in
the end-to-end API tests; here we focus on the small helpers so
``pyotp`` is covered even on machines without Postgres available.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pyotp

from bvphoenix.api.mfa import (
    BACKUP_CODE_ALPHABET,
    BACKUP_CODE_COUNT,
    BACKUP_CODE_LENGTH,
    _consume_backup_code,
    _generate_backup_code,
    _qr_png_base64,
    verify_mfa_code,
    verify_totp,
)
from bvphoenix.auth.passwords import hash_password
from bvphoenix.db.models import User


def _fake_user(*, secret: str | None = None, codes: list[str] | None = None) -> User:
    # The User mapper does not require a session until it's added to one,
    # so we can instantiate it in memory for helpers that only touch
    # attributes.
    u = User()
    u.subject_id = uuid.uuid4()
    u.email = "t@example.com"
    u.password_hash = None
    u.is_admin = False
    u.mfa_secret = secret
    u.mfa_enabled_at = datetime.now(UTC) if secret else None
    u.backup_codes_hash = [hash_password(c) for c in (codes or [])] or None
    return u


def test_backup_code_shape() -> None:
    code = _generate_backup_code()
    assert len(code) == BACKUP_CODE_LENGTH
    assert all(c in BACKUP_CODE_ALPHABET for c in code)


def test_backup_code_count_constant() -> None:
    assert BACKUP_CODE_COUNT == 10


def test_qr_png_is_valid_png() -> None:
    import base64

    blob = base64.b64decode(_qr_png_base64("otpauth://totp/x?secret=JBSWY3DPEHPK3PXP"))
    # PNG signature: 89 50 4E 47 0D 0A 1A 0A
    assert blob[:8] == b"\x89PNG\r\n\x1a\n"


def test_verify_totp_accepts_current_code() -> None:
    secret = pyotp.random_base32()
    code = pyotp.TOTP(secret).now()
    assert verify_totp(secret, code) is True


def test_verify_totp_rejects_garbage() -> None:
    secret = pyotp.random_base32()
    assert verify_totp(secret, "000000") is False
    assert verify_totp(secret, "") is False
    assert verify_totp(None, "123456") is False


def test_consume_backup_code_removes_on_match() -> None:
    user = _fake_user(codes=["AAAA1111", "BBBB2222"])
    assert _consume_backup_code(user, "aaaa1111") is True  # case-insensitive
    assert user.backup_codes_hash is not None
    assert len(user.backup_codes_hash) == 1
    # Second use of the same code must fail
    assert _consume_backup_code(user, "AAAA1111") is False


def test_consume_backup_code_no_codes() -> None:
    user = _fake_user()
    assert _consume_backup_code(user, "WHATEVER") is False


def test_verify_mfa_code_prefers_totp_then_backup() -> None:
    secret = pyotp.random_base32()
    user = _fake_user(secret=secret, codes=["ZZZZ9999"])
    # Valid TOTP works without consuming a backup code.
    code = pyotp.TOTP(secret).now()
    before = list(user.backup_codes_hash or [])
    assert verify_mfa_code(user, code) is True
    assert user.backup_codes_hash == before
    # Backup code still works after TOTP success.
    assert verify_mfa_code(user, "ZZZZ9999") is True
    assert user.backup_codes_hash == []
