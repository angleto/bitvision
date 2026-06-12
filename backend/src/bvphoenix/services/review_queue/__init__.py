"""Shared staging + review engine (``services/review_queue``).

One engine, two consumers with opposite PHI postures:

* the **patient inbound inbox** (store ``inbox_items``) — items are
  patient-scoped and PHI is *preserved*;
* the **public contribution pipeline** (store ``submissions``) — items
  are platform-owned and PHI is *removed* before promotion.

The engine owns the lifecycle (state machine + transition validation),
the auto-check plugin registry, the decision gate and the
promotion/rejection orchestration — it does NOT own the consumer
tables. Each consumer includes :class:`ReviewableItemMixin` in its own
model, registers a :class:`ReviewProfile` (checks, decision policy,
hooks, store accessors) and drives its items through
:mod:`~bvphoenix.services.review_queue.engine`.

Profile status aliases (the contract in task fbbf5270 §4): a consumer
may *present* states under domain names — ``anonymizing`` /
``screening`` are the public profile's view of ``processing``,
``approved`` of ``accepted``, ``published`` of ``promoted`` — but the
stored status is always one of :data:`REVIEW_STATUSES`.
"""

from bvphoenix.services.review_queue.actor import SYSTEM_ACTOR, ReviewActor
from bvphoenix.services.review_queue.checks import (
    CheckContext,
    CheckResult,
    ReviewCheck,
    StagedComponent,
    StagedItem,
    aggregate_verdicts,
)
from bvphoenix.services.review_queue.decision import (
    DecisionPolicy,
    ReviewDecisionError,
)
from bvphoenix.services.review_queue.profile import (
    ReviewProfile,
    get_profile,
    register_profile,
)
from bvphoenix.services.review_queue.states import (
    REVIEW_STATUSES,
    REVIEW_TERMINAL_STATUSES,
    REVIEW_TRANSITIONS,
    ReviewTransitionError,
    validate_transition,
)
from bvphoenix.services.review_queue.store import (
    REVIEW_AUTO_VERDICTS,
    REVIEW_STATUS_ENUM_NAME,
    ReviewableItemMixin,
)

__all__ = [
    "REVIEW_AUTO_VERDICTS",
    "REVIEW_STATUSES",
    "REVIEW_STATUS_ENUM_NAME",
    "REVIEW_TERMINAL_STATUSES",
    "REVIEW_TRANSITIONS",
    "SYSTEM_ACTOR",
    "CheckContext",
    "CheckResult",
    "DecisionPolicy",
    "ReviewActor",
    "ReviewCheck",
    "ReviewDecisionError",
    "ReviewProfile",
    "ReviewTransitionError",
    "ReviewableItemMixin",
    "StagedComponent",
    "StagedItem",
    "aggregate_verdicts",
    "get_profile",
    "register_profile",
    "validate_transition",
]
