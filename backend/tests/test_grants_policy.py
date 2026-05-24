"""Grant policy helpers — ``is_external_grantee`` + deidentify default.

Stub session so we don't need a live Postgres: the SQL is a raw text
query with named parameters, so matching on the bound params is enough
to verify the helper asked the right question, and we hand back a
scripted response to exercise both branches.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from bvphoenix.db.models.sharing import PUBLIC_SUBJECT_ID
from bvphoenix.services.grants import (
    is_external_grantee,
    resolve_deidentify_default,
)


class _Result:
    def __init__(self, row: Any) -> None:
        self._row = row

    def first(self) -> Any:
        return self._row


class _StubSession:
    """Captures the bound params of the single ``text()`` query the
    helper makes so tests can assert on the inputs, and returns a
    scripted ``first()`` result."""

    def __init__(self, response: Any = None) -> None:
        self._response = response
        self.calls: list[dict[str, Any]] = []

    async def execute(self, _stmt: Any, params: dict[str, Any] | None = None) -> _Result:
        self.calls.append(params or {})
        return _Result(self._response)


@pytest.mark.asyncio
async def test_public_subject_is_always_external() -> None:
    db = _StubSession(response=(1,))  # even if membership existed, public wins
    grantor = uuid.uuid4()
    assert (
        await is_external_grantee(
            db, grantor_subject_id=grantor, grantee_subject_id=PUBLIC_SUBJECT_ID
        )
        is True
    )
    # Short-circuit: no SQL was executed for the public branch.
    assert db.calls == []


@pytest.mark.asyncio
async def test_self_grant_is_internal() -> None:
    db = _StubSession()
    same = uuid.uuid4()
    assert await is_external_grantee(db, grantor_subject_id=same, grantee_subject_id=same) is False
    assert db.calls == []


@pytest.mark.asyncio
async def test_shared_org_membership_is_internal() -> None:
    # Response shape is "a row exists" -> not external.
    db = _StubSession(response=(1,))
    grantor = uuid.uuid4()
    grantee = uuid.uuid4()
    assert (
        await is_external_grantee(db, grantor_subject_id=grantor, grantee_subject_id=grantee)
        is False
    )
    assert db.calls == [{"grantor": grantor, "grantee": grantee}]


@pytest.mark.asyncio
async def test_unrelated_grantee_is_external() -> None:
    db = _StubSession(response=None)
    grantor = uuid.uuid4()
    grantee = uuid.uuid4()
    assert (
        await is_external_grantee(db, grantor_subject_id=grantor, grantee_subject_id=grantee)
        is True
    )


@pytest.mark.asyncio
async def test_resolve_honors_explicit_false() -> None:
    # The stub would have said "external" (no membership row), but an
    # explicit False must win per authorization.md §7.
    db = _StubSession(response=None)
    out = await resolve_deidentify_default(
        db,
        grantor_subject_id=uuid.uuid4(),
        grantee_subject_id=uuid.uuid4(),
        explicit=False,
    )
    assert out is False
    # Skipped membership query entirely when an explicit answer was given.
    assert db.calls == []


@pytest.mark.asyncio
async def test_resolve_honors_explicit_true() -> None:
    db = _StubSession(response=(1,))  # membership exists; still overridden
    out = await resolve_deidentify_default(
        db,
        grantor_subject_id=uuid.uuid4(),
        grantee_subject_id=uuid.uuid4(),
        explicit=True,
    )
    assert out is True
    assert db.calls == []


@pytest.mark.asyncio
async def test_resolve_defaults_external_to_true() -> None:
    db = _StubSession(response=None)  # no shared org
    out = await resolve_deidentify_default(
        db,
        grantor_subject_id=uuid.uuid4(),
        grantee_subject_id=uuid.uuid4(),
        explicit=None,
    )
    assert out is True


@pytest.mark.asyncio
async def test_resolve_defaults_internal_to_false() -> None:
    db = _StubSession(response=(1,))  # shared org found
    out = await resolve_deidentify_default(
        db,
        grantor_subject_id=uuid.uuid4(),
        grantee_subject_id=uuid.uuid4(),
        explicit=None,
    )
    assert out is False


@pytest.mark.asyncio
async def test_resolve_public_link_defaults_to_true() -> None:
    db = _StubSession(response=None)
    out = await resolve_deidentify_default(
        db,
        grantor_subject_id=uuid.uuid4(),
        grantee_subject_id=PUBLIC_SUBJECT_ID,
        explicit=None,
    )
    assert out is True
    # Public short-circuit also skips the query.
    assert db.calls == []
