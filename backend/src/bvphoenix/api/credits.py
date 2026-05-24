"""Credit wallet read endpoint + admin top-up (F7.3).

The user-facing surface is read-only for now: there is no self-serve
top-up until the payment integration lands (F9 Stripe Connect is
explicitly out of scope in this phase of the roadmap). Admins can
credit a wallet manually via the admin endpoint below for demo /
onboarding / refund purposes.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.auth import require_admin, require_user
from bvphoenix.db.models import User
from bvphoenix.db.session import get_db
from bvphoenix.middleware.audit_dependency import AuditDep
from bvphoenix.services.credits import (
    get_balance_cents,
    topup,
)

router = APIRouter(tags=["credits"])


class WalletBalanceOut(BaseModel):
    balance_cents: int
    # Repeated as USD for display convenience — the authoritative
    # value is always the cents integer.
    balance_usd: float


class AdminTopupIn(BaseModel):
    user_subject_id: uuid.UUID
    amount_cents: int = Field(gt=0, description="Positive amount to credit, in cents.")
    idempotency_key: str = Field(
        min_length=4,
        max_length=255,
        description=(
            "Caller-supplied key. Re-posting the same key is a safe no-op, so "
            "a network retry does not double-credit."
        ),
    )
    reason: str | None = Field(
        default=None,
        max_length=255,
        description="Free-text note for the ledger audit trail.",
    )


class AdminTopupOut(BaseModel):
    ledger_row_id: str
    balance_after_cents: int


@router.get("/me/credits", response_model=WalletBalanceOut)
async def my_balance(
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> WalletBalanceOut:
    """Return the caller's current wallet balance."""
    balance = await get_balance_cents(db, user_subject_id=user.subject_id)
    return WalletBalanceOut(
        balance_cents=balance,
        balance_usd=round(balance / 100.0, 4),
    )


@router.post(
    "/admin/credits/topup",
    response_model=AdminTopupOut,
    status_code=status.HTTP_201_CREATED,
)
async def admin_topup(
    body: AdminTopupIn,
    _admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
    audit: AuditDep,
) -> AdminTopupOut:
    """Admin-only wallet credit.

    Exists to support demo onboarding, refunds outside the Stripe
    flow (F9), and operational top-ups. The idempotency key keeps a
    retrying ticketing system from double-crediting.
    """
    try:
        movement = await topup(
            db,
            user_subject_id=body.user_subject_id,
            amount_cents=body.amount_cents,
            idempotency_key=body.idempotency_key,
            notes={"source": "admin", "reason": body.reason},
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    await db.commit()
    await audit.log(
        action="credits.admin_topup",
        actor_subject_id=_admin.subject_id,
        resource_kind="credit_ledger",
        resource_id=movement.row_id,
        metadata={
            "target_user": str(body.user_subject_id),
            "amount_cents": body.amount_cents,
            "balance_after_cents": movement.balance_after_cents,
        },
    )
    return AdminTopupOut(
        ledger_row_id=str(movement.row_id),
        balance_after_cents=movement.balance_after_cents,
    )
