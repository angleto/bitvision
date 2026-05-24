"""Wallet sponsorship endpoints.

Two collections live under ``/api/me/sponsorships``:

* ``emitted``: rows where the caller is the sponsor (their wallet pays).
* ``received``: rows where the caller is the sponsored (somebody else's
  wallet pays for the caller's spend).

Only the sponsor may create / modify / revoke a sponsorship. The
``GET /defaults`` endpoint exposes the workspace fallbacks so the UI
can pre-fill the cap input.

Authorization is positional: the FastAPI dependency loads the current
``User`` via ``require_user``; the service layer compares
``sponsor_subject_id`` against ``user.subject_id`` for every mutation.
There is no admin override — sponsoring is always self-service.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.auth import require_user
from bvphoenix.db.models import User, WalletSponsorship
from bvphoenix.db.session import get_db
from bvphoenix.middleware.audit_dependency import AuditDep
from bvphoenix.services import sponsorship as sponsorship_svc

router = APIRouter(tags=["sponsorships"], prefix="/me/sponsorships")


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class SponsorshipDefaultsOut(BaseModel):
    default_cap_cents: int
    ceiling_cents: int | None
    scope_kinds: list[str]


class SponsorshipIn(BaseModel):
    sponsored_subject_id: uuid.UUID
    scope_kind: str = Field(description="One of: patient, consultation, organization, global")
    scope_id: uuid.UUID | None = Field(
        default=None,
        description="UUID of the scope resource. Required unless scope_kind=='global'.",
    )
    cap_cents: int | None = Field(
        default=None,
        gt=0,
        description="Total cap (cents). Defaults to the workspace default when omitted.",
    )
    valid_until: datetime | None = None
    purpose: str | None = Field(default=None, max_length=255)


class SponsorshipPatchIn(BaseModel):
    cap_cents: int = Field(gt=0)


class SponsorshipOut(BaseModel):
    id: uuid.UUID
    sponsor_subject_id: uuid.UUID
    sponsored_subject_id: uuid.UUID
    scope_kind: str
    scope_id: uuid.UUID | None
    cap_cents: int
    spent_cents: int
    remaining_cents: int
    valid_from: datetime
    valid_until: datetime | None
    revoked_at: datetime | None
    purpose: str | None
    created_at: datetime

    @classmethod
    def from_row(cls, row: WalletSponsorship) -> SponsorshipOut:
        return cls(
            id=row.id,
            sponsor_subject_id=row.sponsor_subject_id,
            sponsored_subject_id=row.sponsored_subject_id,
            scope_kind=row.scope_kind,
            scope_id=row.scope_id,
            cap_cents=int(row.cap_cents),
            spent_cents=int(row.spent_cents),
            remaining_cents=max(0, int(row.cap_cents) - int(row.spent_cents)),
            valid_from=row.valid_from,
            valid_until=row.valid_until,
            revoked_at=row.revoked_at,
            purpose=row.purpose,
            created_at=row.created_at,
        )


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------


@router.get("/defaults", response_model=SponsorshipDefaultsOut)
async def read_defaults(
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SponsorshipDefaultsOut:
    default_cap = await sponsorship_svc.get_default_cap_cents(db)
    ceiling = await sponsorship_svc.get_cap_ceiling_cents(db)
    return SponsorshipDefaultsOut(
        default_cap_cents=default_cap,
        ceiling_cents=ceiling,
        scope_kinds=list(sponsorship_svc.SCOPE_KINDS),
    )


@router.get("/emitted", response_model=list[SponsorshipOut])
async def list_emitted(
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    include_revoked: bool = False,
) -> list[SponsorshipOut]:
    rows = await sponsorship_svc.list_emitted(
        db,
        sponsor_subject_id=user.subject_id,
        include_revoked=include_revoked,
    )
    return [SponsorshipOut.from_row(r) for r in rows]


@router.get("/received", response_model=list[SponsorshipOut])
async def list_received(
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    include_revoked: bool = False,
) -> list[SponsorshipOut]:
    rows = await sponsorship_svc.list_received(
        db,
        sponsored_subject_id=user.subject_id,
        include_revoked=include_revoked,
    )
    return [SponsorshipOut.from_row(r) for r in rows]


# ---------------------------------------------------------------------------
# Mutate
# ---------------------------------------------------------------------------


@router.post("", response_model=SponsorshipOut, status_code=status.HTTP_201_CREATED)
async def create_sponsorship(
    body: SponsorshipIn,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    _audit: AuditDep,
) -> SponsorshipOut:
    if body.sponsored_subject_id == user.subject_id:
        raise HTTPException(
            status_code=400,
            detail={"code": "self_sponsorship_forbidden", "message": "cannot sponsor yourself"},
        )
    try:
        row = await sponsorship_svc.create_sponsorship(
            db,
            sponsor_subject_id=user.subject_id,
            sponsored_subject_id=body.sponsored_subject_id,
            scope_kind=body.scope_kind,
            scope_id=body.scope_id,
            cap_cents=body.cap_cents,
            valid_until=body.valid_until,
            purpose=body.purpose,
        )
    except sponsorship_svc.CapCeilingError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "cap_exceeds_ceiling",
                "requested_cents": exc.requested_cents,
                "ceiling_cents": exc.ceiling_cents,
            },
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail={"code": "invalid", "message": str(exc)}
        ) from exc
    return SponsorshipOut.from_row(row)


@router.patch("/{sponsorship_id}", response_model=SponsorshipOut)
async def patch_sponsorship(
    sponsorship_id: uuid.UUID,
    body: SponsorshipPatchIn,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    _audit: AuditDep,
) -> SponsorshipOut:
    try:
        row = await sponsorship_svc.update_cap(
            db,
            sponsorship_id=sponsorship_id,
            actor_subject_id=user.subject_id,
            new_cap_cents=body.cap_cents,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail={"code": "not_found"}) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail={"code": "not_sponsor"}) from exc
    except sponsorship_svc.CapCeilingError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "cap_exceeds_ceiling",
                "requested_cents": exc.requested_cents,
                "ceiling_cents": exc.ceiling_cents,
            },
        ) from exc
    except sponsorship_svc.SponsorshipError as exc:
        raise HTTPException(
            status_code=409, detail={"code": "invalid_state", "message": str(exc)}
        ) from exc
    return SponsorshipOut.from_row(row)


@router.delete("/{sponsorship_id}", response_model=SponsorshipOut)
async def revoke_sponsorship(
    sponsorship_id: uuid.UUID,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    _audit: AuditDep,
) -> SponsorshipOut:
    try:
        row = await sponsorship_svc.revoke_sponsorship(
            db,
            sponsorship_id=sponsorship_id,
            actor_subject_id=user.subject_id,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail={"code": "not_found"}) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail={"code": "not_sponsor"}) from exc
    return SponsorshipOut.from_row(row)
