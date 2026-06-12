"""Engine — orchestrates the review lifecycle over any profile/store.

Every public function takes ``(db, profile, item, actor, ...)``,
validates the edge against the state machine, mutates the mixin
columns, bumps the optimistic-concurrency ``etag`` and appends a
``ProvenanceEvent`` (activity ``transition.<to_status>``) — one row per
transition, attributed to the human / agent / system actor that drove
it. The caller owns the surrounding transaction (``db.commit``), same
contract as ``record_provenance``.

The engine never touches consumer columns beyond the mixin, and never
imports a consumer model: store access goes through the profile's
accessors, outcomes through its hooks.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.services.provenance_log import record_provenance_event
from bvphoenix.services.review_queue.actor import SYSTEM_ACTOR, ReviewActor
from bvphoenix.services.review_queue.checks import CheckContext, StagedItem, run_checks
from bvphoenix.services.review_queue.decision import Decision
from bvphoenix.services.review_queue.profile import ReviewProfile
from bvphoenix.services.review_queue.states import validate_transition

logger = logging.getLogger(__name__)


@runtime_checkable
class ReviewableItem(Protocol):
    """Structural view of a store row carrying the mixin columns."""

    id: uuid.UUID
    status: str
    auto_checks: dict | None
    auto_verdict: str | None
    reviewed_by_subject_id: uuid.UUID | None
    reviewed_at: datetime | None
    review_note: str | None
    etag: uuid.UUID


def _transition(
    db: AsyncSession,
    profile: ReviewProfile,
    item: ReviewableItem,
    to_status: str,
    actor: ReviewActor,
    *,
    diff: dict | None = None,
) -> None:
    """Validated state move + etag bump + provenance row. Internal: the
    public functions below are the only entry points, so every caller
    gets the full bookkeeping or nothing."""
    from_status = item.status
    validate_transition(from_status, to_status)
    item.status = to_status
    item.etag = uuid.uuid4()
    record_provenance_event(
        db,
        target_kind=profile.provenance_target_kind,
        target_id=item.id,
        activity=f"transition.{to_status}",
        agent_kind=actor.kind,
        agent_subject_id=actor.subject_id,
        agent_token_id=actor.agent_token_id,
        agent_assistant_id=actor.agent_assistant_id,
        diff={"from": from_status, "to": to_status, **(diff or {})},
    )


async def start_processing(
    db: AsyncSession,
    profile: ReviewProfile,
    item: ReviewableItem,
    *,
    actor: ReviewActor = SYSTEM_ACTOR,
) -> None:
    """``received -> processing`` (or ``needs_review``/``blocked`` ->
    ``processing`` for an idempotent re-run)."""
    _transition(db, profile, item, "processing", actor)


async def run_auto_checks(
    db: AsyncSession,
    profile: ReviewProfile,
    item: ReviewableItem,
    *,
    staged: StagedItem | None = None,
    actor: ReviewActor = SYSTEM_ACTOR,
) -> str:
    """Run the profile's checks on a ``processing`` item and land it on
    ``blocked`` (any ``block`` verdict) or ``needs_review``.

    Returns the aggregated ``auto_verdict``. Safe to re-run: results are
    keyed by check name and overwrite in place.
    """
    if item.status != "processing":
        # Fail loudly: the worker should have driven received->processing
        # first; accepting any state here would reintroduce jumps.
        validate_transition(item.status, "needs_review")  # raises with context
    if staged is None:
        staged = await profile.load_staged(db, item)
    auto_checks, verdict = await run_checks(
        CheckContext(db=db, staged=staged),
        profile.checks,
        previous=item.auto_checks,
    )
    item.auto_checks = auto_checks
    item.auto_verdict = verdict
    to_status = "blocked" if verdict == "block" else "needs_review"
    _transition(db, profile, item, to_status, actor, diff={"auto_verdict": verdict})
    return verdict


async def decide(
    db: AsyncSession,
    profile: ReviewProfile,
    item: ReviewableItem,
    *,
    decision: Decision,
    actor: ReviewActor,
    reason: str | None = None,
) -> None:
    """Apply a human/agent decision on a ``needs_review``/``blocked`` item.

    The profile's :class:`DecisionPolicy` gates the actor first
    (``human_only`` vs ``agent_capable``, mandatory reason, RBAC hook);
    the state machine then enforces that a ``blocked`` item can only be
    rejected. On rejection the profile's ``on_reject`` hook runs exactly
    once, inside the same transaction.
    """
    await profile.decision.authorize(db, actor, item, decision=decision, reason=reason)
    _transition(db, profile, item, decision, actor, diff={"reason": reason})
    item.reviewed_by_subject_id = actor.subject_id
    item.reviewed_at = datetime.now(UTC)
    item.review_note = reason
    if decision == "rejected":
        await profile.on_reject(db, item, actor, reason)


async def promote(
    db: AsyncSession,
    profile: ReviewProfile,
    item: ReviewableItem,
    *,
    actor: ReviewActor = SYSTEM_ACTOR,
) -> dict[str, Any] | None:
    """Drive ``accepted -> promoting -> promoted`` through the profile's
    promotion hook.

    The hook performs the concrete outcome (fascicolo ingest / OpenData
    publish). If it raises, the item lands on ``failed`` (terminal) with
    the error recorded in provenance, and the exception propagates so
    the arq layer can count the retry; re-driving a failed item means
    re-staging it, not silently re-promoting.
    """
    _transition(db, profile, item, "promoting", actor)
    try:
        outcome = await profile.on_accept(db, item, actor)
    except Exception as exc:
        logger.exception("promotion hook failed for %s item %s", profile.name, item.id)
        _transition(db, profile, item, "failed", actor, diff={"error": str(exc)})
        raise
    _transition(db, profile, item, "promoted", actor, diff={"outcome": outcome or {}})
    return outcome


async def expire(
    db: AsyncSession,
    profile: ReviewProfile,
    item: ReviewableItem,
    *,
    actor: ReviewActor = SYSTEM_ACTOR,
    reason: str | None = None,
) -> None:
    """Retention sweep: an undecided item ages out (terminal)."""
    _transition(db, profile, item, "expired", actor, diff={"reason": reason})


__all__ = [
    "ReviewableItem",
    "decide",
    "expire",
    "promote",
    "run_auto_checks",
    "start_processing",
]
