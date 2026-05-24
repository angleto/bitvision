"""Patient tree navigation — Google Drive-style hierarchy over a fascicolo.

A folder is "in" a patient's tree when it contains at least one
:class:`FolderItem` referencing a patient-owned resource (study, document)
or — once F1 extends the polymorphic ``resource_kind`` — a direct
``patient`` marker. Items not filed into any folder live at the root ``/``.

If F1's polymorphic extension hasn't landed yet, inserts with an
unsupported ``resource_kind`` fail on the DB check constraint and the
endpoint returns 404/409 instead of 500.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import and_, delete, func, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.auth import enforce_agent_patient_scope, optional_user, require_user
from bvphoenix.db.models import (
    Document,
    Folder,
    FolderItem,
    ImagingStudy,
    Patient,
    Series,
    User,
)
from bvphoenix.db.session import get_db
from bvphoenix.middleware.audit_dependency import AuditDep
from bvphoenix.services.permissions import (
    DELETE,
    READ_METADATA,
    can_patient,
    effective_permissions_on_patient,
)

router = APIRouter(tags=["patient-tree"])


ROOT_LABEL = "Fascicolo"
PATIENT_MARKER_KIND = "patient"
MOVABLE_KINDS: frozenset[str] = frozenset(
    {"folder", "study", "series", "document", "report", "consultation"}
)


# ---- Pydantic schemas ----


class BreadcrumbEntry(BaseModel):
    id: str | None
    name: str
    # Cumulative path from the patient root to this segment. ``"/"`` for
    # the root entry, ``"/A/B"`` for a nested folder. Lets the frontend
    # navigate by clicking any breadcrumb segment without rebuilding
    # paths from segment names (which it would have to do twice — once
    # to render and once on click — and would mis-handle slashes / spaces).
    path: str = "/"


class FolderPreviewEntry(BaseModel):
    """One peek-item in a folder's transparent glimpse.

    The frontend grid renders 2-3 of these stacked behind the folder
    icon to convey "what's inside" without entering the folder. Light:
    just enough to drive a stack visualisation, not the full payload.
    """

    type: str
    name: str
    modality: str | None = None
    thumbnail_series_id: str | None = None
    # For document entries: the underlying document UUID and its MIME
    # type, so the grid can fetch the rendered thumbnail (PDF first
    # page / downscaled image) instead of falling back to a generic
    # icon that hides the studies stacked beneath it.
    target_id: str | None = None
    mime_type: str | None = None
    # When the preview entry was promoted from a sub-folder (recursive
    # preview) the chain of intermediate folder names is captured here
    # so the UI can show "TAC › studio.dcm". ``None`` for direct
    # children of the parent folder.
    via_folder_path: list[str] | None = None
    # Folder-typed entries only — total descendants in that sub-folder
    # so the tile can render "TAC (4)" instead of just "TAC".
    folder_descendant_count: int | None = None


class TreeNode(BaseModel):
    type: str
    id: str
    name: str
    modality: str | None = None
    document_type: str | None = None
    item_count: int | None = None
    created_at: str | None = None
    # Last server-side update of the underlying resource. Populated
    # for folders, studies, documents (DB-backed) and consultations
    # (already had updated_at). Null for series/reports/annotations
    # whose models don't track it. Drives the "Modifica" sort mode
    # and the secondary date stamp on each fascicolo card.
    updated_at: str | None = None
    # Frontend-friendly aliases. ``target_id`` mirrors ``id`` for non-folder
    # leaves so the client can distinguish navigable folders (no target_id)
    # from openable resources. ``date`` is the canonical "when" the user
    # cares about (study_date / document_date / created_at fallback).
    # ``path`` is populated for folder nodes so the breadcrumb-driven
    # navigation in the frontend keeps working without an extra round-trip
    # to the server.
    path: str | None = None
    parent_path: str | None = None
    target_id: str | None = None
    date: str | None = None
    size_bytes: int | None = None
    mime_type: str | None = None
    # Set on study nodes when a thumbnail can be served. The frontend
    # composes ``${API}/api/series/${thumbnail_series_id}/thumbnail``;
    # we don't bake the URL here so the auth header can be added at
    # fetch time.
    thumbnail_series_id: str | None = None
    # Per-card aggregates rendered under the title:
    #   * On study nodes: ``series_count`` = number of series in the
    #     study; ``instance_count`` = total received instances summed
    #     across those series.
    #   * On series nodes: ``instance_count`` = received instances
    #     for that one series.
    # Both are optional — folder/document/report/etc. nodes leave them
    # null. Computed in ``_attach_study_thumbnails`` for studies and
    # set inline in ``_series_node`` for series.
    series_count: int | None = None
    instance_count: int | None = None
    # First ~3 representative children of a folder, used by the grid
    # view to show a transparent stack of mini-tiles behind the folder
    # icon. ``None`` for non-folder nodes; empty list for empty folders.
    # Always populated alongside ``item_count`` so the renderer can
    # decide between "fan out" vs "single-tile" layouts without a
    # second round-trip.
    preview: list[FolderPreviewEntry] | None = None
    # Per-kind breakdown of the children directly inside this folder
    # (``study``, ``report``, ``document``, ``consultation``, ...).
    # Lets the grid render a kind-dominant icon ("studio + referto",
    # "documento", ...) without re-walking ``preview``. Subfolder
    # entries from ``folder_items`` are excluded here for parity with
    # ``item_count``.
    #
    # Note: this counts only the kinds visible in ``preview`` (capped
    # at 8 by the row-numbered query that builds the stack). For the
    # full per-kind breakdown rendered in the hover tooltip use
    # ``kind_counts`` instead.
    preview_kinds: dict[str, int] | None = None
    # Full per-kind aggregate of every child of this folder, ignoring
    # the preview-stack cap. Mirrors ``item_count`` but split by kind.
    # Folder-only; null for non-folder nodes. Used by the grid hover
    # / long-press preview to render lines like "3 studi · 2 PDF · 1
    # nota". Subfolder markers (``resource_kind = 'folder'``) are
    # excluded so the totals match what the grid actually shows.
    kind_counts: dict[str, int] | None = None
    # Recursive per-kind aggregate: every entity reachable from this
    # folder *through* its sub-folder tree (depth capped at
    # _RECURSIVE_DEPTH). Includes a ``"folder"`` entry counting the
    # nested sub-folders themselves. The grid renders this when
    # populated so a parent folder like ``2024/`` shows the studies
    # buried in ``2024/TAC/`` etc., not just its direct children.
    recursive_kind_counts: dict[str, int] | None = None
    # Recursive smart pairing — number of studies whose canonical
    # ``DocumentStudyLink.report_of`` is also under this folder.
    # Lets the UI label "3 esami refertati" instead of summing
    # studies + referti separately. Folder-only; null when no pairs
    # exist below.
    paired_study_report_count: int | None = None
    # Folder-only: short description rendered in the grid hover
    # preview (FE renders Markdown). Capped at 500 chars in the API
    # layer; for longer commentary see ``narrative_md`` below.
    description: str | None = None
    # Folder-only: extended Markdown clinical commentary, no length
    # cap. Rendered in the folder detail panel (not in the compact
    # hover tooltip).
    narrative_md: str | None = None
    # Folder-only: editable clinical / display date the folder
    # represents in the patient timeline (distinct from
    # ``created_at`` system audit). Null = the FE falls back to
    # ``created_at`` for the displayed date.
    clinical_date: str | None = None
    # Document-only: number of folders that contain this document
    # (hardlink count). Always ≥ 1 for live documents post-0088.
    # Surfaced here so the grid card can render the chain-link badge
    # when the count is ≥ 2 (reuses ``HardlinkBadge`` in
    # ``ContentPane.tsx``).
    folder_count: int | None = None
    # Document-only: true iff the only containment is the materialised
    # patient root.
    is_in_root_only: bool | None = None
    # Study-only: provenance / license for OpenData public-dataset imports
    # (tier=t4). Populated by ``_study_node`` so the FE can render the
    # LicenseBadge directly on the study card without an extra round-trip
    # per card. NULL on every user-uploaded private study.
    source_collection: str | None = None
    license_spdx: str | None = None
    license_url: str | None = None
    citation_required: bool | None = None
    citation_text: str | None = None
    # Pathology-slide only: stain + magnification render as chips on
    # the card. ``source_format`` distinguishes SVS / NDPI / OME-TIFF
    # so the (future, Step 2) viewer can pick the right reader. All
    # NULL on non-pathology nodes.
    stain: str | None = None
    magnification: float | None = None
    source_format: str | None = None
    has_macro: bool | None = None


class TreeOut(BaseModel):
    patient_id: str
    path: str
    folder_id: str | None
    breadcrumb: list[BreadcrumbEntry]
    nodes: list[TreeNode]
    total: int
    # Folder being listed, surfaced as a TreeNode so the FE can render
    # description / narrative_md / clinical_date in a header strip
    # without a second round-trip. Null at the patient root (no folder
    # context) and for shared-link / anonymous callers (which always
    # land on the flat root).
    current_folder: TreeNode | None = None


class MoveIn(BaseModel):
    item_kind: str = Field(min_length=1, max_length=32)
    item_id: uuid.UUID
    target_folder_id: uuid.UUID | None = None
    # Ordering hook for a future "insert after X" drag-and-drop; we do
    # not persist a sort order yet so the field is accepted but ignored.
    after_item_id: uuid.UUID | None = None


class FolderCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    parent_folder_id: uuid.UUID | None = None
    description: str | None = Field(default=None, max_length=500)


class FolderOut(BaseModel):
    id: str
    name: str
    parent_folder_id: str | None
    created_at: str
    description: str | None = None


# ---- Helpers ----


async def _get_patient_or_404(
    db: AsyncSession,
    patient_id: uuid.UUID,
    user: User | None,
    request: Request,
    *,
    action: str = READ_METADATA,
) -> Patient:
    patient = (
        await db.execute(select(Patient).where(Patient.id == patient_id))
    ).scalar_one_or_none()
    if patient is None:
        raise HTTPException(status_code=404, detail="patient not found")
    enforce_agent_patient_scope(request, patient.id, scope="patient:read")
    if not await can_patient(db, user=user, action=action, patient=patient):
        raise HTTPException(status_code=404, detail="patient not found")
    return patient


async def _owned_folder_or_404(db: AsyncSession, folder_id: uuid.UUID, user: User) -> Folder:
    folder = (await db.execute(select(Folder).where(Folder.id == folder_id))).scalar_one_or_none()
    if folder is None or not (user.is_admin or folder.owner_subject_id == user.subject_id):
        raise HTTPException(status_code=404, detail="folder not found")
    return folder


async def _patient_study_ids(db: AsyncSession, patient_id: uuid.UUID) -> list[uuid.UUID]:
    rows = (
        await db.execute(select(ImagingStudy.id).where(ImagingStudy.patient_id == patient_id))
    ).all()
    return [r[0] for r in rows]


async def _patient_doc_ids(db: AsyncSession, patient_id: uuid.UUID) -> list[uuid.UUID]:
    rows = (await db.execute(select(Document.id).where(Document.patient_id == patient_id))).all()
    return [r[0] for r in rows]


async def _folders_in_patient_tree(
    db: AsyncSession, patient: Patient, owner_subject_id: uuid.UUID
) -> set[uuid.UUID]:
    """Folders owned by ``owner_subject_id`` that belong to the patient
    tree.

    A folder belongs to the patient tree when EITHER:

    * it is patient-scoped by construction
      (``Folder.patient_id == patient.id``) — typically created via
      ``POST /api/folders`` with ``patient_id`` set, ie. the agent /
      operator explicitly intended this folder to live in the
      fascicolo. Empty organisational scaffolding (``2024``,
      ``2024/05-RM-pre-op``, …) qualifies regardless of whether items
      have landed yet; OR
    * it (transitively) contains a study / document / patient-marker
      item belonging to the patient — covers user-personal-workspace
      folders that were retroactively populated with patient items
      without ``patient_id`` ever being set on the folder row.

    Pre-2026-05-03 this function tracked only the second branch, so a
    freshly-created empty folder disappeared from the tree view until
    something landed in it. The internal session report flagged this
    as "GUI shows 3 items where the agent listed 5 subfolders": the
    missing ones were empty patient-scoped folders the agent had
    just created, hidden by the leaf-only filter.
    """
    study_ids = await _patient_study_ids(db, patient.id)
    doc_ids = await _patient_doc_ids(db, patient.id)

    leaf_filters = [
        and_(
            FolderItem.resource_kind == PATIENT_MARKER_KIND,
            FolderItem.resource_id == patient.id,
        )
    ]
    if study_ids:
        leaf_filters.append(
            and_(FolderItem.resource_kind == "study", FolderItem.resource_id.in_(study_ids))
        )
    if doc_ids:
        leaf_filters.append(
            and_(FolderItem.resource_kind == "document", FolderItem.resource_id.in_(doc_ids))
        )

    # Leaf items live in the *user's* workspace folder when they are
    # not patient-scoped. Filtering by owner_subject_id is correct
    # here: a personal workspace folder of another user that happens
    # to point at this patient's items is not part of *this* user's
    # tree view. (Sharing surfaces those via grants, not the tree.)
    leaf_rows = (
        await db.execute(
            select(FolderItem.folder_id)
            .join(Folder, Folder.id == FolderItem.folder_id)
            .where(Folder.owner_subject_id == owner_subject_id, or_(*leaf_filters))
        )
    ).all()
    leaf_ids: set[uuid.UUID] = {r[0] for r in leaf_rows}

    # Patient-scoped folders belong to the *fascicolo*, not the
    # creator's personal workspace. The caller has already proved
    # access to the patient (read permission verified upstream by
    # ``_resolve_patient`` / ``can_patient``), so the owner of each
    # row is irrelevant for visibility — what matters is that
    # ``patient_id`` matches. Filtering by ``owner_subject_id`` here
    # would hide a folder created by an agent token whose subject
    # differs from the human user, even when both manage the same
    # patient — exactly the symptom that triggered this fix.
    patient_scoped_rows = (
        await db.execute(select(Folder.id).where(Folder.patient_id == patient.id))
    ).all()
    patient_scoped_ids: set[uuid.UUID] = {r[0] for r in patient_scoped_rows}

    if not leaf_ids and not patient_scoped_ids:
        return set()

    # Build the parent/children index across every folder relevant
    # to *either* lens — the user's personal workspace OR the
    # patient fascicolo — so the walk-UP and walk-DOWN below can
    # cross owner boundaries when (and only when) the bridge node
    # is patient-scoped.
    all_relevant = (
        await db.execute(
            select(Folder.id, Folder.parent_folder_id).where(
                or_(
                    Folder.owner_subject_id == owner_subject_id,
                    Folder.patient_id == patient.id,
                )
            )
        )
    ).all()
    parent_of: dict[uuid.UUID, uuid.UUID | None] = dict(all_relevant)
    children_of: dict[uuid.UUID | None, list[uuid.UUID]] = {}
    for fid, pid in all_relevant:
        children_of.setdefault(pid, []).append(fid)

    reachable: set[uuid.UUID] = set()
    # Walk up from every seed (leaf items + patient-scoped folders) to
    # collect ancestors. Empty patient-scoped folders are first-class
    # citizens of the tree, not just "ancestors of a leaf".
    for fid in leaf_ids | patient_scoped_ids:
        cur: uuid.UUID | None = fid
        while cur is not None and cur not in reachable:
            reachable.add(cur)
            cur = parent_of.get(cur)

    # Walk down from every patient-scoped folder to include its
    # descendant scaffolding. A subfolder created under a patient-
    # scoped parent is part of the fascicolo by construction even
    # when it holds no leaf items of its own (year / topic / lesion
    # nodes are organisational, not data-bearing). Without this pass
    # an empty intermediate folder, or one whose leaves lost their
    # FolderItem rows in some past write, falls off the tree and
    # _resolve_path 404s on its name segment. Only descend from
    # patient-scoped seeds: descending from leaf-only seeds (a user
    # workspace folder retroactively populated with one patient
    # item) would pull unrelated user folders into the patient tree.
    queue = list(patient_scoped_ids)
    while queue:
        fid = queue.pop()
        for child in children_of.get(fid, []):
            if child not in reachable:
                reachable.add(child)
                queue.append(child)
    return reachable


async def _child_folders_with_counts(
    db: AsyncSession,
    *,
    parent_folder_id: uuid.UUID | None,
    tree_folders: set[uuid.UUID],
) -> list[tuple[Folder, int]]:
    """Child folders of ``parent_folder_id`` that belong to the patient
    tree, paired with their item counts (single query, no N+1)."""
    if not tree_folders:
        return []
    parent_clause = (
        Folder.parent_folder_id == parent_folder_id
        if parent_folder_id is not None
        else Folder.parent_folder_id.is_(None)
    )
    # Exclude bookkeeping rows (patient marker, raw "folder" links) from
    # the visible count: those rows aren't rendered as children by
    # ``_folder_resource_nodes``, so counting them would report N+1
    # while the user sees only N cards.
    count_subq = (
        select(FolderItem.folder_id, func.count().label("n"))
        .where(FolderItem.resource_kind.notin_(["folder", PATIENT_MARKER_KIND]))
        .group_by(FolderItem.folder_id)
        .subquery()
    )
    rows = (
        await db.execute(
            select(Folder, func.coalesce(count_subq.c.n, 0))
            .outerjoin(count_subq, count_subq.c.folder_id == Folder.id)
            .where(
                Folder.id.in_(tree_folders),
                parent_clause,
                # The materialised root row exists only to anchor the
                # no-orphan trigger; it is never surfaced as a folder
                # card. The path ``/`` already opens its contents
                # directly (see ``get_tree``), so users navigate "into"
                # the root without seeing a folder for it.
                Folder.is_root.is_(False),
            )
            .order_by(Folder.name)
        )
    ).all()
    return [(f, int(n)) for f, n in rows]


async def _resolve_path(
    db: AsyncSession,
    path: str,
    *,
    owner_subject_id: uuid.UUID,  # kept for signature compatibility
    tree_folders: set[uuid.UUID],
    root_folder_id: uuid.UUID | None = None,
) -> uuid.UUID | None:
    """Resolve ``/A/B/C`` to a folder_id within the patient tree. Returns
    ``None`` for root. Raises 404 if any segment doesn't resolve.

    When ``root_folder_id`` is provided (the patient's materialised
    root post-0088), the first segment is resolved against children of
    that folder rather than the legacy "parent_folder_id IS NULL" set.
    The materialised root is invisible to the user — its contents are
    surfaced as the patient-level listing — but folders the user
    creates land under it via ``parent_folder_id = root.id``.

    Visibility is delegated to ``tree_folders``: the set already
    encodes the rule (patient-scoped folders are visible regardless
    of their owner; user-workspace folders only when the caller owns
    them). Filtering this query by ``owner_subject_id`` would
    re-introduce the bug where a patient-scoped folder created by a
    different subject (e.g. an agent token) is hidden from the human
    manager of the same patient.
    """
    del owner_subject_id  # intentionally unused — see docstring
    if path in ("", "/"):
        return None

    segments = [s for s in path.strip("/").split("/") if s]
    current: uuid.UUID | None = root_folder_id
    for segment in segments:
        parent_clause = (
            Folder.parent_folder_id == current
            if current is not None
            else Folder.parent_folder_id.is_(None)
        )
        match = (
            (
                await db.execute(
                    select(Folder).where(
                        Folder.id.in_(tree_folders),
                        Folder.name == segment,
                        parent_clause,
                    )
                )
            )
            .scalars()
            .first()
        )
        if match is None:
            raise HTTPException(status_code=404, detail=f"path segment {segment!r} not found")
        current = match.id
    return current


async def _breadcrumb_for_folder(
    db: AsyncSession, folder_id: uuid.UUID, owner_subject_id: uuid.UUID
) -> list[BreadcrumbEntry]:
    """Build breadcrumb from ``folder_id`` up to the synthetic root.

    Skips the materialised patient root row: it is invisible to the
    user (path ``/`` already opens its contents directly), so listing
    it as a separate crumb between the synthetic ``Fascicolo`` segment
    and the first user-visible folder would surface a confusing
    ``Fascicolo / __root__ / Referti TC`` chain.
    """
    crumbs: list[BreadcrumbEntry] = []
    cur: uuid.UUID | None = folder_id
    seen: set[uuid.UUID] = set()
    while cur is not None and cur not in seen:
        seen.add(cur)
        row = (
            await db.execute(
                select(Folder.id, Folder.name, Folder.parent_folder_id, Folder.is_root).where(
                    Folder.id == cur, Folder.owner_subject_id == owner_subject_id
                )
            )
        ).first()
        if row is None:
            break
        if not row[3]:
            crumbs.append(BreadcrumbEntry(id=str(row[0]), name=row[1]))
        cur = row[2]
    crumbs.reverse()
    crumbs.insert(0, BreadcrumbEntry(id=None, name=ROOT_LABEL))
    return crumbs


def _path_from_breadcrumb(breadcrumb: list[BreadcrumbEntry]) -> str:
    names = [c.name for c in breadcrumb[1:]]
    return "/" + "/".join(names) if names else "/"


def _populate_breadcrumb_paths(breadcrumb: list[BreadcrumbEntry]) -> None:
    """Set the ``path`` field on each breadcrumb segment in place.

    Run once after the breadcrumb list is built so the frontend can
    navigate by clicking any segment without recomputing paths.
    """
    accum = ""
    for i, entry in enumerate(breadcrumb):
        if i == 0:
            entry.path = "/"
        else:
            accum = f"{accum}/{entry.name}"
            entry.path = accum or "/"


async def _filed_patient_ids(
    db: AsyncSession, *, patient: Patient, owner_subject_id: uuid.UUID
) -> tuple[set[uuid.UUID], set[uuid.UUID]]:
    """(study_ids, doc_ids) belonging to ``patient`` that are already
    filed into a folder owned by ``owner_subject_id``. Used to avoid
    double-listing them at the virtual root.
    """
    study_ids = await _patient_study_ids(db, patient.id)
    doc_ids = await _patient_doc_ids(db, patient.id)
    if not study_ids and not doc_ids:
        return set(), set()

    conds = []
    if study_ids:
        conds.append(
            and_(FolderItem.resource_kind == "study", FolderItem.resource_id.in_(study_ids))
        )
    if doc_ids:
        conds.append(
            and_(FolderItem.resource_kind == "document", FolderItem.resource_id.in_(doc_ids))
        )
    rows = (
        await db.execute(
            select(FolderItem.resource_kind, FolderItem.resource_id)
            .join(Folder, Folder.id == FolderItem.folder_id)
            .where(Folder.owner_subject_id == owner_subject_id, or_(*conds))
        )
    ).all()
    filed_studies: set[uuid.UUID] = set()
    filed_docs: set[uuid.UUID] = set()
    for kind, rid in rows:
        if kind == "study":
            filed_studies.add(rid)
        elif kind == "document":
            filed_docs.add(rid)
    return filed_studies, filed_docs


async def _root_resource_nodes(
    db: AsyncSession, *, patient: Patient, owner_subject_id: uuid.UUID
) -> list[TreeNode]:
    """Studies + documents for ``patient`` that aren't filed anywhere."""
    filed_studies, filed_docs = await _filed_patient_ids(
        db, patient=patient, owner_subject_id=owner_subject_id
    )
    study_q = select(ImagingStudy).where(ImagingStudy.patient_id == patient.id)
    if filed_studies:
        study_q = study_q.where(ImagingStudy.id.notin_(filed_studies))
    studies = (await db.execute(study_q)).scalars().all()

    doc_q = select(Document).where(Document.patient_id == patient.id)
    if filed_docs:
        doc_q = doc_q.where(Document.id.notin_(filed_docs))
    docs = (await db.execute(doc_q)).scalars().all()

    # Step 1 of the pathology spike: surface pathology slides at the
    # patient root (folder-aware listing comes in Step 2 alongside the
    # viewer). Slides cannot be moved into folders yet so there is no
    # ``filed_pathology_ids`` analogue — every row is visible here.
    from bvphoenix.db.models import PathologySlide

    slides = (
        (await db.execute(select(PathologySlide).where(PathologySlide.patient_id == patient.id)))
        .scalars()
        .all()
    )

    return (
        [_study_node(s) for s in studies]
        + [_document_node(d) for d in docs]
        + [_pathology_slide_node(s) for s in slides]
    )


