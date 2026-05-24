"""Data Use Committee workflow (F10.3).

High-level contract:

* ``submit_request(db, *, license_id, submitted_by, summary)`` —
  opens a review on a training license. Flips the license to
  ``pending_duc``. Refuses if an open request already exists for
  that licence (partial unique index in the schema also enforces
  this at the DB level).
* ``record_vote(db, *, request_id, member_id, decision, rationale)``
  — inserts or replaces the vote row for (request, member). After
  the write the request is re-evaluated via ``try_close``.
* ``try_close(db, request)`` — counts active members and votes and
  transitions the request to ``approved`` / ``rejected`` once a
  majority of the active committee has voted. ``abstain`` counts
  toward the quorum but does not sway the outcome.
* ``active_member_count(db)`` — cardinality of the current
  committee; used for the majority math.

The decision rule is deliberately simple:

* quorum = ⌈ active_members / 2 ⌉ + 1  — strict majority.
* ``approve`` ≥ quorum → approved.
* ``reject`` ≥ quorum → rejected.
* otherwise the request stays pending until more members vote.

External-advisor roles vote the same weight as regular members.
Weighted voting is a tightening we can add later without touching
the schema — the ``role`` column is already there.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.db.models import (
    DUCMember,
    DUCRequest,
    DUCVote,
    TrainingLicense,
)

logger = logging.getLogger(__name__)


class DUCError(RuntimeError):
    pass


async def active_member_count(db: AsyncSession) -> int:
    rows = (
        (await db.execute(select(DUCMember).where(DUCMember.revoked_at.is_(None)))).scalars().all()
    )
    return len(rows)


def _quorum(active_members: int) -> int:
    """Strict majority: more than half of active members must agree."""
    if active_members <= 0:
        return 1
    return active_members // 2 + 1


async def submit_request(
    db: AsyncSession,
    *,
    license_id: uuid.UUID,
    submitted_by: uuid.UUID | None,
    summary: str,
) -> DUCRequest:
    """Open a review for ``license_id``. Refuses if one already is
    pending (the partial unique index enforces this regardless)."""
    license_ = (
        await db.execute(select(TrainingLicense).where(TrainingLicense.id == license_id))
    ).scalar_one_or_none()
    if license_ is None:
        raise DUCError(f"training license {license_id} does not exist")

    existing = (
        await db.execute(
            select(DUCRequest).where(
                DUCRequest.license_id == license_id,
                DUCRequest.status == "pending",
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise DUCError(f"a DUC request is already pending on license {license_id}")

    request = DUCRequest(
        license_id=license_id,
        submitted_by_subject_id=submitted_by,
        summary=summary,
    )
    db.add(request)
    # Keep the license status in sync so a reader of the license row
    # alone can tell the deal is under review.
    license_.status = "pending_duc"
    license_.duc_request_id = request.id
    await db.flush()
    return request


async def record_vote(
    db: AsyncSession,
    *,
    request_id: uuid.UUID,
    member_id: uuid.UUID,
    decision: str,
    rationale: str | None,
) -> DUCVote:
    if decision not in ("approve", "reject", "abstain"):
        raise ValueError("decision must be approve / reject / abstain")

    request = (
        await db.execute(select(DUCRequest).where(DUCRequest.id == request_id))
    ).scalar_one_or_none()
    if request is None:
        raise DUCError(f"DUC request {request_id} not found")
    if request.status != "pending":
        raise DUCError(f"request is already {request.status}; cannot vote")

    member = (
        await db.execute(select(DUCMember).where(DUCMember.id == member_id))
    ).scalar_one_or_none()
    if member is None or member.revoked_at is not None:
        raise DUCError(f"DUC member {member_id} is not active")

    # Upsert: an existing vote is replaced with the new decision, so a
    # member can revise before the request closes.
    existing = (
        await db.execute(
            select(DUCVote).where(
                DUCVote.request_id == request_id,
                DUCVote.member_id == member_id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.decision = decision
        existing.rationale = rationale
        vote = existing
    else:
        vote = DUCVote(
            request_id=request_id,
            member_id=member_id,
            decision=decision,
            rationale=rationale,
        )
        db.add(vote)
    await db.flush()

    await try_close(db, request)
    return vote


async def try_close(db: AsyncSession, request: DUCRequest) -> str:
    """If a majority has been reached, close the request and push
    the linked license into the matching state. Returns the (possibly
    unchanged) status."""
    if request.status != "pending":
        return request.status

    active = await active_member_count(db)
    quorum = _quorum(active)

    votes: Sequence[DUCVote] = (
        (await db.execute(select(DUCVote).where(DUCVote.request_id == request.id))).scalars().all()
    )
    approves = sum(1 for v in votes if v.decision == "approve")
    rejects = sum(1 for v in votes if v.decision == "reject")

    if approves >= quorum:
        new_status = "approved"
        license_new_status = "approved"
    elif rejects >= quorum:
        new_status = "rejected"
        license_new_status = "draft"  # back to draft so it can be revised
    else:
        return request.status

    request.status = new_status
    request.closed_at = datetime.now(UTC)

    await db.execute(
        update(TrainingLicense)
        .where(TrainingLicense.id == request.license_id)
        .values(status=license_new_status)
    )
    await db.flush()
    return new_status


__all__ = [
    "DUCError",
    "active_member_count",
    "record_vote",
    "submit_request",
    "try_close",
]
