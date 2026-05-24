"""Tag management API.

Tag-first search (DESIGN.md §5) means every interactive surface — the
search bar, the filter chip palette, the auto-tag worker output — talks
to this router. Routes fall into three buckets:

* **Read / autocomplete** — ``GET /tags`` and ``GET /tags/tree`` power
  typeahead and the faceted nav; cheap, anonymous-friendly.
* **Write** — ``POST /tags`` (manual add) and ``DELETE /tags/{id}``
  (remove). Auth-required; write is gated on
  ``write:annotations`` over the parent study, same as free-text
  annotations.
* **Admin** — ``POST /tags/merge`` and ``POST /tags/alias`` let an
  operator collapse synonyms and rewrite mis-spelled values across
  every target. ``require_admin`` by design.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Literal

from arq.connections import create_pool
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy import or_ as sa_or
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.api._dry_run import dry_run_flag
from bvphoenix.auth import (
    enforce_agent_scope,
    optional_user,
    require_admin,
    require_user,
)
from bvphoenix.config import get_settings
from bvphoenix.db.models import (
    ImagingStudy,
    Instance,
    Patient,
    Series,
    Tag,
    TagAlias,
    User,
)
from bvphoenix.db.session import get_db
from bvphoenix.middleware.idempotency import IdempotencyContext, idempotent
from bvphoenix.middleware.problem_details import problem
from bvphoenix.services.arq_redis import redis_settings
from bvphoenix.services.permissions import (
    READ_METADATA,
    WRITE_ANNOTATIONS,
    can,
    visible_patients_filter,
)

router = APIRouter(prefix="/tags", tags=["tags"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class TagOut(BaseModel):
    id: str
    target_kind: str
    target_id: str
    namespace: str
    value: str
    source: str
    confidence: float | None
    created_by_subject_id: str | None
    created_at: str


class TagMatchOut(BaseModel):
    """Autocomplete hit — no per-target id, we return one row per
    ``(namespace, value)`` with a count of how many targets carry it."""

    namespace: str
    value: str
    count: int


class TagTreeNode(BaseModel):
    value: str
    count: int
    # Per-source breakdown of ``count``. Sums to ``count`` minus any
    # legacy rows whose source falls outside the known set; the UI
    # uses these to badge a chip's provenance (manual/auto/imported).
    manual_count: int = 0
    auto_count: int = 0
    imported_count: int = 0
    children: list[TagTreeNode] = []


class TagTreeOut(BaseModel):
    namespace: str
    roots: list[TagTreeNode]


class TagCreateIn(BaseModel):
    target_kind: Literal["study", "series", "instance", "dataset"]
    target_id: uuid.UUID
    namespace: str = Field(min_length=1, max_length=64)
    value: str = Field(min_length=1, max_length=255)


class TagMergeIn(BaseModel):
    """Collapse one tag row onto another. Every row with the same
    ``(namespace, value)`` as ``from_id``'s tag is rewritten to
    ``to_id``'s ``(namespace, value)``; the source side is then
    deleted."""

    from_id: uuid.UUID
    to_id: uuid.UUID


class TagAliasIn(BaseModel):
    namespace: str = Field(min_length=1, max_length=64)
    primary_value: str = Field(min_length=1, max_length=255)
    alias_values: list[str] = Field(min_length=1, max_length=64)


# Forward-ref resolution for the recursive TagTreeNode.
TagTreeNode.model_rebuild()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _resolve_study_for_target(
    db: AsyncSession, target_kind: str, target_id: uuid.UUID
) -> ImagingStudy | None:
    """Return the parent study for permission checks, or ``None`` for
    ``dataset`` targets (there is no per-study owner for those and
    dataset-scoped tags fall under admin policy)."""
    if target_kind == "study":
        return (
            await db.execute(select(ImagingStudy).where(ImagingStudy.id == target_id))
        ).scalar_one_or_none()
    if target_kind == "series":
        return (
            await db.execute(
                select(ImagingStudy)
                .join(Series, Series.study_id == ImagingStudy.id)
                .where(Series.id == target_id)
            )
        ).scalar_one_or_none()
    if target_kind == "instance":
        return (
            await db.execute(
                select(ImagingStudy)
                .join(Series, Series.study_id == ImagingStudy.id)
                .join(Instance, Instance.series_id == Series.id)
                .where(Instance.id == target_id)
            )
        ).scalar_one_or_none()
    return None


# ---------------------------------------------------------------------------
# Autocomplete / facet endpoints
# ---------------------------------------------------------------------------


@router.get("", response_model=list[TagMatchOut])
async def list_tags(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(optional_user)],
    namespace: str | None = Query(None, max_length=64),
    q: str | None = Query(
        None,
        max_length=128,
        description="Prefix match on ``value`` (case-insensitive).",
    ),
    limit: int = Query(20, ge=1, le=200),
) -> list[TagMatchOut]:
    """Autocomplete endpoint. Returns distinct ``(namespace, value)``
    pairs with their usage count, filtered to the patients the caller
    can see. Anonymous callers see only system-wide / public tags
    (those with ``patient_id IS NULL`` or owned by a public patient).

    Until v3.0.0-beta.5 this endpoint streamed every tag in the system
    in exchange for a fast GROUP BY, which leaked PHI-bearing values
    (``cognome_distretto_anno``) typed by another operator. The
    visibility filter against ``visible_patients_filter`` keeps the
    autocomplete shape but restricts the result set to tags the
    caller is already entitled to read.
    """
    visible = await visible_patients_filter(db, user)
    # ``visible`` is a SELECT on Patient (all columns). For an IN
    # subquery we need exactly one column — narrow it to Patient.id
    # before passing to ``in_``. The pre-fix form
    # ``visible.select()`` returned the full Patient row and Postgres
    # rejected with ``subquery has too many columns``.
    visible_ids = visible.with_only_columns(Patient.id).scalar_subquery()
    stmt = select(Tag.namespace, Tag.value, func.count(Tag.id).label("cnt")).where(
        sa_or(Tag.patient_id.is_(None), Tag.patient_id.in_(visible_ids)),
    )
    if namespace:
        stmt = stmt.where(Tag.namespace == namespace)
    if q:
        # Prefix match with ILIKE for case-insensitivity. Escape LIKE
        # wildcards so a user typing ``%`` doesn't widen the search.
        escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        stmt = stmt.where(Tag.value.ilike(f"{escaped}%", escape="\\"))
    stmt = (
        stmt.group_by(Tag.namespace, Tag.value)
        .order_by(func.count(Tag.id).desc(), Tag.value.asc())
        .limit(limit)
    )
    rows = (await db.execute(stmt)).all()
    return [TagMatchOut(namespace=ns, value=v, count=int(c)) for (ns, v, c) in rows]


@router.get("/tree", response_model=list[TagTreeOut])
async def tag_tree(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(optional_user)],
    namespace: str | None = Query(
        None,
        max_length=64,
        description="Restrict the tree to one namespace.",
    ),
) -> list[TagTreeOut]:
    """Grouped tag tree. Every distinct ``value`` is split on ``/`` to
    build a nested tree (e.g. ``lung`` → ``lung/upper-lobe``). Counts
    are per exact-value, not cumulative through the tree — the UI can
    aggregate if it wants a rollup. Filtered by visibility just like
    ``GET /api/tags`` so PHI-bearing values do not leak across patient
    boundaries.
    """
    # We pull a per-source breakdown so the UI can badge chips by
    # provenance. ``GROUP BY (namespace, value, source)`` then we
    # collapse rows in Python — keeps the SQL portable across drivers.
    visible = await visible_patients_filter(db, user)
    visible_ids = visible.with_only_columns(Patient.id).scalar_subquery()
    stmt = select(Tag.namespace, Tag.value, Tag.source, func.count(Tag.id)).where(
        sa_or(Tag.patient_id.is_(None), Tag.patient_id.in_(visible_ids)),
    )
    if namespace:
        stmt = stmt.where(Tag.namespace == namespace)
    stmt = stmt.group_by(Tag.namespace, Tag.value, Tag.source).order_by(Tag.namespace, Tag.value)
    rows = (await db.execute(stmt)).all()

    # Group by namespace, then by value: { ns: { val: {source: count} } }.
    ns_buckets: dict[str, dict[str, dict[str, int]]] = {}
    for ns, val, src, cnt in rows:
        ns_buckets.setdefault(ns, {}).setdefault(val, {})[src] = int(cnt)

    out: list[TagTreeOut] = []
    for ns, values in ns_buckets.items():
        # Build a map: full_path → node. Walk the slash-separated path,
        # create intermediate nodes (count 0) as needed.
        nodes: dict[str, TagTreeNode] = {}
        for full, src_counts in values.items():
            segments = full.split("/")
            path_so_far = ""
            parent: TagTreeNode | None = None
            for i, seg in enumerate(segments):
                path_so_far = f"{path_so_far}/{seg}" if path_so_far else seg
                node = nodes.get(path_so_far)
                if node is None:
                    node = TagTreeNode(value=path_so_far, count=0, children=[])
                    nodes[path_so_far] = node
                    if parent is not None:
                        parent.children.append(node)
                # Only the final segment gets the real count — the
                # intermediate levels are synthetic.
                if i == len(segments) - 1:
                    node.count = sum(src_counts.values())
                    node.manual_count = src_counts.get("manual", 0)
                    node.auto_count = src_counts.get("auto", 0)
                    node.imported_count = src_counts.get("imported", 0)
                parent = node
        roots = [n for k, n in nodes.items() if "/" not in k]
        out.append(TagTreeOut(namespace=ns, roots=roots))
    return out


# ---------------------------------------------------------------------------
# Mutations — add / remove a tag
# ---------------------------------------------------------------------------


@router.post("", response_model=TagOut, status_code=status.HTTP_201_CREATED)
async def create_tag(
    request: Request,
    body: TagCreateIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
) -> TagOut:
    """Add a manual tag. Idempotent via the ``(target_kind, target_id,
    namespace, value)`` unique constraint — re-posting the same tag
    returns the existing row (with ``source`` preserved) rather than a
    409, matching the UX of check-marking a chip twice.
    """
    enforce_agent_scope(request, "tags:write")
    # Permission check — dataset targets have no per-study owner, so
    # we require admin for those.
    patient_id_for_tag: uuid.UUID | None = None
    if body.target_kind == "dataset":
        if not user.is_admin:
            raise HTTPException(status_code=403, detail="dataset tags require admin")
    else:
        study = await _resolve_study_for_target(db, body.target_kind, body.target_id)
        if study is None:
            raise HTTPException(status_code=404, detail=f"{body.target_kind} not found")
        if not await can(db, user=user, action=WRITE_ANNOTATIONS, study=study):
            raise HTTPException(status_code=403, detail="cannot tag this resource")
        # Denormalise the patient pointer so the autocomplete /
        # tag-tree endpoints can filter visibility without a 3-level
        # join through study → series → instance on every read.
        patient_id_for_tag = study.patient_id

    is_agent = bool(getattr(request.state, "is_agent", False))
    agent_assistant_id_for_tag = (
        getattr(request.state, "agent_assistant_id", None) if is_agent else None
    )
    canonical_source = "agent" if is_agent else "manual"

    # Upsert by unique key. If an existing row covers the same
    # (target, ns, val) we promote / re-pin its source + author so the
    # latest writer's intent is recorded.
    existing = (
        await db.execute(
            select(Tag).where(
                Tag.target_kind == body.target_kind,
                Tag.target_id == body.target_id,
                Tag.namespace == body.namespace,
                Tag.value == body.value,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.source = canonical_source
        existing.confidence = None
        existing.created_by_subject_id = user.subject_id
        existing.agent_assistant_id = agent_assistant_id_for_tag
        if existing.patient_id is None:
            existing.patient_id = patient_id_for_tag
        await db.commit()
        await db.refresh(existing)
        return _to_out(existing)

    tag = Tag(
        target_kind=body.target_kind,
        target_id=body.target_id,
        patient_id=patient_id_for_tag,
        namespace=body.namespace,
        value=body.value,
        source=canonical_source,
        confidence=None,
        created_by_subject_id=user.subject_id,
        agent_assistant_id=agent_assistant_id_for_tag,
    )
    db.add(tag)
    await db.commit()
    await db.refresh(tag)
    return _to_out(tag)


@router.delete("/{tag_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_tag(
    request: Request,
    tag_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
) -> None:
    """Delete a tag. The caller needs ``write:annotations`` on the
    parent study; admins can always delete."""
    enforce_agent_scope(request, "tags:write")
    tag = (await db.execute(select(Tag).where(Tag.id == tag_id))).scalar_one_or_none()
    if tag is None:
        raise HTTPException(status_code=404, detail="tag not found")

    if tag.target_kind == "dataset":
        if not user.is_admin:
            raise HTTPException(status_code=403, detail="dataset tags require admin")
    else:
        study = await _resolve_study_for_target(db, tag.target_kind, tag.target_id)
        if study is None:
            # Orphan tag — only admin can clean up.
            if not user.is_admin:
                raise HTTPException(status_code=404, detail="tag not found")
        elif not await can(db, user=user, action=WRITE_ANNOTATIONS, study=study):
            raise HTTPException(status_code=403, detail="cannot delete this tag")

    await db.delete(tag)
    await db.commit()


# ---------------------------------------------------------------------------
# Enqueue an auto-tag pass
# ---------------------------------------------------------------------------


@router.post("/autotag/{target_kind}/{target_id}", status_code=status.HTTP_202_ACCEPTED)
async def enqueue_autotag(
    request: Request,
    target_kind: Literal["study", "series", "instance"],
    target_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
) -> dict:
    """Queue the background ``autotag_target`` worker for this resource.

    The caller needs ``write:annotations`` on the parent study — same
    gate as the manual POST route, because every successful run can
    append ``source='auto'`` tags that then show up in the UI.
    """
    enforce_agent_scope(request, "tags:write")
    study = await _resolve_study_for_target(db, target_kind, target_id)
    if study is None:
        raise HTTPException(status_code=404, detail=f"{target_kind} not found")
    if not await can(db, user=user, action=WRITE_ANNOTATIONS, study=study):
        raise HTTPException(status_code=403, detail="cannot autotag this resource")

    settings = get_settings()
    redis = await create_pool(redis_settings(settings.redis_url))
    try:
        await redis.enqueue_job("autotag_target", target_kind, str(target_id))
    finally:
        await redis.close()
    return {
        "status": "enqueued",
        "target_kind": target_kind,
        "target_id": str(target_id),
    }


# ---------------------------------------------------------------------------
# Admin endpoints — merge + alias
# ---------------------------------------------------------------------------


@router.post("/merge", status_code=status.HTTP_200_OK)
async def merge_tags(
    body: TagMergeIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    _admin: Annotated[User, Depends(require_admin)],
) -> dict:
    """Collapse the ``(namespace, value)`` of ``from_id`` onto that of
    ``to_id``. Every matching tag row across every target is rewritten
    in-place, conflicts (same target already carries the destination
    tag) are deleted rather than duplicated, then the original tag row
    is removed. Returns counts so the operator can confirm."""
    src = (await db.execute(select(Tag).where(Tag.id == body.from_id))).scalar_one_or_none()
    dst = (await db.execute(select(Tag).where(Tag.id == body.to_id))).scalar_one_or_none()
    if src is None or dst is None:
        raise HTTPException(status_code=404, detail="tag not found")
    if src.id == dst.id:
        return {"moved": 0, "deleted": 0, "status": "noop"}

    # Rows sharing the source (ns, val) are rewritten to dst's pair,
    # except where the target already carries dst — those collisions
    # would violate the unique constraint so we drop the source row.
    src_rows = (
        (
            await db.execute(
                select(Tag).where(Tag.namespace == src.namespace, Tag.value == src.value)
            )
        )
        .scalars()
        .all()
    )

    moved = 0
    deleted = 0
    for row in src_rows:
        already_has_dst = (
            await db.execute(
                select(Tag.id).where(
                    Tag.target_kind == row.target_kind,
                    Tag.target_id == row.target_id,
                    Tag.namespace == dst.namespace,
                    Tag.value == dst.value,
                )
            )
        ).first()
        if already_has_dst is not None:
            await db.delete(row)
            deleted += 1
        else:
            row.namespace = dst.namespace
            row.value = dst.value
            moved += 1

    await db.commit()
    return {
        "moved": moved,
        "deleted": deleted,
        "from": {"namespace": src.namespace, "value": src.value},
        "to": {"namespace": dst.namespace, "value": dst.value},
    }


@router.post("/alias", status_code=status.HTTP_201_CREATED)
async def create_aliases(
    body: TagAliasIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    _admin: Annotated[User, Depends(require_admin)],
) -> dict:
    """Register one or more synonyms for a canonical tag. Search / UI
    layers query ``tag_aliases`` to rewrite user-typed values to their
    canonical form before issuing the tag filter. Idempotent per the
    ``(namespace, alias_value)`` unique constraint — re-posting does
    not duplicate rows."""
    # Drop any alias that happens to equal the primary — that pair is
    # trivially self-aliased and would only clutter the table.
    distinct_aliases = {a.strip() for a in body.alias_values if a.strip()}
    distinct_aliases.discard(body.primary_value)
    inserted = 0
    for alias in distinct_aliases:
        existing = (
            await db.execute(
                select(TagAlias).where(
                    TagAlias.namespace == body.namespace,
                    TagAlias.alias_value == alias,
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            db.add(
                TagAlias(
                    namespace=body.namespace,
                    primary_value=body.primary_value,
                    alias_value=alias,
                )
            )
            inserted += 1
        elif existing.primary_value != body.primary_value:
            # Re-pointing an alias is legitimate — typo cleanup — but
            # surface the change in the response.
            existing.primary_value = body.primary_value
    await db.commit()
    return {
        "namespace": body.namespace,
        "primary_value": body.primary_value,
        "aliases": sorted(distinct_aliases),
        "inserted": inserted,
    }


# ---------------------------------------------------------------------------
# Read helpers
# ---------------------------------------------------------------------------


@router.get("/for-target", response_model=list[TagOut])
async def list_tags_for_target(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(optional_user)],
    target_kind: Literal["study", "series", "instance", "dataset"] = Query(...),
    target_id: uuid.UUID = Query(...),
) -> list[TagOut]:
    """Return every tag attached to a single target, ordered manual →
    auto so the UI can emphasise human-curated entries."""
    if target_kind != "dataset":
        study = await _resolve_study_for_target(db, target_kind, target_id)
        if study is None:
            raise HTTPException(status_code=404, detail=f"{target_kind} not found")
        if not await can(db, user=user, action=READ_METADATA, study=study):
            raise HTTPException(status_code=404, detail=f"{target_kind} not found")
    # ``manual`` sorts lexically *after* ``auto``/``imported``, so
    # DESC here puts human-curated rows first.
    rows = (
        (
            await db.execute(
                select(Tag)
                .where(Tag.target_kind == target_kind, Tag.target_id == target_id)
                .order_by(Tag.source.desc(), Tag.namespace.asc(), Tag.value.asc())
            )
        )
        .scalars()
        .all()
    )
    return [_to_out(r) for r in rows]


def _to_out(tag: Tag) -> TagOut:
    return TagOut(
        id=str(tag.id),
        target_kind=tag.target_kind,
        target_id=str(tag.target_id),
        namespace=tag.namespace,
        value=tag.value,
        source=tag.source,
        confidence=tag.confidence,
        created_by_subject_id=(
            str(tag.created_by_subject_id) if tag.created_by_subject_id else None
        ),
        created_at=tag.created_at.isoformat(),
    )


# ---------------------------------------------------------------------------
# Sprint 3.5: bulk tag operations (agent + human)
# ---------------------------------------------------------------------------


class TagSpecIn(BaseModel):
    namespace: str = Field(min_length=1, max_length=64)
    value: str = Field(min_length=1, max_length=255)


class StudyTagsBulkIn(BaseModel):
    """Bulk write of tags on a single study (Sprint 3.5).

    ``mode`` controls the semantics:

    * ``add`` — every entry is upserted (manual source). No-op for
      duplicates. Existing tags outside the manifest are untouched.
    * ``replace`` — the study's manual tags are *aligned* to the
      manifest: tags in the manifest are upserted, tags currently
      manual but missing from the manifest are deleted.
    * ``remove`` — every entry is removed (no-op for missing).

    Auto / imported tags (``source`` other than ``manual``) are never
    deleted by this endpoint; the autotag worker owns that namespace.
    """

    items: list[TagSpecIn] = Field(min_length=0, max_length=200)
    mode: Literal["add", "replace", "remove"] = "add"


class StudyTagsBulkOut(BaseModel):
    study_id: str
    mode: str
    n_added: int
    n_removed: int
    n_unchanged: int
    diff: dict


router_writes = APIRouter(tags=["tags"])


@router_writes.patch(
    "/studies/{study_id}/tags",
    response_model=StudyTagsBulkOut,
)
async def bulk_update_study_tags(
    request: Request,
    study_id: uuid.UUID,
    body: StudyTagsBulkIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    idem: Annotated[IdempotencyContext, Depends(idempotent)],
    dry_run: Annotated[bool, Depends(dry_run_flag)] = False,
):
    """Apply a tag manifest to a study (add / replace / remove).

    Sprint 3.5 contract: Idempotency-Key replay, ``?dry_run=true``
    returns the diff without committing, errors as Problem Details.
    Permission gate: ``WRITE_ANNOTATIONS`` over the study (mirrors
    the single-tag POST endpoint).
    """
    enforce_agent_scope(request, "tags:write")
    if idem.replay is not None:
        return idem.replay  # type: ignore[return-value]

    # Row-level lock on the study so concurrent tag writes serialise.
    # Without ``FOR UPDATE`` the classic lost-update race fires under
    # multi-agent traffic: two replace-mode calls race on the same
    # study, both SELECT the same existing tag set, both compute a
    # diff against the now-stale snapshot, and the second commit
    # silently undoes the first agent's adds (or, with the unique
    # constraint in play, surfaces as a generic IntegrityError 500
    # instead of an actionable 409). The lock window is microseconds —
    # the manifest application below is a handful of INSERTs / DELETEs
    # on a small set of tag rows.
    study = (
        await db.execute(select(ImagingStudy).where(ImagingStudy.id == study_id).with_for_update())
    ).scalar_one_or_none()
    if study is None:
        raise problem(404, "not_found", "study not found")
    if not await can(db, user=user, action=WRITE_ANNOTATIONS, study=study):
        raise problem(403, "forbidden", "cannot tag this study")

    requested = {(it.namespace, it.value) for it in body.items}

    existing_rows = (
        (
            await db.execute(
                select(Tag).where(
                    Tag.target_kind == "study",
                    Tag.target_id == study.id,
                )
            )
        )
        .scalars()
        .all()
    )
    existing_manual = {(t.namespace, t.value) for t in existing_rows if t.source == "manual"}
    existing_all = {(t.namespace, t.value): t for t in existing_rows}

    if body.mode == "add":
        to_add = sorted(requested - {(t.namespace, t.value) for t in existing_rows})
        to_remove: list[tuple[str, str]] = []
    elif body.mode == "replace":
        to_add = sorted(requested - {(t.namespace, t.value) for t in existing_rows})
        to_remove = sorted(existing_manual - requested)
    else:  # remove
        to_add = []
        to_remove = sorted(requested & existing_manual)

    diff = {
        "added": [{"namespace": ns, "value": v} for (ns, v) in to_add],
        "removed": [{"namespace": ns, "value": v} for (ns, v) in to_remove],
    }

    if dry_run:
        # Counters mirror what a real apply would do: derive from the
        # diff lengths so dry_run and apply share the same arithmetic.
        # Pre-fix the dry_run path returned ``n_added=0`` even when
        # ``diff.added`` had entries, which is the "the preview lies"
        # bug the internal report flagged.
        return idem.capture(  # type: ignore[return-value]
            StudyTagsBulkOut(
                study_id=str(study.id),
                mode=body.mode,
                n_added=len(to_add),
                n_removed=len(to_remove),
                n_unchanged=len(existing_rows) - len(to_remove),
                diff=diff,
            ).model_dump(),
            status_code=200,
        )

    n_added = 0
    n_removed = 0

    for ns, val in to_add:
        existing = existing_all.get((ns, val))
        if existing is not None:
            # Promote auto -> manual (same logic as POST /tags).
            if existing.source != "manual":
                existing.source = "manual"
                existing.confidence = None
                existing.created_by_subject_id = user.subject_id
                n_added += 1
        else:
            db.add(
                Tag(
                    target_kind="study",
                    target_id=study.id,
                    namespace=ns,
                    value=val,
                    source="manual",
                    created_by_subject_id=user.subject_id,
                )
            )
            n_added += 1

    for ns, val in to_remove:
        row = existing_all.get((ns, val))
        if row is not None and row.source == "manual":
            await db.delete(row)
            n_removed += 1

    await db.commit()

    n_unchanged = len(existing_rows) - n_removed
    return idem.capture(  # type: ignore[return-value]
        StudyTagsBulkOut(
            study_id=str(study.id),
            mode=body.mode,
            n_added=n_added,
            n_removed=n_removed,
            n_unchanged=n_unchanged,
            diff=diff,
        ).model_dump(),
        status_code=200,
    )
