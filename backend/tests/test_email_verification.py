"""Unit tests for the email verification helpers.

DB-backed end-to-end tests live alongside the other auth integration
tests and run against the dev Postgres; here we cover the pieces that
don't need a live database.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from bvphoenix.config import Settings
from bvphoenix.services.email import (
    DevEmailSender,
    _build_verification_message,
)


def test_dev_sender_appends_to_file(tmp_path: Path) -> None:
    log = tmp_path / "dev_emails.eml"
    sender = DevEmailSender(log_path=log)
    settings = Settings(
        smtp_from_address="no-reply@test",
        smtp_from_name="tester",
    )
    msg = _build_verification_message(
        "alice@example.com", "https://app.test/verify-email?token=abc", settings
    )
    sender.send(msg)
    sender.send(msg)
    body = log.read_text()
    # Two separators for two sends.
    assert body.count("BEGIN DEV EMAIL") == 2
    assert "alice@example.com" in body
    # stdlib email serialises with quoted-printable: "=" becomes "=3D".
    # Check the path survives (the query separator is the delicate bit).
    assert "token=3Dabc" in body or "token=abc" in body
    assert "verify-email" in body


def test_verification_url_contains_token() -> None:
    from bvphoenix.api.auth import _build_verification_url, _hash_token

    settings = Settings(frontend_base_url="https://app.test/")
    url = _build_verification_url("rawtoken-xyz", settings)
    assert url == "https://app.test/verify-email?token=rawtoken-xyz"
    # Hash is deterministic SHA-256.
    assert _hash_token("rawtoken-xyz") == hashlib.sha256(b"rawtoken-xyz").hexdigest()