async def _folder_resource_nodes(
    db: AsyncSession, *, folder_id: uuid.UUID, patient: Patient
) -> list[TreeNode]:
    """Resources filed directly in ``folder_id`` that belong to
    ``patient``. Child folders are handled separately by the caller."""
    item_rows = (
        await db.execute(
            select(FolderItem.resource_kind, FolderItem.resource_id).where(
                FolderItem.folder_id == folder_id,
                FolderItem.resource_kind.notin_(["folder", PATIENT_MARKER_KIND]),
            )
        )
    ).all()
    by_kind: dict[str, list[uuid.UUID]] = {}
    for kind, rid in item_rows:
        by_kind.setdefault(kind, []).append(rid)

    nodes: list[TreeNode] = []

    if "study" in by_kind:
        rows = (
            (
                await db.execute(
                    select(ImagingStudy).where(
                        ImagingStudy.id.in_(by_kind["study"]), ImagingStudy.patient_id == patient.id
                    )
                )
            )
            .scalars()
            .all()
        )
        nodes.extend(_study_node(s) for s in rows)

    if "series" in by_kind:
        rows = (
            (
                await db.execute(
                    select(Series)
                    .join(ImagingStudy, ImagingStudy.id == Series.study_id)
                    .where(Series.id.in_(by_kind["series"]), ImagingStudy.patient_id == patient.id)
                )
            )
            .scalars()
            .all()
        )
        nodes.extend(_series_node(s) for s in rows)

    if "document" in by_kind:
        rows = (
            (
                await db.execute(
                    select(Document).where(
                        Document.id.in_(by_kind["document"]),
                        Document.patient_id == patient.id,
                    )
                )
            )
            .scalars()
            .all()
        )
        nodes.extend(_document_node(d) for d in rows)

    # v3 phase 3b: 'report' (Study report) and 'consultation' (BitVision
    # synthesis) folder items both resolve to ``ReportContent`` rows
    # now. The tree node renders as 'report_content' with the authority
    # surfaced inline so the UI can distinguish original/derived from
    # canonical_synthesis.
    if "report" in by_kind or "consultation" in by_kind:
        from bvphoenix.db.models import ClinicalEvent, ReportContent

        wanted_ids: list = list(by_kind.get("report", [])) + list(by_kind.get("consultation", []))
        if wanted_ids:
            rows = (
                (
                    await db.execute(
                        select(ReportContent)
                        .join(ClinicalEvent, ClinicalEvent.id == ReportContent.clinical_event_id)
                        .where(
                            ReportContent.id.in_(wanted_ids),
                            ClinicalEvent.patient_id == patient.id,
                        )
                    )
                )
                .scalars()
                .all()
            )
            nodes.extend(_report_content_node(r) for r in rows)

    return nodes


