"""Review profile — how a consumer plugs its store into the engine.

A profile bundles everything consumer-specific: the store accessors
(load an item, build its staged view), the ordered check list, the
decision policy and the promotion/rejection hooks. The engine stays
generic; registering a profile is the *only* integration step a
consumer performs besides including :class:`ReviewableItemMixin` in its
model.

The registry is process-global (same pattern as the worker task
registry): consumers register at import time, and the arq worker
imports the modules listed in ``BVP_REVIEW_PROFILE_MODULES`` before
resolving a profile by name.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.services.review_queue.checks import ReviewCheck, StagedItem
from bvphoenix.services.review_queue.decision import DecisionPolicy

if TYPE_CHECKING:
    from bvphoenix.services.review_queue.actor import ReviewActor
    from bvphoenix.services.review_queue.engine import ReviewableItem


@dataclass(frozen=True, slots=True)
class ReviewProfile:
    """One consumer's binding of the shared engine.

    ``provenance_target_kind`` must be a value admitted by the
    ``provenance_events`` target-kind CHECK (``inbox_item`` /
    ``submission``, added in migration ``0024_review_queue_shared``).

    ``on_accept`` is the promotion hook: it performs the concrete
    outcome (ingest into the fascicolo / publish to OpenData) and runs
    inside the ``promoting`` state; raising marks the item ``failed``.
    ``on_reject`` runs exactly once when a rejection lands; it must not
    raise for ordinary cleanup problems (log and continue) because the
    rejection itself has already been decided.
    """

    name: str
    provenance_target_kind: str
    checks: tuple[ReviewCheck, ...]
    decision: DecisionPolicy
    load_item: Callable[[AsyncSession, uuid.UUID], Awaitable[ReviewableItem | None]]
    load_staged: Callable[[AsyncSession, ReviewableItem], Awaitable[StagedItem]]
    on_accept: Callable[
        [AsyncSession, ReviewableItem, ReviewActor], Awaitable[dict[str, Any] | None]
    ]
    on_reject: Callable[[AsyncSession, ReviewableItem, ReviewActor, str | None], Awaitable[None]]


_PROFILES: dict[str, ReviewProfile] = {}


def register_profile(profile: ReviewProfile) -> ReviewProfile:
    """Register (or idempotently re-register) a profile by name.

    Re-registration with a *different* object is refused: two modules
    silently fighting over a profile name is a deployment bug.
    """
    existing = _PROFILES.get(profile.name)
    if existing is not None and existing is not profile:
        raise ValueError(f"review profile {profile.name!r} is already registered")
    _PROFILES[profile.name] = profile
    return profile


def get_profile(name: str) -> ReviewProfile:
    try:
        return _PROFILES[name]
    except KeyError:
        raise KeyError(
            f"unknown review profile {name!r}; registered: {sorted(_PROFILES)} — "
            "did the consumer module get imported (BVP_REVIEW_PROFILE_MODULES)?"
        ) from None


def registered_profiles() -> tuple[str, ...]:
    return tuple(sorted(_PROFILES))


def _clear_profiles_for_tests() -> None:
    _PROFILES.clear()


__all__ = [
    "ReviewProfile",
    "get_profile",
    "register_profile",
    "registered_profiles",
]
