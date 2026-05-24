"""Unit tests for the auth primitives — password hashing and JWTs."""

from __future__ import annotations

import time
import uuid

import pytest

from bvphoenix.auth.passwords import hash_password, verify_password
from bvphoenix.auth.tokens import decode_token, issue_access_token


def test_password_roundtrip() -> None:
    h = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", h)
    assert not verify_password("wrong password", h)


def test_password_unique_salt() -> None:
    """Different hashes for the same password — bcrypt mixes random salt."""
    a = hash_password("samepassword")
    b = hash_password("samepassword")
    assert a != b
    assert verify_password("samepassword", a)
    assert verify_password("samepassword", b)


def test_password_invalid_hash_returns_false() -> None:
    assert not verify_password("anything", "not-a-bcrypt-hash")


def test_jwt_roundtrip() -> None:
    sid = uuid.uuid4()
    token = issue_access_token(subject_id=sid, email="x@y.z", is_admin=True)
    payload = decode_token(token)
    assert payload is not None
    assert payload.subject_id == sid
    assert payload.email == "x@y.z"
    assert payload.is_admin is True
    assert payload.exp > int(time.time())


def test_jwt_rejects_garbage() -> None:
    assert decode_token("not-a-jwt") is None
    assert decode_token("") is None


@pytest.mark.parametrize("admin", [True, False])
def test_jwt_carries_admin_flag(admin: bool) -> None:
    token = issue_access_token(subject_id=uuid.uuid4(), email="a@b.c", is_admin=admin)
    payload = decode_token(token)
    assert payload is not None
    assert payload.is_admin is admin
