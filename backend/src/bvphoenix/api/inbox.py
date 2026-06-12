"""Patient inbox API — addresses, trusted senders, review queue.

Every route lives under ``/patients/{patient_id}/inbox/*`` and is gated
by ``REVIEW_INBOX`` on the patient (owner/self by construction, never
part of share-link grants). Share-link sessions are additionally
refused outright: a delegated viewer must not control what enters the
record, whatever permission list their grant carries.

Concurrency: every mutation requires ``If-Match`` (the 428/412
contract of clinical events / patient tasks); the engine bumps the
item etag on every transition so a reviewer holding a stale view
cannot decide over a state that moved.

Decisions run through the shared review engine
(``services/review_queue``) with the registered ``patient_inbox``
profile — importing :mod:`bvphoenix.services.inbox.profile` here IS
the registration. Accepting transitions the item and *enqueues* the
promotion (the actual ingest runs in the worker; multi-hundred-MB DICOM
lots have no business inside an HTTP handler).
"""

from __future__ import annotations

import logging
import uuid
from typing import Annotated, Any, Literal

from arq import create_pool
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.auth.deps import require_user
from bvphoenix.config import get_settings
from bvphoenix.db.models import (
    Folder,
    InboundEmail,
    InboxItem,
    InboxSenderAllowlist,
    User,
)
from bvphoenix.db.session import get_db
from bvphoenix.services.arq_redis import redis_settings
from bvphoenix.services.etag import enforce_if_match_value
from bvphoenix.services.inbox import addresses as addr_service
from bvphoenix.services.inbox.addresses import InboxAddressError
from bvphoenix.services.inbox.profile import INBOX_PROFILE
from bvphoenix.services.inbox.promotion import promotable_components
from bvphoenix.services.permissions import (
    REVIEW_INBOX,
    _share_scope,
    get_patient_or_404,
)
from bvphoenix.services.review_queue import ReviewDecisionError, ReviewTransitionError
from bvphoenix.services.review_queue import engine as review_engine
from bvphoenix.services.review_queue.actor import ReviewActor
from bvphoenix.services.storage_quota import check_storage_quota

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/patients/{patient_id}/inbox", tags=["inbox"])


# ---- shared gate -----------------------------------------------------


async def _gate(db: AsyncSession, *, patient_id: uuid.UUID, user: User, request: Request):
    """Patient + RBAC gate for every inbox route: 404-shaped access
    control via the shared layered check, plus the explicit share-link
    refusal (403 even when a grant somehow carries the permission)."""
    if _share_scope(user) is not None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="share-link sessions cannot access the inbox",
        )
    return await get_patient_or_404(
        db, patient_id=patient_id, user=user, request=request, action=REVIEW_INBOX
    )


# ---- schemas ---------------------------------------------------------


class InboxAddressOut(BaseModel):
    id: str
    address: str
    label: str | None
    active: bool
    created_at: str
    revoked_at: str | None
    etag: str

    @classmethod
    def from_row(cls, row) -> InboxAddressOut:
        return cls(
            id=str(row.id),
            address=addr_service.render_address(row),
            label=row.label,
            active=row.active,
            created_at=row.created_at.isoformat(),
            revoked_at=row.revoked_at.isoformat() if row.revoked_at else None,
            etag=str(row.etag),
        )


class CreateAddressIn(BaseModel):
    label: str | None = Field(default=None, max_length=200)


class SetLabelIn(BaseModel):
    label: str | None = Field(default=None, max_length=200)


class RevokeAddressIn(BaseModel):
    reason: str | None = Field(default=None, max_length=2000)


class AllowlistEntryOut(BaseModel):
    id: str
    sender_email: str
    require_alignment: bool
    active: bool
    etag: str


class AllowlistEntryIn(BaseModel):
    sender_email: str = Field(min_length=3, max_length=320)
    require_alignment: bool = True


