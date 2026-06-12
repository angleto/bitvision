"""Per-profile decision policy — who may accept/reject, and how.

The two consumers sit at opposite ends of the gate:

* patient inbox → ``agent_capable``: the patient's own assistant may
  triage their inbox (with full provenance, ``author_kind='agent'``);
* public contribution → ``human_only`` + mandatory reason: publishing
  to the OpenData library is a human-accountable act, same rule as
  ``consultations:finalize``.

``system`` actors can never decide: the auto-check sweep flags, it does
not judge. The optional ``can_decide`` hook is where the consumer plugs
its RBAC (e.g. "reviewer role on the OpenData desk", "owner of the
patient fascicolo").
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.services.review_queue.actor import ReviewActor

if TYPE_CHECKING:
    from bvphoenix.services.review_queue.engine import ReviewableItem

DecisionGate = Literal["human_only", "agent_capable"]
Decision = Literal["accepted", "rejected"]


class ReviewDecisionError(PermissionError):
    """The actor may not take this decision (gate or RBAC hook refused,
    or the mandatory reason is missing). API layers map it onto 403/422."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class DecisionPolicy:
    gate: DecisionGate
    require_reason: bool = False
    # async (db, actor, item) -> bool; None ⇒ gate-only.
    can_decide: Callable[[AsyncSession, ReviewActor, ReviewableItem], Awaitable[bool]] | None = None

    async def authorize(
        self,
        db: AsyncSession,
        actor: ReviewActor,
        item: ReviewableItem,
        *,
        decision: Decision,
        reason: str | None,
    ) -> None:
        """Raise :class:`ReviewDecisionError` unless ``actor`` may take
        ``decision`` on ``item`` under this policy."""
        if actor.kind == "system":
            raise ReviewDecisionError(
                "decision.system_forbidden",
                "system actors cannot decide review items",
            )
        if self.gate == "human_only" and actor.kind != "human":
            raise ReviewDecisionError(
                "decision.human_only",
                f"deciding ({decision}) on this profile is restricted to human users",
            )
        if self.require_reason and not (reason or "").strip():
            raise ReviewDecisionError(
                "decision.reason_required",
                "this profile requires a non-empty reason for every decision",
            )
        if self.can_decide is not None and not await self.can_decide(db, actor, item):
            raise ReviewDecisionError(
                "decision.not_authorized",
                "the actor is not authorized to decide this item",
            )


__all__ = ["Decision", "DecisionGate", "DecisionPolicy", "ReviewDecisionError"]
