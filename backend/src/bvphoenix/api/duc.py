"""DUC REST surface (F10.3) — admin-only endpoints.

* ``POST   /api/admin/duc/requests``             — open a review.
* ``GET    /api/admin/duc/requests``             — list (optionally filtered).
* ``POST   /api/admin/duc/requests/{id}/vote``   — cast / revise a vote.

Non-admin callers get 403 via ``require_admin``. Committee
membership management (``duc_members``) is out of scope for this
slice — today the rows are seeded by operators directly in the DB;
a full CRUD API lands when the governance page (a future F11.x
task) exposes it.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.auth import require_admin
from bvphoenix.db.models import DUCMember, DUCRequest, LicensedDataset, TrainingLicense, User
from bvphoenix.db.session import get_db
from bvphoenix.middleware.audit_dependency import AuditDep
from bvphoenix.services.duc import DUCError, record_vote, submit_request
from bvphoenix.services.training_licenses import (
    TrainingLicenseError,
    sign_license,
)

router = APIRouter(prefix="/admin/duc", tags=["duc"])


class SubmitRequestIn(BaseModel):
    license_id: uuid.UUID
    summary: str = Field(
        min_length=20,
        max_length=8000,
        description=(
            "Plain-text rationale the committee reads before voting. "
            "Aim for scope + consent profile + anonymisation strategy."
        ),
    )


class VoteIn(BaseModel):
    decision: str = Field(description="approve / reject / abstain")
    rationale: str | None = Field(default=None, max_length=4000)


class RequestOut(BaseModel):
    id: str
    license_id: str
    status: str
    summary: str
    submitted_at: str
    closed_at: str | None


def _to_out(row: DUCRequest) -> RequestOut:
    return RequestOut(
        id=str(row.id),
        license_id=str(row.license_id),
        status=row.status,
        summary=row.summary,
        submitted_at=row.submitted_at.isoformat(),
        closed_at=row.closed_at.isoformat() if row.closed_at else None,
    )


@router.post("/requests", response_model=RequestOut, status_code=201)
async def open_request(
    body: SubmitRequestIn,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
    audit: AuditDep,
) -> RequestOut:
    try:
        req = await submit_request(
            db,
            license_id=body.license_id,
            submitted_by=admin.subject_id,
            summary=body.summary,
        )
    except DUCError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await db.commit()
    await audit.log(
        action="duc.request_opened",
        actor_subject_id=admin.subject_id,
        resource_kind="duc_request",
        resource_id=req.id,
        metadata={"license_id": str(body.license_id)},
    )
    return _to_out(req)


@router.get("/requests", response_model=list[RequestOut])
async def list_requests(
    _admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
    status: str | None = Query(
        default=None,
        description="Filter by status (pending by default gives the review queue).",
    ),
) -> list[RequestOut]:
    q = select(DUCRequest).order_by(DUCRequest.submitted_at.desc())
    if status:
        q = q.where(DUCRequest.status == status)
    rows = (await db.execute(q)).scalars().all()
    return [_to_out(r) for r in rows]


@router.post("/requests/{request_id}/vote", response_model=RequestOut)
async def cast_vote(
    request_id: uuid.UUID,
    body: VoteIn,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
    audit: AuditDep,
) -> RequestOut:
    member = (
        await db.execute(
            select(DUCMember).where(
                DUCMember.user_subject_id == admin.subject_id,
                DUCMember.revoked_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if member is None:
        raise HTTPException(
            status_code=403,
            detail=("only active DUC members can vote; add yourself to duc_members and retry."),
        )

    try:
        await record_vote(
            db,
            request_id=request_id,
            member_id=member.id,
            decision=body.decision,
            rationale=body.rationale,
        )
    except (DUCError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await db.commit()

    req = (await db.execute(select(DUCRequest).where(DUCRequest.id == request_id))).scalar_one()
    await audit.log(
        action="duc.vote_cast",
        actor_subject_id=admin.subject_id,
        resource_kind="duc_request",
        resource_id=request_id,
        metadata={"decision": body.decision, "status_after": req.status},
    )
    return _to_out(req)


# --- training-license signing ---------------------------------------------
#
# The endpoint lives here rather than in a dedicated router because the
# DUC-approval gate is the whole point of the action: signing is the
# moment the DUC veto either holds or is bypassed. Keeping
# it adjacent to the review workflow keeps the policy surface visible.


class LicenseOut(BaseModel):
    id: str
    licensee_name: str
    status: str
    duc_request_id: str | None
    dataset_id: str | None
    signed_at: str | None


def _license_out(row: TrainingLicense) -> LicenseOut:
    return LicenseOut(
        id=str(row.id),
        licensee_name=row.licensee_name,
        status=row.status,
        duc_request_id=(str(row.duc_request_id) if row.duc_request_id else None),
        dataset_id=(str(row.dataset_id) if row.dataset_id else None),
        signed_at=row.signed_at.isoformat() if row.signed_at else None,
    )


class LicenseCreateIn(BaseModel):
    licensee_name: str = Field(min_length=1, max_length=255)
    licensee_email: str = Field(min_length=3, max_length=320)
    price_usd_cents: int = Field(ge=0)
    term_months: int = Field(default=12, gt=0)
    dataset_id: uuid.UUID | None = Field(
        default=None,
        description="The open (or already-frozen) dataset this deal grants.",
    )


@router.post(
    "/licenses",
    response_model=LicenseOut,
    status_code=201,
    summary="Create a draft training license, optionally bound to a dataset",
)
async def create_training_license(
    body: LicenseCreateIn,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
    audit: AuditDep,
) -> LicenseOut:
    """Create a ``draft`` training license, optionally bound to a Dataset via
    ``dataset_id`` — the frozen cohort this deal will grant. Signing later
    freezes an ``open`` dataset; a ``stale`` dataset (a contributor revoked
    consent) cannot be licensed and is refused here with 409."""
    if body.dataset_id is not None:
        dataset = (
            await db.execute(select(LicensedDataset).where(LicensedDataset.id == body.dataset_id))
        ).scalar_one_or_none()
        if dataset is None:
            raise HTTPException(status_code=404, detail="dataset not found")
        if dataset.status == "stale":
            raise HTTPException(
                status_code=409,
                detail=(
                    "dataset is stale (a contributor revoked consent); rebuild it before licensing"
                ),
            )
    lic = TrainingLicense(
        licensee_name=body.licensee_name,
        licensee_email=body.licensee_email,
        price_usd_cents=body.price_usd_cents,
        term_months=body.term_months,
        dataset_id=body.dataset_id,
    )
    db.add(lic)
    await db.flush()
    await db.commit()
    await audit.log(
        action="training_license.created",
        actor_subject_id=admin.subject_id,
        resource_kind="training_license",
        resource_id=lic.id,
        metadata={"dataset_id": str(body.dataset_id) if body.dataset_id else None},
    )
    return _license_out(lic)


@router.post(
    "/licenses/{license_id}/sign",
    response_model=LicenseOut,
    summary="Sign a DUC-approved training license",
)
async def sign_training_license(
    license_id: uuid.UUID,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
    audit: AuditDep,
) -> LicenseOut:
    """Flip a training license from ``approved`` to ``signed``.

    Refuses with 409 when the linked :class:`DUCRequest` is not itself
    ``approved`` (or missing entirely). The Data Use Committee has a
    veto on every license deal, so this endpoint is the single place
    backend code is allowed to mint a signed license.
    """
    try:
        row = await sign_license(db, license_id=license_id)
    except TrainingLicenseError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await db.commit()

    await audit.log(
        action="training_license.signed",
        actor_subject_id=admin.subject_id,
        resource_kind="training_license",
        resource_id=row.id,
        metadata={
            "duc_request_id": str(row.duc_request_id) if row.duc_request_id else None,
        },
    )
    return _license_out(row)
