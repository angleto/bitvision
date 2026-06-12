"""Proposals API: pull-request lifecycle for the F12 versioning.

A proposal is the technical record of "this consultation branch wants
to merge into main". The proposal carries the source/target refs, the
precomputed conflicts, and the merge commit hash; the consultation
itself has no table of its own since v3 (D2 folded it into
ReportContent) — its surviving identity is the ``consultation/<id>``
source ref, whose ``is_locked`` flag freezes the branch once the
review closes it (merge / withdraw / reject).

Endpoints:
  - ``GET    /proposals/{id}`` — full detail incl. conflicts.
  - ``GET    /patients/{id}/proposals`` — list for the patient.
  - ``POST   /proposals/{id}/conflicts/{cid}/resolve`` — record a
    user's resolution choice (take_source / take_target / manual + new
    object_hash).
  - ``POST   /proposals/{id}/merge`` — execute the merge once all
    conflicts are resolved. Fast-forward when possible; three-way
    merge otherwise.
  - ``POST   /proposals/{id}/withdraw`` — drop the proposal without
    merging. The source branch is locked.

Permissions:
  - Reading a proposal: the underlying patient must be readable
    (``READ_METADATA``).
  - Reviewing / merging: only the patient owner or platform admin.
    The proposer cannot self-approve.
  - Resolving conflicts: same as reviewing (the reviewer chooses how
    to merge, not the proposer).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.auth import enforce_agent_patient_scope, require_user
from bvphoenix.db.models import Patient, User
from bvphoenix.db.session import get_db
from bvphoenix.middleware.audit_dependency import AuditDep
from bvphoenix.services.permissions import (
    READ_METADATA,
    can_patient,
)
from bvphoenix.services.versioning import (
    ActorContext,
    ConflictsUnresolved,
    MergeResolution,
    fast_forward_merge,
    three_way_merge,
)

router = APIRouter(tags=["proposals"])


# ---- Schemas ----


class _Hex(str):
    """Just an alias for clarity in the API surface — wire format for
    BYTEA hashes is lower-case hex (no 0x prefix)."""


class ConflictOut(BaseModel):
    id: str
    entity_kind: str
    entity_id: str
    base_object_hash: str | None
    source_object_hash: str | None
    target_object_hash: str | None
    conflict_kind: Literal["add_add", "edit_edit", "edit_delete", "delete_edit"]
    resolution: Literal["take_source", "take_target", "manual", "auto_merge"] | None
    resolved_object_hash: str | None
    resolved_by_subject_id: str | None
    resolved_at: str | None


class ProposalOut(BaseModel):
    id: str
    patient_id: str
    consultation_id: str | None
    source_ref_name: str
    target_ref_name: str
    source_head_commit: str
    target_head_commit: str
    base_commit: str | None
    proposer_subject_id: str
    title: str
    description: str | None
    status: Literal["open", "approved", "rejected", "merged", "withdrawn", "superseded"]
    conflict_count: int
    merge_commit: str | None
    reviewed_by_subject_id: str | None
    reviewed_at: str | None
    review_decision: Literal["approve", "request_changes", "reject"] | None
    review_notes: str | None
    created_at: str
    closed_at: str | None
    conflicts: list[ConflictOut] = Field(default_factory=list)


class ResolveConflictIn(BaseModel):
    kind: Literal["take_source", "take_target", "manual"]
    # For 'manual' the caller posts the new payload, the server
    # canonicalises + hashes it. For take_source/take_target the
    # server just pins the corresponding object_hash from the
    # conflict row.
    payload: dict | None = None


class MergeIn(BaseModel):
    review_notes: str | None = None


class WithdrawIn(BaseModel):
    reason: str | None = None


class RejectIn(BaseModel):
    """Owner-side rejection of a proposal.

    Distinct from :class:`WithdrawIn`: ``withdraw`` is the proposer's
    "I changed my mind" path (and historically also covered owner
    cancellation, marking the row as ``withdrawn``). ``reject`` is
    explicitly the reviewer's "I refuse to merge" path; it sets
    ``status='rejected'`` so audit and downstream filters can tell the
    two apart.

    A non-empty ``review_notes`` is required: a rejection without a
    reason is not actionable for the consultation author and reads as
    abusive in audit.
    """

    review_notes: str = Field(..., min_length=1, max_length=2000)


# ---- Helpers ----


def _hex(b: bytes | None) -> str | None:
    return b.hex() if b is not None else None


def _consultation_id_from_ref(ref_name: str) -> uuid.UUID | None:
    """Derive the consultation id from a ``consultation/<uuid>`` ref name.

    There is no ``proposals.consultation_id`` column (the Consultation
    table was dropped in v3); the source ref name is the linkage. Kept
    in the API shape so clients can still join proposal ↔ consultation.
    """
    prefix = "consultation/"
    if not ref_name.startswith(prefix):
        return None
    try:
        return uuid.UUID(ref_name[len(prefix) :])
    except ValueError:
        return None


async def _proposal_or_404(db: AsyncSession, proposal_id: uuid.UUID) -> dict:
    row = (
        await db.execute(
            text(
                "SELECT id, patient_id, source_ref_name, "
                "  target_ref_name, source_head_commit, target_head_commit, "
                "  base_commit, proposer_subject_id, title, description, "
                "  status, conflict_count, merge_commit, "
                "  reviewed_by_subject_id, reviewed_at, review_decision, "
                "  review_notes, created_at, closed_at "
                "FROM proposals WHERE id = :p"
            ),
            {"p": proposal_id},
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="proposal not found")
    return {
        "id": row[0],
        "patient_id": row[1],
        "consultation_id": _consultation_id_from_ref(row[2]),
        "source_ref_name": row[2],
        "target_ref_name": row[3],
        "source_head_commit": row[4],
        "target_head_commit": row[5],
        "base_commit": row[6],
        "proposer_subject_id": row[7],
        "title": row[8],
        "description": row[9],
        "status": row[10],
        "conflict_count": row[11],
        "merge_commit": row[12],
        "reviewed_by_subject_id": row[13],
        "reviewed_at": row[14],
        "review_decision": row[15],
        "review_notes": row[16],
        "created_at": row[17],
        "closed_at": row[18],
    }


async def _conflicts_for(db: AsyncSession, proposal_id: uuid.UUID) -> list[ConflictOut]:
    rows = (
        await db.execute(
            text(
                "SELECT id, entity_kind, entity_id, "
                "  base_object_hash, source_object_hash, target_object_hash, "
                "  conflict_kind, resolution, resolved_object_hash, "
                "  resolved_by_subject_id, resolved_at "
                "FROM merge_conflicts WHERE proposal_id = :p "
                "ORDER BY entity_kind, entity_id"
            ),
            {"p": proposal_id},
        )
    ).all()
    return [
        ConflictOut(
            id=str(r[0]),
            entity_kind=r[1],
            entity_id=str(r[2]),
            base_object_hash=_hex(r[3]),
            source_object_hash=_hex(r[4]),
            target_object_hash=_hex(r[5]),
            conflict_kind=r[6],
            resolution=r[7],
            resolved_object_hash=_hex(r[8]),
            resolved_by_subject_id=str(r[9]) if r[9] else None,
            resolved_at=r[10].isoformat() if r[10] else None,
        )
        for r in rows
    ]


async def _proposal_to_out(db: AsyncSession, p: dict) -> ProposalOut:
    return ProposalOut(
        id=str(p["id"]),
        patient_id=str(p["patient_id"]),
        consultation_id=str(p["consultation_id"]) if p["consultation_id"] else None,
        source_ref_name=p["source_ref_name"],
        target_ref_name=p["target_ref_name"],
        source_head_commit=p["source_head_commit"].hex(),
        target_head_commit=p["target_head_commit"].hex(),
        base_commit=_hex(p["base_commit"]),
        proposer_subject_id=str(p["proposer_subject_id"]),
        title=p["title"],
        description=p["description"],
        status=p["status"],
        conflict_count=p["conflict_count"],
        merge_commit=_hex(p["merge_commit"]),
        reviewed_by_subject_id=(
            str(p["reviewed_by_subject_id"]) if p["reviewed_by_subject_id"] else None
        ),
        reviewed_at=p["reviewed_at"].isoformat() if p["reviewed_at"] else None,
        review_decision=p["review_decision"],
        review_notes=p["review_notes"],
        created_at=p["created_at"].isoformat(),
        closed_at=p["closed_at"].isoformat() if p["closed_at"] else None,
        conflicts=await _conflicts_for(db, p["id"]),
    )


async def _check_read_patient(
    db: AsyncSession, request: Request, user: User, patient_id: uuid.UUID
) -> Patient:
    from sqlalchemy import select

    patient = (
        await db.execute(select(Patient).where(Patient.id == patient_id))
    ).scalar_one_or_none()
    if patient is None:
        raise HTTPException(status_code=404, detail="patient not found")
    enforce_agent_patient_scope(request, patient.id, scope="consultation:read")
    if not await can_patient(db, user=user, action=READ_METADATA, patient=patient):
        raise HTTPException(status_code=404, detail="patient not found")
    return patient


def _is_owner_or_admin(user: User, patient: Patient) -> bool:
    return (
        user.is_admin
        or patient.managed_by_subject_id == user.subject_id
        or patient.self_user_subject_id == user.subject_id
    )


# ---- Endpoints ----


@router.get("/proposals/{proposal_id}", response_model=ProposalOut)
async def get_proposal(
    request: Request,
    proposal_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
) -> ProposalOut:
    p = await _proposal_or_404(db, proposal_id)
    await _check_read_patient(db, request, user, p["patient_id"])
    return await _proposal_to_out(db, p)


@router.get(
    "/patients/{patient_id}/proposals",
    response_model=list[ProposalOut],
)
async def list_proposals_for_patient(
    request: Request,
    patient_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    status_filter: Literal["open", "merged", "rejected", "withdrawn"] | None = Query(
        None,
        alias="status",
        description="Filter by proposal status; omit for all states.",
    ),
) -> list[ProposalOut]:
    await _check_read_patient(db, request, user, patient_id)
    sql = (
        "SELECT id FROM proposals WHERE patient_id = :p"
        + (" AND status = :s" if status_filter else "")
        + " ORDER BY created_at DESC LIMIT 200"
    )
    params: dict = {"p": patient_id}
    if status_filter:
        params["s"] = status_filter
    ids = [r[0] for r in (await db.execute(text(sql), params)).all()]
    out: list[ProposalOut] = []
    for pid in ids:
        p = await _proposal_or_404(db, pid)
        out.append(await _proposal_to_out(db, p))
    return out


@router.post(
    "/proposals/{proposal_id}/conflicts/{conflict_id}/resolve",
    response_model=ConflictOut,
)
async def resolve_conflict(
    request: Request,
    proposal_id: uuid.UUID,
    conflict_id: uuid.UUID,
    body: ResolveConflictIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    audit: AuditDep,
) -> ConflictOut:
    p = await _proposal_or_404(db, proposal_id)
    patient = await _check_read_patient(db, request, user, p["patient_id"])
    if not _is_owner_or_admin(user, patient):
        raise HTTPException(status_code=403, detail="only the patient owner can resolve conflicts")
    if p["status"] != "open":
        raise HTTPException(status_code=409, detail=f"proposal is in status '{p['status']}'")

    conflict_row = (
        await db.execute(
            text(
                "SELECT source_object_hash, target_object_hash, "
                "  entity_kind, entity_id "
                "FROM merge_conflicts "
                "WHERE id = :c AND proposal_id = :p"
            ),
            {"c": conflict_id, "p": proposal_id},
        )
    ).first()
    if conflict_row is None:
        raise HTTPException(status_code=404, detail="conflict not found")
    sh, th, kind, eid = conflict_row

    # Decide which object_hash the resolution pins.
    if body.kind == "take_source":
        resolved_hash = sh
    elif body.kind == "take_target":
        resolved_hash = th
    else:  # manual
        if body.payload is None:
            raise HTTPException(status_code=400, detail="manual resolution requires a payload")
        from bvphoenix.services.versioning import (
            _ensure_entity_object,  # type: ignore[attr-defined]
        )

        # Use the service helper to materialise the new entity_object.
        resolved_hash = await _ensure_entity_object(
            db,
            entity_kind=kind,
            schema_version=int(body.payload.get("schema_version") or 1),
            payload=body.payload,
        )

    await db.execute(
        text(
            "UPDATE merge_conflicts SET resolution = :r, "
            "  resolved_object_hash = :rh, resolved_by_subject_id = :rs, "
            "  resolved_at = now() "
            "WHERE id = :c AND proposal_id = :p"
        ),
        {
            "r": body.kind,
            "rh": resolved_hash,
            "rs": user.subject_id,
            "c": conflict_id,
            "p": proposal_id,
        },
    )
    await db.commit()
    await audit.log(
        action="proposal_conflict_resolve",
        actor_subject_id=user.subject_id,
        resource_kind="proposal",
        resource_id=proposal_id,
        metadata={
            "conflict_id": str(conflict_id),
            "resolution": body.kind,
            "entity_kind": kind,
            "entity_id": str(eid),
        },
    )
    cs = await _conflicts_for(db, proposal_id)
    for c in cs:
        if c.id == str(conflict_id):
            return c
    raise HTTPException(status_code=500, detail="conflict vanished after resolve")


@router.post("/proposals/{proposal_id}/merge", response_model=ProposalOut)
async def merge_proposal(
    request: Request,
    proposal_id: uuid.UUID,
    body: MergeIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    audit: AuditDep,
) -> ProposalOut:
    p = await _proposal_or_404(db, proposal_id)
    patient = await _check_read_patient(db, request, user, p["patient_id"])
    if not _is_owner_or_admin(user, patient):
        raise HTTPException(status_code=403, detail="only the patient owner can merge")
    if p["status"] != "open":
        raise HTTPException(status_code=409, detail=f"proposal is in status '{p['status']}'")

    # Fast-forward path: base == current target head.
    current_target = (
        await db.execute(
            text("SELECT commit_hash FROM refs WHERE patient_id = :p AND ref_name = :r"),
            {"p": p["patient_id"], "r": p["target_ref_name"]},
        )
    ).scalar_one_or_none()

    if (
        p["base_commit"] is not None
        and current_target == p["base_commit"]
        and current_target == p["target_head_commit"]
    ):
        try:
            await fast_forward_merge(
                db,
                proposal_id=proposal_id,
                reviewer_subject_id=user.subject_id,
                review_notes=body.review_notes,
            )
        except NotImplementedError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        await db.commit()
    else:
        # Three-way merge: gather resolved conflicts and execute.
        unresolved = (
            await db.execute(
                text(
                    "SELECT id FROM merge_conflicts "
                    "WHERE proposal_id = :p AND resolved_object_hash IS NULL"
                ),
                {"p": proposal_id},
            )
        ).all()
        if unresolved:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"{len(unresolved)} conflict(s) still unresolved; resolve "
                    "all of them via /conflicts/<id>/resolve before merging"
                ),
            )
        resolutions_rows = (
            await db.execute(
                text(
                    "SELECT entity_kind, entity_id, resolution, resolved_object_hash "
                    "FROM merge_conflicts WHERE proposal_id = :p"
                ),
                {"p": proposal_id},
            )
        ).all()
        resolutions = [
            MergeResolution(
                entity_kind=r[0],
                entity_id=r[1],
                kind=r[2],
                resolved_object_hash=r[3],
            )
            for r in resolutions_rows
        ]
        try:
            merge_hash = await three_way_merge(
                db,
                base_commit=p["base_commit"],
                source_commit=p["source_head_commit"],
                target_commit=p["target_head_commit"],
                target_ref_name=p["target_ref_name"],
                patient_id=p["patient_id"],
                actor=ActorContext(subject_id=user.subject_id, kind="human"),
                message=(body.review_notes or f"merge proposal {proposal_id}"),
                resolutions=resolutions,
            )
        except ConflictsUnresolved as exc:
            raise HTTPException(
                status_code=409,
                detail=f"{len(exc.conflicts)} conflict(s) unresolved",
            ) from exc

        # Update proposal record + freeze the reviewed source branch
        # (mirrors fast_forward_merge: locked refs refuse further writes).
        await db.execute(
            text(
                "UPDATE proposals SET status='merged', merge_commit=:mc, "
                "  reviewed_by_subject_id=:rs, reviewed_at=now(), "
                "  review_decision='approve', review_notes=:rn, "
                "  closed_at=now(), updated_at=now() "
                "WHERE id = :p"
            ),
            {
                "mc": merge_hash,
                "rs": user.subject_id,
                "rn": body.review_notes,
                "p": proposal_id,
            },
        )
        await db.execute(
            text(
                "UPDATE refs SET is_locked = true, updated_at = now() "
                "WHERE patient_id = :pid AND ref_name = :r"
            ),
            {"pid": p["patient_id"], "r": p["source_ref_name"]},
        )
        await db.commit()

    await audit.log(
        action="proposal_merge",
        actor_subject_id=user.subject_id,
        resource_kind="proposal",
        resource_id=proposal_id,
        metadata={"patient_id": str(p["patient_id"])},
    )
    refreshed = await _proposal_or_404(db, proposal_id)
    return await _proposal_to_out(db, refreshed)


@router.post("/proposals/{proposal_id}/withdraw", response_model=ProposalOut)
async def withdraw_proposal(
    request: Request,
    proposal_id: uuid.UUID,
    body: WithdrawIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    audit: AuditDep,
) -> ProposalOut:
    p = await _proposal_or_404(db, proposal_id)
    patient = await _check_read_patient(db, request, user, p["patient_id"])
    # Either the proposer (the consultation author) or the owner can
    # withdraw. The proposer self-withdraw is the natural "I changed
    # my mind" path; owner-withdraw is the rejection.
    is_proposer = p["proposer_subject_id"] == user.subject_id
    if not (is_proposer or _is_owner_or_admin(user, patient)):
        raise HTTPException(status_code=403, detail="cannot withdraw this proposal")
    if p["status"] != "open":
        raise HTTPException(status_code=409, detail=f"proposal is in status '{p['status']}'")

    now = datetime.now(UTC)
    await db.execute(
        text(
            "UPDATE proposals SET status='withdrawn', closed_at=:n, "
            "  reviewed_by_subject_id=:rs, reviewed_at=:n, "
            "  review_decision='reject', review_notes=:rn, updated_at=:n "
            "WHERE id = :p"
        ),
        {
            "n": now,
            "rs": user.subject_id,
            "rn": body.reason,
            "p": proposal_id,
        },
    )
    await db.execute(
        text(
            "UPDATE refs SET is_locked = true, updated_at = :n "
            "WHERE patient_id = :pid AND ref_name = :r"
        ),
        {"n": now, "pid": p["patient_id"], "r": p["source_ref_name"]},
    )
    await db.commit()

    await audit.log(
        action="proposal_withdraw",
        actor_subject_id=user.subject_id,
        resource_kind="proposal",
        resource_id=proposal_id,
        metadata={"patient_id": str(p["patient_id"]), "reason": body.reason},
    )
    refreshed = await _proposal_or_404(db, proposal_id)
    return await _proposal_to_out(db, refreshed)


@router.post("/proposals/{proposal_id}/reject", response_model=ProposalOut)
async def reject_proposal(
    request: Request,
    proposal_id: uuid.UUID,
    body: RejectIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    audit: AuditDep,
) -> ProposalOut:
    """Owner / admin rejects a proposal without merging.

    Sets ``proposals.status='rejected'``, ``review_decision='reject'``
    with the reason in ``review_notes``, and locks the source ref (so
    the proposer cannot push more commits to that branch).

    Refused with 403 if the caller is not the patient owner or an
    admin: the proposer self-cancels via ``/withdraw`` instead.
    """
    p = await _proposal_or_404(db, proposal_id)
    patient = await _check_read_patient(db, request, user, p["patient_id"])
    if not _is_owner_or_admin(user, patient):
        raise HTTPException(
            status_code=403,
            detail=(
                "only the patient owner or an admin can reject; "
                "the proposer should use /withdraw instead"
            ),
        )
    if p["status"] != "open":
        raise HTTPException(status_code=409, detail=f"proposal is in status '{p['status']}'")

    now = datetime.now(UTC)
    await db.execute(
        text(
            "UPDATE proposals SET status='rejected', closed_at=:n, "
            "  reviewed_by_subject_id=:rs, reviewed_at=:n, "
            "  review_decision='reject', review_notes=:rn, updated_at=:n "
            "WHERE id = :p"
        ),
        {
            "n": now,
            "rs": user.subject_id,
            "rn": body.review_notes,
            "p": proposal_id,
        },
    )
    # Lock the source branch so no more commits land on it. The branch
    # row stays for audit; archiving it is left to a periodic worker.
    # The rejection reason lives on the proposal row (review_notes);
    # the canonical_synthesis ReportContent has its own reject FSM.
    await db.execute(
        text(
            "UPDATE refs SET is_locked = true, updated_at = :n "
            "WHERE patient_id = :pid AND ref_name = :r"
        ),
        {"n": now, "pid": p["patient_id"], "r": p["source_ref_name"]},
    )
    await db.commit()

    await audit.log(
        action="proposal_reject",
        actor_subject_id=user.subject_id,
        resource_kind="proposal",
        resource_id=proposal_id,
        metadata={
            "patient_id": str(p["patient_id"]),
            "review_notes": body.review_notes,
        },
    )
    refreshed = await _proposal_or_404(db, proposal_id)
    return await _proposal_to_out(db, refreshed)
