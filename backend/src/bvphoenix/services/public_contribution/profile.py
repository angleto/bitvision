"""The ``public_contribution`` review profile — the engine binding.

Importing this module registers the profile (idempotently). Static import sites:
``api/contributions.py`` (the review endpoints) and the worker tasks; the generic
``run_review_checks`` arq task reaches it through
``BVP_REVIEW_PROFILE_MODULES=...,bvphoenix.services.public_contribution.profile``.

Decision policy: gate ``human_only`` + ``require_reason`` — publishing
PHI-bearing imaging to the public web is irreversible and not an agent-delegable
act. ``can_decide`` is the RBAC floor: admin-gated today (publishing to the
public library is platform-level, not a per-patient grant).
"""

from __future__ import annotations

import asyncio
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.config import get_settings
from bvphoenix.db.models import Submission, User
from bvphoenix.services.public_contribution.checks import (
    CsamScreenCheck,
    HeaderDeidCheck,
    PixelPhiCheck,
)
from bvphoenix.services.public_contribution.promotion import (
    promote_submission,
    purge_submission_staged,
)
from bvphoenix.services.review_queue import (
    DecisionPolicy,
    ReviewProfile,
    StagedComponent,
    StagedItem,
    register_profile,
)
from bvphoenix.services.review_queue.actor import ReviewActor
from bvphoenix.services.review_queue.engine import ReviewableItem
from bvphoenix.services.review_queue.plugins import (
    ArchiveGuardCheck,
    ClamAVCheck,
    DicomRouteCheck,
    MagicAllowlistCheck,
)
from bvphoenix.storage import get_s3_storage

PROFILE_NAME = "public_contribution"


async def _load_item(db: AsyncSession, item_id: uuid.UUID) -> ReviewableItem | None:
    return (
        await db.execute(select(Submission).where(Submission.id == item_id))
    ).scalar_one_or_none()


async def _load_staged(db: AsyncSession, item: ReviewableItem) -> StagedItem:
    assert isinstance(item, Submission)
    settings = get_settings()
    storage = get_s3_storage()
    manifest = dict(item.manifest or {})

    def _reader(bucket: str, key: str):
        async def _read() -> bytes:
            return await asyncio.to_thread(storage.get_object_bytes, bucket=bucket, key=key)

        return _read

    components = [
        StagedComponent(
            name=str(inst.get("name") or inst.get("instance_id") or inst["s3_key"]),
            size_bytes=int(inst.get("size_bytes") or 0),
            content_type="application/dicom",
            read=_reader(inst.get("s3_bucket") or settings.s3_bucket_raw, inst["s3_key"]),
        )
        for inst in manifest.get("instances", [])
        if inst.get("s3_key")
    ]
    return StagedItem(item_id=item.id, components=components, manifest=manifest)


async def _can_decide(db: AsyncSession, actor: ReviewActor, item: ReviewableItem) -> bool:
    # human-only is enforced by the DecisionPolicy gate before this runs; this
    # is the RBAC floor. Publishing to the public library is admin-gated today
    # (a dedicated reviewer role can replace is_admin later).
    if not actor.is_human:
        return False
    user = (
        await db.execute(select(User).where(User.subject_id == actor.subject_id))
    ).scalar_one_or_none()
    return bool(user and getattr(user, "is_admin", False))


async def _on_accept(db: AsyncSession, item: ReviewableItem, actor: ReviewActor):
    assert isinstance(item, Submission)
    return await promote_submission(db, item=item, actor=actor)


async def _on_reject(
    db: AsyncSession, item: ReviewableItem, actor: ReviewActor, reason: str | None
) -> None:
    assert isinstance(item, Submission)
    await purge_submission_staged(item)


PUBLIC_CONTRIBUTION_PROFILE = register_profile(
    ReviewProfile(
        name=PROFILE_NAME,
        provenance_target_kind="submission",
        checks=(
            # Cheap structural gates first, content scan, then de-id / pixel /
            # CSAM signals. A ``block`` from any flips the item to ``blocked``.
            ArchiveGuardCheck(),
            MagicAllowlistCheck(),
            ClamAVCheck(),
            DicomRouteCheck(),
            HeaderDeidCheck(),
            PixelPhiCheck(),
            CsamScreenCheck(),
        ),
        decision=DecisionPolicy(
            gate="human_only",
            require_reason=True,
            can_decide=_can_decide,
        ),
        load_item=_load_item,
        load_staged=_load_staged,
        on_accept=_on_accept,
        on_reject=_on_reject,
    )
)

__all__ = ["PROFILE_NAME", "PUBLIC_CONTRIBUTION_PROFILE"]
