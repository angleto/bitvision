"""Bcrypt password hashing. Used only for the local-password fallback
(admin bootstrap, dev). OIDC users have ``password_hash IS NULL``."""

from __future__ import annotations

import bcrypt


def hash_password(plain: str) -> str:
    # ``rounds=14`` brings the per-hash cost to ~250 ms on the ARM
    # nodes we deploy on. OWASP's 2024 guidance is "≥ 10 rounds"; 14
    # buys an extra security margin against offline GPU cracking
    # without making interactive login feel slow. Pinning the value
    # explicitly defends against a future bcrypt release that quietly
    # changes the default cost.
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt(rounds=14)).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except ValueError:
        return False