def _folder_node(folder: Folder, item_count: int) -> TreeNode:
    # ``date`` is what the FE renders on the folder card. Prefer
    # ``clinical_date`` (the editable display date — what the folder
    # represents in the patient timeline) and fall back to
    # ``created_at`` (system audit) when unset, so legacy folders
    # without a clinical_date keep working.
    display_date = (
        folder.clinical_date.isoformat()
        if folder.clinical_date is not None
        else folder.created_at.isoformat()
    )
    return TreeNode(
        type="folder",
        id=str(folder.id),
        name=folder.name,
        item_count=item_count,
        created_at=folder.created_at.isoformat(),
        updated_at=folder.updated_at.isoformat(),
        date=display_date,
        description=folder.description,
        narrative_md=folder.narrative_md,
        clinical_date=(
            folder.clinical_date.isoformat() if folder.clinical_date is not None else None
        ),
        # ``path`` is filled in by ``_enrich_with_paths`` once the parent
        # folder's path is known. ``kind_counts`` is populated alongside
        # ``preview`` / ``preview_kinds`` by ``_attach_folder_previews``.
    )


def _pathology_slide_node(s) -> TreeNode:  # type: ignore[no-untyped-def]
    """TreeNode for a pathology slide. Mirrors _study_node shape so
    ContentPane can render the card with the same scaffold; the extra
    pathology-specific fields drive the stain / magnification chips."""
    when = s.created_at.isoformat()
    label = f"Vetrino {s.stain or ''}".strip()
    return TreeNode(
        type="pathology_slide",
        id=str(s.id),
        name=label or f"Vetrino {s.slide_instance_uid[:12]}",
        modality="PATH",
        created_at=s.created_at.isoformat(),
        updated_at=s.updated_at.isoformat(),
        target_id=str(s.id),
        date=when,
        stain=s.stain,
        magnification=s.magnification,
        source_format=s.source_format,
        has_macro=s.s3_macro_key is not None,
        source_collection=s.source_collection,
        license_spdx=s.license_spdx,
        license_url=s.license_url,
        citation_required=s.citation_required,
        citation_text=s.citation_text,
    )


def _study_node(s: ImagingStudy) -> TreeNode:
    when = s.study_date.isoformat() if s.study_date else s.created_at.isoformat()
    return TreeNode(
        type="study",
        id=str(s.id),
        name=s.study_description or f"ImagingStudy {s.study_instance_uid}",
        modality=(s.modalities[0] if s.modalities else None),
        created_at=s.created_at.isoformat(),
        updated_at=s.updated_at.isoformat(),
        target_id=str(s.id),
        date=when,
        # License + provenance — surface them on every study card so
        # the public-dataset chip renders without a per-card fetch. NULL
        # for user uploads (no license metadata), populated for tier=t4
        # imports from infra/public_datasets/manifest.yaml.
        source_collection=s.source_collection,
        license_spdx=s.license_spdx,
        license_url=s.license_url,
        citation_required=s.citation_required,
        citation_text=s.citation_text,
    )


