"""Single source of truth for the agent-vs-human request context.

Two auth paths populate ``request.state``:

* ``_resolve_assistant_secret`` — Claude.ai connector / any modern
  MCP client. Sets ``is_agent=True`` plus
  ``agent_assistant_id``; leaves ``agent_token_id=None`` because there
  is no ``agent_tokens`` row for this flow.
* legacy JWT agent tokens (pre v2.1.11). Sets ``is_agent=True`` plus
  ``agent_token_id``; ``agent_assistant_id`` is currently None on
  this branch (could be backfilled from
  ``agent_tokens.assistant_id`` if needed).

Endpoints consume this context to:

* stamp ``author_kind='agent'`` on writers (ReportContent, Tag,
  ProvenanceEvent, …);
* propagate ``model_id`` / ``provider`` / ``parser_version`` only when
  the caller is an agent;
* populate the FK back to the assistant row so the audit chain stays
  identifiable for the per-assistant secret path.

Pre-2026-05-03 each writer derived ``is_agent`` from
``request.state.agent_token_id is not None``, missing the
per-assistant flow entirely. The internal MCP session report flagged
the resulting "author_kind=human, model_id=null" rows as the most
visible symptom. This module exists so the next writer that lands
in the codebase has a single helper to call instead of re-deriving
the same logic with subtle variations.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from fastapi import Request


@dataclass(frozen=True, slots=True)
class AgentContext:
    """Snapshot of the agent / human context for a single request.

    Use ``AgentContext.from_request(request)`` at the top of any
    writer endpoint, then read the fields directly. The dataclass is
    frozen so the caller cannot mutate request state through it.
    """

    is_agent: bool
    agent_token_id: uuid.UUID | None
    agent_assistant_id: uuid.UUID | None

    @classmethod
    def from_request(cls, request: Request) -> AgentContext:
        return cls(
            is_agent=bool(getattr(request.state, "is_agent", False)),
            agent_token_id=getattr(request.state, "agent_token_id", None),
            agent_assistant_id=getattr(request.state, "agent_assistant_id", None),
        )

    @property
    def author_kind(self) -> str:
        """``'agent'`` for agent-authored writes, ``'human'`` otherwise.

        The string value matches the CHECK constraint on
        ``report_contents.author_kind`` and
        ``provenance_events.agent_kind``."""
        return "agent" if self.is_agent else "human"


__all__ = ["AgentContext"]