class InboxItemOut(BaseModel):
    id: str
    patient_id: str
    source_channel: str
    status: str
    auto_verdict: str | None
    manifest: dict | None
    auto_checks: dict | None
    promoted_refs: dict | None
    review_note: str | None
    reviewed_at: str | None
    created_at: str
    etag: str
    email: dict | None = None

    @classmethod
    def from_row(cls, row: InboxItem, email: InboundEmail | None = None) -> InboxItemOut:
        return cls(
            id=str(row.id),
            patient_id=str(row.patient_id),
            source_channel=row.source_channel,
            status=row.status,
            auto_verdict=row.auto_verdict,
            manifest=row.manifest,
            auto_checks=row.auto_checks,
            promoted_refs=row.promoted_refs,
            review_note=row.review_note,
            reviewed_at=row.reviewed_at.isoformat() if row.reviewed_at else None,
            created_at=row.created_at.isoformat(),
            etag=str(row.etag),
            email=(
                {
                    "from": email.from_address,
                    "subject": email.subject,
                    "received_at": email.received_at.isoformat() if email.received_at else None,
                    "spf": email.spf_result,
                    "dkim": email.dkim_result,
                    "dmarc": email.dmarc_result,
                }
                if email is not None
                else None
            ),
        )


class AcceptItemIn(BaseModel):
    folder_id: uuid.UUID | None = None
    excluded_components: list[str] = Field(default_factory=list)
    include_body: bool = False
    note: str | None = Field(default=None, max_length=2000)


class RejectItemIn(BaseModel):
    reason: str | None = Field(default=None, max_length=2000)


class DecisionOut(BaseModel):
    item: InboxItemOut
    dry_run: bool = False
    would_promote: list[str] | None = None
    would_skip: list[dict] | None = None


# ---- addresses -------------------------------------------------------


@router.get("/addresses", response_model=list[InboxAddressOut])
async def list_addresses(
    patient_id: uuid.UUID,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
) -> list[InboxAddressOut]:
    await _gate(db, patient_id=patient_id, user=user, request=request)
    rows = await addr_service.list_addresses(db, patient_id=patient_id)
    return [InboxAddressOut.from_row(r) for r in rows]


@router.post("/addresses", response_model=InboxAddressOut, status_code=201)
async def create_address(
    patient_id: uuid.UUID,
    body: CreateAddressIn,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
) -> InboxAddressOut:
    patient = await _gate(db, patient_id=patient_id, user=user, request=request)
    actor = ReviewActor.from_request(user, request)
    try:
        row = await addr_service.create_address(db, patient=patient, actor=actor, label=body.label)
    except InboxAddressError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT
            if exc.code == "inbox.address_cap"
            else status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    await db.commit()
    return InboxAddressOut.from_row(row)