def _series_node(s: Series) -> TreeNode:
    return TreeNode(
        type="series",
        id=str(s.id),
        name=s.series_description or f"Series {s.series_instance_uid}",
        modality=s.modality,
        created_at=s.created_at.isoformat(),
        target_id=str(s.id),
        date=s.created_at.isoformat(),
        instance_count=int(s.received_instance_count or 0),
    )


def _document_node(d: Document) -> TreeNode:
    when = d.document_date.isoformat() if d.document_date else d.created_at.isoformat()
    return TreeNode(
        type="document",
        id=str(d.id),
        name=d.title,
        document_type=d.kind_id,
        created_at=d.created_at.isoformat(),
        updated_at=d.updated_at.isoformat(),
        target_id=str(d.id),
        date=when,
        mime_type=d.file_content_type,
    )


def _report_content_node(r) -> TreeNode:  # type: ignore[no-untyped-def]
    """v3 successor of _report_node + _consultation_node. Surfaces
    authority inline so the FE can route 'canonical_synthesis' to the
    Referto UI and 'original' / 'derived' to the extraction-detail
    view."""
    type_label = "consultation" if r.authority_id == "canonical_synthesis" else "report"
    return TreeNode(
        type=type_label,
        id=str(r.id),
        name=r.title or f"Report ({r.authority_id})",
        created_at=r.created_at.isoformat(),
        updated_at=r.updated_at.isoformat(),
        target_id=str(r.id),
        date=r.created_at.isoformat(),
    )


async def _attach_document_folder_counts(
    db: AsyncSession,
    nodes: list[TreeNode],
) -> None:
    """Populate ``folder_count`` + ``is_in_root_only`` on document
    nodes so the grid card can render the chain-link hardlink badge.
    Single grouped query against ``folder_items`` joined with
    ``folders``; nodes without a row default to ``folder_count=1``
    which matches the no-orphan invariant for live documents."""
    doc_nodes = [n for n in nodes if n.type == "document"]
    if not doc_nodes:
        return
    doc_ids = [uuid.UUID(n.id) for n in doc_nodes]
    rows = (
        await db.execute(
            select(
                FolderItem.resource_id,
                func.count(FolderItem.folder_id).label("folder_count"),
                func.bool_and(Folder.is_root).label("only_root"),
            )
            .join(Folder, Folder.id == FolderItem.folder_id)
            .where(
                FolderItem.resource_kind == "document",
                FolderItem.resource_id.in_(doc_ids),
            )
            .group_by(FolderItem.resource_id)
        )
    ).all()
    by_doc: dict[uuid.UUID, tuple[int, bool]] = {
        rid: (int(fc), bool(only_root)) for rid, fc, only_root in rows
    }
    for n in doc_nodes:
        fc, only_root = by_doc.get(uuid.UUID(n.id), (1, True))
        n.folder_count = fc
        n.is_in_root_only = only_root


async def _attach_folder_previews(
    db: AsyncSession, folder_nodes: list[TreeNode], patient: Patient
) -> None:
    """Populate ``preview`` + ``preview_kinds`` on every folder node.

    Single batched query that fans across all folder ids in the listing,
    pulling up to 8 ``FolderItem`` rows per folder. Subfolder markers
    and the ``patient`` placeholder are excluded. Each item is then
    materialised into a lightweight ``FolderPreviewEntry`` by joining
    against the kind-specific table.

    O(N) rows scanned where N is the sum of items across visible
    folders — far cheaper than per-folder fetches and bounded because
    each folder only contributes up to 8 preview rows.
    """
    folder_ids = [uuid.UUID(n.id) for n in folder_nodes if n.type == "folder"]
    if not folder_ids:
        return

    # Pull up to 8 representative items per folder. ROW_NUMBER over
    # ``added_at`` is the cleanest cap-per-group on Postgres without
    # a LATERAL join.
    item_rows = (
        await db.execute(
            text(
                """
                SELECT folder_id, resource_kind, resource_id
                FROM (
                    SELECT folder_id, resource_kind, resource_id,
                        ROW_NUMBER() OVER (
                            PARTITION BY folder_id ORDER BY added_at ASC
                        ) AS rn
                    FROM folder_items
                    WHERE folder_id = ANY(:fids)
                      AND resource_kind NOT IN ('folder', 'patient')
                ) t
                WHERE rn <= 8
                """
            ),
            {"fids": folder_ids},
        )
    ).all()

    by_folder: dict[uuid.UUID, list[tuple[str, uuid.UUID]]] = {}
    kind_count: dict[uuid.UUID, dict[str, int]] = {}
    for fid, kind, rid in item_rows:
        by_folder.setdefault(fid, []).append((kind, rid))
        kind_count.setdefault(fid, {})[kind] = kind_count.get(fid, {}).get(kind, 0) + 1

    # Full per-kind aggregate (no row-number cap), used by the grid
    # hover preview to render lines like "3 studi · 2 PDF · 1 nota".
    # Single grouped query over the same row set the preview tiles
    # came from; cheap enough that we don't merge it into the previous
    # query (which only returns the top 8 rows per folder).
    full_count_rows = (
        await db.execute(
            text(
                """
                SELECT folder_id, resource_kind, COUNT(*) AS n
                FROM folder_items
                WHERE folder_id = ANY(:fids)
                  AND resource_kind NOT IN ('folder', 'patient')
                GROUP BY folder_id, resource_kind
                """
            ),
            {"fids": folder_ids},
        )
    ).all()
    full_kind_count: dict[uuid.UUID, dict[str, int]] = {}
    for fid, kind, n in full_count_rows:
        full_kind_count.setdefault(fid, {})[kind] = int(n)

    # Last-activity timestamp per folder = MAX(folder_items.added_at).
    # Used as ``node.date`` so the grid card surfaces a date that
    # actually reflects when the folder was last touched, not just
    # when it was created. Empty folders fall back to folder.created_at
    # because the row set is empty here for them.
    last_activity_rows = (
        await db.execute(
            text(
                """
                SELECT folder_id, MAX(added_at) AS last_added
                FROM folder_items
                WHERE folder_id = ANY(:fids)
                GROUP BY folder_id
                """
            ),
            {"fids": folder_ids},
        )
    ).all()
    last_activity: dict[uuid.UUID, datetime | None] = dict(last_activity_rows)

    # Bulk-load each kind's relevant rows in one SELECT per kind.
    studies_needed: set[uuid.UUID] = set()
    docs_needed: set[uuid.UUID] = set()
    reports_needed: set[uuid.UUID] = set()
    consultations_needed: set[uuid.UUID] = set()
    series_needed: set[uuid.UUID] = set()
    for items in by_folder.values():
        for kind, rid in items:
            if kind == "study":
                studies_needed.add(rid)
            elif kind == "document":
                docs_needed.add(rid)
            elif kind == "report":
                reports_needed.add(rid)
            elif kind == "consultation":
                consultations_needed.add(rid)
            elif kind == "series":
                series_needed.add(rid)

    studies_by_id: dict[uuid.UUID, ImagingStudy] = {}
    if studies_needed:
        rows = (
            (
                await db.execute(
                    select(ImagingStudy).where(
                        ImagingStudy.id.in_(studies_needed),
                        ImagingStudy.patient_id == patient.id,
                    )
                )
            )
            .scalars()
            .all()
        )
        studies_by_id = {s.id: s for s in rows}

    # Pick a thumbnail series for each fetched study (first one with
    # received instances). Single batched query; mirrors what
    # ``_attach_study_thumbnails`` does for study cards.
    study_thumb: dict[uuid.UUID, uuid.UUID] = {}
    if studies_by_id:
        thumb_rows = (
            await db.execute(
                select(
                    Series.study_id,
                    Series.id,
                    Series.received_instance_count,
                )
                .where(Series.study_id.in_(studies_by_id.keys()))
                .order_by(Series.study_id, Series.series_number.asc().nullslast())
            )
        ).all()
        for sid, series_id, received in thumb_rows:
            if sid in study_thumb:
                continue
            if (received or 0) > 0:
                study_thumb[sid] = series_id

    docs_by_id: dict[uuid.UUID, Document] = {}
    if docs_needed:
        rows = (
            (
                await db.execute(
                    select(Document).where(
                        Document.id.in_(docs_needed),
                        Document.patient_id == patient.id,
                    )
                )
            )
            .scalars()
            .all()
        )
        docs_by_id = {d.id: d for d in rows}

    # v3 phase 3b: 'report' and 'consultation' folder items are both
    # ReportContent rows now (the legacy Report + Consultation entities
    # were retired). Resolve them in a single query against the
    # Expression layer; the caller distinguishes by ``authority_id``.
    from bvphoenix.db.models import ClinicalEvent as _CE
    from bvphoenix.db.models import ReportContent as _RC

    rc_needed = list(reports_needed) + list(consultations_needed)
    reports_by_id: dict = {}
    consultations_by_id: dict = {}
    if rc_needed:
        rows = (
            (
                await db.execute(
                    select(_RC)
                    .join(_CE, _CE.id == _RC.clinical_event_id)
                    .where(
                        _RC.id.in_(rc_needed),
                        _CE.patient_id == patient.id,
                    )
                )
            )
            .scalars()
            .all()
        )
        for r in rows:
            if r.authority_id == "canonical_synthesis":
                consultations_by_id[r.id] = r
            else:
                reports_by_id[r.id] = r

    series_by_id: dict[uuid.UUID, Series] = {}
    if series_needed:
        rows = (
            (
                await db.execute(
                    select(Series)
                    .join(ImagingStudy, ImagingStudy.id == Series.study_id)
                    .where(
                        Series.id.in_(series_needed),
                        ImagingStudy.patient_id == patient.id,
                    )
                )
            )
            .scalars()
            .all()
        )
        series_by_id = {s.id: s for s in rows}

    for node in folder_nodes:
        if node.type != "folder":
            continue
        fid = uuid.UUID(node.id)
        items = by_folder.get(fid, [])
        last_ts = last_activity.get(fid)
        if last_ts is not None:
            # Push the most-recent item-add into ``updated_at`` so the
            # secondary "ultima modifica" stamp reflects activity
            # inside the folder, not just edits to the folder row
            # itself (renames, description tweaks). Folders whose
            # only item is the ``patient`` marker still see ``last_ts``
            # equal to their own ``folder.created_at`` to the second,
            # which is the desired no-op outcome for empty folders.
            # ``node.date`` is left alone so it keeps reflecting the
            # user-controlled creation date editable via the folder
            # edit dialog.
            current = node.updated_at
            try:
                current_dt = datetime.fromisoformat(current) if current else None
            except ValueError:
                current_dt = None
            if current_dt is None or last_ts > current_dt:
                node.updated_at = last_ts.isoformat()
        preview: list[FolderPreviewEntry] = []
        for kind, rid in items[:4]:
            entry: FolderPreviewEntry | None = None
            if kind == "study" and rid in studies_by_id:
                s = studies_by_id[rid]
                entry = FolderPreviewEntry(
                    type="study",
                    name=s.study_description or f"ImagingStudy {s.study_instance_uid}",
                    modality=(s.modalities[0] if s.modalities else None),
                    thumbnail_series_id=(str(study_thumb[s.id]) if s.id in study_thumb else None),
                )
            elif kind == "document" and rid in docs_by_id:
                d = docs_by_id[rid]
                entry = FolderPreviewEntry(
                    type="document",
                    name=d.title,
                    target_id=str(d.id),
                    mime_type=d.file_content_type,
                )
            elif kind == "report" and rid in reports_by_id:
                r = reports_by_id[rid]
                entry = FolderPreviewEntry(type="report", name=f"Referto v{r.version}")
            elif kind == "consultation" and rid in consultations_by_id:
                c = consultations_by_id[rid]
                entry = FolderPreviewEntry(type="consultation", name=c.title)
            elif kind == "series" and rid in series_by_id:
                s2 = series_by_id[rid]
                entry = FolderPreviewEntry(
                    type="series",
                    name=s2.series_description or f"Series {s2.series_instance_uid}",
                    modality=s2.modality,
                )
            if entry is not None:
                preview.append(entry)
        node.preview = preview
        node.preview_kinds = kind_count.get(fid, {})
        node.kind_counts = full_kind_count.get(fid, {})


