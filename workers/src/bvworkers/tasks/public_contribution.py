"""Public-contribution worker tasks: promote an accepted submission.

The auto-check pass is driven by the generic ``run_review_checks`` task (any
profile, resolved via ``BVP_REVIEW_PROFILE_MODULES``). This module adds the
promotion step: an ``accepted`` submission is published to the OpenData tier
through the profile's ``on_accept`` hook (``accepted -> promoting -> promoted``),
retried by the maintenance sweep if the hook raises.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

import bvphoenix.services.review_queue.engine as review_engine
from bvphoenix.db.session import SERVICE_SUBJECT, set_current_subject
from bvphoenix.services.review_queue.profile import get_profile
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from bvworkers.config import get_settings

log = logging.getLogger(__name__)

PROFILE_NAME = "public_contribution"


def _ensure_profile_registered() -> None:
    # Importing the profile module registers it (idempotent). The generic
    # checks task relies on BVP_REVIEW_PROFILE_MODULES; the promote path imports
    # directly so it works even if the env is unset in this worker.
    import bvphoenix.services.public_contribution.profile  # noqa: F401


async def promote_submission(ctx: dict[str, Any], submission_id: str) -> dict[str, Any]:
    """Publish an ``accepted`` submission to the OpenData tier."""
    _ensure_profile_registered()
    settings = get_settings()
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    try:
        profile = get_profile(PROFILE_NAME)
        async with AsyncSession(engine) as db:
            await set_current_subject(db, SERVICE_SUBJECT)
            item = await profile.load_item(db, uuid.UUID(submission_id))
            if item is None:
                return {"status": "not_found", "submission_id": submission_id}
            if item.status != "accepted":
                # Idempotent: another runner already promoted it, or it was
                # rejected/expired. Never re-drive a terminal item.
                return {"status": "skipped", "submission_id": submission_id, "current": item.status}
            outcome = await review_engine.promote(db, profile, item)
            await db.commit()
        return {"status": "promoted", "submission_id": submission_id, "outcome": outcome or {}}
    except Exception:
        log.exception("promote_submission failed for %s", submission_id)
        raise
    finally:
        await engine.dispose()


__all__ = ["promote_submission"]
