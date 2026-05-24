"""Time-travel & history API for the F12 versioning.

Read-only endpoints that wrap the service helpers
(``read_at_commit``, ``diff_commits``, ref_log query) into REST
surface. The frontend uses these for the cronologia view, the
diff viewer, and the share-link ``?at=<hash>`` pin feature.

Permission: every endpoint requires ``READ_METADATA`` on the patient,
mirroring the rest of the patient API. No writes.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.auth import enforce_agent_patient_scope, require_user
from bvphoenix.db.models import Patient, User
from bvphoenix.db.session import get_db
from bvphoenix.middleware.audit_dependency import AuditDep
from bvphoenix.services.permissions import READ_METADATA, can_patient
from bvphoenix.services.versioning import (
    ActorContext,
    RevertConflict,
    diff_commits,
    read_at_commit,
    resolve_branch_for_write,
    restore_entity_at_commit,
    revert_commit,
)

router = APIRouter(tags=["history"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class CommitOut(BaseModel):
    commit_hash: str
    parent_hashes: list[str]
    tree_hash: str
    author_subject_id: str | None
    author_kind: Literal["human", "agent", "system", "link"]
    # Resolved display_name of the author subject, or None for system commits
    # without a subject row (initial-import migrations).
    author_display_name: str | None
    model_id: str | None
    provider: str | None
    agent_token_id: str | None
    # Direct FK to ``agent_assistants.id`` populated by the modern
    # per-assistant client_secret flow. Legacy commits (pre-0081) only
    # carry ``agent_token_id`` and the join falls through to it; the
    # backfill in migration 0081 populates this column for existing
    # rows where the FK chain still resolves.
    agent_assistant_id: str | None = None
    # Resolved label of the AI assistant when either ``agent_assistant_id``
    # or ``agent_token_id → assistant_id`` resolves. The raw model_id
    # (e.g. ``claude-opus-4-7``) is opaque to clinicians; the assistant
    # label is the human-set name (e.g. "Claude in studio").
    agent_assistant_label: str | None
    # Set when the commit was authored via an anonymous share link
    # (``mode='anonymous'`` in ``share_links``). The revision-history UI
    # uses it to render a "modality A" badge so reviewers can identify
    # at a glance which writes came from a token-only credential rather
    # than a verified human session.
    share_link_id: str | None = None
    share_link_label: str | None = None
    share_link_recipient: str | None = None
    branch_at_creation: str | None
    message: str
    created_at: str


class RefLogEntryOut(BaseModel):
    id: str
    ref_name: str
    from_commit: str | None
    to_commit: str
    op_kind: Literal["init", "commit", "merge", "reset", "revert", "rebase", "delete"]
    actor_subject_id: str | None
    reason: str | None
    created_at: str


class DiffEntryOut(BaseModel):
    entity_kind: str
    entity_id: str
    change: Literal["added", "removed", "modified"]
    hash_a: str | None
    hash_b: str | None


class HistoryOut(BaseModel):
    patient_id: str
    ref_name: str
    head_commit: str | None
    commits: list[CommitOut]


class RefOut(BaseModel):
    """One mutable branch reference of a patient."""

    ref_name: str
    head_commit: str
    is_locked: bool


class MultiBranchHistoryOut(BaseModel):
    """Timeline that aggregates commits from every branch of a patient.

    The UI uses ``commits[*].branch_at_creation`` to assign each row to
    a lane (main vs consultation/<id>). Commits reachable from more
    than one ref appear once. Sorted newest-first by ``created_at``.
    """

    patient_id: str
    refs: list[RefOut]
    commits: list[CommitOut]


class RevertIn(BaseModel):
    """Body of POST /revert/{commit_hash}.

    ``message`` is the audit string for the new revert commit.
    ``consultation_id`` routes the write to a consultation branch when
    the caller is not the patient owner; owners can omit it to revert
    on ``main`` directly.
    """

    message: str = Field(..., min_length=1, max_length=500)
    consultation_id: uuid.UUID | None = None


class RestoreEntityIn(BaseModel):
    """Body of POST /restore-entity.

    Restore one (entity_kind, entity_id) to its state at
    ``source_commit_hash``. ``consultation_id`` follows the same routing
    rules as :class:`RevertIn`.
    """

    source_commit_hash: str = Field(..., min_length=64, max_length=64)
    entity_kind: str = Field(..., min_length=1, max_length=32)
    entity_id: uuid.UUID
    message: str = Field(..., min_length=1, max_length=500)
    consultation_id: uuid.UUID | None = None


class RevertConflictItem(BaseModel):
    entity_kind: str
    entity_id: str
    head_hash: str | None
    target_hash: str | None


class RevertResultOut(BaseModel):
    commit_hash: str
    branch_ref: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _hex(b: bytes | None) -> str | None:
    return b.hex() if b is not None else None


async def _patient_or_404(
    db: AsyncSession,
    request: Request,
    user: User,
    patient_id: uuid.UUID,
) -> Patient:
    patient = (
        await db.execute(select(Patient).where(Patient.id == patient_id))
    ).scalar_one_or_none()
    if patient is None:
        raise HTTPException(status_code=404, detail="patient not found")
    enforce_agent_patient_scope(request, patient.id, scope="patient:read")
    if not await can_patient(db, user=user, action=READ_METADATA, patient=patient):
        raise HTTPException(status_code=404, detail="patient not found")
    return patient


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/patients/{patient_id}/history",
    response_model=HistoryOut,
)
async def get_patient_history(
    request: Request,
    patient_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    ref: str = Query(
        "main",
        description=(
            "Branch ref to walk; 'main' by default. "
            "Use 'consultation/<id>' to inspect a consultation branch."
        ),
    ),
    limit: int = Query(100, ge=1, le=500),
) -> HistoryOut:
    """Return the commit chain from the head of ``ref`` walking parents.

    Linear walk for now: when the chain forks (merge commits with two
    parents), we follow ``parent_hashes[0]`` (target side, the
    "mainline" by convention). The advanced viewer can expand to a
    DAG visualisation later.
    """
    await _patient_or_404(db, request, user, patient_id)

    head = (
        await db.execute(
            text("SELECT commit_hash FROM refs WHERE patient_id = :p AND ref_name = :r"),
            {"p": patient_id, "r": ref},
        )
    ).scalar_one_or_none()
    if head is None:
        return HistoryOut(
            patient_id=str(patient_id),
            ref_name=ref,
            head_commit=None,
            commits=[],
        )

    # Walk parent[0] up to ``limit`` hops.
    commits: list[CommitOut] = []
    current = head
    seen: set[bytes] = set()
    while current is not None and len(commits) < limit:
        if current in seen:
            break
        seen.add(current)
        row = (
            await db.execute(
                text(
                    "SELECT c.commit_hash, c.parent_hashes, c.tree_hash, "
                    "  c.author_subject_id, c.author_kind, c.model_id, "
                    "  c.provider, c.agent_token_id, c.branch_at_creation, "
                    "  c.message, c.created_at, "
                    "  s.display_name AS author_display_name, "
                    "  aa.label       AS agent_assistant_label, "
                    "  c.share_link_id, "
                    "  sl.label          AS share_link_label, "
                    "  sl.recipient_name AS share_link_recipient, "
                    "  COALESCE(c.agent_assistant_id, agt.assistant_id) "
                    "    AS resolved_assistant_id "
                    "FROM commits c "
                    "LEFT JOIN subjects s "
                    "  ON s.id = c.author_subject_id "
                    "LEFT JOIN agent_tokens agt "
                    "  ON agt.id = c.agent_token_id "
                    "LEFT JOIN agent_assistants aa "
                    "  ON aa.id = COALESCE(c.agent_assistant_id, agt.assistant_id) "
                    "LEFT JOIN share_links sl "
                    "  ON sl.id = c.share_link_id "
                    "WHERE c.commit_hash = :c"
                ),
                {"c": current},
            )
        ).first()
        if row is None:
            break
        commits.append(
            CommitOut(
                commit_hash=row[0].hex(),
                parent_hashes=[p.hex() for p in (row[1] or [])],
                tree_hash=row[2].hex(),
                author_subject_id=str(row[3]) if row[3] else None,
                author_kind=row[4],
                author_display_name=row[11],
                model_id=row[5],
                provider=row[6],
                agent_token_id=str(row[7]) if row[7] else None,
                agent_assistant_id=str(row[16]) if row[16] else None,
                agent_assistant_label=row[12],
                share_link_id=str(row[13]) if row[13] else None,
                share_link_label=row[14],
                share_link_recipient=row[15],
                branch_at_creation=row[8],
                message=row[9],
                created_at=row[10].isoformat(),
            )
        )
        # Move to first parent (linear walk; merge commits have target as [0]).
        parents = row[1] or []
        current = parents[0] if parents else None

    return HistoryOut(
        patient_id=str(patient_id),
        ref_name=ref,
        head_commit=head.hex(),
        commits=commits,
    )


@router.get(
    "/patients/{patient_id}/history/all",
    response_model=MultiBranchHistoryOut,
)
async def get_multi_branch_history(
    request: Request,
    patient_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    per_ref_limit: int = Query(
        100,
        ge=1,
        le=500,
        description=(
            "Maximum number of ancestor commits to walk PER ref. "
            "The total response size is bounded by this value times "
            "the number of refs (typically a handful)."
        ),
    ),
) -> MultiBranchHistoryOut:
    """Aggregate the timeline across all refs of a patient.

    Walks every ref (``main`` plus the live ``consultation/<id>``
    branches) and unions the commit lists. Each commit appears once;
    its ``branch_at_creation`` field tells the UI which lane to render
    it in. Ordering is newest-first by ``created_at``.

    This is the "branch-aware" alternative to the linear
    ``/history?ref=...`` walker; the legacy endpoint still serves
    callers that only care about ``main``.
    """
    await _patient_or_404(db, request, user, patient_id)

    ref_rows = (
        await db.execute(
            text("SELECT ref_name, commit_hash, is_locked FROM refs WHERE patient_id = :p"),
            {"p": patient_id},
        )
    ).all()
    refs = [RefOut(ref_name=r[0], head_commit=r[1].hex(), is_locked=r[2]) for r in ref_rows]

    # Walk each ref's parent chain, de-duplicating by commit_hash.
    seen: set[bytes] = set()
    commit_payloads: dict[bytes, CommitOut] = {}
    commit_created_at: dict[bytes, str] = {}
    for r in ref_rows:
        head_hash = r[1]
        current = head_hash
        steps = 0
        while current is not None and steps < per_ref_limit:
            if current in seen:
                break
            seen.add(current)
            row = (
                await db.execute(
                    text(
                        "SELECT c.commit_hash, c.parent_hashes, c.tree_hash, "
                        "  c.author_subject_id, c.author_kind, c.model_id, "
                        "  c.provider, c.agent_token_id, c.branch_at_creation, "
                        "  c.message, c.created_at, "
                        "  s.display_name AS author_display_name, "
                        "  aa.label       AS agent_assistant_label, "
                        "  c.share_link_id, "
                        "  sl.label          AS share_link_label, "
                        "  sl.recipient_name AS share_link_recipient, "
                        # Resolved assistant id: prefer the explicit
                        # ``c.agent_assistant_id`` (pinned by the writer
                        # at commit time), fall back to the
                        # ``agent_tokens.assistant_id`` join. Without
                        # this column ``row[16]`` below raises
                        # IndexError and the page returns 500
                        # ("Internal Server Error" on the Versions
                        # tab). Mirrors the SELECT in the linear
                        # ``/history`` endpoint above.
                        "  COALESCE(c.agent_assistant_id, agt.assistant_id) "
                        "    AS resolved_assistant_id "
                        "FROM commits c "
                        "LEFT JOIN subjects s "
                        "  ON s.id = c.author_subject_id "
                        "LEFT JOIN agent_tokens agt "
                        "  ON agt.id = c.agent_token_id "
                        "LEFT JOIN agent_assistants aa "
                        "  ON aa.id = COALESCE(c.agent_assistant_id, agt.assistant_id) "
                        "LEFT JOIN share_links sl "
                        "  ON sl.id = c.share_link_id "
                        "WHERE c.commit_hash = :c"
                    ),
                    {"c": current},
                )
            ).first()
            if row is None:
                break
            commit_payloads[row[0]] = CommitOut(
                commit_hash=row[0].hex(),
                parent_hashes=[p.hex() for p in (row[1] or [])],
                tree_hash=row[2].hex(),
                author_subject_id=str(row[3]) if row[3] else None,
                author_kind=row[4],
                author_display_name=row[11],
                model_id=row[5],
                provider=row[6],
                agent_token_id=str(row[7]) if row[7] else None,
                agent_assistant_id=str(row[16]) if row[16] else None,
                agent_assistant_label=row[12],
                share_link_id=str(row[13]) if row[13] else None,
                share_link_label=row[14],
                share_link_recipient=row[15],
                branch_at_creation=row[8],
                message=row[9],
                created_at=row[10].isoformat(),
            )
            commit_created_at[row[0]] = row[10].isoformat()
            parents = row[1] or []
            current = parents[0] if parents else None
            steps += 1

    sorted_hashes = sorted(
        commit_payloads.keys(),
        key=lambda h: commit_created_at[h],
        reverse=True,
    )
    commits = [commit_payloads[h] for h in sorted_hashes]

    return MultiBranchHistoryOut(
        patient_id=str(patient_id),
        refs=refs,
        commits=commits,
    )


@router.get("/patients/{patient_id}/at/{commit_hash}")
async def get_state_at_commit(
    request: Request,
    patient_id: uuid.UUID,
    commit_hash: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    entity_kind: str | None = Query(None, description="Optional filter to one entity_kind."),
) -> dict:
    """Return the state of the patient's entities at a specific commit.

    Output: ``{ entity_kind: { entity_id: payload, ... }, ... }``. The
    payload is the canonical-form dict that was stored at the commit;
    tombstoned entities surface as ``{"_tombstoned": true}``.

    The endpoint validates that the commit belongs to this patient
    (defense in depth: RLS already filters, but a 404 is clearer than
    an empty result).
    """
    await _patient_or_404(db, request, user, patient_id)
    try:
        ch = bytes.fromhex(commit_hash)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid commit hash hex")

    own = (
        await db.execute(
            text("SELECT 1 FROM commits WHERE commit_hash = :c AND patient_id = :p"),
            {"c": ch, "p": patient_id},
        )
    ).first()
    if own is None:
        raise HTTPException(status_code=404, detail="commit not found for this patient")

    state = await read_at_commit(db, commit_hash=ch, entity_kind=entity_kind)
    # Re-shape for the wire: nest by entity_kind.
    out: dict[str, dict[str, dict]] = {}
    for (kind, eid), payload in state.items():
        out.setdefault(kind, {})[str(eid)] = payload
    return out


@router.get(
    "/patients/{patient_id}/diff",
    response_model=list[DiffEntryOut],
)
async def get_diff(
    request: Request,
    patient_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    from_commit: str = Query(..., alias="from"),
    to_commit: str = Query(..., alias="to"),
) -> list[DiffEntryOut]:
    """Return the entities that differ between ``from`` and ``to``.

    Each entry: ``(entity_kind, entity_id, change, hash_a, hash_b)``
    where ``change`` is ``added`` (only in ``to``) / ``removed`` (only
    in ``from``) / ``modified`` (in both, different hash). Equal
    entries are omitted.

    The two commits must both belong to this patient.
    """
    await _patient_or_404(db, request, user, patient_id)
    try:
        a = bytes.fromhex(from_commit)
        b = bytes.fromhex(to_commit)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid commit hash hex")

    own = (
        await db.execute(
            text(
                "SELECT count(*) FROM commits "
                "WHERE commit_hash = ANY(ARRAY[:a, :b]::bytea[]) "
                "  AND patient_id = :p"
            ),
            {"a": a, "b": b, "p": patient_id},
        )
    ).scalar_one()
    if own != 2:
        raise HTTPException(status_code=404, detail="commits not found for this patient")

    rows = await diff_commits(db, a_hash=a, b_hash=b)
    return [
        DiffEntryOut(
            entity_kind=kind,
            entity_id=str(eid),
            change=change,
            hash_a=_hex(ha),
            hash_b=_hex(hb),
        )
        for (kind, eid, change, ha, hb) in rows
    ]


@router.get(
    "/patients/{patient_id}/ref-log",
    response_model=list[RefLogEntryOut],
)
async def get_ref_log(
    request: Request,
    patient_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    ref: str | None = Query(
        None,
        description=(
            "Filter to a single branch (e.g. 'main' or "
            "'consultation/<id>'). Default: all branches of the patient."
        ),
    ),
    limit: int = Query(100, ge=1, le=500),
) -> list[RefLogEntryOut]:
    """Return the history of ref movements for this patient.

    ``op_kind`` ∈ init / commit / merge / reset / revert / rebase /
    delete. Sorted newest first.
    """
    await _patient_or_404(db, request, user, patient_id)

    sql = (
        "SELECT id, ref_name, from_commit, to_commit, op_kind, "
        "  actor_subject_id, reason, created_at "
        "FROM ref_log WHERE patient_id = :p"
        + (" AND ref_name = :r" if ref else "")
        + " ORDER BY created_at DESC LIMIT :l"
    )
    params: dict = {"p": patient_id, "l": limit}
    if ref:
        params["r"] = ref
    rows = (await db.execute(text(sql), params)).all()
    return [
        RefLogEntryOut(
            id=str(r[0]),
            ref_name=r[1],
            from_commit=r[2].hex() if r[2] else None,
            to_commit=r[3].hex(),
            op_kind=r[4],
            actor_subject_id=str(r[5]) if r[5] else None,
            reason=r[6],
            created_at=r[7].isoformat(),
        )
        for r in rows
    ]


def _is_owner(patient: Patient, user: User) -> bool:
    """Owner of the fascicolo: the managing physician or the patient
    themselves (for self-managed records). Admins are treated as owners
    so admin-driven reverts on main are not gated behind a consultation.
    """
    return (
        patient.managed_by_subject_id == user.subject_id
        or patient.self_user_subject_id == user.subject_id
        or user.is_admin
    )


def _actor_for_request(request: Request, user: User) -> ActorContext:
    """Build an ``ActorContext`` from the auth context.

    Mirrors the per-route logic used by clinical_notes / reports: if the
    request was authenticated with an agent token, the commit is
    attributed to that agent (still under the human's subject_id so the
    audit trail keeps the linking subject); otherwise it is a human
    write.
    """
    request_agent_token = getattr(request.state, "agent_token", None)
    if request_agent_token is not None:
        return ActorContext(
            subject_id=user.subject_id,
            kind="agent",
            model_id=getattr(request_agent_token, "model_id", None),
            provider=getattr(request_agent_token, "provider", None),
            agent_token_id=request_agent_token.id,
        )
    return ActorContext(subject_id=user.subject_id, kind="human")


async def _resolve_branch_or_http(
    db: AsyncSession,
    *,
    patient_id: uuid.UUID,
    user: User,
    consultation_id: uuid.UUID | None,
    is_owner: bool,
) -> str:
    """Wrapper that translates :func:`resolve_branch_for_write` errors
    into HTTP responses (403 / 404), mirroring the convention in
    api/clinical_notes.
    """
    try:
        return await resolve_branch_for_write(
            db,
            patient_id=patient_id,
            user_subject_id=user.subject_id,
            consultation_id=consultation_id,
            is_owner=is_owner,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post(
    "/patients/{patient_id}/revert/{commit_hash}",
    response_model=RevertResultOut,
    status_code=status.HTTP_201_CREATED,
)
async def post_revert_commit(
    request: Request,
    patient_id: uuid.UUID,
    commit_hash: str,
    body: RevertIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    audit: AuditDep,
) -> RevertResultOut:
    """Append a revert commit that undoes ``commit_hash`` on the routed branch.

    Routing follows the standard write rules: owner → main, non-owner
    must supply ``consultation_id`` and the revert lands on that
    consultation's branch.

    Returns 409 with the conflict list if any entity touched by
    ``commit_hash`` has been modified since.
    """
    patient = await _patient_or_404(db, request, user, patient_id)
    try:
        target = bytes.fromhex(commit_hash)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid commit hash hex") from exc

    # Defense in depth: confirm the commit belongs to this patient
    # before invoking the service. The service re-checks but a 404 here
    # is clearer than a generic ValueError.
    own = (
        await db.execute(
            text("SELECT 1 FROM commits WHERE commit_hash = :c AND patient_id = :p"),
            {"c": target, "p": patient_id},
        )
    ).first()
    if own is None:
        raise HTTPException(status_code=404, detail="commit not found for this patient")

    branch_ref = await _resolve_branch_or_http(
        db,
        patient_id=patient.id,
        user=user,
        consultation_id=body.consultation_id,
        is_owner=_is_owner(patient, user),
    )
    actor = _actor_for_request(request, user)

    try:
        result = await revert_commit(
            db,
            patient_id=patient.id,
            commit_to_revert=target,
            branch_ref=branch_ref,
            actor=actor,
            message=body.message,
        )
    except RevertConflict as exc:
        # 409 with the structured conflict list so the UI can offer
        # the per-entity restore_entity flow as a fallback.
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "revert_conflict",
                "message": str(exc),
                "conflicts": [
                    RevertConflictItem(
                        entity_kind=c.entity_kind,
                        entity_id=str(c.entity_id),
                        head_hash=_hex(c.head_hash),
                        target_hash=_hex(c.target_hash),
                    ).model_dump()
                    for c in exc.conflicts
                ],
            },
        ) from exc
    except NotImplementedError as exc:
        await db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    await db.commit()

    await audit.log(
        action="revision_revert",
        actor_subject_id=user.subject_id,
        resource_kind="patient",
        resource_id=patient.id,
        metadata={
            "patient_id": str(patient.id),
            "branch_ref": branch_ref,
            "reverted_commit": commit_hash,
            "new_commit": result.commit_hash.hex(),
            "consultation_id": (str(body.consultation_id) if body.consultation_id else None),
        },
    )

    return RevertResultOut(
        commit_hash=result.commit_hash.hex(),
        branch_ref=branch_ref,
    )


@router.post(
    "/patients/{patient_id}/restore-entity",
    response_model=RevertResultOut,
    status_code=status.HTTP_201_CREATED,
)
async def post_restore_entity(
    request: Request,
    patient_id: uuid.UUID,
    body: RestoreEntityIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    audit: AuditDep,
) -> RevertResultOut:
    """Restore a single entity (kind, id) to its state at a historical commit.

    Granular complement of ``POST /revert``: the user picks the source
    commit and one entity, no conflict detection is performed because
    the action is explicitly opt-in.
    """
    patient = await _patient_or_404(db, request, user, patient_id)
    try:
        source = bytes.fromhex(body.source_commit_hash)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid source commit hash hex") from exc

    own = (
        await db.execute(
            text("SELECT 1 FROM commits WHERE commit_hash = :c AND patient_id = :p"),
            {"c": source, "p": patient_id},
        )
    ).first()
    if own is None:
        raise HTTPException(status_code=404, detail="source commit not found for this patient")

    branch_ref = await _resolve_branch_or_http(
        db,
        patient_id=patient.id,
        user=user,
        consultation_id=body.consultation_id,
        is_owner=_is_owner(patient, user),
    )
    actor = _actor_for_request(request, user)

    try:
        result = await restore_entity_at_commit(
            db,
            patient_id=patient.id,
            source_commit=source,
            entity_kind=body.entity_kind,
            entity_id=body.entity_id,
            branch_ref=branch_ref,
            actor=actor,
            message=body.message,
        )
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    await db.commit()

    await audit.log(
        action="revision_restore_entity",
        actor_subject_id=user.subject_id,
        resource_kind="patient",
        resource_id=patient.id,
        metadata={
            "patient_id": str(patient.id),
            "branch_ref": branch_ref,
            "source_commit": body.source_commit_hash,
            "entity_kind": body.entity_kind,
            "entity_id": str(body.entity_id),
            "new_commit": result.commit_hash.hex(),
            "consultation_id": (str(body.consultation_id) if body.consultation_id else None),
        },
    )

    return RevertResultOut(
        commit_hash=result.commit_hash.hex(),
        branch_ref=branch_ref,
    )
