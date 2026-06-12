"""Actor identity for review-queue transitions.

The engine runs from three call sites with different identity sources:
HTTP endpoints (human or agent, identity on ``request.state``), arq
workers (no request — the auto-check sweep acts as ``system``) and the
promotion hooks (inherit the deciding actor). ``ReviewActor`` is the
request-independent snapshot the engine threads through every
transition so provenance rows stay correctly attributed in all three.

Mirrors the ``provenance_events`` CHECK constraints: a ``human`` actor
must carry ``subject_id``; an ``agent`` actor must carry at least one
of ``agent_token_id`` / ``agent_assistant_id``; ``system`` carries
nothing. Violations fail fast here instead of surfacing as an
IntegrityError mid-transaction.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from fastapi import Request

from bvphoenix.services.agent_context import AgentContext

if TYPE_CHECKING:
    # Typing-only: consumer models import the store mixin through this
    # package, so a runtime ``db.models`` import here would close the
    # circle (db.models.<consumer> → review_queue → actor → db.models).
    from bvphoenix.db.models import User

ActorKind = Literal["human", "agent", "system"]


@dataclass(frozen=True, slots=True)
class ReviewActor:
    kind: ActorKind
    subject_id: uuid.UUID | None = None
    agent_token_id: uuid.UUID | None = None
    agent_assistant_id: uuid.UUID | None = None

    def __post_init__(self) -> None:
        if self.kind == "human" and self.subject_id is None:
            raise ValueError("human actor requires subject_id")
        if self.kind == "agent" and self.agent_token_id is None and self.agent_assistant_id is None:
            raise ValueError("agent actor requires agent_token_id or agent_assistant_id")

    @property
    def is_agent(self) -> bool:
        return self.kind == "agent"

    @property
    def is_human(self) -> bool:
        return self.kind == "human"

    @classmethod
    def from_request(cls, user: User, request: Request) -> ReviewActor:
        """Snapshot the calling identity of an HTTP request — the same
        derivation :func:`~bvphoenix.services.provenance_log.record_provenance`
        applies, reified so it can outlive the request."""
        ctx = AgentContext.from_request(request)
        if ctx.is_agent:
            return cls(
                kind="agent",
                subject_id=None,
                agent_token_id=ctx.agent_token_id,
                agent_assistant_id=ctx.agent_assistant_id,
            )
        return cls(kind="human", subject_id=user.subject_id)


# Single shared instance for worker-driven transitions. ``system`` is an
# established ``provenance_events.agent_kind`` value, distinct from
# ``agent`` (an LLM acting deliberately) — the auto-check sweep is
# infrastructure, not an accountable decision-maker, and the decision
# gate refuses it accordingly.
SYSTEM_ACTOR = ReviewActor(kind="system")

__all__ = ["SYSTEM_ACTOR", "ActorKind", "ReviewActor"]
