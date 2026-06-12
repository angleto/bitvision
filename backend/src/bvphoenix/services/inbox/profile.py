"""The ``patient_inbox`` review profile — the engine binding.

Importing this module registers the profile (idempotently). Static
import sites: ``api/inbox.py`` (the review endpoints) and the worker
tasks; the generic ``run_review_checks`` arq task reaches it through
``BVP_REVIEW_PROFILE_MODULES=bvphoenix.services.inbox.profile``.

Decision policy (fbbf5270 §4): gate ``agent_capable`` — triaging the
patient's own inbox is not a clinical act, an authorised assistant may
do it with full ``author_kind='agent'`` provenance. ``can_decide``
plugs the RBAC: humans need ``REVIEW_INBOX`` on the patient
(owner/self by construction), agents need their assistant's explicit
patient binding (``AgentAssistantPatient``).
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.config import get_settings
from bvphoenix.db.models import (
    AgentAssistantPatient,
    AgentToken,
    Document,
    InboxItem,
    Patient,
    User,
)
from bvphoenix.services.inbox.checks import SenderVerifyCheck
from bvphoenix.services.inbox.emails import purge_staged
from bvphoenix.services.inbox.promotion import promote_item_payload, reject_item_cleanup
from bvphoenix.services.permissions import REVIEW_INBOX, can_patient
from bvphoenix.services.review_queue import (
    CheckContext,
    CheckResult,
    DecisionPolicy,
    ReviewProfile,
    StagedComponent,
    StagedItem,
    register_profile,
)
from bvphoenix.services.review_queue.actor import ReviewActor
from bvphoenix.services.review_queue.checks import aggregate_verdicts
from bvphoenix.services.review_queue.engine import ReviewableItem
from bvphoenix.services.review_queue.plugins import (
    ArchiveGuardCheck,
    ClamAVCheck,
    DicomRouteCheck,
    MagicAllowlistCheck,
)
from bvphoenix.storage import get_s3_storage

PROFILE_NAME = "patient_inbox"


class PatientDocDedupCheck:
    """Patient-scoped duplicate detection.

    The shared :class:`~bvphoenix.services.review_queue.plugins.DedupCheck`
    takes a ``(db, sha)`` lookup with no item context — for the inbox
    that would mean a cross-patient hash sweep whose matches (document
    ids of *other* patients) would leak into the reviewer-visible
    details. This check scopes strictly to the item's own patient: a
    re-sent referto is a ``warn``, anything outside the namespace is
    invisible by construction.
    """

    name = "dedup"

    async def run(self, ctx: CheckContext) -> CheckResult:
        patient_raw = (ctx.staged.manifest or {}).get("patient_id")
        if not patient_raw:
            return CheckResult(verdict="error", details={"reason": "manifest missing patient_id"})
        patient_id = uuid.UUID(str(patient_raw))
        components: dict[str, dict] = {}
        verdicts: list[str] = []
        for comp in ctx.staged.components:
            digest = hashlib.sha256(await comp.read()).hexdigest()
            matches = (
                (
                    await ctx.db.execute(
                        select(Document.id).where(
                            Document.patient_id == patient_id,
                            Document.content_sha256 == digest,
                            Document.deleted_at.is_(None),
                        )
                    )
                )
                .scalars()
                .all()
            )
            entry: dict = {"sha256": digest}
            if matches:
                entry["duplicate_of"] = [str(m) for m in matches]
                verdicts.append("warn")
            else:
                verdicts.append("pass")
            components[comp.name] = entry
        return CheckResult(verdict=aggregate_verdicts(verdicts), details={"components": components})


async def _load_item(db: AsyncSession, item_id: uuid.UUID) -> ReviewableItem | None:
    return (await db.execute(select(InboxItem).where(InboxItem.id == item_id))).scalar_one_or_none()


async def _load_staged(db: AsyncSession, item: ReviewableItem) -> StagedItem:
    assert isinstance(item, InboxItem)
    settings = get_settings()
    storage = get_s3_storage()
    manifest = dict(item.manifest or {})
    manifest.setdefault("patient_id", str(item.patient_id))

    def _reader(key: str):
        async def _read() -> bytes:
            return await asyncio.to_thread(
                storage.get_object_bytes, bucket=settings.s3_bucket_raw, key=key
            )

        return _read

    components = [
        StagedComponent(
            name=comp["name"],
            size_bytes=int(comp.get("size_bytes") or 0),
            content_type=comp.get("content_type"),
            read=_reader(comp["s3_key"]),
        )
        for comp in manifest.get("components", [])
        if comp.get("s3_key")
    ]
    return StagedItem(item_id=item.id, components=components, manifest=manifest)


async def _can_decide(db: AsyncSession, actor: ReviewActor, item: ReviewableItem) -> bool:
    assert isinstance(item, InboxItem)
    if actor.is_human:
        user = (
            await db.execute(select(User).where(User.subject_id == actor.subject_id))
        ).scalar_one_or_none()
        patient = await db.get(Patient, item.patient_id)
        if user is None or patient is None:
            return False
        # ``can_patient`` resolves owner/self to ALL_PERMS (which
        # includes REVIEW_INBOX) and explicit grants otherwise. The
        # share-link 403 is enforced at the API layer before the engine
        # is ever reached; this re-check is the defense-in-depth floor.
        return await can_patient(db, user=user, action=REVIEW_INBOX, patient=patient)
    # Agent actor: the assistant must be explicitly bound to this
    # patient. Resolve the assistant from the token row when the legacy
    # token path is in play.
    assistant_id = actor.agent_assistant_id
    if assistant_id is None and actor.agent_token_id is not None:
        assistant_id = (
            await db.execute(
                select(AgentToken.assistant_id).where(AgentToken.id == actor.agent_token_id)
            )
        ).scalar_one_or_none()
    if assistant_id is None:
        return False
    binding = (
        await db.execute(
            select(AgentAssistantPatient.assistant_id).where(
                AgentAssistantPatient.assistant_id == assistant_id,
                AgentAssistantPatient.patient_id == item.patient_id,
            )
        )
    ).scalar_one_or_none()
    return binding is not None


async def _on_accept(
    db: AsyncSession, item: ReviewableItem, actor: ReviewActor
) -> dict[str, Any] | None:
    assert isinstance(item, InboxItem)
    return await promote_item_payload(db, item=item, actor=actor)


async def _on_reject(
    db: AsyncSession, item: ReviewableItem, actor: ReviewActor, reason: str | None
) -> None:
    assert isinstance(item, InboxItem)
    await reject_item_cleanup(db, item=item)
    await purge_staged(item)


INBOX_PROFILE = register_profile(
    ReviewProfile(
        name=PROFILE_NAME,
        provenance_target_kind="inbox_item",
        checks=(
            # Order: cheap structural gates first, content scan, then
            # signals. A ``block`` from any of them flips the item.
            ArchiveGuardCheck(),
            MagicAllowlistCheck(),
            ClamAVCheck(),
            PatientDocDedupCheck(),
            DicomRouteCheck(),
            SenderVerifyCheck(),
        ),
        decision=DecisionPolicy(
            gate="agent_capable",
            require_reason=False,
            can_decide=_can_decide,
        ),
        load_item=_load_item,
        load_staged=_load_staged,
        on_accept=_on_accept,
        on_reject=_on_reject,
    )
)

__all__ = ["INBOX_PROFILE", "PROFILE_NAME", "PatientDocDedupCheck"]
