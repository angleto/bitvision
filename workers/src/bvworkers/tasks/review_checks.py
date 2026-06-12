"""Arq task: run the review-queue auto-checks for one staged item.

Generic over review profiles: the consumer modules named in
``BVP_REVIEW_PROFILE_MODULES`` are imported once per worker process
(registering their profiles), then the profile is resolved by name and
the shared engine drives ``received -> processing ->
needs_review|blocked``.

Exceptions re-raise so arq applies its retry/failure accounting; the
deterministic job id (etag-keyed, see
``bvphoenix.services.review_queue.jobs``) keeps duplicate enqueues
collapsed while a run is in flight, and the consumer-side
``requeue_stale_processing`` sweep recovers items orphaned by a worker
death (the ``embed_series`` recovery pattern).
"""

from __future__ import annotations

import importlib
import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from bvworkers.config import get_settings

logger = logging.getLogger(__name__)

_profiles_loaded = False


def _load_profile_modules() -> None:
    """Import (once) the modules that register review profiles."""
    global _profiles_loaded
    if _profiles_loaded:
        return
    settings = get_settings()
    for path in filter(None, (p.strip() for p in settings.review_profile_modules.split(","))):
        importlib.import_module(path)
    _profiles_loaded = True


async def run_review_checks(ctx: dict, profile_name: str, item_id: str) -> dict:
    from bvphoenix.services.review_queue import engine as review_engine
    from bvphoenix.services.review_queue.profile import get_profile

    _load_profile_modules()
    profile = get_profile(profile_name)

    settings = get_settings()
    db_engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    try:
        async with AsyncSession(db_engine) as db:
            item = await profile.load_item(db, uuid.UUID(item_id))
            if item is None:
                return {"status": "not_found", "item_id": item_id}
            if item.status not in ("received", "processing", "needs_review", "blocked"):
                # Decided/terminal items are a terminal SKIP, not a failure —
                # a stale enqueue must not fight a reviewer's decision.
                return {"status": "skipped", "item_status": item.status}
            if item.status != "processing":
                await review_engine.start_processing(db, profile, item)
            verdict = await review_engine.run_auto_checks(db, profile, item)
            await db.commit()
            return {"status": "checked", "auto_verdict": verdict, "item_id": item_id}
    finally:
        await db_engine.dispose()


__all__ = ["run_review_checks"]
