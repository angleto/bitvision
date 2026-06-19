"""Enqueue the DZI tiling background job for pathology slides.

Counterpart of :func:`services.ingest_jobs.enqueue_postprocess_jobs` for
the DICOM path: a redis-only helper (no DB access) so it composes with
both the short-lived pools the CLI spins up and a worker's long-lived
pool. The caller owns the pool lifecycle.

``tile_wsi`` is idempotent (self-skips on ``dzi_ready``) and bounds its
own concurrency with a per-pod lock, so a re-enqueue is cheap and a
backfill stampede drains serially. A fresh arq job id is minted per
slide; the viewer tracks readiness by polling the slide's ``dzi_ready``
flag, not a Job row, so no ``jobs`` row is required here.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable


async def enqueue_tile_jobs(redis, slide_ids: Iterable[str | uuid.UUID]) -> int:
    """Enqueue ``tile_wsi`` for every slide id. Returns the count."""
    count = 0
    for sid in slide_ids:
        # First positional arg is the Job-row id slot (tile_wsi tracks
        # progress against it when present); we mint a throwaway uuid
        # because the viewer polls dzi_ready rather than a Job row.
        await redis.enqueue_job("tile_wsi", str(uuid.uuid4()), str(sid))
        count += 1
    return count