# Depth cap for the recursive folder walk. 5 is enough for the
# realistic ``year/category/source/CD/...`` clinical layout while
# bounding the worst-case CTE so an accidentally cyclic structure
# can't pin a worker.
_RECURSIVE_DEPTH = 5


async def _attach_folder_recursive_view(
    db: AsyncSession, folder_nodes: list[TreeNode], patient: Patient
) -> None:
    """Layer recursive aggregates on top of the direct-child preview.

    Walks the folder sub-tree (depth capped at ``_RECURSIVE_DEPTH``)
    and:

    * computes ``recursive_kind_counts`` per root folder, including a
      ``"folder"`` bucket counting nested sub-folders;
    * promotes the most representative descendants into ``preview`` so
      a folder like ``2024/`` shows the studies buried in
      ``2024/TAC/`` instead of just listing ``TAC`` as a subfolder;
    * counts study↔report pairs reachable below the root (via
      ``DocumentStudyLink.report_of``) so the UI can render
      "3 esami refertati" instead of summing studies + documents.
    """
    folder_ids = [uuid.UUID(n.id) for n in folder_nodes if n.type == "folder"]
    if not folder_ids:
        return

    desc_rows = (
        await db.execute(
            text(
                """
                WITH RECURSIVE folder_tree(root_id, descendant_id, depth, path_names) AS (
                    SELECT id, id, 0, ARRAY[]::text[]
                    FROM folders
                    WHERE id = ANY(:fids)
                    UNION ALL
                    SELECT ft.root_id, f.id, ft.depth + 1,
                           ft.path_names || ARRAY[f.name]
                    FROM folder_tree ft
                    JOIN folders f ON f.parent_folder_id = ft.descendant_id
                    WHERE ft.depth < :depth
                )
                SELECT root_id, descendant_id, depth, path_names FROM folder_tree
                """
            ),
            {"fids": folder_ids, "pid": patient.id, "depth": _RECURSIVE_DEPTH},
        )
    ).all()

    descendants_by_root: dict[uuid.UUID, list[tuple[uuid.UUID, int, list[str]]]] = {}
    for root_id, desc_id, depth, path_names in desc_rows:
        descendants_by_root.setdefault(root_id, []).append(
            (desc_id, int(depth), list(path_names or []))
        )

    if not descendants_by_root:
        return

    # Per-(root, kind) recursive counts.
    rec_count_rows = (
        await db.execute(
            text(
                """
                SELECT ft.root_id, fi.resource_kind, COUNT(*) AS n
                FROM (
                    WITH RECURSIVE folder_tree(root_id, descendant_id, depth) AS (
                        SELECT id, id, 0 FROM folders
                        WHERE id = ANY(:fids)
                        UNION ALL
                        SELECT ft.root_id, f.id, ft.depth + 1
                        FROM folder_tree ft
                        JOIN folders f ON f.parent_folder_id = ft.descendant_id
                        WHERE ft.depth < :depth
                    )
                    SELECT * FROM folder_tree
                ) ft
                JOIN folder_items fi ON fi.folder_id = ft.descendant_id
                WHERE fi.resource_kind NOT IN ('patient', 'subfolder', 'folder')
                GROUP BY ft.root_id, fi.resource_kind
                """
            ),
            {"fids": folder_ids, "pid": patient.id, "depth": _RECURSIVE_DEPTH},
        )
    ).all()
    rec_kind_count: dict[uuid.UUID, dict[str, int]] = {}
    for root_id, kind, n in rec_count_rows:
        rec_kind_count.setdefault(root_id, {})[kind] = int(n)
    for root_id, descs in descendants_by_root.items():
        nested = sum(1 for (_, depth, _) in descs if depth >= 1)
        if nested > 0:
            rec_kind_count.setdefault(root_id, {})["folder"] = nested

    # Recursive items for the preview promotion.
    items_by_root: dict[uuid.UUID, list[tuple[str, uuid.UUID, uuid.UUID, list[str]]]] = {}
    item_rows = (
        await db.execute(
            text(
                """
                SELECT ft.root_id, fi.folder_id, fi.resource_kind,
                       fi.resource_id, fi.added_at
                FROM (
                    WITH RECURSIVE folder_tree(root_id, descendant_id, depth) AS (
                        SELECT id, id, 0 FROM folders
                        WHERE id = ANY(:fids)
                        UNION ALL
                        SELECT ft.root_id, f.id, ft.depth + 1
                        FROM folder_tree ft
                        JOIN folders f ON f.parent_folder_id = ft.descendant_id
                        WHERE ft.depth < :depth
                    )
                    SELECT * FROM folder_tree
                ) ft
                JOIN folder_items fi ON fi.folder_id = ft.descendant_id
                WHERE fi.resource_kind NOT IN ('patient', 'subfolder', 'folder')
                ORDER BY ft.root_id, fi.added_at DESC
                """
            ),
            {"fids": folder_ids, "pid": patient.id, "depth": _RECURSIVE_DEPTH},
        )
    ).all()

    path_for_desc: dict[uuid.UUID, list[str]] = {}
    for descs in descendants_by_root.values():
        for did, _, path_names in descs:
            path_for_desc[did] = path_names
    for root_id, fid_in, kind, rid, _added in item_rows:
        items_by_root.setdefault(root_id, []).append(
            (kind, rid, fid_in, path_for_desc.get(fid_in, []))
        )

    studies_needed: set[uuid.UUID] = set()
    docs_needed: set[uuid.UUID] = set()
    reports_needed: set[uuid.UUID] = set()
    consultations_needed: set[uuid.UUID] = set()
    series_needed: set[uuid.UUID] = set()
    for items in items_by_root.values():
        for kind, rid, _fid, _path in items[:32]:
            if kind == "study":
                studies_needed.add(rid)
            elif kind == "document":
                docs_needed.add(rid)
            elif kind == "report":
                reports_needed.add(rid)
            elif kind == "consultation":
                consultations_needed.add(rid)
            elif kind == "series":
                series_needed.add(rid)

    studies_by_id: dict[uuid.UUID, ImagingStudy] = {}
    if studies_needed:
        rows = (
            (
                await db.execute(
                    select(ImagingStudy).where(
                        ImagingStudy.id.in_(studies_needed),
                        ImagingStudy.patient_id == patient.id,
                    )
                )
            )
            .scalars()
            .all()
        )
        studies_by_id = {s.id: s for s in rows}

    study_thumb: dict[uuid.UUID, uuid.UUID] = {}
    if studies_by_id:
        thumb_rows = (
            await db.execute(
                select(
                    Series.study_id,
                    Series.id,
                    Series.received_instance_count,
                )
                .where(Series.study_id.in_(studies_by_id.keys()))
                .order_by(Series.study_id, Series.series_number.asc().nullslast())
            )
        ).all()
        for sid, series_id, received in thumb_rows:
            if sid in study_thumb:
                continue
            if (received or 0) > 0:
                study_thumb[sid] = series_id

    docs_by_id: dict[uuid.UUID, Document] = {}
    if docs_needed:
        rows = (
            (
                await db.execute(
                    select(Document).where(
                        Document.id.in_(docs_needed),
                        Document.patient_id == patient.id,
                    )
                )
            )
            .scalars()
            .all()
        )
        docs_by_id = {d.id: d for d in rows}

    # v3 phase 3b: 'report' and 'consultation' folder items are both
    # ReportContent rows now (the legacy Report + Consultation entities
    # were retired). Resolve them in a single query against the
    # Expression layer; the caller distinguishes by ``authority_id``.
    from bvphoenix.db.models import ClinicalEvent as _CE
    from bvphoenix.db.models import ReportContent as _RC

    rc_needed = list(reports_needed) + list(consultations_needed)
    reports_by_id: dict = {}
    consultations_by_id: dict = {}
    if rc_needed:
        rows = (
            (
                await db.execute(
                    select(_RC)
                    .join(_CE, _CE.id == _RC.clinical_event_id)
                    .where(
                        _RC.id.in_(rc_needed),
                        _CE.patient_id == patient.id,
                    )
                )
            )
            .scalars()
            .all()
        )
        for r in rows:
            if r.authority_id == "canonical_synthesis":
                consultations_by_id[r.id] = r
            else:
                reports_by_id[r.id] = r

    series_by_id: dict[uuid.UUID, Series] = {}
    if series_needed:
        rows = (
            (
                await db.execute(
                    select(Series)
                    .join(ImagingStudy, ImagingStudy.id == Series.study_id)
                    .where(
                        Series.id.in_(series_needed),
                        ImagingStudy.patient_id == patient.id,
                    )
                )
            )
            .scalars()
            .all()
        )
        series_by_id = {s.id: s for s in rows}

    # Smart pairing — count studies whose canonical structured report
    # is also reachable below the same root. v3: ``document_study_links``
    # was dropped in 0078 in favour of ``content_document_links``
    # routed through ``report_contents → clinical_events →
    # imaging_studies``. The semantics carry: "this document is the
    # extracted source of a ReportContent that lives on the same
    # ClinicalEvent the imaging study projects from".
    paired_count_by_root: dict[uuid.UUID, int] = {}
    if studies_by_id and docs_by_id:
        link_rows = (
            await db.execute(
                text(
                    """
                    SELECT i.id AS study_id, cdl.document_id
                    FROM content_document_links cdl
                    JOIN report_contents rc
                      ON rc.id = cdl.report_content_id
                    JOIN imaging_studies i
                      ON i.clinical_event_id = rc.clinical_event_id
                    WHERE cdl.role = 'extracted_from'
                      AND i.id = ANY(:sids)
                      AND cdl.document_id = ANY(:dids)
                    """
                ),
                {
                    "sids": list(studies_by_id.keys()),
                    "dids": list(docs_by_id.keys()),
                },
            )
        ).all()
        for root_id, items in items_by_root.items():
            present_studies = {rid for kind, rid, _f, _p in items if kind == "study"}
            present_docs = {rid for kind, rid, _f, _p in items if kind == "document"}
            paired = sum(
                1 for sid, did in link_rows if sid in present_studies and did in present_docs
            )
            if paired > 0:
                paired_count_by_root[root_id] = paired

    def _preview_priority(kind: str, rid: uuid.UUID) -> int:
        if kind == "study":
            return 0 if rid in study_thumb else 1
        if kind == "document":
            d = docs_by_id.get(rid)
            return 2 if d and d.file_content_type else 3
        if kind == "report":
            return 4
        if kind == "consultation":
            return 5
        if kind == "series":
            return 6
        return 9

    for node in folder_nodes:
        if node.type != "folder":
            continue
        fid = uuid.UUID(node.id)
        node.recursive_kind_counts = rec_kind_count.get(fid, {}) or None
        node.paired_study_report_count = paired_count_by_root.get(fid)

        items = items_by_root.get(fid, [])
        ranked = sorted(items, key=lambda t: _preview_priority(t[0], t[1]))

        new_preview: list[FolderPreviewEntry] = []
        seen_resource: set[tuple[str, uuid.UUID]] = set()
        for kind, rid, _src_folder_id, path_names in ranked:
            if (kind, rid) in seen_resource:
                continue
            seen_resource.add((kind, rid))
            entry: FolderPreviewEntry | None = None
            via = path_names[:-1] if path_names else None
            if kind == "study" and rid in studies_by_id:
                s = studies_by_id[rid]
                entry = FolderPreviewEntry(
                    type="study",
                    name=s.study_description or f"ImagingStudy {s.study_instance_uid}",
                    modality=(s.modalities[0] if s.modalities else None),
                    thumbnail_series_id=(str(study_thumb[s.id]) if s.id in study_thumb else None),
                    via_folder_path=via or None,
                )
            elif kind == "document" and rid in docs_by_id:
                d = docs_by_id[rid]
                entry = FolderPreviewEntry(
                    type="document",
                    name=d.title,
                    target_id=str(d.id),
                    mime_type=d.file_content_type,
                    via_folder_path=via or None,
                )
            elif kind == "report" and rid in reports_by_id:
                r = reports_by_id[rid]
                entry = FolderPreviewEntry(
                    type="report",
                    name=f"Referto v{r.version}",
                    via_folder_path=via or None,
                )
            elif kind == "consultation" and rid in consultations_by_id:
                c = consultations_by_id[rid]
                entry = FolderPreviewEntry(
                    type="consultation",
                    name=c.title,
                    via_folder_path=via or None,
                )
            elif kind == "series" and rid in series_by_id:
                s2 = series_by_id[rid]
                entry = FolderPreviewEntry(
                    type="series",
                    name=s2.series_description or f"Series {s2.series_instance_uid}",
                    modality=s2.modality,
                    via_folder_path=via or None,
                )
            if entry is not None:
                new_preview.append(entry)
                if len(new_preview) >= 4:
                    break

        # Fill the remaining slots with sub-folder tiles when the
        # parent has too few entity descendants to fill 4 tiles. Lets
        # a "structure-only" parent (only sub-folders, no studies)
        # still convey navigation.
        if len(new_preview) < 4:
            descs = descendants_by_root.get(fid, [])
            seen_folders: set[uuid.UUID] = set()
            for did, depth, path_names in descs:
                if depth != 1 or did in seen_folders:
                    continue
                seen_folders.add(did)
                if len(new_preview) >= 4:
                    break
                # Skip if a descendant of this sub-folder was already
                # promoted as a preview tile (avoids duplication).
                first_seg = path_names[0] if path_names else None
                if any(
                    e.via_folder_path
                    and len(e.via_folder_path) >= 1
                    and e.via_folder_path[0] == first_seg
                    for e in new_preview
                ):
                    continue
                # Recursive descendant count of THIS sub-folder.
                sub_count = sum(
                    1
                    for (_other_did, other_depth, other_path) in descs
                    if other_depth >= 2 and len(other_path) >= 1 and other_path[0] == first_seg
                )
                new_preview.append(
                    FolderPreviewEntry(
                        type="folder",
                        name=path_names[-1] if path_names else "Folder",
                        folder_descendant_count=sub_count,
                    )
                )

        if new_preview:
            node.preview = new_preview


