"""Central rate limiter + progressive login lockout.

Public surface:

* ``limiter`` — a ``slowapi.Limiter`` used as a decorator on route
  handlers. Falls back to in-memory storage when Redis is unavailable so
  dev/tests don't require a live Redis.
* ``LOGIN_LIMIT`` / ``REGISTER_LIMIT`` / ``SHARE_VERIFY_LIMIT`` — the
  default per-IP string limits used by the auth + sharing routes.
* ``record_login_failure`` / ``clear_login_failures`` /
  ``is_locked_out`` — progressive per-email lockout. After
  ``LOGIN_LOCKOUT_THRESHOLD`` consecutive failures the email is locked
  for ``LOGIN_LOCKOUT_SECONDS``. Email is hashed (sha256) before being
  used as a Redis key to avoid storing raw addresses.

Design notes:

* The limiter is built with ``get_remote_address`` as the key function,
  which reads ``request.client.host``. Deployments behind a reverse
  proxy should set ``X-Forwarded-For`` forwarding on the proxy and rely
  on Uvicorn's ``--proxy-headers`` to populate ``request.client``
  correctly.
* Redis access for the lockout uses the same async client the rest of
  the codebase uses (see ``services/a2a_store.py``). If Redis is down
  we silently fall back to an in-process counter — good enough for a
  single-worker dev setup and fail-open for availability.
"""

from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, Request, status
from slowapi import Limiter
from slowapi.util import get_remote_address

try:
    from redis import asyncio as aioredis  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover — arq pulls redis transitively
    aioredis = None  # type: ignore[assignment]

from bvphoenix.config import get_settings

# Default per-IP limits. Tuned for interactive UX (a human signing in)
# while tight enough that credential stuffing becomes expensive.
LOGIN_LIMIT = "5/minute"
REGISTER_LIMIT = "3/minute"
SHARE_VERIFY_LIMIT = "10/minute"
# Per-IP cap on share-link payload streams. A leaked link should not
# be able to exhaust outbound S3 bandwidth or burn the per-grantor
# job-result presigned cache; 5/minute is enough for a human who
# clicks "Scarica" twice while debugging a download but tight
# against a script. Applied to both the study-cache download and
# the patient-document download under /api/shared/{token}/.
SHARE_DOWNLOAD_LIMIT = "5/minute"
# Per-IP cap on share-link metadata reads (``/info``). The landing
# page polls ``/info`` every 4s while prep is queued/running, so the
# limit must be loose enough for a single recipient (15/min) and
# tight enough that a scraper can't enumerate study summaries from
# a leaked token. 30/min covers two parallel tabs comfortably.
SHARE_METADATA_LIMIT = "30/minute"
# Per-IP cap on the email send-out endpoint. Stops a compromised
# grantor credential from blasting emails to a recipient (or a
# different recipient_email after a sneaky PATCH). Email delivery
# itself runs through Scaleway TEM with its own throughput controls.
SHARE_NOTIFY_LIMIT = "5/minute"

# Upload / expensive-write limits. Generous enough for a human uploading
# a stack of studies in quick succession, tight enough that a script
# can't flood the ingestion pipeline.
UPLOAD_LIMIT = "10/minute"  # POST /api/dicom/studies (drag-drop)
STOW_LIMIT = "30/minute"  # POST /api/dicom/stow-rs (DICOMweb, machine-driven)
BULK_UPLOAD_LIMIT = "3/minute"  # POST /api/upload/bulk (whole folders)

# Search limits. Semantic is tighter because each query runs a CLIP /
# MiniLM forward pass if it misses the Redis cache.
SEARCH_LIMIT = "60/minute"  # /api/search, /api/search/hybrid, /api/similar-to
SEARCH_SEMANTIC_LIMIT = "30/minute"  # /api/search/semantic

# LLM limits. These bound how often an authenticated caller can trigger
# a paid upstream call. BYOK / credit-gating (F7) will still apply on
# top; this is a safety net against a runaway client or stolen token.
LLM_LIMIT = (
    "20/minute"  # POST /api/series/{id}/llm/describe, /api/llm/stream, /api/summaries/generate
)
A2A_LIMIT = "30/minute"  # POST /api/a2a (JSON-RPC; anon card lookup included)

# Progressive lockout thresholds.
LOGIN_LOCKOUT_THRESHOLD = 5
LOGIN_LOCKOUT_SECONDS = 15 * 60  # 15 minutes
_LOCKOUT_FAIL_WINDOW_SECONDS = 15 * 60  # counter TTL when still under threshold

_LOCKOUT_KEY_PREFIX = "login:lockout:"
_FAILCOUNT_KEY_PREFIX = "login:fails:"


def _build_limiter() -> Limiter:
    """Construct the global ``Limiter``.

    We try Redis first (shared across workers) and fall back to the
    in-process default so tests don't need a live Redis instance.
    """

    settings = get_settings()
    try:
        return Limiter(
            key_func=get_remote_address,
            storage_uri=settings.redis_url,
            default_limits=[],
        )
    except Exception:  # pragma: no cover — slowapi rarely raises here
        return Limiter(key_func=get_remote_address, default_limits=[])


limiter = _build_limiter()


def _hash_email(email: str) -> str:
    return hashlib.sha256(email.strip().lower().encode("utf-8")).hexdigest()


