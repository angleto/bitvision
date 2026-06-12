"""Git-like versioning service for the patient fascicolo.

Public API exposed by this module:

  * :func:`commit_change` writes one or more entity changes (insert /
    update / delete) to a branch, producing a new commit. Atomic:
    everything happens inside the caller's DB transaction; the caller
    must :func:`db.commit` to make the write durable.

  * :func:`read_at_commit` reconstructs the state of one entity (or
    every entity of a kind) at a specific commit hash.

  * :func:`diff_commits` returns the entities that changed between
    two commits (added, removed, modified) with their object hashes.

  * :func:`read_object` returns a fully-resolved payload given an
    ``object_hash``, transparently walking ``storage_kind='delta'``
    chains. ``EntityObject.payload`` is the canonical input so the
    caller does not need to re-canonicalise.

The "repository" is the patient. Branches are ``main`` (always
present) and ``consultation/<consultation_id>`` (materialised when a
consultation opens). The owner of a fascicolo writing on their own
``main`` is the trivial path; non-owners write on a consultation
branch and the close-consult flow opens a proposal.

Current-table synchronisation (clinical_notes, reports, ...) is NOT
done here: each endpoint owns its own current-table semantics and is
expected to upsert/delete inside the same DB transaction. This keeps
the versioning core decoupled from the schema of every clinical
entity, and makes the contract explicit for callers.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

import merge3
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.db.models.versioning import (
    Commit,
    EntityObject,
    ManifestEntry,
    Ref,
    RefLog,
)
from bvphoenix.services.canonical import canonicalize, payload_hash

# Mapping of entity_kind → name of the textual field inside the
# canonical payload. Used by ``_attempt_text_auto_merge`` to decide
# whether an edit_edit conflict can be three-way merged automatically.
# Only kinds whose payload is structured around a single dominant text
# blob are listed here; entities with multiple co-equal text fields
# (or none) fall back to manual resolution.
TEXTUAL_FIELDS: dict[str, str] = {
    "clinical_note": "body",
    "report": "text",
    "summary": "summary_md",
}


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


AuthorKind = Literal["human", "agent", "system", "link"]


@dataclass(slots=True)
class ActorContext:
    """Author identity + AI provenance for the commit being created.

    For a human commit, ``subject_id`` is the user's subject_id and
    the AI fields are None. For an agent commit, ``kind='agent'``,
    ``model_id`` and ``agent_token_id`` are required, and
    ``subject_id`` is the subject that minted the token (the human
    on whose behalf the agent acts). For a link commit, ``kind='link'``
    and ``share_link_id`` carries the originating share-link row;
    ``subject_id`` is the synthetic ``PUBLIC_SUBJECT_ID`` resolved by
    the share JWT auth path.
    """

    subject_id: uuid.UUID | None
    kind: AuthorKind = "human"
    model_id: str | None = None
    provider: str | None = None
    agent_token_id: uuid.UUID | None = None
    # Modern per-assistant flow: the row id of the AI assistant that
    # authored the write, picked up from
    # ``request.state.agent_assistant_id`` by ``resolve_actor``.
    # Persisted onto ``commits.agent_assistant_id`` so the revision
    # history badge can resolve the assistant label even when no
    # legacy ``agent_token_id`` row was minted.
    agent_assistant_id: uuid.UUID | None = None
    # Populated when ``kind='link'``: the ``share_links.id`` row that
    # minted the JWT used for this write. Persisted onto
    # ``commits.share_link_id`` so the revision-history UI can badge
    # the row as "modality A" (anonymous link share).
    share_link_id: uuid.UUID | None = None


@dataclass(slots=True)
class EntityChange:
    """One entity to mutate inside a commit.

    ``payload=None`` means delete (the entity will be absent from the
    new manifest). Otherwise the payload is canonicalised, hashed,
    stored in ``entity_objects`` if not already present, and pinned
    in the new manifest at ``(kind, id) -> object_hash``.
    """

    entity_kind: str
    entity_id: uuid.UUID
    payload: dict | None
    schema_version: int = 1


@dataclass(slots=True)
class CommitResult:
    commit_hash: bytes
    parent_hashes: list[bytes]
    tree_hash: bytes
    entity_object_hashes: dict[tuple[str, uuid.UUID], bytes | None]
    """``(kind, id) -> object_hash`` for each change applied (None if deleted)."""


# ---------------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------------


async def commit_change(
    db: AsyncSession,
    *,
    patient_id: uuid.UUID,
    branch_ref: str,
    actor: ActorContext,
    message: str,
    changes: list[EntityChange],
    branch_visibility: Literal["private", "shared", "public"] = "shared",
    op_kind: Literal["commit", "revert"] = "commit",
) -> CommitResult:
    """Append a commit to ``branch_ref`` with the given ``changes``.

    The commit contains the union of the parent's manifest with the
    changes overridden. If ``branch_ref`` does not yet exist on the
    patient, it is created with the new commit as its first commit
    (orphan root). Otherwise the parent is the current head, locked
    via SELECT FOR UPDATE for the duration of the transaction.

    Caller MUST be inside a DB transaction. The function does NOT
    commit; it stages writes so the caller can attach further work
    (current-table upsert, audit_log insert, consultation status
    update) and commit atomically.

    Args:
        db: AsyncSession in an open transaction.
        patient_id: the fascicolo this commit belongs to.
        branch_ref: ``main`` or ``consultation/<id>``.
        actor: who is committing + AI provenance.
        message: commit message (audit), 1+ char.
        changes: at least one entity to mutate.
        branch_visibility: only used when the branch is being created.

    Returns:
        :class:`CommitResult` with the new commit hash plus the
        per-entity object hashes resolved during the write.

    Raises:
        ValueError: empty ``changes``, empty ``message``, branch is locked.
    """
    if not changes:
        raise ValueError("commit_change requires at least one EntityChange")
    if not message.strip():
        raise ValueError("commit_change requires a non-empty message")

    parent_hash = await _lock_and_read_ref(db, patient_id, branch_ref)
    parent_manifest = await _load_manifest(db, parent_hash) if parent_hash else {}

    # Apply changes to the parent manifest.
    new_manifest: dict[tuple[str, uuid.UUID], bytes] = dict(parent_manifest)
    entity_object_hashes: dict[tuple[str, uuid.UUID], bytes | None] = {}

    for change in changes:
        key = (change.entity_kind, change.entity_id)
        if change.payload is None:
            new_manifest.pop(key, None)
            entity_object_hashes[key] = None
        else:
            obj_hash = await _ensure_entity_object(
                db,
                entity_kind=change.entity_kind,
                schema_version=change.schema_version,
                payload=change.payload,
            )
            new_manifest[key] = obj_hash
            entity_object_hashes[key] = obj_hash

    # Build the canonical tree blob and pin it.
    tree_payload = _serialise_manifest(new_manifest)
    tree_hash = await _ensure_entity_object(
        db,
        entity_kind="_tree_",
        schema_version=1,
        payload=tree_payload,
    )

    # Build the canonical commit header. Hashing happens AFTER all the
    # parent / tree / author fields are fixed so the hash uniquely
    # identifies the commit's content.
    now = datetime.now(UTC)
    commit_header = {
        "parent_hashes": [_hex(p) for p in (parent_hash and [parent_hash]) or []],
        "tree_hash": _hex(tree_hash),
        "patient_id": str(patient_id),
        "author_subject_id": str(actor.subject_id) if actor.subject_id else None,
        "author_kind": actor.kind,
        "model_id": actor.model_id,
        "provider": actor.provider,
        "agent_token_id": str(actor.agent_token_id) if actor.agent_token_id else None,
        "branch_at_creation": branch_ref,
        "message": message,
        "created_at": now,
    }
    commit_hash = payload_hash(commit_header)

    # Insert commits row (idempotent on hash).
    insert_commit = (
        pg_insert(Commit)
        .values(
            commit_hash=commit_hash,
            patient_id=patient_id,
            tree_hash=tree_hash,
            parent_hashes=[parent_hash] if parent_hash else [],
            author_subject_id=actor.subject_id,
            author_kind=actor.kind,
            model_id=actor.model_id,
            provider=actor.provider,
            agent_token_id=actor.agent_token_id,
            agent_assistant_id=actor.agent_assistant_id,
            share_link_id=actor.share_link_id,
            branch_at_creation=branch_ref,
            message=message,
            created_at=now,
        )
        .on_conflict_do_nothing(index_elements=[Commit.commit_hash])
    )
    await db.execute(insert_commit)

    # Bulk insert manifest_entries for the new commit. We do NOT skip
    # if the commit already existed (idempotent path) because the
    # ON CONFLICT in the commit insert means we're either fresh or
    # the manifest is already there; ON CONFLICT DO NOTHING here
    # handles the latter.
    if new_manifest:
        manifest_rows = [
            {
                "commit_hash": commit_hash,
                "entity_kind": kind,
                "entity_id": eid,
                "object_hash": obj_hash,
            }
            for (kind, eid), obj_hash in new_manifest.items()
        ]
        await db.execute(
            pg_insert(ManifestEntry)
            .values(manifest_rows)
            .on_conflict_do_nothing(
                index_elements=[
                    ManifestEntry.commit_hash,
                    ManifestEntry.entity_kind,
                    ManifestEntry.entity_id,
                ]
            )
        )

    # Move the ref. Either UPDATE the existing row or INSERT if first
    # commit on this branch.
    if parent_hash is None:
        await db.execute(
            pg_insert(Ref)
            .values(
                patient_id=patient_id,
                ref_name=branch_ref,
                commit_hash=commit_hash,
                owner_subject_id=actor.subject_id,
                visibility=branch_visibility,
                is_locked=False,
            )
            .on_conflict_do_nothing(index_elements=[Ref.patient_id, Ref.ref_name])
        )
    else:
        await db.execute(
            text(
                "UPDATE refs SET commit_hash = :ch, updated_at = now() "
                "WHERE patient_id = :pid AND ref_name = :rn"
            ),
            {"ch": commit_hash, "pid": patient_id, "rn": branch_ref},
        )

    # Append to ref_log (audit / reflog). The first commit on a fresh
    # branch is always 'init'; otherwise the caller's op_kind decides
    # ('commit' for a normal write, 'revert' for an undo / restore).
    effective_op_kind: Literal["init", "commit", "merge", "reset", "revert", "rebase", "delete"] = (
        "init" if parent_hash is None else op_kind
    )
    await db.execute(
        pg_insert(RefLog).values(
            patient_id=patient_id,
            ref_name=branch_ref,
            from_commit=parent_hash,
            to_commit=commit_hash,
            op_kind=effective_op_kind,
            actor_subject_id=actor.subject_id,
            reason=message if op_kind == "revert" else None,
        )
    )

    return CommitResult(
        commit_hash=commit_hash,
        parent_hashes=[parent_hash] if parent_hash else [],
        tree_hash=tree_hash,
        entity_object_hashes=entity_object_hashes,
    )


async def read_at_commit(
    db: AsyncSession,
    *,
    commit_hash: bytes,
    entity_kind: str | None = None,
    entity_id: uuid.UUID | None = None,
) -> dict[tuple[str, uuid.UUID], dict]:
    """Materialise entity payloads as they existed at ``commit_hash``.

    With no filters, returns every entity in the commit's manifest
    (modulo tombstoned and ``_tree_`` blobs). Filters narrow to a
    kind, or to a single (kind, id) for cheap point lookup. Tombstoned
    payloads come back as ``{}`` with the row marker; the caller
    decides whether to surface "[redacted]" UI.
    """
    query = text(
        "SELECT me.entity_kind, me.entity_id, me.object_hash, "
        "eo.payload, eo.is_tombstoned, eo.storage_kind "
        "FROM manifest_entries me "
        "JOIN entity_objects eo ON eo.object_hash = me.object_hash "
        "WHERE me.commit_hash = :ch "
        "AND me.entity_kind != '_tree_' "
        + ("AND me.entity_kind = :kind " if entity_kind else "")
        + ("AND me.entity_id = :eid " if entity_id else "")
    )
    params: dict = {"ch": commit_hash}
    if entity_kind:
        params["kind"] = entity_kind
    if entity_id:
        params["eid"] = entity_id

    rows = (await db.execute(query, params)).all()
    out: dict[tuple[str, uuid.UUID], dict] = {}
    for r in rows:
        kind, eid, object_hash, payload, is_tombstoned, storage_kind = r
        if is_tombstoned:
            out[(kind, eid)] = {"_tombstoned": True}
            continue
        if storage_kind != "full":
            # ``delta`` (F12.6) and ``s3`` (F12.8) require the
            # ``read_object`` walk: delta walks back to a full snapshot
            # and decompresses, s3 downloads the canonical bytes from
            # the cold tier and parses them. Both paths preserve the
            # canonical-JSON round trip so the result is dict-equal to
            # the original payload.
            resolved = await read_object(db, object_hash)
            out[(kind, eid)] = resolved if resolved is not None else {}
            continue
        out[(kind, eid)] = payload or {}
    return out


async def diff_commits(
    db: AsyncSession,
    *,
    a_hash: bytes,
    b_hash: bytes,
) -> list[tuple[str, uuid.UUID, str, bytes | None, bytes | None]]:
    """Return the entities that differ between commits ``a`` and ``b``.

    Each row is ``(entity_kind, entity_id, change, hash_a, hash_b)``
    where ``change`` is one of ``added`` (only in b), ``removed``
    (only in a), ``modified`` (in both, different hash). Equal
    entries are omitted. Order is unspecified.
    """
    query = text(
        "WITH at_a AS ("
        "  SELECT entity_kind, entity_id, object_hash"
        "  FROM manifest_entries WHERE commit_hash = :a"
        "    AND entity_kind != '_tree_'"
        "), at_b AS ("
        "  SELECT entity_kind, entity_id, object_hash"
        "  FROM manifest_entries WHERE commit_hash = :b"
        "    AND entity_kind != '_tree_'"
        ")"
        "SELECT "
        "  COALESCE(b.entity_kind, a.entity_kind) AS kind,"
        "  COALESCE(b.entity_id, a.entity_id) AS eid,"
        "  CASE"
        "    WHEN a.object_hash IS NULL THEN 'added'"
        "    WHEN b.object_hash IS NULL THEN 'removed'"
        "    ELSE 'modified'"
        "  END AS change,"
        "  a.object_hash AS hash_a,"
        "  b.object_hash AS hash_b "
        "FROM at_a a FULL OUTER JOIN at_b b USING (entity_kind, entity_id) "
        "WHERE a.object_hash IS DISTINCT FROM b.object_hash"
    )
    rows = (await db.execute(query, {"a": a_hash, "b": b_hash})).all()
    return [(kind, eid, change, ha, hb) for (kind, eid, change, ha, hb) in rows]


async def ensure_main_seeded(
    db: AsyncSession,
    *,
    patient_id: uuid.UUID,
    actor: ActorContext | None = None,
) -> bytes:
    """Make sure ``main`` exists for this patient; seed it if missing.

    The seed is an empty-manifest commit authored by ``actor`` (or system
    if ``actor`` is None). It serves as the orphan root that consultation
    branches can fork from. Idempotent: if ``main`` already exists,
    returns the current head without modification.

    Returns the commit_hash that ``main`` points at.
    """
    head = (
        await db.execute(
            text("SELECT commit_hash FROM refs WHERE patient_id = :p AND ref_name = 'main'"),
            {"p": patient_id},
        )
    ).scalar_one_or_none()
    if head is not None:
        return head

    seed_actor = actor or ActorContext(subject_id=None, kind="system")
    result = await commit_change(
        db,
        patient_id=patient_id,
        branch_ref="main",
        actor=seed_actor,
        message="[init] patient main branch seeded",
        # Empty changes are not allowed by commit_change; we create a
        # placeholder _meta entity so the manifest has at least one row.
        # The placeholder is harmless and conventionally hidden in views.
        changes=[
            EntityChange(
                entity_kind="patient",
                entity_id=patient_id,
                payload={"id": str(patient_id), "schema_version": 1, "_seed": True},
            )
        ],
    )
    return result.commit_hash


async def open_consultation_branch(
    db: AsyncSession,
    *,
    patient_id: uuid.UUID,
    consultation_id: uuid.UUID,
    actor: ActorContext,
    base_ref: str = "main",
) -> str:
    """Materialise a ``consultation/<id>`` branch at the current ``base_ref`` head.

    Called when a Consultation is opened: a fresh branch is created so all
    writes during the consult can be tracked separately, and the eventual
    "submit" turns into a proposal fork → main without needing review of
    the writer's intermediate state.

    If ``main`` does not yet have any commit (patient never written to via
    the versioning service), :func:`ensure_main_seeded` is called first.

    Returns:
        The new branch ref name (``consultation/<consultation_id>``).
    """
    if base_ref == "main":
        base_head = await ensure_main_seeded(db, patient_id=patient_id, actor=actor)
    else:
        base_head_value = (
            await db.execute(
                text("SELECT commit_hash FROM refs WHERE patient_id = :p AND ref_name = :r"),
                {"p": patient_id, "r": base_ref},
            )
        ).scalar_one_or_none()
        if base_head_value is None:
            raise ValueError(
                f"cannot open consultation branch on patient {patient_id}: "
                f"base ref '{base_ref}' has no commit yet"
            )
        base_head = base_head_value

    branch_name = f"consultation/{consultation_id}"
    await db.execute(
        pg_insert(Ref)
        .values(
            patient_id=patient_id,
            ref_name=branch_name,
            commit_hash=base_head,
            owner_subject_id=actor.subject_id,
            visibility="private",
            is_locked=False,
        )
        .on_conflict_do_nothing(index_elements=[Ref.patient_id, Ref.ref_name])
    )
    await db.execute(
        pg_insert(RefLog).values(
            patient_id=patient_id,
            ref_name=branch_name,
            from_commit=None,
            to_commit=base_head,
            op_kind="init",
            actor_subject_id=actor.subject_id,
            reason=f"open consultation {consultation_id}",
        )
    )
    return branch_name


async def resolve_branch_for_write(
    db: AsyncSession,
    *,
    patient_id: uuid.UUID,
    user_subject_id: uuid.UUID,
    consultation_id: uuid.UUID | None,
    is_owner: bool,
) -> str:
    """Pick the branch a write should land on.

    Rules:
      - If a consultation_id is supplied AND its branch ref is owned by
        the same user and not locked, the write goes to that branch.
      - Otherwise, owner writes go to ``main`` directly.
      - Non-owners with no active consultation are rejected (raises
        :class:`PermissionError`); they must open a consultation first.

    The gating state lives on the ``refs`` row itself
    (``owner_subject_id`` + ``is_locked``): v3 folded the Consultation
    entity into ReportContent and dropped its table, so the branch ref
    is the only — and authoritative — record of who may write and
    whether the review closed the branch (merge / reject / withdraw all
    lock it). Looking the ref up by ``(patient_id, ref_name)`` also
    makes a cross-patient consultation id indistinguishable from a
    non-existent one (no existence oracle).

    The caller is expected to have already loaded the patient and
    determined ``is_owner``; this function handles the branch policy
    only, not the read-side authorization.
    """
    if consultation_id is not None:
        row = (
            await db.execute(
                text(
                    "SELECT owner_subject_id, is_locked FROM refs "
                    "WHERE patient_id = :p AND ref_name = :r"
                ),
                {"p": patient_id, "r": f"consultation/{consultation_id}"},
            )
        ).first()
        if row is None:
            raise ValueError(f"consultation {consultation_id} not found")
        branch_owner, is_locked = row
        if branch_owner != user_subject_id:
            raise PermissionError("only the consultation author can write on its branch")
        if is_locked:
            raise PermissionError(
                "consultation branch is locked (reviewed / rejected / withdrawn) "
                "and cannot accept new writes"
            )
        return f"consultation/{consultation_id}"

    if is_owner:
        return "main"

    raise PermissionError("non-owner writes require an active consultation; open one first")


async def submit_consultation_proposal(
    db: AsyncSession,
    *,
    patient_id: uuid.UUID,
    consultation_id: uuid.UUID,
    proposer_subject_id: uuid.UUID,
    title: str,
    description: str | None = None,
) -> uuid.UUID:
    """Create the ``proposals`` row representing a submitted consultation.

    Called when a consultation transitions to ``status='submitted'``. The
    source ref is the consultation's branch; the target is ``main``. The
    base commit is the LCA, which for fast-forward (target_head ==
    base) reduces to target_head; for divergent histories the three-way
    merge engine (F12.3) computes the actual LCA.

    Returns the new ``proposals.id``.
    """
    branch_name = f"consultation/{consultation_id}"
    source_head = (
        await db.execute(
            text("SELECT commit_hash FROM refs WHERE patient_id = :p AND ref_name = :r"),
            {"p": patient_id, "r": branch_name},
        )
    ).scalar_one_or_none()
    if source_head is None:
        raise ValueError(f"consultation {consultation_id} has no branch ref to submit")
    target_head = (
        await db.execute(
            text("SELECT commit_hash FROM refs WHERE patient_id = :p AND ref_name = 'main'"),
            {"p": patient_id},
        )
    ).scalar_one_or_none()
    if target_head is None:
        raise ValueError("patient has no main branch; cannot submit")

    # Compute base = LCA(source, target). Fast-forward path: source
    # descends linearly from target_head, base == target_head.
    # Divergent: base is the most recent common ancestor; conflicts
    # may exist and are pre-computed into ``merge_conflicts``.
    base_commit = await _compute_lca(db, a=source_head, b=target_head)

    detected: list[DetectedConflict] = []
    auto_resolved_hashes: dict[tuple[str, uuid.UUID], bytes] = {}
    if base_commit is not None and base_commit != target_head:
        # Divergent: pre-compute conflicts so the review UI has something
        # to render without recomputing on every load.
        detected = await detect_conflicts(
            db,
            base_commit=base_commit,
            source_commit=source_head,
            target_commit=target_head,
        )

        # Try to auto-merge edit_edit conflicts on textual fields. The
        # row is still inserted in merge_conflicts so the reviewer sees
        # what was merged, but ``resolution='auto_merge'`` and
        # ``resolved_object_hash`` are pre-set; no human input needed.
        for c in detected:
            if c.conflict_kind != "edit_edit":
                continue
            merged_hash = await _attempt_text_auto_merge(
                db,
                base_hash=c.base_hash,
                source_hash=c.source_hash,
                target_hash=c.target_hash,
                entity_kind=c.entity_kind,
            )
            if merged_hash is not None:
                auto_resolved_hashes[(c.entity_kind, c.entity_id)] = merged_hash

    # ``conflict_count`` is the number of rows that still need a human
    # decision. Auto-merged rows are excluded.
    conflict_count = sum(
        1 for c in detected if (c.entity_kind, c.entity_id) not in auto_resolved_hashes
    )

    proposal_id = uuid.uuid4()
    # No consultation_id column: the linkage to the consultation is the
    # ``consultation/<id>`` source ref name itself (v3 dropped the
    # Consultation table; the branch ref is the surviving record).
    await db.execute(
        text(
            "INSERT INTO proposals "
            "(id, patient_id, source_ref_name, "
            " target_ref_name, source_head_commit, target_head_commit, "
            " base_commit, proposer_subject_id, title, description, "
            " status, conflict_count) "
            "VALUES (:id, :pid, :src, :tgt, :sh, :th, :bc, :ps, :ti, "
            " :de, 'open', :cc)"
        ),
        {
            "id": proposal_id,
            "pid": patient_id,
            "src": branch_name,
            "tgt": "main",
            "sh": source_head,
            "th": target_head,
            "bc": base_commit,
            "ps": proposer_subject_id,
            "ti": title,
            "de": description,
            "cc": conflict_count,
        },
    )

    # Persist detected conflicts. Pre-resolve the auto-merged ones so
    # the reviewer doesn't have to touch them.
    for c in detected:
        merged_hash = auto_resolved_hashes.get((c.entity_kind, c.entity_id))
        if merged_hash is not None:
            await db.execute(
                text(
                    "INSERT INTO merge_conflicts "
                    "(proposal_id, entity_kind, entity_id, "
                    " base_object_hash, source_object_hash, target_object_hash, "
                    " conflict_kind, resolution, resolved_object_hash, "
                    " resolved_at) "
                    "VALUES (:pid, :ek, :ei, :bh, :sh, :th, :ck, "
                    "  'auto_merge', :rh, now())"
                ),
                {
                    "pid": proposal_id,
                    "ek": c.entity_kind,
                    "ei": c.entity_id,
                    "bh": c.base_hash,
                    "sh": c.source_hash,
                    "th": c.target_hash,
                    "ck": c.conflict_kind,
                    "rh": merged_hash,
                },
            )
        else:
            await db.execute(
                text(
                    "INSERT INTO merge_conflicts "
                    "(proposal_id, entity_kind, entity_id, "
                    " base_object_hash, source_object_hash, target_object_hash, "
                    " conflict_kind) "
                    "VALUES (:pid, :ek, :ei, :bh, :sh, :th, :ck)"
                ),
                {
                    "pid": proposal_id,
                    "ek": c.entity_kind,
                    "ei": c.entity_id,
                    "bh": c.base_hash,
                    "sh": c.source_hash,
                    "th": c.target_hash,
                    "ck": c.conflict_kind,
                },
            )
    return proposal_id


async def _attempt_text_auto_merge(
    db: AsyncSession,
    *,
    base_hash: bytes | None,
    source_hash: bytes | None,
    target_hash: bytes | None,
    entity_kind: str,
) -> bytes | None:
    """Attempt a three-way line-level merge of an entity's textual field.

    Returns the new ``object_hash`` of the merged payload, or ``None``
    when the merge cannot be performed safely. ``None`` cases:

      * ``entity_kind`` is not in :data:`TEXTUAL_FIELDS`;
      * any of base / source / target is missing or tombstoned;
      * the textual field is not a string in any side;
      * source and target disagree on a non-text field (we refuse to
        guess which side wins on metadata);
      * ``merge3`` produces a conflict region (overlapping edits).

    On success, the merged payload is the source payload with its
    textual field replaced by the merge3 output. Source is chosen as
    the skeleton because it carries the consultation's intent (e.g.
    pinned status, anchor) that the reviewer is being asked to accept;
    when it differs from target on metadata we already bailed.
    """
    text_field = TEXTUAL_FIELDS.get(entity_kind)
    if text_field is None:
        return None
    if base_hash is None or source_hash is None or target_hash is None:
        return None

    base_payload = await read_object(db, base_hash)
    source_payload = await read_object(db, source_hash)
    target_payload = await read_object(db, target_hash)

    if base_payload is None or source_payload is None or target_payload is None:
        return None
    if any(p.get("_tombstoned") for p in (base_payload, source_payload, target_payload)):
        return None

    base_text = base_payload.get(text_field)
    source_text = source_payload.get(text_field)
    target_text = target_payload.get(text_field)
    if not (
        isinstance(base_text, str) and isinstance(source_text, str) and isinstance(target_text, str)
    ):
        return None

    # Refuse if source and target disagree on any non-text field. We do
    # not attempt structural three-way merge of metadata in this iteration.
    other_keys = (set(source_payload.keys()) | set(target_payload.keys())) - {text_field}
    for k in other_keys:
        if source_payload.get(k) != target_payload.get(k):
            return None

    base_lines = base_text.splitlines(keepends=True)
    source_lines = source_text.splitlines(keepends=True)
    target_lines = target_text.splitlines(keepends=True)
    m = merge3.Merge3(base_lines, source_lines, target_lines)
    if any(g[0] == "conflict" for g in m.merge_groups()):
        return None

    merged_text = "".join(m.merge_lines())
    new_payload = dict(source_payload)
    new_payload[text_field] = merged_text

    schema_version_raw = new_payload.get("schema_version", 1)
    schema_version = schema_version_raw if isinstance(schema_version_raw, int) else 1
    return await _ensure_entity_object(
        db,
        entity_kind=entity_kind,
        schema_version=schema_version,
        payload=new_payload,
    )


@dataclass(slots=True)
class DetectedConflict:
    """One conflicting entity in a three-way merge.

    ``base_hash``/``source_hash``/``target_hash`` are the entity's
    object_hash at the LCA, on the source branch, and on the target
    branch respectively. Any of them may be None (a None hash means
    "the entity does not exist at that commit", surfaced for
    add_add / edit_delete / delete_edit kinds).
    """

    entity_kind: str
    entity_id: uuid.UUID
    base_hash: bytes | None
    source_hash: bytes | None
    target_hash: bytes | None
    conflict_kind: Literal["add_add", "edit_edit", "edit_delete", "delete_edit"]


@dataclass(slots=True)
class MergeResolution:
    """User (or server) decision on how to resolve a single conflict.

    ``kind='take_source'`` keeps the source branch's payload;
    ``'take_target'`` keeps the target branch's; ``'manual'`` uses a
    freshly-canonicalised payload supplied by the user (the resolved
    text in the case of clinical_note.body, or a take-and-edit on a
    structured payload); ``'auto_merge'`` is reserved for resolutions
    produced by the three-way text merge engine (``merge3``) at proposal
    submission time. ``three_way_merge`` treats ``manual`` and
    ``auto_merge`` identically (trust the supplied hash); the kind is
    preserved for audit / UX.
    """

    entity_kind: str
    entity_id: uuid.UUID
    kind: Literal["take_source", "take_target", "manual", "auto_merge"]
    resolved_object_hash: bytes


class ConflictsUnresolved(RuntimeError):
    """Raised by :func:`three_way_merge` when at least one conflict
    has no resolution. The caller should fetch the conflict list,
    show it to the user, then retry with resolutions.
    """

    def __init__(self, conflicts: list[DetectedConflict]) -> None:
        super().__init__(f"three-way merge has {len(conflicts)} unresolved conflicts")
        self.conflicts = conflicts


@dataclass(slots=True)
class RevertConflictEntry:
    """One entity that cannot be reverted cleanly.

    ``head_hash`` is what the entity currently is on the branch we are
    trying to revert on; ``target_hash`` is what it was at the commit
    we are trying to undo. They differ because at least one commit
    between target and HEAD touched the same entity.
    """

    entity_kind: str
    entity_id: uuid.UUID
    head_hash: bytes | None
    target_hash: bytes | None


class RevertConflict(RuntimeError):
    """Raised by :func:`revert_commit` when one or more entities have
    been modified between the target commit and the branch head. The
    caller should surface the list to the user and offer the per-entity
    :func:`restore_entity_at_commit` path as a workaround.
    """

    def __init__(self, conflicts: list[RevertConflictEntry]) -> None:
        super().__init__(
            f"revert blocked: {len(conflicts)} entities changed since the target commit"
        )
        self.conflicts = conflicts


async def detect_conflicts(
    db: AsyncSession,
    *,
    base_commit: bytes,
    source_commit: bytes,
    target_commit: bytes,
) -> list[DetectedConflict]:
    """Compute the conflict list for a three-way merge.

    For every entity that differs in source-vs-base and/or in
    target-vs-base, classify into one of four categories. Entities
    that changed only on one side (or identically on both) are not
    conflicts: they are merged automatically by :func:`three_way_merge`.

    Returns rows in deterministic order by ``(entity_kind, entity_id)``.
    """
    base_manifest = await _load_manifest(db, base_commit)
    source_manifest = await _load_manifest(db, source_commit)
    target_manifest = await _load_manifest(db, target_commit)

    # Set of entities that changed on either side wrt base.
    candidates: set[tuple[str, uuid.UUID]] = set()
    for k, h in source_manifest.items():
        if base_manifest.get(k) != h:
            candidates.add(k)
    for k, h in target_manifest.items():
        if base_manifest.get(k) != h:
            candidates.add(k)
    # Also entities removed wrt base on either side.
    for k in base_manifest.keys() - source_manifest.keys():
        candidates.add(k)
    for k in base_manifest.keys() - target_manifest.keys():
        candidates.add(k)

    conflicts: list[DetectedConflict] = []
    for key in sorted(candidates, key=lambda kv: (kv[0], str(kv[1]))):
        bh = base_manifest.get(key)
        sh = source_manifest.get(key)
        th = target_manifest.get(key)

        # Skip non-conflicts: only one side moved (or both moved to
        # the same hash). These don't need user input.
        source_changed = sh != bh
        target_changed = th != bh
        if not (source_changed and target_changed):
            continue
        if sh == th:
            continue  # both sides converged, no conflict

        # Classify
        if bh is None:
            kind: Literal["add_add", "edit_edit", "edit_delete", "delete_edit"] = "add_add"
        elif sh is None:
            kind = "delete_edit"
        elif th is None:
            kind = "edit_delete"
        else:
            kind = "edit_edit"
        conflicts.append(
            DetectedConflict(
                entity_kind=key[0],
                entity_id=key[1],
                base_hash=bh,
                source_hash=sh,
                target_hash=th,
                conflict_kind=kind,
            )
        )
    return conflicts


async def three_way_merge(
    db: AsyncSession,
    *,
    base_commit: bytes,
    source_commit: bytes,
    target_commit: bytes,
    target_ref_name: str,
    patient_id: uuid.UUID,
    actor: ActorContext,
    message: str,
    resolutions: list[MergeResolution] | None = None,
) -> bytes:
    """Three-way merge: produce a merge commit on ``target_ref_name``.

    Walks the entity sets of base/source/target. For every entity:

    * if no source-side change → keep target;
    * if no target-side change → take source;
    * if both moved to the same hash → take that hash;
    * otherwise → require a :class:`MergeResolution` for the entity.

    If any conflict lacks a resolution, raises :class:`ConflictsUnresolved`
    with the full list, so the UI can prompt the user once and retry
    with the answers.

    Idempotent on the commit hash: re-running with the same inputs
    produces the same merge commit (ON CONFLICT DO NOTHING on the
    commit insert).

    Returns the new commit's hash. Caller is responsible for
    ``db.commit()``.
    """
    base_manifest = await _load_manifest(db, base_commit)
    source_manifest = await _load_manifest(db, source_commit)
    target_manifest = await _load_manifest(db, target_commit)

    conflicts = await detect_conflicts(
        db,
        base_commit=base_commit,
        source_commit=source_commit,
        target_commit=target_commit,
    )
    resolutions_by_key = {(r.entity_kind, r.entity_id): r for r in (resolutions or [])}
    unresolved = [c for c in conflicts if (c.entity_kind, c.entity_id) not in resolutions_by_key]
    if unresolved:
        raise ConflictsUnresolved(unresolved)

    # Build the merged manifest.
    merged: dict[tuple[str, uuid.UUID], bytes] = {}
    all_keys = set(base_manifest) | set(source_manifest) | set(target_manifest)
    for key in all_keys:
        bh = base_manifest.get(key)
        sh = source_manifest.get(key)
        th = target_manifest.get(key)

        # Resolved conflict path
        if (key[0], key[1]) in resolutions_by_key:
            res = resolutions_by_key[(key[0], key[1])]
            # 'take_source'/'take_target'/'manual' all carry an
            # explicit resolved_object_hash; we trust the caller's
            # choice, modulo a sanity check that matches the kind.
            if res.kind == "take_source" and res.resolved_object_hash != sh:
                raise ValueError(f"resolution take_source for {key} must use the source hash")
            if res.kind == "take_target" and res.resolved_object_hash != th:
                raise ValueError(f"resolution take_target for {key} must use the target hash")
            if res.resolved_object_hash is not None:
                merged[key] = res.resolved_object_hash
            # If the resolution drops the entity (None hash) skip the assignment.
            continue

        # Non-conflict paths
        source_changed = sh != bh
        target_changed = th != bh
        if source_changed and target_changed:
            # We already required a resolution above; if we land here,
            # it means sh == th (converged) so just keep that.
            if sh is not None:
                merged[key] = sh
            continue
        if source_changed:
            if sh is not None:
                merged[key] = sh
            continue
        if target_changed:
            if th is not None:
                merged[key] = th
            continue
        # No change either side: keep base.
        if bh is not None:
            merged[key] = bh

    # Materialise the merge commit.
    tree_payload = _serialise_manifest(merged)
    tree_hash = await _ensure_entity_object(
        db,
        entity_kind="_tree_",
        schema_version=1,
        payload=tree_payload,
    )

    now = datetime.now(UTC)
    commit_header = {
        "parent_hashes": [_hex(target_commit), _hex(source_commit)],
        "tree_hash": _hex(tree_hash),
        "patient_id": str(patient_id),
        "author_subject_id": str(actor.subject_id) if actor.subject_id else None,
        "author_kind": actor.kind,
        "model_id": actor.model_id,
        "provider": actor.provider,
        "agent_token_id": str(actor.agent_token_id) if actor.agent_token_id else None,
        "branch_at_creation": target_ref_name,
        "message": message,
        "created_at": now,
        # Tag the merge so two parallel three-way merges with identical
        # inputs but different actor metadata don't collide on hash.
        "merge": True,
    }
    merge_hash = payload_hash(commit_header)

    await db.execute(
        pg_insert(Commit)
        .values(
            commit_hash=merge_hash,
            patient_id=patient_id,
            tree_hash=tree_hash,
            parent_hashes=[target_commit, source_commit],
            author_subject_id=actor.subject_id,
            author_kind=actor.kind,
            model_id=actor.model_id,
            provider=actor.provider,
            agent_token_id=actor.agent_token_id,
            agent_assistant_id=actor.agent_assistant_id,
            share_link_id=actor.share_link_id,
            branch_at_creation=target_ref_name,
            message=message,
            created_at=now,
        )
        .on_conflict_do_nothing(index_elements=[Commit.commit_hash])
    )

    if merged:
        manifest_rows = [
            {
                "commit_hash": merge_hash,
                "entity_kind": kind,
                "entity_id": eid,
                "object_hash": obj_hash,
            }
            for (kind, eid), obj_hash in merged.items()
        ]
        await db.execute(
            pg_insert(ManifestEntry)
            .values(manifest_rows)
            .on_conflict_do_nothing(
                index_elements=[
                    ManifestEntry.commit_hash,
                    ManifestEntry.entity_kind,
                    ManifestEntry.entity_id,
                ]
            )
        )

    # Move the target ref forward.
    await db.execute(
        text(
            "UPDATE refs SET commit_hash = :ch, updated_at = now() "
            "WHERE patient_id = :p AND ref_name = :r"
        ),
        {"ch": merge_hash, "p": patient_id, "r": target_ref_name},
    )
    await db.execute(
        pg_insert(RefLog).values(
            patient_id=patient_id,
            ref_name=target_ref_name,
            from_commit=target_commit,
            to_commit=merge_hash,
            op_kind="merge",
            actor_subject_id=actor.subject_id,
            reason=message,
        )
    )
    return merge_hash


async def fast_forward_merge(
    db: AsyncSession,
    *,
    proposal_id: uuid.UUID,
    reviewer_subject_id: uuid.UUID,
    review_notes: str | None = None,
) -> bytes:
    """Approve a proposal via fast-forward merge: move main to source head.

    Only applies when ``base_commit == target_head_commit`` at the moment
    of the merge (i.e. main has not diverged since the consultation was
    opened). Otherwise raises ``NotImplementedError`` and the caller must
    use the three-way merge engine (F12.3).

    Updates: refs(main) → source_head, ref_log(merge), proposal.status
    ('merged'), and the SOURCE ref is locked so the reviewed branch
    accepts no further writes (the post-review write freeze used to ride
    the dropped consultations.status; the ref lock is its v3+ form —
    new work means a new consultation branch).

    Returns the new merge_commit hash (== source_head for fast-forward).
    """
    row = (
        await db.execute(
            text(
                "SELECT patient_id, source_ref_name, target_ref_name, "
                "       source_head_commit, target_head_commit, "
                "       base_commit, status "
                "FROM proposals WHERE id = :p FOR UPDATE"
            ),
            {"p": proposal_id},
        )
    ).first()
    if row is None:
        raise ValueError(f"proposal {proposal_id} not found")
    patient_id, src_ref, tgt_ref, src_head, tgt_head, base, status_ = row
    if status_ != "open":
        raise ValueError(f"proposal already in status '{status_}'")
    # Re-read target head WITH a row lock (FOR UPDATE) so a concurrent
    # ``fast_forward_merge`` for a different proposal cannot read the
    # same head, both conclude "no divergence", and overwrite each
    # other's UPDATE on ``refs``. The proposal-row lock above only
    # serialises retries for the SAME proposal; the target ref must be
    # locked separately to serialise across proposals targeting the
    # same branch.
    current_target_head = (
        await db.execute(
            text("SELECT commit_hash FROM refs WHERE patient_id = :p AND ref_name = :r FOR UPDATE"),
            {"p": patient_id, "r": tgt_ref},
        )
    ).scalar_one_or_none()
    if current_target_head != tgt_head or base != tgt_head:
        raise NotImplementedError(
            "non-fast-forward merge requires the three-way merge engine "
            "(F12.3); main has moved since the proposal was opened"
        )

    # Fast-forward: move target ref to source head.
    await db.execute(
        text(
            "UPDATE refs SET commit_hash = :ch, updated_at = now() "
            "WHERE patient_id = :p AND ref_name = :r"
        ),
        {"ch": src_head, "p": patient_id, "r": tgt_ref},
    )
    await db.execute(
        pg_insert(RefLog).values(
            patient_id=patient_id,
            ref_name=tgt_ref,
            from_commit=tgt_head,
            to_commit=src_head,
            op_kind="merge",
            actor_subject_id=reviewer_subject_id,
            reason=f"fast-forward merge proposal {proposal_id}",
        )
    )
    await db.execute(
        text(
            "UPDATE proposals SET status='merged', merge_commit=:mc, "
            "  reviewed_by_subject_id=:rs, reviewed_at=now(), "
            "  review_decision='approve', review_notes=:rn, "
            "  closed_at=now(), updated_at=now() "
            "WHERE id = :p"
        ),
        {
            "mc": src_head,
            "rs": reviewer_subject_id,
            "rn": review_notes,
            "p": proposal_id,
        },
    )
    # Freeze the reviewed branch: resolve_branch_for_write refuses locked
    # refs, so no commit can land on a consultation after its merge. The
    # row stays for audit (mirrors the reject / withdraw endpoints).
    await db.execute(
        text(
            "UPDATE refs SET is_locked = true, updated_at = now() "
            "WHERE patient_id = :pid AND ref_name = :r"
        ),
        {"pid": patient_id, "r": src_ref},
    )
    return src_head


async def revert_commit(
    db: AsyncSession,
    *,
    patient_id: uuid.UUID,
    commit_to_revert: bytes,
    branch_ref: str,
    actor: ActorContext,
    message: str,
) -> CommitResult:
    """Append a commit on ``branch_ref`` that undoes ``commit_to_revert``.

    The reverse-effect is computed entity-by-entity from the target's
    parent: entities the target added are deleted, entities it modified
    are restored to the parent's payload, entities it deleted are
    re-added at the parent's payload.

    Strict semantics: if any entity affected by ``commit_to_revert`` has
    been touched between the target and the current head of the branch,
    the function raises :class:`RevertConflict` listing the offending
    entities. The caller can either offer the user the per-entity
    :func:`restore_entity_at_commit` path or refuse the action.

    Caller MUST be inside a DB transaction. The function locks the
    target ref, computes the inverse change set, and delegates to
    :func:`commit_change` with ``op_kind='revert'`` so the new commit
    appears in ``ref_log`` as a revert (not a regular commit).

    Raises:
        ValueError: ``commit_to_revert`` is the root, branch has no
            head, or the inverse turned out empty (target was a no-op).
        NotImplementedError: ``commit_to_revert`` is a merge commit
            (two parents); revert of merges is deferred to F12.3.
        RevertConflict: at least one affected entity diverged.
    """
    target_row = (
        await db.execute(
            text("SELECT patient_id, parent_hashes FROM commits WHERE commit_hash = :c"),
            {"c": commit_to_revert},
        )
    ).first()
    if target_row is None:
        raise ValueError(f"commit {commit_to_revert.hex()[:12]} not found")
    target_patient_id, target_parents = target_row
    if target_patient_id != patient_id:
        raise ValueError(
            f"commit {commit_to_revert.hex()[:12]} does not belong to patient {patient_id}"
        )
    parents = list(target_parents or [])
    if len(parents) == 0:
        raise ValueError("cannot revert the root commit (no parent state to restore)")
    if len(parents) > 1:
        raise NotImplementedError("revert of merge commits is not supported in this iteration")
    parent_commit = parents[0]

    # Lock the destination branch first so a concurrent revert cannot
    # advance the head between our preflight read and the commit_change
    # write that follows.
    head_hash = await _lock_and_read_ref(db, patient_id, branch_ref)
    if head_hash is None:
        raise ValueError(f"branch '{branch_ref}' has no head; cannot revert into it")

    target_manifest = await _load_manifest(db, commit_to_revert)
    parent_manifest = await _load_manifest(db, parent_commit)
    head_manifest = await _load_manifest(db, head_hash)

    affected: set[tuple[str, uuid.UUID]] = set(target_manifest.keys()) | set(parent_manifest.keys())

    conflicts: list[RevertConflictEntry] = []
    changes: list[EntityChange] = []
    for key in sorted(affected, key=lambda kv: (kv[0], str(kv[1]))):
        kind, eid = key
        target_h = target_manifest.get(key)
        parent_h = parent_manifest.get(key)
        head_h = head_manifest.get(key)

        # Entity unchanged by target → nothing to revert.
        if target_h == parent_h:
            continue

        # Conflict: head diverged from target's effect on this entity.
        # We expected head_h == target_h (no commit between target and
        # head touched this entity); if it differs, refuse.
        if head_h != target_h:
            conflicts.append(
                RevertConflictEntry(
                    entity_kind=kind,
                    entity_id=eid,
                    head_hash=head_h,
                    target_hash=target_h,
                )
            )
            continue

        # Apply inverse: head currently equals target's effect; replace
        # it with parent's value.
        if parent_h is None:
            # target added the entity → revert by deleting it.
            changes.append(EntityChange(entity_kind=kind, entity_id=eid, payload=None))
        else:
            payload = await read_object(db, parent_h)
            if payload is None:
                # Parent's object_hash points at a missing row. This is
                # a data-integrity anomaly; refuse loudly rather than
                # silently dropping the entity.
                raise RuntimeError(
                    f"parent object_hash {parent_h.hex()[:12]} for ({kind}, {eid}) is missing"
                )
            changes.append(EntityChange(entity_kind=kind, entity_id=eid, payload=payload))

    if conflicts:
        raise RevertConflict(conflicts)

    if not changes:
        raise ValueError("commit had no observable effect on the manifest; nothing to revert")

    return await commit_change(
        db,
        patient_id=patient_id,
        branch_ref=branch_ref,
        actor=actor,
        message=message,
        changes=changes,
        op_kind="revert",
    )


async def restore_entity_at_commit(
    db: AsyncSession,
    *,
    patient_id: uuid.UUID,
    source_commit: bytes,
    entity_kind: str,
    entity_id: uuid.UUID,
    branch_ref: str,
    actor: ActorContext,
    message: str,
) -> CommitResult:
    """Append a commit that sets one entity to its state at ``source_commit``.

    Granular complement of :func:`revert_commit`: the user picks a
    specific (entity_kind, entity_id) and a historical commit, and we
    create a new commit on the branch where only that entity is
    rolled back. Other entities are inherited from the current head.

    No conflict detection: this is the explicit "I know what I want"
    path. If the entity does not exist at ``source_commit``, the new
    commit deletes it from the branch (tombstone in the manifest).

    Caller MUST be inside a DB transaction. Records the new commit in
    ``ref_log`` with ``op_kind='revert'`` so audit shows it as an undo
    rather than a fresh write.

    Raises:
        ValueError: ``source_commit`` does not belong to ``patient_id``,
            or the resulting commit would be a no-op (the entity is
            already at the target hash on the branch head).
    """
    source_row = (
        await db.execute(
            text("SELECT patient_id FROM commits WHERE commit_hash = :c"),
            {"c": source_commit},
        )
    ).first()
    if source_row is None:
        raise ValueError(f"source commit {source_commit.hex()[:12]} not found")
    if source_row[0] != patient_id:
        raise ValueError(f"source commit does not belong to patient {patient_id}")

    head_hash = await _lock_and_read_ref(db, patient_id, branch_ref)
    if head_hash is None:
        raise ValueError(f"branch '{branch_ref}' has no head; cannot restore into it")

    source_manifest = await _load_manifest(db, source_commit)
    head_manifest = await _load_manifest(db, head_hash)
    key = (entity_kind, entity_id)
    target_h = source_manifest.get(key)
    head_h = head_manifest.get(key)

    if target_h == head_h:
        # No-op: entity is already at the requested state on the head.
        # Refusing keeps the audit trail clean; the user gets a clear
        # signal that the click had no effect.
        raise ValueError("entity is already at the requested historical state; nothing to restore")

    if target_h is None:
        # Entity did not exist at source_commit → revert means delete.
        change = EntityChange(entity_kind=entity_kind, entity_id=entity_id, payload=None)
    else:
        payload = await read_object(db, target_h)
        if payload is None:
            raise RuntimeError(
                f"source object_hash {target_h.hex()[:12]} for "
                f"({entity_kind}, {entity_id}) is missing"
            )
        change = EntityChange(entity_kind=entity_kind, entity_id=entity_id, payload=payload)

    return await commit_change(
        db,
        patient_id=patient_id,
        branch_ref=branch_ref,
        actor=actor,
        message=message,
        changes=[change],
        op_kind="revert",
    )


async def _compute_lca(
    db: AsyncSession, *, a: bytes, b: bytes, max_depth: int = 1000
) -> bytes | None:
    """Lowest common ancestor of two commits, or None if disjoint.

    Walks ancestors of ``a`` collecting hashes, then walks ancestors of
    ``b`` until it hits one in the set. Capped at ``max_depth`` per side
    to keep the worst-case bounded; for our typical ~200 commits/year
    per patient this is generous.
    """
    # Collect a's ancestry first (BFS)
    visited: set[bytes] = set()
    frontier: list[bytes] = [a]
    depth = 0
    while frontier and depth < max_depth:
        rows = (
            await db.execute(
                text("SELECT commit_hash, parent_hashes FROM commits WHERE commit_hash = ANY(:hs)"),
                {"hs": frontier},
            )
        ).all()
        next_frontier: list[bytes] = []
        for ch, parents in rows:
            if ch in visited:
                continue
            visited.add(ch)
            next_frontier.extend(parents or [])
        frontier = list(set(next_frontier) - visited)
        depth += 1
    # Walk b's ancestry, return first match.
    seen_b: set[bytes] = set()
    frontier = [b]
    depth = 0
    while frontier and depth < max_depth:
        rows = (
            await db.execute(
                text("SELECT commit_hash, parent_hashes FROM commits WHERE commit_hash = ANY(:hs)"),
                {"hs": frontier},
            )
        ).all()
        next_frontier: list[bytes] = []
        for ch, parents in rows:
            if ch in visited:
                return ch
            if ch in seen_b:
                continue
            seen_b.add(ch)
            next_frontier.extend(parents or [])
        frontier = list(set(next_frontier) - seen_b)
        depth += 1
    return None


async def read_object(db: AsyncSession, object_hash: bytes) -> dict | None:
    """Return the resolved payload for ``object_hash``, or None if missing.

    Tombstoned objects come back as ``{"_tombstoned": True}``. Three
    storage tiers are walked transparently:

      * ``storage_kind='full'`` returns ``payload`` (already canonical).
      * ``storage_kind='delta'`` walks back via ``delta_parent_hash`` to
        the closest full snapshot, decompressing each delta along the
        way (zlib with dictionary = parent payload). Chains are bounded
        by the pack worker policy, so worst-case cost is O(snapshot
        spacing) DB lookups + decompresses.
      * ``storage_kind='s3'`` (F12.8) downloads the canonical bytes
        from ``(s3_bucket, s3_key)`` and parses them locally. Tier-down
        is reserved for cold history, so this path costs one S3 round
        trip per read; the boto3 call is wrapped in ``asyncio.to_thread``
        so the event loop is not blocked.
    """
    chain: list[tuple[bytes, bytes]] = []  # (delta_bytes, current_hash)
    current = object_hash
    visited: set[bytes] = set()
    while True:
        if current in visited:
            raise RuntimeError(f"delta cycle detected at object_hash {current.hex()[:16]}")
        visited.add(current)
        row = (
            await db.execute(
                text(
                    "SELECT payload, is_tombstoned, storage_kind, "
                    "  delta_parent_hash, delta_bytes, "
                    "  s3_bucket, s3_key "
                    "FROM entity_objects WHERE object_hash = :h"
                ),
                {"h": current},
            )
        ).first()
        if row is None:
            return None
        (
            payload,
            is_tombstoned,
            storage_kind,
            parent_hash,
            delta_bytes,
            s3_bucket,
            s3_key,
        ) = row
        if is_tombstoned:
            return {"_tombstoned": True}
        if storage_kind == "full":
            base_payload = payload or {}
            break
        if storage_kind == "s3":
            if s3_bucket is None or s3_key is None:
                raise RuntimeError(f"s3 object_hash {current.hex()[:16]} missing bucket/key")
            base_bytes = await _read_s3_canonical(s3_bucket, s3_key)
            return _bytes_to_payload(_apply_delta_chain(base_bytes, chain))
        # storage_kind == 'delta'
        if parent_hash is None or delta_bytes is None:
            raise RuntimeError(f"delta object_hash {current.hex()[:16]} missing parent or bytes")
        chain.append((bytes(delta_bytes), current))
        current = bytes(parent_hash)

    # Apply deltas back down the chain. The first chain entry is the
    # latest delta (closest to the requested hash); we resolve oldest
    # first by walking in reverse.
    base_bytes = canonicalize(base_payload)
    return _bytes_to_payload(_apply_delta_chain(base_bytes, chain))


def _apply_delta_chain(base_bytes: bytes, chain: list[tuple[bytes, bytes]]) -> bytes:
    """Replay the delta chain (latest-first) onto ``base_bytes`` to
    materialise the canonical payload of the originally-requested hash.
    """
    out = base_bytes
    for delta_bytes, _ in reversed(chain):
        out = decode_delta_bytes(out, delta_bytes)
    return out


async def _read_s3_canonical(bucket: str, key: str) -> bytes:
    """Fetch the canonical payload bytes of an ``entity_objects`` row
    that has been moved to the S3 cold tier. Boto3 is synchronous, so
    we offload the round trip to a thread pool to keep the asyncio
    event loop responsive.
    """
    from bvphoenix.storage import get_s3_storage

    storage = get_s3_storage()
    return await asyncio.to_thread(storage.get_object_bytes, bucket=bucket, key=key)


# ---------------------------------------------------------------------------
# F12.6: pack worker support — delta encoding via zlib dictionary
# ---------------------------------------------------------------------------


def encode_delta_bytes(parent_canonical: bytes, current_canonical: bytes) -> bytes:
    """Delta-compress ``current_canonical`` using ``parent_canonical`` as
    a zlib dictionary. The result is a tight encoding that is small
    when the two payloads are similar (small edits), and degrades
    gracefully (close to standalone zlib output) when they aren't.

    zlib's preset dict is capped at 32 KiB; for parent payloads larger
    than that, only the trailing 32 KiB is used as dict context. In
    practice our canonical JSON payloads are well under this limit.
    """
    import zlib

    if len(parent_canonical) > 32768:
        parent_canonical = parent_canonical[-32768:]
    compressor = zlib.compressobj(level=9, zdict=parent_canonical)
    out = compressor.compress(current_canonical) + compressor.flush()
    return out


def decode_delta_bytes(parent_canonical: bytes, delta_bytes: bytes) -> bytes:
    """Inverse of :func:`encode_delta_bytes`."""
    import zlib

    if len(parent_canonical) > 32768:
        parent_canonical = parent_canonical[-32768:]
    decompressor = zlib.decompressobj(zdict=parent_canonical)
    out = decompressor.decompress(delta_bytes) + decompressor.flush()
    return out


def _bytes_to_payload(b: bytes) -> dict:
    """Parse canonical JSON bytes back to a Python dict. Used after
    walking a delta chain. Note: round-trip is bytes→dict, NOT
    bytes→original Python types (UUIDs, datetimes are strings here)."""
    import json

    return json.loads(b.decode("utf-8"))


async def pack_entity_objects(
    db: AsyncSession,
    *,
    entity_kind: str,
    entity_id: uuid.UUID,
    snapshot_every: int = 10,
    delta_threshold: float = 0.5,
) -> int:
    """Convert long chains of full entity_objects into delta-encoded ones.

    For the given (entity_kind, entity_id), order the rows chronologically.
    For each row that is not a "snapshot" (every ``snapshot_every``-th
    row remains full), compute a delta from the previous row's payload
    and replace the row in-place if the delta is smaller than
    ``delta_threshold * full_size``. Rows already in 'delta' form are
    skipped.

    Idempotent: re-running on a fully packed entity is a no-op.

    Returns the number of rows converted to delta form.
    """
    rows = (
        await db.execute(
            text(
                "SELECT eo.object_hash, eo.payload, eo.payload_size, "
                "  eo.storage_kind, me.commit_hash, c.created_at "
                "FROM entity_objects eo "
                "JOIN manifest_entries me ON me.object_hash = eo.object_hash "
                "JOIN commits c ON c.commit_hash = me.commit_hash "
                "WHERE me.entity_kind = :ek AND me.entity_id = :ei "
                "  AND eo.entity_kind = :ek "
                "  AND eo.is_tombstoned = false "
                "ORDER BY c.created_at ASC"
            ),
            {"ek": entity_kind, "ei": entity_id},
        )
    ).all()

    converted = 0
    last_full_payload_bytes: bytes | None = None
    last_full_index = -1
    for i, row in enumerate(rows):
        obj_hash, payload, _payload_size, storage_kind, _commit_hash, _ts = row
        if storage_kind != "full":
            # Already delta or unknown; chain is broken, skip until next full.
            last_full_payload_bytes = None
            last_full_index = i
            continue
        # Snapshots: every snapshot_every-th full row stays full.
        is_snapshot = (
            (i - last_full_index) % snapshot_every == 0 if last_full_index >= 0 else (i == 0)
        )
        if i == 0 or is_snapshot or last_full_payload_bytes is None:
            last_full_payload_bytes = canonicalize(payload or {})
            last_full_index = i
            continue

        current_bytes = canonicalize(payload or {})
        delta = encode_delta_bytes(last_full_payload_bytes, current_bytes)
        if len(delta) >= delta_threshold * len(current_bytes):
            # Delta not small enough; keep full and reset chain anchor.
            last_full_payload_bytes = current_bytes
            last_full_index = i
            continue

        # Replace the row in place: clear payload, set delta fields,
        # set storage_kind. The CHECK constraint enforces invariants.
        await db.execute(
            text(
                "UPDATE entity_objects SET storage_kind = 'delta', "
                "  payload = NULL, "
                "  delta_parent_hash = (SELECT object_hash FROM entity_objects "
                "                        WHERE object_hash = :ph LIMIT 1), "
                "  delta_bytes = :db "
                "WHERE object_hash = :h"
            ),
            {
                # The parent of a delta is the *full* row that anchors
                # this delta chain (the most recent snapshot before this
                # row). Storing it explicitly lets read_object walk back
                # in O(1) hops instead of recomputing the chain.
                "ph": rows[last_full_index][0],
                "db": delta,
                "h": obj_hash,
            },
        )
        converted += 1
        # Advance the conceptual cursor but keep the snapshot anchor.
    return converted


# ---------------------------------------------------------------------------
# F12.8: cold-tier worker — move old large entity_objects to S3
# ---------------------------------------------------------------------------


def _s3_key_for_object_hash(object_hash: bytes) -> str:
    """Sharded S3 key for a versioning object.

    Mirrors git's loose-object directory layout: the first two hex
    chars become a directory prefix so a single S3 listing never grows
    past ~ (2^256 / 256) objects, and listings under one prefix scale
    with usage instead of patient count.
    """
    h = object_hash.hex()
    return f"entity_objects/{h[:2]}/{h}"


async def tier_down_entity_objects(
    db: AsyncSession,
    *,
    min_payload_bytes: int = 16 * 1024,
    age_days: int = 365,
    batch_limit: int = 200,
    bucket: str | None = None,
) -> int:
    """Move cold ``storage_kind='full'`` rows to the S3 cold tier.

    Selection rules (intersection):

      * ``storage_kind = 'full'`` — delta and s3 rows are skipped;
      * ``is_tombstoned = false`` — GDPR-erased rows stay where they
        are (the row is preserved for hash stability, the payload was
        already zeroed);
      * ``payload_size >= min_payload_bytes`` — small payloads cost
        more in DB rows + S3 round trips than they save in storage;
      * ``created_at < now() - INTERVAL '<age_days> days'`` — keep
        recent history hot in Postgres so the live read path never
        round-trips S3.

    No reachability check is performed in this iteration: a row is
    eligible based on its absolute age, not on whether a current ref's
    HEAD still references it. The trade-off is that an old commit's
    occasional read may hit S3, which is the intended behaviour for
    cold tier; a future iteration can layer a reachability filter on
    top to keep the recent-DAG always inline.

    Returns the number of rows moved. The function processes up to
    ``batch_limit`` rows per call so a long backlog can be drained by
    repeated invocations without holding open transactions or saturating
    boto3 connections.

    Caller MUST be inside a DB transaction. The S3 uploads happen
    before the row update so a partial run leaves the bytes available
    on S3 (idempotent on the same object_hash). The DB UPDATE only
    flips storage_kind once the upload returned.
    """
    from bvphoenix.config import get_settings
    from bvphoenix.storage import default_put_extra_args, get_s3_storage

    settings = get_settings()
    target_bucket = bucket or settings.s3_bucket_versioning

    storage = get_s3_storage()
    storage.ensure_bucket(target_bucket)
    put_extra = default_put_extra_args(settings)

    rows = (
        await db.execute(
            text(
                "SELECT object_hash, entity_kind, payload "
                "FROM entity_objects "
                "WHERE storage_kind = 'full' "
                "  AND is_tombstoned = false "
                "  AND payload_size >= :min_bytes "
                "  AND created_at < now() - make_interval(days => :age_days) "
                "ORDER BY created_at ASC "
                "LIMIT :lim"
            ),
            {
                "min_bytes": min_payload_bytes,
                "age_days": age_days,
                "lim": batch_limit,
            },
        )
    ).all()

    moved = 0
    for row in rows:
        object_hash, _entity_kind, payload = row
        if payload is None:
            # Defensive: ck_storage_invariant guarantees payload IS NOT
            # NULL for storage_kind='full', so this is unreachable in a
            # consistent DB. Skip rather than crash the batch.
            continue
        canonical = canonicalize(payload)
        s3_key = _s3_key_for_object_hash(bytes(object_hash))
        # Sync boto3 inside an async function: offload to a worker
        # thread so we don't stall the event loop. Each upload is small
        # (canonical JSON), so the thread cost is dominated by network.
        await asyncio.to_thread(
            _upload_canonical_bytes,
            storage,
            target_bucket,
            s3_key,
            canonical,
            put_extra,
        )
        await db.execute(
            text(
                "UPDATE entity_objects SET "
                "  storage_kind = 's3', "
                "  payload = NULL, "
                "  s3_bucket = :bucket, "
                "  s3_key = :key "
                "WHERE object_hash = :h "
                "  AND storage_kind = 'full'"
            ),
            {
                "bucket": target_bucket,
                "key": s3_key,
                "h": object_hash,
            },
        )
        moved += 1
    return moved


def _upload_canonical_bytes(
    storage: object,
    bucket: str,
    key: str,
    canonical: bytes,
    put_extra: dict,
) -> None:
    """Synchronous helper used by ``tier_down_entity_objects`` via
    ``asyncio.to_thread``. Kept as a module-level function so the
    thread offload doesn't accidentally capture an asyncio loop.
    """
    # The S3Storage class doesn't expose put_extra on upload_bytes
    # directly (it uses a per-instance default), so call the raw client
    # with the explicit extra args. ``storage._client`` is private but
    # stable; this is the same pattern used elsewhere in storage/.
    s = storage  # type: ignore[assignment]
    client = s._client  # type: ignore[attr-defined]
    client.put_object(Bucket=bucket, Key=key, Body=canonical, **put_extra)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _lock_and_read_ref(
    db: AsyncSession, patient_id: uuid.UUID, branch_ref: str
) -> bytes | None:
    """SELECT FOR UPDATE on the ref row; return current head or None."""
    row = (
        await db.execute(
            text(
                "SELECT commit_hash, is_locked FROM refs "
                "WHERE patient_id = :pid AND ref_name = :rn "
                "FOR UPDATE"
            ),
            {"pid": patient_id, "rn": branch_ref},
        )
    ).first()
    if row is None:
        return None
    commit_hash, is_locked = row
    if is_locked:
        raise ValueError(f"branch '{branch_ref}' is locked for patient {patient_id}")
    return commit_hash


async def _load_manifest(
    db: AsyncSession, commit_hash: bytes
) -> dict[tuple[str, uuid.UUID], bytes]:
    """Return ``{(kind, id): object_hash}`` for every non-tree entry of commit."""
    rows = (
        await db.execute(
            text(
                "SELECT entity_kind, entity_id, object_hash "
                "FROM manifest_entries WHERE commit_hash = :ch "
                "AND entity_kind != '_tree_'"
            ),
            {"ch": commit_hash},
        )
    ).all()
    return {(kind, eid): ohash for (kind, eid, ohash) in rows}


async def _ensure_entity_object(
    db: AsyncSession,
    *,
    entity_kind: str,
    schema_version: int,
    payload: dict,
) -> bytes:
    """Insert ``payload`` into entity_objects if absent; return its hash.

    The hash is the sha256 of the canonicalised payload (RFC 8785). If
    a row with that hash exists we keep it; the ON CONFLICT ensures
    deduplication across commits / patients / branches.
    """
    canonical_bytes = canonicalize(payload)
    h = hashlib.sha256(canonical_bytes).digest()
    await db.execute(
        pg_insert(EntityObject)
        .values(
            object_hash=h,
            entity_kind=entity_kind,
            schema_version=schema_version,
            payload=payload,
            payload_size=len(canonical_bytes),
            storage_kind="full",
        )
        .on_conflict_do_nothing(index_elements=[EntityObject.object_hash])
    )
    return h


def _serialise_manifest(
    manifest: dict[tuple[str, uuid.UUID], bytes],
) -> dict:
    """Canonical-friendly form of a manifest, for the ``_tree_`` blob.

    The dict is sorted by (kind, id) when canonicalize is invoked, so
    the resulting hash is deterministic. We use lists of (kind, id_str,
    hash_hex) triples since canonical JSON requires string keys; a
    single nested dict keyed by a stringified tuple would also work
    but is harder to diff visually.
    """
    entries = [
        {
            "kind": kind,
            "id": str(eid),
            "object_hash": _hex(ohash),
        }
        for (kind, eid), ohash in sorted(manifest.items(), key=lambda kv: (kv[0][0], str(kv[0][1])))
    ]
    return {"entries": entries, "schema_version": 1}


def _hex(b: bytes) -> str:
    """Lower-case hex of raw bytes; used inside canonical payloads."""
    return b.hex()
