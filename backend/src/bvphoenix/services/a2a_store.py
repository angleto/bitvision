"""Redis-backed task store for the A2A protocol.

Tasks are stored as JSON with a 7-day TTL keyed by task id. A secondary
set per context id lets us list tasks scoped to a conversation.

Falls back to an in-process dict when Redis is unreachable — keeps the
HTTP handlers working in dev/test environments without a live Redis.
"""

from __future__ import annotations

import json
from typing import Any

try:
    from redis import asyncio as aioredis  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover — arq pulls redis transitively
    aioredis = None  # type: ignore[assignment]

from bvphoenix.config import get_settings

_TASK_KEY_PREFIX = "a2a:task:"
_CONTEXT_KEY_PREFIX = "a2a:context:"
_TASK_TTL_SECONDS = 7 * 24 * 60 * 60  # 7 days


class A2AStore:
    """Async task store — Redis-backed with an in-memory fallback."""

    def __init__(self) -> None:
        self._client: Any | None = None
        self._memory: dict[str, dict] = {}
        self._memory_contexts: dict[str, set[str]] = {}
        self._use_memory: bool = aioredis is None

    async def _get_client(self) -> Any | None:
        if self._use_memory:
            return None
        if self._client is None:
            settings = get_settings()
            try:
                self._client = aioredis.from_url(
                    settings.redis_url, encoding="utf-8", decode_responses=True
                )
                # Probe once; if Redis is down we fall back to memory for this process.
                await self._client.ping()
            except Exception:
                self._client = None
                self._use_memory = True
        return self._client

    @staticmethod
    def _task_key(task_id: str) -> str:
        return f"{_TASK_KEY_PREFIX}{task_id}"

    @staticmethod
    def _context_key(context_id: str) -> str:
        return f"{_CONTEXT_KEY_PREFIX}{context_id}"

    async def save_task(self, task_id: str, task: dict) -> None:
        client = await self._get_client()
        context_id = task.get("contextId")
        if client is None:
            self._memory[task_id] = task
            if context_id:
                self._memory_contexts.setdefault(context_id, set()).add(task_id)
            return
        payload = json.dumps(task, default=str)
        await client.set(self._task_key(task_id), payload, ex=_TASK_TTL_SECONDS)
        if context_id:
            await client.sadd(self._context_key(context_id), task_id)
            await client.expire(self._context_key(context_id), _TASK_TTL_SECONDS)

    async def get_task(self, task_id: str) -> dict | None:
        client = await self._get_client()
        if client is None:
            return self._memory.get(task_id)
        raw = await client.get(self._task_key(task_id))
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    async def list_tasks(self, context_id: str | None) -> list[dict]:
        client = await self._get_client()
        if client is None:
            if context_id:
                ids = self._memory_contexts.get(context_id, set())
                return [self._memory[tid] for tid in ids if tid in self._memory]
            return list(self._memory.values())

        if context_id:
            ids = await client.smembers(self._context_key(context_id))
        else:
            # Scan all task keys — O(n) but fine for a handful of tasks per agent.
            ids = []
            async for key in client.scan_iter(match=f"{_TASK_KEY_PREFIX}*"):
                ids.append(key[len(_TASK_KEY_PREFIX) :])
        if not ids:
            return []
        keys = [self._task_key(tid) for tid in ids]
        raws = await client.mget(keys)
        tasks: list[dict] = []
        for raw in raws:
            if raw is None:
                continue
            try:
                tasks.append(json.loads(raw))
            except json.JSONDecodeError:
                continue
        return tasks

    async def delete_task(self, task_id: str) -> None:
        client = await self._get_client()
        if client is None:
            task = self._memory.pop(task_id, None)
            if task and task.get("contextId"):
                self._memory_contexts.get(task["contextId"], set()).discard(task_id)
            return
        task = await self.get_task(task_id)
        await client.delete(self._task_key(task_id))
        if task and task.get("contextId"):
            await client.srem(self._context_key(task["contextId"]), task_id)


_store: A2AStore | None = None


def get_store() -> A2AStore:
    """Module-level singleton accessor — lazily created."""
    global _store
    if _store is None:
        _store = A2AStore()
    return _store
