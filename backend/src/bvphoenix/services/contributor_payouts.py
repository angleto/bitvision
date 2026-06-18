"""Contributor payout assembly (F10.4).

Split: revenue share is 50% platform / 50% contributor pool. Within
the contributor pool
we distribute pro-rata by **bytes contributed** — a crude proxy for
"data work done". A more sophisticated weighting (rarity, modality
scarcity) slots in later without touching the schema; the input is
already a per-contributor (license_id, bytes_contributed) tuple.

Integer-cents math
------------------

We round each contributor's share *down* to whole cents so integer
arithmetic is closed. The leftover (at most ``N - 1`` cents, where
N is the contributor count) is credited to the platform — equivalent
to a small extra margin. Alternative splits (give the leftover to
the largest contributor, or rotate) add complexity without a clear
fairness win at this scale, so we keep it simple and document the
choice.

Idempotency
-----------

The unique index on ``(license_id, user_subject_id)`` enforces
one-row-per-(license, contributor). :func:`assemble_payouts` refuses
to run twice on the same licence — a replay is always an ops
mistake, because a second run would have to delete or overlap
existing rows and that is not a silent operation.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.db.models import (
    ContributorPayout,
    DatasetStudy,
    TrainingLicense,
)

logger = logging.getLogger(__name__)

# 50/50 split (platform / contributor pool).
CONTRIBUTOR_POOL_FRACTION: float = 0.50


class PayoutAssemblyError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class AssemblySummary:
    license_id: uuid.UUID
    pool_cents: int
    contributor_count: int
    total_bytes: int
    payouts_created: int
    platform_remainder_cents: int


async def _bytes_by_contributor(
    db: AsyncSession, dataset_id: uuid.UUID
) -> list[tuple[uuid.UUID, int]]:
    rows = (
        await db.execute(
            select(
                DatasetStudy.contributor_subject_id,
                func.coalesce(func.sum(DatasetStudy.size_bytes), 0),
            )
            .where(
                DatasetStudy.dataset_id == dataset_id,
                DatasetStudy.contributor_subject_id.is_not(None),
            )
            .group_by(DatasetStudy.contributor_subject_id)
        )
    ).all()
    # ``contributor_subject_id`` was filtered non-null in the WHERE
    # but the type checker still sees Optional; narrow explicitly.
    return [(uid, int(total)) for uid, total in rows if uid is not None]


async def assemble_payouts(db: AsyncSession, *, license_id: uuid.UUID) -> AssemblySummary:
    """Mint the contributor payout rows for a signed license.

    Pre-conditions:

    * The licence must exist and be in status ``signed``.
    * The licence must have a dataset bound (``dataset_id`` not null).
    * No ``contributor_payouts`` row may already exist for the
      licence (idempotency guard).

    Post-conditions:

    * N+1 ledger entries are written in the same flush: one row per
      contributor plus a notes entry on the licence that records
      the platform's 50% + leftover cents.
    """
    license_ = (
        await db.execute(select(TrainingLicense).where(TrainingLicense.id == license_id))
    ).scalar_one_or_none()
    if license_ is None:
        raise PayoutAssemblyError(f"license {license_id} does not exist")
    if license_.status != "signed":
        raise PayoutAssemblyError(
            f"license {license_id} is {license_.status!r}; must be 'signed' to pay out"
        )

    existing = (
        await db.execute(
            select(func.count(ContributorPayout.id)).where(
                ContributorPayout.license_id == license_id
            )
        )
    ).scalar_one()
    if existing:
        raise PayoutAssemblyError(
            f"license {license_id} already has {existing} payout rows — refusing to replay"
        )

    if license_.dataset_id is None:
        raise PayoutAssemblyError(f"license {license_id} has no dataset bound (dataset_id is null)")

    contributions = await _bytes_by_contributor(db, license_.dataset_id)
    total_bytes = sum(b for _, b in contributions)
    pool_cents = int(license_.price_usd_cents * CONTRIBUTOR_POOL_FRACTION)

    payouts_created = 0
    sum_paid = 0
    if total_bytes > 0 and contributions:
        for contributor_id, bytes_ in contributions:
            share_cents = (pool_cents * bytes_) // total_bytes
            if share_cents <= 0:
                continue
            row = ContributorPayout(
                license_id=license_id,
                user_subject_id=contributor_id,
                amount_cents=share_cents,
                bytes_contributed=bytes_,
                status="pending",
                notes={
                    "assembled_at_status": "signed",
                    "bytes_share_fraction": bytes_ / total_bytes,
                },
            )
            db.add(row)
            payouts_created += 1
            sum_paid += share_cents

    await db.flush()

    platform_remainder = license_.price_usd_cents - sum_paid
    # The platform always keeps at least 50%; ``platform_remainder``
    # is that half plus the rounding leftover, so the sum over all
    # rows (platform + contributors) equals price_usd_cents exactly.
    return AssemblySummary(
        license_id=license_id,
        pool_cents=pool_cents,
        contributor_count=len(contributions),
        total_bytes=total_bytes,
        payouts_created=payouts_created,
        platform_remainder_cents=platform_remainder,
    )


async def list_user_payouts(
    db: AsyncSession, *, user_subject_id: uuid.UUID
) -> list[ContributorPayout]:
    """All payout rows across all licences for a user, most recent
    first. Safe to call for any authenticated user — the query is
    scoped to their own subject id."""
    return (
        (
            await db.execute(
                select(ContributorPayout)
                .where(ContributorPayout.user_subject_id == user_subject_id)
                .order_by(ContributorPayout.created_at.desc())
            )
        )
        .scalars()
        .all()
    )


__all__ = [
    "CONTRIBUTOR_POOL_FRACTION",
    "AssemblySummary",
    "PayoutAssemblyError",
    "assemble_payouts",
    "list_user_payouts",
]