@router.patch("/addresses/{address_id}", response_model=InboxAddressOut)
async def set_address_label(
    patient_id: uuid.UUID,
    address_id: uuid.UUID,
    body: SetLabelIn,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> InboxAddressOut:
    await _gate(db, patient_id=patient_id, user=user, request=request)
    row = await addr_service.get_address(db, patient_id=patient_id, address_id=address_id)
    if row is None:
        raise HTTPException(status_code=404, detail="address not found")
    enforce_if_match_value(if_match, str(row.etag))
    actor = ReviewActor.from_request(user, request)
    await addr_service.set_label(db, address=row, actor=actor, label=body.label)
    await db.commit()
    return InboxAddressOut.from_row(row)


@router.post("/addresses/{address_id}/revoke", response_model=InboxAddressOut)
async def revoke_address(
    patient_id: uuid.UUID,
    address_id: uuid.UUID,
    body: RevokeAddressIn,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> InboxAddressOut:
    await _gate(db, patient_id=patient_id, user=user, request=request)
    row = await addr_service.get_address(db, patient_id=patient_id, address_id=address_id)
    if row is None:
        raise HTTPException(status_code=404, detail="address not found")
    enforce_if_match_value(if_match, str(row.etag))
    actor = ReviewActor.from_request(user, request)
    await addr_service.revoke_address(db, address=row, actor=actor, reason=body.reason)
    await db.commit()
    return InboxAddressOut.from_row(row)


# ---- sender allowlist ------------------------------------------------


@router.get("/trusted-senders", response_model=list[AllowlistEntryOut])
async def list_trusted_senders(
    patient_id: uuid.UUID,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
) -> list[AllowlistEntryOut]:
    await _gate(db, patient_id=patient_id, user=user, request=request)
    rows = (
        (
            await db.execute(
                select(InboxSenderAllowlist)
                .where(
                    InboxSenderAllowlist.patient_id == patient_id,
                    InboxSenderAllowlist.active.is_(True),
                )
                .order_by(InboxSenderAllowlist.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return [
        AllowlistEntryOut(
            id=str(r.id),
            sender_email=r.sender_email,
            require_alignment=r.require_alignment,
            active=r.active,
            etag=str(r.etag),
        )
        for r in rows
    ]


@router.post("/trusted-senders", response_model=AllowlistEntryOut, status_code=201)
async def add_trusted_sender(
    patient_id: uuid.UUID,
    body: AllowlistEntryIn,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
) -> AllowlistEntryOut:
    await _gate(db, patient_id=patient_id, user=user, request=request)
    sender = body.sender_email.strip().lower()
    if "@" not in sender:
        raise HTTPException(status_code=422, detail="sender_email must be an address")
    existing = (
        await db.execute(
            select(InboxSenderAllowlist).where(
                InboxSenderAllowlist.patient_id == patient_id,
                InboxSenderAllowlist.sender_email == sender,
                InboxSenderAllowlist.active.is_(True),
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=409, detail="sender already allowlisted")
    row = InboxSenderAllowlist(
        id=uuid.uuid4(),
        patient_id=patient_id,
        sender_email=sender,
        require_alignment=body.require_alignment,
        created_by_subject_id=user.subject_id,
    )
    db.add(row)
    await db.commit()
    return AllowlistEntryOut(
        id=str(row.id),
        sender_email=row.sender_email,
        require_alignment=row.require_alignment,
        active=row.active,
        etag=str(row.etag),
    )


@router.delete("/trusted-senders/{entry_id}", status_code=204)
async def remove_trusted_sender(
    patient_id: uuid.UUID,
    entry_id: uuid.UUID,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
) -> None:
    await _gate(db, patient_id=patient_id, user=user, request=request)
    row = (
        await db.execute(
            select(InboxSenderAllowlist).where(
                InboxSenderAllowlist.patient_id == patient_id,
                InboxSenderAllowlist.id == entry_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="entry not found")
    if row.active:
        from datetime import UTC, datetime

        row.active = False
        row.revoked_at = datetime.now(UTC)
        row.etag = uuid.uuid4()
        await db.commit()


# ---- review queue ----------------------------------------------------

_LISTABLE_STATUSES = Literal[
    "received",
    "processing",
    "needs_review",
    "blocked",
    "accepted",
    "promoting",
    "promoted",
    "rejected",
    "expired",
    "failed",
]


async def _load_item(db: AsyncSession, *, patient_id: uuid.UUID, item_id: uuid.UUID) -> InboxItem:
    row = (
        await db.execute(
            select(InboxItem).where(
                InboxItem.patient_id == patient_id,
                InboxItem.id == item_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="inbox item not found")
    return row


async def _email_for(db: AsyncSession, item: InboxItem) -> InboundEmail | None:
    if item.inbound_email_id is None:
        return None
    return await db.get(InboundEmail, item.inbound_email_id)


@router.get("/items", response_model=list[InboxItemOut])
async def list_items(
    patient_id: uuid.UUID,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    item_status: Annotated[_LISTABLE_STATUSES | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[InboxItemOut]:
    await _gate(db, patient_id=patient_id, user=user, request=request)
    stmt = (
        select(InboxItem)
        .where(InboxItem.patient_id == patient_id)
        .order_by(InboxItem.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    if item_status is not None:
        stmt = stmt.where(InboxItem.status == item_status)
    rows = (await db.execute(stmt)).scalars().all()
    out: list[InboxItemOut] = []
    for row in rows:
        out.append(InboxItemOut.from_row(row, await _email_for(db, row)))
    return out


@router.get("/items/{item_id}", response_model=InboxItemOut)
async def get_item(
    patient_id: uuid.UUID,
    item_id: uuid.UUID,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
) -> InboxItemOut:
    await _gate(db, patient_id=patient_id, user=user, request=request)
    row = await _load_item(db, patient_id=patient_id, item_id=item_id)
    return InboxItemOut.from_row(row, await _email_for(db, row))


def _map_decision_errors(exc: Exception) -> HTTPException:
    if isinstance(exc, ReviewDecisionError):
        code = status.HTTP_422_UNPROCESSABLE_ENTITY
        if exc.code in ("decision.human_only", "decision.not_authorized"):
            code = status.HTTP_403_FORBIDDEN
        return HTTPException(status_code=code, detail={"code": exc.code, "message": str(exc)})
    if isinstance(exc, ReviewTransitionError):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "review.invalid_transition",
                "from": exc.current,
                "to": exc.requested,
            },
        )
    raise exc


@router.post("/items/{item_id}/accept", response_model=DecisionOut, status_code=202)
async def accept_item(
    patient_id: uuid.UUID,
    item_id: uuid.UUID,
    body: AcceptItemIn,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
    dry_run: Annotated[bool, Query()] = False,
) -> DecisionOut:
    """Accept the lot: the engine validates the decision (gate + RBAC +
    state machine), the worker performs the ingest. ``dry_run`` reports
    what would be promoted/skipped without transitioning anything."""
    patient = await _gate(db, patient_id=patient_id, user=user, request=request)
    item = await _load_item(db, patient_id=patient_id, item_id=item_id)
    enforce_if_match_value(if_match, str(item.etag))

    # Stash the reviewer options on the manifest BEFORE computing the
    # dry-run view so both paths describe the same promotion.
    manifest = dict(item.manifest or {})
    target_folder_id: str | None = None
    if body.folder_id is not None:
        folder = (
            await db.execute(
                select(Folder).where(Folder.id == body.folder_id, Folder.patient_id == patient.id)
            )
        ).scalar_one_or_none()
        if folder is None:
            raise HTTPException(status_code=422, detail="folder does not belong to the patient")
        target_folder_id = str(folder.id)
    manifest["review_options"] = {
        "folder_id": target_folder_id,
        "excluded_components": body.excluded_components,
        "include_body": body.include_body,
    }
    item.manifest = manifest

    to_promote, skipped = promotable_components(item)
    if item.source_channel == "email" and not to_promote and not body.include_body:
        raise HTTPException(
            status_code=422,
            detail={"code": "inbox.nothing_to_promote", "skipped": skipped},
        )

    if dry_run:
        await db.rollback()  # discard the manifest stash
        item = await _load_item(db, patient_id=patient_id, item_id=item_id)
        return DecisionOut(
            item=InboxItemOut.from_row(item, await _email_for(db, item)),
            dry_run=True,
            would_promote=[c.get("name") for c in to_promote],
            would_skip=skipped,
        )

    # Quota pre-check: a clean 413 here beats a failed promotion later.
    owner_subject_id = patient.managed_by_subject_id or patient.self_user_subject_id
    if owner_subject_id is not None and item.source_channel == "email":
        total = sum(int(c.get("size_bytes") or 0) for c in to_promote)
        await check_storage_quota(db, subject_id=owner_subject_id, additional_bytes=total)

    actor = ReviewActor.from_request(user, request)
    try:
        await review_engine.decide(
            db, INBOX_PROFILE, item, decision="accepted", actor=actor, reason=body.note
        )
    except (ReviewDecisionError, ReviewTransitionError) as exc:
        await db.rollback()
        raise _map_decision_errors(exc) from exc
    await db.commit()

    # Promotion in the worker; the etag-keyed job id dedupes replays
    # and the maintenance sweep recovers a lost enqueue.
    settings = get_settings()
    try:
        redis = await create_pool(redis_settings(settings.redis_url))
        try:
            await redis.enqueue_job(
                "promote_inbox_item",
                str(item.id),
                _job_id=f"inbox-promote:{item.id}:{item.etag}",
            )
        finally:
            await redis.close()
    except Exception:
        logger.exception("failed to enqueue promote_inbox_item for %s", item.id)

    return DecisionOut(item=InboxItemOut.from_row(item, await _email_for(db, item)))


@router.post("/items/{item_id}/reject", response_model=DecisionOut)
async def reject_item(
    patient_id: uuid.UUID,
    item_id: uuid.UUID,
    body: RejectItemIn,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
    dry_run: Annotated[bool, Query()] = False,
) -> DecisionOut:
    await _gate(db, patient_id=patient_id, user=user, request=request)
    item = await _load_item(db, patient_id=patient_id, item_id=item_id)
    enforce_if_match_value(if_match, str(item.etag))
    if dry_run:
        return DecisionOut(
            item=InboxItemOut.from_row(item, await _email_for(db, item)), dry_run=True
        )
    actor = ReviewActor.from_request(user, request)
    try:
        await review_engine.decide(
            db, INBOX_PROFILE, item, decision="rejected", actor=actor, reason=body.reason
        )
    except (ReviewDecisionError, ReviewTransitionError) as exc:
        await db.rollback()
        raise _map_decision_errors(exc) from exc
    await db.commit()
    return DecisionOut(item=InboxItemOut.from_row(item, await _email_for(db, item)))


__all__ = ["router"]