async def _attach_study_thumbnails(db: AsyncSession, nodes: list[TreeNode]) -> None:
    """Populate ``thumbnail_series_id``, ``series_count`` and
    ``instance_count`` on every study node in place.

    Single pass over all series of every study referenced by ``nodes``:

    * thumbnail = first series (by series_number) with at least one
      received instance — best guess for "an image worth showing as
      the cover". Studies that only carry SR / RTSTRUCT / pixel-less
      series get no thumbnail; the frontend falls back to the icon.
    * ``series_count`` = total series of the study, including pixel-
      less ones (so a study with 4 series and 1 SR shows ``5 series``,
      matching the radiologist's mental count).
    * ``instance_count`` = sum of ``received_instance_count`` across
      all series.
    """
    study_node_by_id: dict[str, TreeNode] = {n.id: n for n in nodes if n.type == "study"}
    if not study_node_by_id:
        return
    rows = (
        await db.execute(
            select(
                Series.study_id,
                Series.id,
                Series.series_number,
                Series.received_instance_count,
            )
            .where(
                Series.study_id.in_([uuid.UUID(k) for k in study_node_by_id]),
            )
            .order_by(Series.study_id, Series.series_number.asc().nullslast())
        )
    ).all()
    series_count: dict[str, int] = {}
    instance_count: dict[str, int] = {}
    thumbnail_picked: set[str] = set()
    for study_id, series_id, _series_number, received in rows:
        sid = str(study_id)
        series_count[sid] = series_count.get(sid, 0) + 1
        instance_count[sid] = instance_count.get(sid, 0) + int(received or 0)
        # Thumbnail: first series with at least one instance (rows are
        # already ordered by series_number ASC).
        if sid not in thumbnail_picked and (received or 0) > 0:
            thumbnail_picked.add(sid)
            node = study_node_by_id.get(sid)
            if node is not None:
                node.thumbnail_series_id = str(series_id)
    for sid, node in study_node_by_id.items():
        node.series_count = series_count.get(sid, 0)
        node.instance_count = instance_count.get(sid, 0)


def _enrich_folder_paths(nodes: list[TreeNode], current_path: str) -> None:
    """Populate ``path`` on folder nodes given the current folder's path.

    Mutates the list in place. Non-folder nodes are skipped — their
    navigation never goes through path-based lookups.
    """
    base = current_path.rstrip("/") or ""
    for n in nodes:
        if n.type == "folder":
            n.path = f"{base}/{n.name}"
            n.parent_path = current_path


async def _item_belongs_to_patient(
    db: AsyncSession, kind: str, item_id: uuid.UUID, patient: Patient
) -> bool:
    if kind == "folder":
        # Folder ownership is enforced separately; patient-tree membership
        # isn't a hard requirement for moving a folder into this tree.
        return True
    if kind == "study":
        return (
            await db.execute(
                select(ImagingStudy.id).where(
                    ImagingStudy.id == item_id, ImagingStudy.patient_id == patient.id
                )
            )
        ).first() is not None
    if kind == "series":
        return (
            await db.execute(
                select(Series.id)
                .join(ImagingStudy, ImagingStudy.id == Series.study_id)
                .where(Series.id == item_id, ImagingStudy.patient_id == patient.id)
            )
        ).first() is not None
    if kind == "document":
        return (
            await db.execute(
                select(Document.id).where(
                    Document.id == item_id,
                    Document.patient_id == patient.id,
                )
            )
        ).first() is not None
    if kind == "report":
        return (
            await db.execute(
                select(Report.id)
                .join(ImagingStudy, ImagingStudy.id == Report.study_id)
                .where(Report.id == item_id, ImagingStudy.patient_id == patient.id)
            )
        ).first() is not None
    if kind == "consultation":
        return (
            await db.execute(
                select(Consultation.id).where(
                    Consultation.id == item_id, Consultation.patient_id == patient.id
                )
            )
        ).first() is not None
    return False


