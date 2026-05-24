"""High-level glue between FastAPI routes and the F12 versioning core.

Each clinically meaningful write that should leave a trace in the
patient's git-like chain (``ClinicalNote``, ``Report``, ``Consultation``
body, ``Document``, ``Patient`` demographic edits) calls
:func:`record_versioned_change` after its ORM mutation, inside the same
DB transaction. This module hides the three repeated steps:

* derive the actor (``human`` vs ``agent`` from ``request.state``);
* resolve the write branch (``main`` for the patient owner, the
  consultation branch otherwise) via :func:`resolve_branch_for_write`;
* commit a single ``EntityChange`` via :func:`commit_change`.

The pilot module ``api/clinical_notes.py`` predates this helper and
inlines the same boilerplate; new call sites should prefer this
function for consistency.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from fastapi import HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.services.versioning import (
    ActorContext,
    CommitResult,
    EntityChange,
    commit_change,
    ensure_main_seeded,
    resolve_branch_for_write,
)

if TYPE_CHECKING:
    from bvphoenix.db.models import Patient, User


def resolve_actor(request: Request, user: User) -> ActorContext:
    """Build an ``ActorContext`` from the request principal.

    Three branches, in priority order:

    * **agent** — MCP-authenticated request. Detected via
      ``request.state.is_agent`` (set by ``auth/deps.py`` on every
      assistant authentication, both legacy ``AgentToken`` and modern
      per-assistant client_secret). For legacy tokens we additionally
      pin the row id on ``commits.agent_token_id`` to preserve the
      foreign key. For the modern flow the FK column stays NULL but
      ``author_kind='agent'`` and the model/provider are still set, so
      the revision UI marks every AI write as such — the contract is
      "every change made by an AI is identifiable", and provenance
      for the modern flow is fully captured by the audit log
      (``agent_token_id``, ``model_version``, ``conversation_id``).
    * **link** — anonymous share-link JWT. The
      ``/shared/{token}/verify`` route mints a JWT with a
      ``share_link_id`` claim that the auth dep pins onto
      ``request.state.share_link_id``. Writes routed through this
      branch get ``kind='link'`` and the share_link id is persisted
      onto ``commits.share_link_id`` so the revision-history UI can
      badge them as "modality A".
    * **human** — default for any other authenticated request.
    """
    if getattr(request.state, "is_agent", False):
        legacy_token = getattr(request.state, "agent_token", None)
        # Modern per-assistant client_secret flow pins the assistant row
        # id directly on ``request.state.agent_assistant_id`` (see
        # ``auth/deps.py:_resolve_assistant_secret``); legacy JWT tokens
        # leave it unset and we follow ``agent_tokens.assistant_id``.
        modern_assistant_id = getattr(request.state, "agent_assistant_id", None)
        legacy_assistant_id = (
            getattr(legacy_token, "assistant_id", None) if legacy_token is not None else None
        )
        return ActorContext(
            subject_id=user.subject_id,
            kind="agent",
            model_id=None,
            provider=None,
            agent_token_id=legacy_token.id if legacy_token is not None else None,
            agent_assistant_id=modern_assistant_id or legacy_assistant_id,
        )
    share_link_id = getattr(request.state, "share_link_id", None)
    if share_link_id is not None:
        return ActorContext(
            subject_id=user.subject_id,
            kind="link",
            share_link_id=share_link_id,
        )
    return ActorContext(subject_id=user.subject_id, kind="human")


def _is_owner(patient: Patient, user: User) -> bool:
    return (
        patient.managed_by_subject_id == user.subject_id
        or patient.self_user_subject_id == user.subject_id
        or bool(getattr(user, "is_admin", False))
    )


async def record_versioned_change(
    db: AsyncSession,
    *,
    patient: Patient,
    user: User,
    request: Request,
    entity_kind: str,
    entity_id: uuid.UUID,
    payload: dict | None,
    message: str,
    consultation_id: uuid.UUID | None = None,
    actor_override: ActorContext | None = None,
) -> CommitResult:
    """Append one entity change to the patient's versioning chain.

    ``payload=None`` records a delete (the entity drops out of the new
    manifest). ``consultation_id`` routes the write to the consultation
    branch; without it, owner writes land on ``main`` and non-owners
    are rejected with 403.

    Caller stays responsible for the ORM mutation and its ``audit_log``
    entry; this helper only handles the versioning side and leaves
    the transaction open for the caller's final ``db.commit()``.
    """
    try:
        branch_ref = await resolve_branch_for_write(
            db,
            patient_id=patient.id,
            user_subject_id=user.subject_id,
            consultation_id=consultation_id,
            is_owner=_is_owner(patient, user),
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    actor = actor_override or resolve_actor(request, user)
    if actor.kind == "agent" and isinstance(payload, dict):
        mp = payload.get("model_id")
        pp = payload.get("provider")
        if mp or pp:
            actor = ActorContext(
                subject_id=actor.subject_id,
                kind="agent",
                model_id=mp,
                provider=pp,
                agent_token_id=actor.agent_token_id,
            )

    return await commit_change(
        db,
        patient_id=patient.id,
        branch_ref=branch_ref,
        actor=actor,
        message=message,
        changes=[
            EntityChange(
                entity_kind=entity_kind,
                entity_id=entity_id,
                payload=payload,
            )
        ],
    )


async def seed_patient_main(
    db: AsyncSession,
    *,
    patient: Patient,
    user: User,
    request: Request,
) -> None:
    """Lazy-seed ``main`` for a freshly created patient.

    Called from ``create_patient`` so the Versions tab is non-empty
    from day zero, even before any clinical entity is written. The
    seed is idempotent: re-invoking on a patient that already has a
    ``main`` ref is a no-op.
    """
    actor = resolve_actor(request, user)
    await ensure_main_seeded(db, patient_id=patient.id, actor=actor)
