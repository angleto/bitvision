"""Single helper to append a ``ProvenanceEvent`` row.

Pre-2026-05-03 each api module that needed to record provenance kept
its own ``_record_provenance`` clone (clinical_events.py,
report_contents.py, ...). The clones drifted: some derived
``is_agent`` from ``agent_token_id`` (broken for the per-assistant
secret path), some only set ``agent_token_id`` (missing
``agent_assistant_id``), some swallowed exceptions silently. The
duplication is what made the internal session report symptoms so
asymmetric — ``add_tag_to_study`` recorded provenance correctly,
``confirm_event_link`` did not.

This module exposes one helper. Call it from any endpoint that
mutates a v3 entity. The helper:

* derives the agent context via :class:`AgentContext.from_request`;
* maps ``is_agent`` onto ``agent_kind``;
* sets ``agent_subject_id`` to the human's subject when human-
  authored, ``None`` when agent-authored (the agent identity is
  captured by ``agent_token_id`` / ``agent_assistant_id``);
* populates BOTH FKs so the audit chain can resolve through either
  the legacy JWT path or the modern per-assistant secret path.

The caller still owns the surrounding transaction and the
``await db.commit()`` decision: this helper only ``db.add(...)`` the
event, so a failed mutation rolls back the provenance entry too.
"""

from __future__ import annotations

import uuid

# Re-import via the stub module name to avoid a circular import of
# ``Request`` at module load time.
from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.db.models import ProvenanceEvent, User
from bvphoenix.services.agent_context import AgentContext


def record_provenance(
    db: AsyncSession,
    *,
    target_kind: str,
    target_id: uuid.UUID,
    activity: str,
    user: User,
    request: Request,
    diff: dict | None = None,
    source_kind: str | None = None,
    source_id: uuid.UUID | None = None,
) -> ProvenanceEvent:
    """Append a ``ProvenanceEvent`` row and return it (already added
    to the session, not yet flushed).

    Parameters mirror the ``ProvenanceEvent`` columns; the
    ``agent_*`` and ``agent_kind`` columns are derived from
    ``request.state`` via :class:`AgentContext`. ``source_kind`` /
    ``source_id`` are optional fields that point at the originating
    artefact (e.g. the document a report_content was extracted from).
    """
    ctx = AgentContext.from_request(request)
    event = ProvenanceEvent(
        target_kind=target_kind,
        target_id=target_id,
        activity=activity,
        agent_kind=ctx.author_kind,
        agent_subject_id=None if ctx.is_agent else user.subject_id,
        agent_token_id=ctx.agent_token_id,
        agent_assistant_id=ctx.agent_assistant_id,
        source_kind=source_kind,
        source_id=source_id,
        diff=diff,
    )
    db.add(event)
    return event


__all__ = ["record_provenance"]