# ---- Endpoints ----


def _shared_grantor_subject_id(request: Request, user: User | None) -> uuid.UUID | None:
    """Return the grantor of a share-link session, if any.

    A share-link JWT carries ``grant_id`` and resolves to a synthetic
    ``User`` with ``_share_grant`` attached (``auth/deps.py``). The
    grant's ``grantor_subject_id`` is the original owner of the folder
    tree the recipient is looking at — using it as the folder owner
    here means the shared view inherits the same hierarchy as the
    grantor sees on their own fascicolo, instead of a flat root.
    """
    grant = getattr(request.state, "share_grant", None)
    if grant is None and user is not None:
        grant = getattr(user, "_share_grant", None)
    if grant is None:
        return None
    return getattr(grant, "grantor_subject_id", None)


@router.get("/patients/{patient_id}/tree", response_model=TreeOut)
async def get_tree(
    request: Request,
    patient_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(optional_user)],
    path: str = Query("/", max_length=2048),
    folder_id: uuid.UUID | None = Query(None),
) -> TreeOut:
    """List nodes under ``path`` or ``folder_id`` (folder_id takes priority)."""
    patient = await _get_patient_or_404(db, patient_id, user, request)

    # Anonymous callers see the flat root (no personal folders).
    if user is None:
        nodes = await _root_resource_nodes(db, patient=patient, owner_subject_id=uuid.UUID(int=0))
        breadcrumb = [BreadcrumbEntry(id=None, name=ROOT_LABEL)]
        return TreeOut(
            patient_id=str(patient.id),
            path="/",
            folder_id=None,
            breadcrumb=breadcrumb,
            nodes=nodes,
            total=len(nodes),
        )

    # Shared-link sessions present as the synthetic ``public`` user;
    # the folder tree those callers should see is the grantor's tree,
    # not the empty one keyed on PUBLIC_SUBJECT_ID. Falls through to
    # the caller's own subject for normal authenticated traffic.
    shared_owner = _shared_grantor_subject_id(request, user)
    owner_subject_id = shared_owner if shared_owner is not None else user.subject_id
    tree_folders = await _folders_in_patient_tree(db, patient, owner_subject_id)

    # Materialised patient root (introduced by 0088) is the FK-able
    # representation of "no specific folder". The user must not see it
    # as a regular folder card alongside its own folders, otherwise
    # the synthetic breadcrumb root collides with a real folder of the
    # same display name. We collapse both views by treating path ``/``
    # as "inside the root" when one exists, by resolving deeper paths
    # against root-as-parent, and by stripping the root row from
    # breadcrumb / child listings.
    patient_root_id = (
        await db.execute(
            select(Folder.id).where(
                Folder.patient_id == patient.id,
                Folder.is_root.is_(True),
            )
        )
    ).scalar_one_or_none()

    if folder_id is not None:
        if folder_id not in tree_folders:
            raise HTTPException(status_code=404, detail="folder not found")
        resolved_folder_id: uuid.UUID | None = folder_id
    else:
        resolved_folder_id = await _resolve_path(
            db,
            path,
            owner_subject_id=owner_subject_id,
            tree_folders=tree_folders,
            root_folder_id=patient_root_id,
        )

    if resolved_folder_id is None and patient_root_id is not None:
        resolved_folder_id = patient_root_id

    if resolved_folder_id is None or resolved_folder_id == patient_root_id:
        breadcrumb = [BreadcrumbEntry(id=None, name=ROOT_LABEL)]
    else:
        breadcrumb = await _breadcrumb_for_folder(db, resolved_folder_id, owner_subject_id)

    child_folders = await _child_folders_with_counts(
        db, parent_folder_id=resolved_folder_id, tree_folders=tree_folders
    )
    nodes: list[TreeNode] = [_folder_node(f, n) for f, n in child_folders]
    if resolved_folder_id is None:
        nodes.extend(
            await _root_resource_nodes(db, patient=patient, owner_subject_id=owner_subject_id)
        )
    else:
        nodes.extend(
            await _folder_resource_nodes(db, folder_id=resolved_folder_id, patient=patient)
        )

    current_path = _path_from_breadcrumb(breadcrumb)
    _populate_breadcrumb_paths(breadcrumb)
    _enrich_folder_paths(nodes, current_path)
    await _attach_study_thumbnails(db, nodes)
    await _attach_folder_previews(db, nodes, patient)
    await _attach_document_folder_counts(db, nodes)
    # Layer the recursive preview / counts on top so each folder card
    # surfaces what's actually buried in its sub-tree, not just the
    # direct children. Promotes representative descendants into the
    # ``preview`` stack so e.g. ``2024/`` shows the CT thumbnail
    # buried inside ``2024/TAC/``.
    await _attach_folder_recursive_view(db, nodes, patient)

    current_folder_node: TreeNode | None = None
    if resolved_folder_id is not None and resolved_folder_id != patient_root_id:
        current_folder_orm = (
            await db.execute(select(Folder).where(Folder.id == resolved_folder_id))
        ).scalar_one_or_none()
        if current_folder_orm is not None:
            current_folder_node = _folder_node(current_folder_orm, len(nodes))

    return TreeOut(
        patient_id=str(patient.id),
        path=current_path,
        folder_id=str(resolved_folder_id) if resolved_folder_id else None,
        breadcrumb=breadcrumb,
        nodes=nodes,
        total=len(nodes),
        current_folder=current_folder_node,
    )


@router.get("/patients/{patient_id}/tree/breadcrumb", response_model=list[BreadcrumbEntry])
async def get_breadcrumb(
    request: Request,
    patient_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    item_id: uuid.UUID = Query(...),
    item_kind: str = Query(..., pattern="^(folder|study|series|document|report|consultation)$"),
) -> list[BreadcrumbEntry]:
    patient = await _get_patient_or_404(db, patient_id, user, request)
    owner = user.subject_id
    tree_folders = await _folders_in_patient_tree(db, patient, owner)

    if item_kind == "folder":
        if item_id not in tree_folders:
            raise HTTPException(status_code=404, detail="folder not found")
        bc = await _breadcrumb_for_folder(db, item_id, owner)
        _populate_breadcrumb_paths(bc)
        return bc

    folder_row = (
        await db.execute(
            select(FolderItem.folder_id)
            .join(Folder, Folder.id == FolderItem.folder_id)
            .where(
                FolderItem.resource_kind == item_kind,
                FolderItem.resource_id == item_id,
                Folder.owner_subject_id == owner,
            )
            .limit(1)
        )
    ).first()
    if folder_row is None:
        bc = [BreadcrumbEntry(id=None, name=ROOT_LABEL)]
        _populate_breadcrumb_paths(bc)
        return bc
    bc = await _breadcrumb_for_folder(db, folder_row[0], owner)
    _populate_breadcrumb_paths(bc)
    return bc


@router.post(
    "/patients/{patient_id}/tree/folder",
    response_model=FolderOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_patient_folder(
    request: Request,
    patient_id: uuid.UUID,
    body: FolderCreateIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    audit: AuditDep,
) -> FolderOut:
    """Create a folder scoped to ``patient`` via a 'patient' marker row.

    If F1's polymorphic ``FolderItem`` extension hasn't landed yet, the
    marker insert trips the check constraint and we rollback + 409.
    """
    patient = await _get_patient_or_404(db, patient_id, user, request)
    perms = await effective_permissions_on_patient(db, user=user, patient=patient)
    if READ_METADATA not in perms:
        raise HTTPException(status_code=403, detail="cannot modify this patient's tree")

    if body.parent_folder_id is not None:
        await _owned_folder_or_404(db, body.parent_folder_id, user)

    description = (body.description or "").strip() or None
    folder = Folder(
        name=body.name,
        owner_subject_id=user.subject_id,
        parent_folder_id=body.parent_folder_id,
        description=description,
    )
    db.add(folder)
    await db.flush()

    db.add(
        FolderItem(
            folder_id=folder.id,
            resource_kind=PATIENT_MARKER_KIND,
            resource_id=patient.id,
        )
    )
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail="patient-scoped folders require polymorphic FolderItem (migration pending)",
        ) from None
    await db.refresh(folder)
    await audit.log(
        action="tree_folder_create",
        actor_subject_id=user.subject_id,
        resource_kind="folder",
        resource_id=folder.id,
        metadata={"patient_id": str(patient.id), "name": folder.name},
    )
    return FolderOut(
        id=str(folder.id),
        name=folder.name,
        parent_folder_id=str(folder.parent_folder_id) if folder.parent_folder_id else None,
        created_at=folder.created_at.isoformat(),
        description=folder.description,
    )


class FolderUpdateIn(BaseModel):
    """Patch payload for a fascicolo folder.

    Backwards compatible with the previous ``FolderRenameIn`` schema:
    callers that send only ``{"name": "..."}`` keep working. New
    callers can also patch ``description`` (set to non-empty to
    write, ``""`` or ``null`` to clear, omit to leave alone) and
    ``created_at`` (the user-facing "data di creazione" surfaced in
    the fascicolo card; editable so the clinician can backdate a
    folder that mirrors a real-world episode of care).
    """

    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=500)
    created_at: datetime | None = None


_RENAMABLE_KINDS: frozenset[str] = frozenset({"folder", "study", "document"})


class TreeRenameIn(BaseModel):
    item_kind: str = Field(min_length=1, max_length=32)
    item_id: uuid.UUID
    name: str = Field(min_length=1, max_length=255)


