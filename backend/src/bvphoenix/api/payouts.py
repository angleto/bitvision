"""Contributor payout REST surface (F10.4).

Two endpoints:

* ``GET  /api/me/payouts`` — authenticated caller sees their own
  payout rows (all licences, all statuses), most recent first.
* ``POST /api/admin/licenses/{id}/assemble-payouts`` — admin-only
  trigger to assemble the 50/50 split after a licence has been
  signed. Self-service payouts lands when F9 payment integration
  is wired.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.auth import require_admin, require_user
from bvphoenix.db.models import User
from bvphoenix.db.session import get_db
from bvphoenix.middleware.audit_dependency import AuditDep
from bvphoenix.services.contributor_payouts import (
    PayoutAssemblyError,
    assemble_payouts,
    list_user_datasets,
    list_user_payouts,
)

router = APIRouter(tags=["payouts"])


class PayoutOut(BaseModel):
    id: str
    license_id: str
    amount_cents: int
    bytes_contributed: int
    status: str
    paid_at: str | None
    created_at: str


class AssemblySummaryOut(BaseModel):
    license_id: str
    pool_cents: int
    contributor_count: int
    total_bytes: int
    payouts_created: int
    platform_remainder_cents: int


@router.get("/me/payouts", response_model=list[PayoutOut])
async def my_payouts(
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[PayoutOut]:
    rows = await list_user_payouts(db, user_subject_id=user.subject_id)
    return [
        PayoutOut(
            id=str(r.id),
            license_id=str(r.license_id),
            amount_cents=int(r.amount_cents),
            bytes_contributed=int(r.bytes_contributed),
            status=r.status,
            paid_at=r.paid_at.isoformat() if r.paid_at else None,
            created_at=r.created_at.isoformat(),
        )
        for r in rows
    ]


class MyDatasetOut(BaseModel):
    dataset_id: str
    status: str
    my_study_count: int
    study_count: int
    contributor_count: int
    tiers: list[str]
    created_at: str


@router.get("/me/datasets", response_model=list[MyDatasetOut])
async def my_datasets(
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[MyDatasetOut]:
    """Datasets that bundled the caller's studies — the data-sovereignty view.

    Aggregate + storage-isolated: dataset id (not study / patient ids),
    status (open / frozen / stale), the tiers you contributed at, how many of
    your studies are in each, and the dataset totals. No bucket / key /
    manifest location / study id is ever exposed."""
    rows = await list_user_datasets(db, user_subject_id=user.subject_id)
    return [
        MyDatasetOut(
            dataset_id=str(r.dataset_id),
            status=r.status,
            my_study_count=r.my_study_count,
            study_count=r.study_count,
            contributor_count=r.contributor_count,
            tiers=r.tiers,
            created_at=r.created_at.isoformat(),
        )
        for r in rows
    ]


@router.post(
    "/admin/licenses/{license_id}/assemble-payouts",
    response_model=AssemblySummaryOut,
    status_code=201,
)
async def admin_assemble_payouts(
    license_id: uuid.UUID,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
    audit: AuditDep,
) -> AssemblySummaryOut:
    try:
        summary = await assemble_payouts(db, license_id=license_id)
    except PayoutAssemblyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await db.commit()
    await audit.log(
        action="payouts.assembled",
        actor_subject_id=admin.subject_id,
        resource_kind="training_license",
        resource_id=license_id,
        metadata={
            "pool_cents": summary.pool_cents,
            "contributor_count": summary.contributor_count,
            "payouts_created": summary.payouts_created,
            "platform_remainder_cents": summary.platform_remainder_cents,
        },
    )
    return AssemblySummaryOut(
        license_id=str(summary.license_id),
        pool_cents=summary.pool_cents,
        contributor_count=summary.contributor_count,
        total_bytes=summary.total_bytes,
        payouts_created=summary.payouts_created,
        platform_remainder_cents=summary.platform_remainder_cents,
    )
