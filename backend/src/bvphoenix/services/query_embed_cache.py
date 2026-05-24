"""Query-embedding cache — avoid re-embedding identical search queries.

Keyed by ``sha256(query.strip().lower() + "|" + model)`` so the same
natural-language query — regardless of surrounding whitespace or case —
reuses a previously-computed vector. TTL is 24h: long enough to amortise
expensive model calls across a user's session, short enough that a model
upgrade rolls through without manual cache busting.

Falls back to a no-op (miss on every read) when ``redis.asyncio`` is not
installed or the Redis server is unreachable — the endpoint still works,
it just re-embeds every call.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

try:
    from redis import asyncio as aioredis  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover — arq pulls redis transitively
    aioredis = None  # type: ignore[assignment]

from bvphoenix.config import get_settings

_KEY_PREFIX = "query_embed:"
_TTL_SECONDS = 24 * 60 * 60  # 24 hours


def _hash_key(query: str, model: str) -> str:
    """Stable cache key — normalise whitespace + case before hashing so
    ``"Lung nodule "`` and ``"lung nodule"`` hit the same vector."""
    normalised = f"{query.strip().lower()}|{model}"
    digest = hashlib.sha256(normalised.encode("utf-8")).hexdigest()
    return f"{_KEY_PREFIX}{digest}"


class _QueryEmbedCache:
    """Redis-backed cache with graceful degradation to no-op."""

    def __init__(self) -> None:
        self._client: Any | None = None
        self._disabled: bool = aioredis is None

    async def _get_client(self) -> Any | None:
        if self._disabled:
            return None
        if self._client is None:
            settings = get_settings()
            try:
                self._client = aioredis.from_url(
                    settings.redis_url, encoding="utf-8", decode_responses=True
                )
                await self._client.ping()
            except Exception:
                # Redis is down — never try again this process, every
                # call becomes a miss and embeds directly.
                self._client = None
                self._disabled = True
        return self._client

    async def get(self, query: str, model: str) -> list[float] | None:
        client = await self._get_client()
        if client is None:
            return None
        try:
            raw = await client.get(_hash_key(query, model))
        except Exception:
            return None
        if raw is None:
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if not isinstance(data, list):
            return None
        # JSON decoded as list[int | float]; coerce to float for pgvector.
        return [float(v) for v in data]

    async def set(self, query: str, model: str, vector: list[float]) -> None:
        client = await self._get_client()
        if client is None:
            return
        try:
            await client.set(
                _hash_key(query, model),
                json.dumps(vector),
                ex=_TTL_SECONDS,
            )
        except Exception:
            # Cache-write failures are non-fatal — the caller already
            # has the vector, we just missed the amortisation window.
            return


_cache: _QueryEmbedCache | None = None


def _get_cache() -> _QueryEmbedCache:
    global _cache
    if _cache is None:
        _cache = _QueryEmbedCache()
    return _cache


async def get_cached_query_embedding(query: str, model: str) -> list[float] | None:
    """Return the cached vector for ``(query, model)`` or ``None`` on miss."""
    return await _get_cache().get(query, model)


async def cache_query_embedding(query: str, model: str, vector: list[float]) -> None:
    """Store ``vector`` for ``(query, model)`` with a 24h TTL."""
    await _get_cache().set(query, model, vector)