@router.post("/patients/{patient_id}/tree/rename", status_code=status.HTTP_200_OK)
async def rename_tree_item(
    request: Request,
    patient_id: uuid.UUID,
    body: TreeRenameIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    audit: AuditDep,
) -> dict[str, Any]:
    """Rename an item in the patient tree. Dispatches by kind:

    * ``folder``   → ``Folder.name``
    * ``study``    → ``ImagingStudy.study_description`` (display field)
    * ``document`` → ``Document.title``

    Other kinds (series, report, consultation) don't have a writable
    ``name`` surface yet and are rejected with 400.
    """
    patient = await _get_patient_or_404(db, patient_id, user, request)
    perms = await effective_permissions_on_patient(db, user=user, patient=patient)
    if READ_METADATA not in perms:
        raise HTTPException(status_code=403, detail="cannot modify this patient's tree")
    if body.item_kind not in _RENAMABLE_KINDS:
        raise HTTPException(
            status_code=400, detail=f"item kind {body.item_kind!r} cannot be renamed"
        )

    new_name = body.name.strip()
    if not new_name:
        raise HTTPException(status_code=400, detail="name cannot be empty")

    if body.item_kind == "folder":
        folder = await _owned_folder_or_404(db, body.item_id, user)
        tree_folders = await _folders_in_patient_tree(db, patient, user.subject_id)
        if folder.id not in tree_folders:
            raise HTTPException(status_code=404, detail="folder not found in patient tree")
        folder.name = new_name
    elif body.item_kind == "study":
        from bvphoenix.db.models import ImagingStudy

        study = (
            await db.execute(
                select(ImagingStudy).where(
                    ImagingStudy.id == body.item_id, ImagingStudy.patient_id == patient.id
                )
            )
        ).scalar_one_or_none()
        if study is None:
            raise HTTPException(status_code=404, detail="study not found")
        study.study_description = new_name
    elif body.item_kind == "document":
        doc = (
            await db.execute(
                select(Document).where(
                    Document.id == body.item_id,
                    Document.patient_id == patient.id,
                )
            )
        ).scalar_one_or_none()
        if doc is None:
            raise HTTPException(status_code=404, detail="document not found")
        doc.title = new_name

    await db.commit()
    await audit.log(
        action=f"tree_{body.item_kind}_rename",
        actor_subject_id=user.subject_id,
        resource_kind=body.item_kind,
        resource_id=body.item_id,
        metadata={"patient_id": str(patient.id), "new_name": new_name},
    )
    return {
        "status": "renamed",
        "item_kind": body.item_kind,
        "item_id": str(body.item_id),
        "name": new_name,
    }


@router.patch(
    "/patients/{patient_id}/tree/folder/{folder_id}",
    response_model=FolderOut,
)
async def update_patient_folder(
    request: Request,
    patient_id: uuid.UUID,
    folder_id: uuid.UUID,
    body: FolderUpdateIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    audit: AuditDep,
) -> FolderOut:
    """Edit folder metadata in the patient tree.

    Accepts ``name`` and / or ``description``. Both follow the
    "exclude_unset" convention: omit to leave alone, send ``""`` /
    ``null`` to clear (description only; name cannot be cleared).
    """
    patient = await _get_patient_or_404(db, patient_id, user, request)
    folder = await _owned_folder_or_404(db, folder_id, user)

    tree_folders = await _folders_in_patient_tree(db, patient, user.subject_id)
    if folder.id not in tree_folders:
        raise HTTPException(status_code=404, detail="folder not found in patient tree")

    update_data = body.model_dump(exclude_unset=True)
    audit_meta: dict[str, str | None] = {"patient_id": str(patient.id)}
    if "name" in update_data and update_data["name"] is not None:
        folder.name = update_data["name"].strip()
        audit_meta["new_name"] = folder.name
    if "description" in update_data:
        raw = update_data["description"]
        folder.description = (raw or "").strip() or None
        audit_meta["new_description"] = folder.description
    if "created_at" in update_data and update_data["created_at"] is not None:
        # User-controlled date for the fascicolo card. The DB column
        # carries this verbatim; ``updated_at`` keeps tracking the
        # actual last edit thanks to the ``onupdate=now()`` hook.
        folder.created_at = update_data["created_at"]
        audit_meta["new_created_at"] = folder.created_at.isoformat()
    await db.commit()
    await db.refresh(folder)
    await audit.log(
        action="tree_folder_update",
        actor_subject_id=user.subject_id,
        resource_kind="folder",
        resource_id=folder.id,
        metadata=audit_meta,
    )
    return FolderOut(
        id=str(folder.id),
        name=folder.name,
        parent_folder_id=str(folder.parent_folder_id) if folder.parent_folder_id else None,
        created_at=folder.created_at.isoformat(),
        description=folder.description,
    )


@router.delete(
    "/patients/{patient_id}/tree/folder/{folder_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_patient_folder(
    request: Request,
    patient_id: uuid.UUID,
    folder_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    audit: AuditDep,
) -> None:
    """Delete a folder. Child folders CASCADE; non-folder resources
    (studies, documents) survive and re-surface at the root."""
    patient = await _get_patient_or_404(db, patient_id, user, request, action=DELETE)
    folder = await _owned_folder_or_404(db, folder_id, user)

    tree_folders = await _folders_in_patient_tree(db, patient, user.subject_id)
    if folder.id not in tree_folders:
        raise HTTPException(status_code=404, detail="folder not found in patient tree")

    await db.delete(folder)
    await db.commit()
    await audit.log(
        action="tree_folder_delete",
        actor_subject_id=user.subject_id,
        resource_kind="folder",
        resource_id=folder.id,
        metadata={"patient_id": str(patient.id)},
    )


@router.post("/patients/{patient_id}/tree/move", status_code=status.HTTP_200_OK)
async def move_item(
    request: Request,
    patient_id: uuid.UUID,
    body: MoveIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    audit: AuditDep,
) -> dict[str, Any]:
    """Move an item to ``target_folder_id`` (None = root)."""
    patient = await _get_patient_or_404(db, patient_id, user, request)
    perms = await effective_permissions_on_patient(db, user=user, patient=patient)
    if READ_METADATA not in perms:
        raise HTTPException(status_code=403, detail="cannot modify this patient's tree")

    if body.item_kind not in MOVABLE_KINDS:
        raise HTTPException(status_code=404, detail=f"unsupported item kind {body.item_kind!r}")

    if not await _item_belongs_to_patient(db, body.item_kind, body.item_id, patient):
        raise HTTPException(status_code=404, detail="item not found for this patient")

    if body.target_folder_id is not None:
        await _owned_folder_or_404(db, body.target_folder_id, user)

    if body.item_kind == "folder":
        # Folder→folder nesting is materialised by ``Folder.parent_folder_id``
        # directly, NOT by a row in ``folder_items``: the polymorphic
        # ``folder_items.resource_kind`` CHECK does not include
        # ``'folder'`` (only ``'subfolder'`` and the heterogeneous
        # leaves), so the prior code path raised IntegrityError →
        # 404 "item kind 'folder' not yet supported". Refuse self-
        # parenting and refuse moving a folder under one of its own
        # descendants — both would create a cycle.
        if body.target_folder_id == body.item_id:
            raise HTTPException(status_code=400, detail="cannot nest folder in itself")
        if body.target_folder_id is not None:
            descendants_stmt = text(
                """
                WITH RECURSIVE descendants AS (
                    SELECT id FROM folders WHERE id = :root
                    UNION ALL
                    SELECT f.id FROM folders f
                    JOIN descendants d ON f.parent_folder_id = d.id
                )
                SELECT 1 FROM descendants WHERE id = :target LIMIT 1
                """
            )
            cycle = (
                await db.execute(
                    descendants_stmt,
                    {"root": body.item_id, "target": body.target_folder_id},
                )
            ).first()
            if cycle is not None:
                raise HTTPException(
                    status_code=400,
                    detail="cannot move a folder into one of its descendants (cycle)",
                )
        moved_folder = (
            await db.execute(select(Folder).where(Folder.id == body.item_id))
        ).scalar_one()
        moved_folder.parent_folder_id = body.target_folder_id
        await db.commit()
    else:
        # Drive semantics for leaf items (study / series / document /
        # report / consultation): drag-to-move keeps the item present
        # in exactly one folder among the caller's owned folders. We
        # remove any existing placements owned by the caller and then
        # re-insert under the new parent. ``target_folder_id = None``
        # historically meant "root" — under the post-0088 model the
        # patient root is a real folder, so we translate NULL to the
        # patient's materialised root before insert. Leaving NULL
        # would either drop the item to zero containment (forbidden by
        # ``trg_folder_items_no_orphan_doc`` for documents) or create
        # an inconsistent state for other resource kinds.
        await db.execute(
            delete(FolderItem).where(
                FolderItem.resource_kind == body.item_kind,
                FolderItem.resource_id == body.item_id,
                FolderItem.folder_id.in_(
                    select(Folder.id).where(Folder.owner_subject_id == user.subject_id)
                ),
            )
        )
        target_folder_id = body.target_folder_id
        if target_folder_id is None:
            from bvphoenix.services.folders import get_or_create_root_folder

            root = await get_or_create_root_folder(db, patient)
            target_folder_id = root.id
        db.add(
            FolderItem(
                folder_id=target_folder_id,
                resource_kind=body.item_kind,
                resource_id=body.item_id,
            )
        )
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            raise HTTPException(
                status_code=404,
                detail=f"item kind {body.item_kind!r} not yet supported by the folder schema",
            ) from None

    await audit.log(
        action="tree_move",
        actor_subject_id=user.subject_id,
        resource_kind=body.item_kind,
        resource_id=body.item_id,
        metadata={
            "patient_id": str(patient.id),
            "target_folder_id": str(body.target_folder_id) if body.target_folder_id else None,
        },
    )
    return {
        "status": "moved",
        "item_kind": body.item_kind,
        "item_id": str(body.item_id),
        "target_folder_id": str(body.target_folder_id) if body.target_folder_id else None,
    }
