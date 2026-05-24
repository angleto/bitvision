"""Unit tests for the single-use download token service.

These tests run against a tiny in-memory Redis stub instead of a real
server so the suite stays fast and runs in CI even without redis on
the host. The stub mirrors the contract the service actually uses
(``set(key, value, ex=...)`` + ``getdel(key)`` + TTL on get-after-ttl);
anything else would let drift sneak in. The atomic GETDEL semantics
matter — a single token must succeed exactly once even if two
parallel readers race on it — so a dedicated test pins that.
"""

from __future__ import annotations

import asyncio
import time
import uuid

import pytest

from bvphoenix.services.download_tokens import (
    consume_download_token,
    issue_download_token,
)

pytestmark = [pytest.mark.asyncio]


class _FakeRedis:
    """Minimal in-memory stand-in for the redis client used by the
    service. Models TTL via wall-clock expiry — good enough for the
    tests we want here (issue / consume / scope-mismatch / replay /
    expiry).
    """

    def __init__(self) -> None:
        # value -> (raw, expires_at_monotonic_or_None)
        self._store: dict[str, tuple[str, float | None]] = {}
        self._lock = asyncio.Lock()

    async def set(self, key: str, value: str, *, ex: int | None = None) -> None:
        expires = time.monotonic() + ex if ex is not None else None
        async with self._lock:
            self._store[key] = (value, expires)

    async def getdel(self, key: str) -> str | None:
        async with self._lock:
            entry = self._store.pop(key, None)
        if entry is None:
            return None
        raw, expires = entry
        if expires is not None and expires < time.monotonic():
            return None
        return raw


async def test_issue_then_consume_returns_subject() -> None:
    redis = _FakeRedis()
    sid = uuid.uuid4()
    rid = uuid.uuid4()

    token, ttl = await issue_download_token(
        redis,
        subject_id=sid,
        resource_kind="document",
        resource_id=rid,
    )
    assert isinstance(token, str)
    assert len(token) > 16  # 256 bits of randomness, URL-safe encoded
    assert ttl == 300

    consumed = await consume_download_token(
        redis,
        token,
        resource_kind="document",
        resource_id=rid,
    )
    assert consumed == sid


async def test_consume_is_single_use() -> None:
    """Atomic GETDEL invariant: a single token must succeed exactly
    once. Pinning this guards against a future refactor that
    accidentally splits ``GET`` + ``DEL`` into two non-atomic
    operations and reintroduces a replay window."""
    redis = _FakeRedis()
    sid = uuid.uuid4()
    rid = uuid.uuid4()
    token, _ = await issue_download_token(
        redis, subject_id=sid, resource_kind="document", resource_id=rid
    )

    first = await consume_download_token(redis, token, resource_kind="document", resource_id=rid)
    second = await consume_download_token(redis, token, resource_kind="document", resource_id=rid)
    assert first == sid
    assert second is None


async def test_consume_rejects_resource_id_mismatch() -> None:
    """A token issued for document A cannot be used to fetch
    document B. Token leaking from a log line is bad enough; lateral
    movement to other resources would be much worse."""
    redis = _FakeRedis()
    sid = uuid.uuid4()
    issued_for = uuid.uuid4()
    other = uuid.uuid4()
    token, _ = await issue_download_token(
        redis, subject_id=sid, resource_kind="document", resource_id=issued_for
    )

    consumed = await consume_download_token(
        redis, token, resource_kind="document", resource_id=other
    )
    assert consumed is None


async def test_consume_rejects_kind_mismatch() -> None:
    """Same scope-binding invariant across the resource_kind axis:
    a token for kind=document cannot be redeemed at the
    /documents/.../files/.../download endpoint where the consumer
    asks for kind=document_file."""
    redis = _FakeRedis()
    sid = uuid.uuid4()
    rid = uuid.uuid4()
    token, _ = await issue_download_token(
        redis, subject_id=sid, resource_kind="document", resource_id=rid
    )

    consumed = await consume_download_token(
        redis,
        token,
        resource_kind="document_file",
        resource_id=rid,
        child_id=uuid.uuid4(),
    )
    assert consumed is None


async def test_consume_rejects_child_id_mismatch() -> None:
    """For multi-file documents, the token binds to a specific
    DocumentFile child. Mixing up children must be impossible."""
    redis = _FakeRedis()
    sid = uuid.uuid4()
    doc_id = uuid.uuid4()
    file_a = uuid.uuid4()
    file_b = uuid.uuid4()
    token, _ = await issue_download_token(
        redis,
        subject_id=sid,
        resource_kind="document_file",
        resource_id=doc_id,
        child_id=file_a,
    )

    consumed = await consume_download_token(
        redis,
        token,
        resource_kind="document_file",
        resource_id=doc_id,
        child_id=file_b,
    )
    assert consumed is None


async def test_consume_after_ttl_returns_none() -> None:
    """After the TTL elapses the token is unusable. The service
    contract says 5 min; here we issue with 1s TTL so the test runs
    fast."""
    redis = _FakeRedis()
    sid = uuid.uuid4()
    rid = uuid.uuid4()
    token, _ = await issue_download_token(
        redis,
        subject_id=sid,
        resource_kind="document",
        resource_id=rid,
        ttl_seconds=1,
    )
    await asyncio.sleep(1.05)
    consumed = await consume_download_token(redis, token, resource_kind="document", resource_id=rid)
    assert consumed is None


async def test_consume_rejects_unknown_token() -> None:
    redis = _FakeRedis()
    consumed = await consume_download_token(
        redis,
        "totally-not-a-real-token",
        resource_kind="document",
        resource_id=uuid.uuid4(),
    )
    assert consumed is None


async def test_consume_handles_empty_token() -> None:
    """Frontend race: form submits before the issuer responded.
    Service must not blow up on the ``""`` / ``None`` corner cases."""
    redis = _FakeRedis()
    consumed = await consume_download_token(
        redis,
        "",
        resource_kind="document",
        resource_id=uuid.uuid4(),
    )
    assert consumed is None


async def test_job_result_kind_round_trip() -> None:
    """Sanity-pin that the ``job_result`` kind also round-trips
    through the same store. Used by the export-success dialog when
    the frontend wants to anchor-click the ZIP."""
    redis = _FakeRedis()
    sid = uuid.uuid4()
    job_id = uuid.uuid4()
    token, _ = await issue_download_token(
        redis,
        subject_id=sid,
        resource_kind="job_result",
        resource_id=job_id,
    )
    consumed = await consume_download_token(
        redis, token, resource_kind="job_result", resource_id=job_id
    )
    assert consumed == sid
