"""``enqueue_postprocess_jobs`` is the single source of truth for the jobs
every ingest path fires per new series: a ``pack_volume`` for all, and an
``embed_series`` for embeddable (diagnostic-image) series only.

The regression these guard: the embed enqueue used to live only in the CLI
import path, so web-uploaded studies were never indexed for visual search
and ``/api/similar-to`` returned ``study_not_indexed`` forever. Every upload
route now funnels through this helper, so pack+embed can't drift apart again.
"""

from __future__ import annotations

import pytest

from bvphoenix.services.ingest_jobs import enqueue_postprocess_jobs


class _RecordingRedis:
    """Minimal arq-pool stand-in that records ``enqueue_job`` calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def enqueue_job(self, name: str, arg: str) -> None:
        self.calls.append((name, arg))


def _by_task(calls: list[tuple[str, str]], task: str) -> list[str]:
    return [arg for name, arg in calls if name == task]


@pytest.mark.asyncio
async def test_packs_all_embeds_only_image_series() -> None:
    redis = _RecordingRedis()
    series = [
        ("s-ct", "CT"),
        ("s-mr", "MR"),
        ("s-sr", "SR"),  # Structured Report — no pixels, not embeddable
        ("s-seg", "SEG"),  # Segmentation label map — not embeddable
    ]

    packed, embedded = await enqueue_postprocess_jobs(redis, series)

    # Every series is pre-packed.
    assert sorted(_by_task(redis.calls, "pack_volume")) == [
        "s-ct",
        "s-mr",
        "s-seg",
        "s-sr",
    ]
    # Only the diagnostic-image series get an embedding.
    assert sorted(_by_task(redis.calls, "embed_series")) == ["s-ct", "s-mr"]
    assert (packed, embedded) == (4, 2)


@pytest.mark.asyncio
async def test_unknown_or_null_modality_is_let_through_to_worker() -> None:
    # Blocklist semantics: an unknown / null modality is NOT dropped at
    # enqueue — the worker's pixel-decode backstop is the final arbiter.
    redis = _RecordingRedis()
    series = [("s-null", None), ("s-weird", "XYZ"), ("s-us", "US")]

    packed, embedded = await enqueue_postprocess_jobs(redis, series)

    assert sorted(_by_task(redis.calls, "embed_series")) == [
        "s-null",
        "s-us",
        "s-weird",
    ]
    assert (packed, embedded) == (3, 3)


@pytest.mark.asyncio
async def test_coerces_ids_to_str_and_handles_empty() -> None:
    redis = _RecordingRedis()
    assert await enqueue_postprocess_jobs(redis, []) == (0, 0)
    assert redis.calls == []

    # Non-str ids (e.g. UUID objects) are coerced so arq gets a plain str.
    import uuid

    sid = uuid.uuid4()
    packed, embedded = await enqueue_postprocess_jobs(redis, [(sid, "CT")])
    assert (packed, embedded) == (1, 1)
    assert redis.calls == [("pack_volume", str(sid)), ("embed_series", str(sid))]
