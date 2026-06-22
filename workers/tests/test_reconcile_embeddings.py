"""reconcile_missing_embeddings — the self-healing Visual Search indexer.

Unit-level. The candidate SELECT and ``set_current_subject`` SET are driven
through a SQL-aware fake session, and the arq pool is a fake that records
enqueues, so the task's enqueue + dedup + counting logic is exercised
without Postgres / Redis. (The candidate *query* semantics — embeddable
modality / SOP class / not-already-embedded — are owned and tested by
``bvphoenix.services.embeddable`` + ``tests/test_embed_backfill_candidates``;
this test owns the worker-side behaviour.)
"""

from __future__ import annotations

import types
import uuid
from typing import Any

import pytest

from bvworkers.tasks import reconcile_embeddings as mod
from bvworkers.tasks.registry import FUNCTIONS


def test_task_is_registered() -> None:
    assert mod.reconcile_missing_embeddings in FUNCTIONS


# --- fakes ---------------------------------------------------------------


class _Result:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows


class _FakeSession:
    """SQL-aware so it doesn't depend on execute call order: the reconcile
    SELECT (the only statement touching ``embeddings``) returns the scripted
    candidate rows; everything else (``set_current_subject``'s set_config)
    returns an empty result the task never reads."""

    def __init__(self, candidate_ids: list[str]) -> None:
        self._rows = [(sid,) for sid in candidate_ids]

    async def execute(self, stmt: Any, params: Any = None) -> _Result:
        return _Result(self._rows if "embeddings" in str(stmt).lower() else [])

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None


class _FakeEngine:
    async def dispose(self) -> None:
        return None


class _FakeRedis:
    """Records enqueue_job calls. Returns ``None`` (arq's "already queued"
    signal) for any ``_job_id`` in ``dups`` so the dedup path is testable."""

    def __init__(self, dups: set[str] | None = None) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], str | None]] = []
        self._dups = dups or set()

    async def enqueue_job(self, function: str, *args: Any, _job_id: str | None = None, **__: Any):
        self.calls.append((function, args, _job_id))
        return None if _job_id in self._dups else object()


def _wire(monkeypatch: pytest.MonkeyPatch, candidate_ids: list[str]) -> None:
    monkeypatch.setattr(mod, "get_settings", lambda: types.SimpleNamespace(database_url="stub://"))
    monkeypatch.setattr(mod, "create_async_engine", lambda *a, **kw: _FakeEngine())
    monkeypatch.setattr(mod, "AsyncSession", lambda *a, **kw: _FakeSession(candidate_ids))


# --- tests ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_enqueues_embed_series_for_each_candidate(monkeypatch: pytest.MonkeyPatch) -> None:
    ids = [str(uuid.uuid4()) for _ in range(3)]
    _wire(monkeypatch, ids)
    redis = _FakeRedis()

    out = await mod.reconcile_missing_embeddings({"redis": redis})

    assert out == {"status": "ok", "enqueued": 3, "candidates": 3}
    # One embed_series per candidate, with a deterministic dedup job id.
    assert [c[0] for c in redis.calls] == ["embed_series"] * 3
    assert [c[1] for c in redis.calls] == [(sid,) for sid in ids]
    assert [c[2] for c in redis.calls] == [f"embed_series:{sid}" for sid in ids]


@pytest.mark.asyncio
async def test_already_queued_series_not_double_counted(monkeypatch: pytest.MonkeyPatch) -> None:
    ids = [str(uuid.uuid4()) for _ in range(3)]
    _wire(monkeypatch, ids)
    # Middle series is "already queued" -> arq returns None -> not counted,
    # but the others still go through.
    redis = _FakeRedis(dups={f"embed_series:{ids[1]}"})

    out = await mod.reconcile_missing_embeddings({"redis": redis})

    assert out["candidates"] == 3
    assert out["enqueued"] == 2
    assert len(redis.calls) == 3  # all attempted; one deduped


@pytest.mark.asyncio
async def test_no_candidates_short_circuits(monkeypatch: pytest.MonkeyPatch) -> None:
    _wire(monkeypatch, [])
    redis = _FakeRedis()

    out = await mod.reconcile_missing_embeddings({"redis": redis})

    assert out == {"status": "ok", "enqueued": 0, "candidates": 0}
    assert redis.calls == []  # nothing enqueued when the index is complete
