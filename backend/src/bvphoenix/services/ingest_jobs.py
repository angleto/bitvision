"""Post-ingest background-job enqueueing — single source of truth.

Every route that ingests new DICOM pixel data must kick off the same two
background jobs per newly-created series:

* ``pack_volume`` — pre-pack the series volume so the viewer doesn't pay the
  pack cost on first open.
* ``embed_series`` — generate the BiomedCLIP image embedding that powers
  visual / similarity search (``/api/similar-to``). Only EMBEDDABLE
  diagnostic-image series qualify; non-image series (SR / PR / SEG, ...) are
  filtered out here so the queue isn't polluted with jobs the worker would
  only terminally skip. The embeddable policy lives in one place:
  :mod:`bvphoenix.services.embeddable`.

Before this module the ``embed_series`` enqueue lived ONLY in the
``bvphoenix-import`` CLI path. Every web upload route (drag-drop
``POST /studies``, STOW-RS, the resumable bulk-upload worker) enqueued only
``pack_volume`` — so studies uploaded through the UI never got an image
vector and ``/api/similar-to`` returned ``study_not_indexed`` forever, with
no periodic sweep to heal it. Funnelling every ingest path through one
helper keeps the job set from drifting between upload routes again.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from bvphoenix.services.embeddable import is_embeddable_modality


async def enqueue_postprocess_jobs(
    redis, series: Iterable[tuple[str | uuid.UUID, str | None]]
) -> tuple[int, int]:
    """Enqueue ``pack_volume`` for every series and ``embed_series`` for every
    embeddable one.

    ``series`` is an iterable of ``(series_id, modality)`` pairs — the
    modality drives the embeddable filter so this helper needs no DB access.
    ``redis`` is an already-connected arq pool; the caller owns its lifecycle
    (creation + close), so this composes with both the short-lived pools the
    HTTP handlers spin up and the long-lived pool a worker already holds.

    Returns ``(packed, embedded)`` job counts for the caller's log line.
    Both job types are idempotent (``pack_volume`` re-packs, ``embed_series``
    skips when the vector already exists), so a re-ingest stays cheap.
    """
    pairs = [(str(sid), mod) for sid, mod in series]
    for sid, _ in pairs:
        await redis.enqueue_job("pack_volume", sid)
    embeddable = [sid for sid, mod in pairs if is_embeddable_modality(mod)]
    for sid in embeddable:
        await redis.enqueue_job("embed_series", sid)
    return len(pairs), len(embeddable)