class _LockoutBackend:
    """Redis-backed failure counter with an in-memory fallback."""

    def __init__(self) -> None:
        self._client: Any | None = None
        self._use_memory: bool = aioredis is None
        # (count, reset_at_epoch)
        self._fail_mem: dict[str, tuple[int, float]] = {}
        self._lock_mem: dict[str, float] = {}

    async def _get_client(self) -> Any | None:
        if self._use_memory:
            return None
        if self._client is None:
            settings = get_settings()
            try:
                self._client = aioredis.from_url(
                    settings.redis_url, encoding="utf-8", decode_responses=True
                )
                await self._client.ping()
            except Exception:
                self._client = None
                self._use_memory = True
        return self._client

    async def is_locked(self, email_hash: str) -> bool:
        client = await self._get_client()
        key = f"{_LOCKOUT_KEY_PREFIX}{email_hash}"
        if client is None:
            exp = self._lock_mem.get(email_hash)
            if exp is None:
                return False
            if exp < time.time():
                self._lock_mem.pop(email_hash, None)
                return False
            return True
        return bool(await client.exists(key))

    async def record_failure(self, email_hash: str) -> int:
        """Increment the consecutive-failure counter. If it reaches the
        threshold, set a lockout key. Returns the new count.
        """

        client = await self._get_client()
        fail_key = f"{_FAILCOUNT_KEY_PREFIX}{email_hash}"
        lock_key = f"{_LOCKOUT_KEY_PREFIX}{email_hash}"

        if client is None:
            count, reset_at = self._fail_mem.get(email_hash, (0, 0.0))
            now = time.time()
            if reset_at < now:
                count = 0
            count += 1
            self._fail_mem[email_hash] = (count, now + _LOCKOUT_FAIL_WINDOW_SECONDS)
            if count >= LOGIN_LOCKOUT_THRESHOLD:
                self._lock_mem[email_hash] = now + LOGIN_LOCKOUT_SECONDS
                self._fail_mem.pop(email_hash, None)
            return count

        count = int(await client.incr(fail_key))
        # Only set TTL on first increment — ``incr`` doesn't touch TTL.
        if count == 1:
            await client.expire(fail_key, _LOCKOUT_FAIL_WINDOW_SECONDS)
        if count >= LOGIN_LOCKOUT_THRESHOLD:
            await client.set(lock_key, "1", ex=LOGIN_LOCKOUT_SECONDS)
            await client.delete(fail_key)
        return count

    async def clear(self, email_hash: str) -> None:
        client = await self._get_client()
        if client is None:
            self._fail_mem.pop(email_hash, None)
            self._lock_mem.pop(email_hash, None)
            return
        await client.delete(
            f"{_FAILCOUNT_KEY_PREFIX}{email_hash}", f"{_LOCKOUT_KEY_PREFIX}{email_hash}"
        )


_backend = _LockoutBackend()


async def is_locked_out(email: str) -> bool:
    """True if the email is currently locked out from login attempts."""
    return await _backend.is_locked(_hash_email(email))


async def record_login_failure(email: str) -> int:
    """Increment the consecutive-failure counter for ``email``.

    Returns the new count. Once the threshold is reached the caller
    should treat the account as locked (subsequent ``is_locked_out``
    calls will return True).
    """
    return await _backend.record_failure(_hash_email(email))


async def clear_login_failures(email: str) -> None:
    """Clear the consecutive-failure counter — call on successful login."""
    await _backend.clear(_hash_email(email))


@dataclass
class _Bucket:
    # Monotonic-seconds timestamps of recent hits, sorted oldest-first.
    hits: list[float]


class SlidingWindowRateLimiter:
    """Counts hits per key within a sliding ``window_seconds`` window.

    Not thread-safe across processes — see module docstring. Within a
    single process we guard the dict with a mutex; the critical section
    is O(n) in the number of hits still inside the window, which is
    bounded by ``max_hits``.
    """

    def __init__(self, *, max_hits: int, window_seconds: float) -> None:
        self._max = max_hits
        self._window = window_seconds
        self._buckets: dict[str, _Bucket] = {}
        self._lock = threading.Lock()

    def check(self, key: str) -> None:
        now = time.monotonic()
        cutoff = now - self._window
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = _Bucket(hits=[])
                self._buckets[key] = bucket
            # Evict old hits. We keep the list sorted by construction.
            while bucket.hits and bucket.hits[0] < cutoff:
                bucket.hits.pop(0)
            if len(bucket.hits) >= self._max:
                # Compute a coarse Retry-After hint from the oldest hit.
                retry_after = max(1, int(self._window - (now - bucket.hits[0])))
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="rate limit exceeded",
                    headers={"Retry-After": str(retry_after)},
                )
            bucket.hits.append(now)


def client_ip(request: Request) -> str:
    """Best-effort client IP, preferring the first ``X-Forwarded-For`` hop.

    Behind a trusted reverse proxy this is the real client; direct access
    falls back to the socket peer. We don't attempt strict CIDR trust
    lists here — rate limiting is a defence-in-depth layer, not an
    authorisation boundary.
    """
    xff = request.headers.get("x-forwarded-for")
    if xff:
        first = xff.split(",", 1)[0].strip()
        if first:
            return first
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


__all__ = [
    "A2A_LIMIT",
    "BULK_UPLOAD_LIMIT",
    "LLM_LIMIT",
    "LOGIN_LIMIT",
    "LOGIN_LOCKOUT_SECONDS",
    "LOGIN_LOCKOUT_THRESHOLD",
    "REGISTER_LIMIT",
    "SEARCH_LIMIT",
    "SEARCH_SEMANTIC_LIMIT",
    "SHARE_DOWNLOAD_LIMIT",
    "SHARE_METADATA_LIMIT",
    "SHARE_NOTIFY_LIMIT",
    "SHARE_VERIFY_LIMIT",
    "STOW_LIMIT",
    "UPLOAD_LIMIT",
    "SlidingWindowRateLimiter",
    "clear_login_failures",
    "client_ip",
    "is_locked_out",
    "limiter",
    "record_login_failure",
]
