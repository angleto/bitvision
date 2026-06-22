"""Report which studies / series carry a BiomedCLIP image embedding.

Listing + detail endpoints use these to surface an ``indexed`` flag so the
Visual Search picker can mark (and disable the "use" action on) exemplars
that would dead-end on ``/api/similar-to`` with ``study_not_indexed``.

Each helper is a single index-backed query over a *bounded* id set (one
results page, <=200 ids), so it is cheap enough to call per request. The
join hits the ``embeddings`` unique index ``(target_kind, target_id,
model_id)``; it never scans the whole table.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.db.models import Embedding, Series

# Single source of truth is ``workers/embed_series.py`` MODEL_ID; mirrored
# here (as in cli/backfill.py and api/search_hybrid.py) because the worker
# package is not importable from the backend.
_IMAGE_MODEL_ID = "biomedclip-v1"


async def embedded_series_ids(db: AsyncSession, series_ids: Iterable[uuid.UUID]) -> set[uuid.UUID]:
    """Subset of ``series_ids`` that already have a BiomedCLIP vector."""
    ids = list(series_ids)
    if not ids:
        return set()
    rows = (
        (
            await db.execute(
                select(Embedding.target_id).where(
                    Embedding.target_kind == "series",
                    Embedding.model_id == _IMAGE_MODEL_ID,
                    Embedding.target_id.in_(ids),
                )
            )
        )
        .scalars()
        .all()
    )
    return {uuid.UUID(str(r)) for r in rows}


async def indexed_study_ids(db: AsyncSession, study_ids: Iterable[uuid.UUID]) -> set[uuid.UUID]:
    """Subset of ``study_ids`` with at least one embedded image series.

    Mirrors how ``find_similar_studies`` resolves a study id: a study is
    "indexed" iff one of its series carries an image vector.
    """
    ids = list(study_ids)
    if not ids:
        return set()
    rows = (
        (
            await db.execute(
                select(Series.study_id)
                .join(
                    Embedding,
                    (Embedding.target_id == Series.id)
                    & (Embedding.target_kind == "series")
                    & (Embedding.model_id == _IMAGE_MODEL_ID),
                )
                .where(Series.study_id.in_(ids))
                .distinct()
            )
        )
        .scalars()
        .all()
    )
    return {uuid.UUID(str(r)) for r in rows}
