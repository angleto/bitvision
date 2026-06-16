"""Folders API — hierarchical grouping with inherited sharing.

A folder is a named polymorphic container (studies, series, reports,
annotations, documents, consultations, sub-folders). It can be:

* **user-owned** — ``patient_id`` is NULL, lives in the user's personal
  workspace, behaves like a Google Drive folder;
* **patient-scoped** — ``patient_id`` is set, lives inside the patient
  fascicolo and inherits grants from the patient-level ACL.

Sharing a folder materialises one grant per contained item plus one
grant on the folder itself. The per-item grants are what downstream
permission checks (``services.permissions``) actually see, so nothing
in the read path needs to know about folders — the cascade happens
at share time.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.auth import enforce_agent_patient_scope, require_user
from bvphoenix.db.models import Document, Folder, FolderItem, Grant, Patient, User
from bvphoenix.db.models.folders import FOLDER_ITEM_KINDS
from bvphoenix.db.session import get_db
from bvphoenix.services.access_levels import level_to_permissions
from bvphoenix.services.folders import link_resource_to_folder

router = APIRouter(prefix="/folders", tags=["folders"])

# Subset of ``FOLDER_ITEM_KINDS`` that also appears in the Grant
# check-constraint. ``document`` / ``consultation`` / ``subfolder``
# are folder-only concepts today; when we add first-class grants for
# them the lookup below can be relaxed.
_ITEM_KIND_TO_GRANT_KIND: dict[str, str] = {
    "study": "study",
    "series": "series",
    "annotation": "annotation",
    "subfolder": "folder",
}

_FOLDER_ITEM_PATTERN = "^(" + "|".join(FOLDER_ITEM_KINDS) + ")$"


def _block_root_mutation(folder: Folder, update_data: dict[str, object]) -> None:
    """Reject rename / reparent / description-overwrite of a patient
    root folder. Other PATCH-able fields (``narrative_md``,
    ``clinical_date``) remain editable so the user can attach
    commentary to the implicit "everything for this patient" view."""
    if not folder.is_root:
        return
    forbidden = {"name", "parent_folder_id"}
    touched = forbidden & set(update_data.keys())
    if touched:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "root_folder_protected",
                "message": "patient root folder cannot be renamed or reparented",
                "fields": sorted(touched),
            },
        )


class FolderCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    parent_folder_id: uuid.UUID | None = None
    patient_id: uuid.UUID | None = None
    description: str | None = Field(default=None, max_length=500)
    # Free-form Markdown commentary; no length cap. Use for the
    # extended clinical narrative on the folder (synthesis + outcome
    # + correlations). Short hover-preview text stays in
    # ``description``.
    narrative_md: str | None = None
    # Optional clinical / display date the folder represents in the
    # patient timeline (e.g. ``2024-09-16`` for the day's TC).
    # Distinct from ``created_at`` (system audit). NULL = fall back
    # to ``created_at`` in the FE.
    clinical_date: date | None = None


class FolderUpdateIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    parent_folder_id: uuid.UUID | None = None
    # ``description`` follows the "exclude_unset" convention: omit the
    # key to leave the field alone, send ``""`` or ``null`` to clear it,
    # send a non-empty string to set it.
    description: str | None = Field(default=None, max_length=500)
    # ``narrative_md`` accepts the same convention as ``description``
    # (omit / null=clear / non-empty=set), no length cap.
    narrative_md: str | None = None
    # ``clinical_date`` follows the same convention: omit to leave
    # alone, ``null`` to clear (fall back to ``created_at`` for
    # display), a real date to set. ``created_at`` itself is system
    # audit and remains immutable.
    clinical_date: date | None = None


class FolderOut(BaseModel):
    id: str
    name: str
    owner_subject_id: str
    parent_folder_id: str | None
    patient_id: str | None
    created_at: str
    clinical_date: str | None = None
    description: str | None = None
    narrative_md: str | None = None
    # Materialised patient root flag (post-0088). Always false for
    # user-workspace folders. The FE uses this to hide the row from
    # listings + pickers (the root folder is invisible to the user;
    # path ``/`` opens its contents directly).
    is_root: bool = False


class FolderItemIn(BaseModel):
    resource_kind: str = Field(pattern=_FOLDER_ITEM_PATTERN)
    resource_id: uuid.UUID


class FolderDetailOut(FolderOut):
    items: list[dict]


class FolderShareIn(BaseModel):
    grantee_subject_id: uuid.UUID
    access_level: str = Field(pattern="^(viewer|editor|manager)$")
    download: bool = False
    expires_in_hours: int | None = Field(default=None, gt=0)
    label: str | None = Field(default=None, max_length=255)


class FolderShareOut(BaseModel):
    folder_grant_id: str
    cascaded_grant_ids: list[str]
    permissions: list[str]
    expires_at: str | None


def _folder_out(folder: Folder) -> FolderOut:
    return FolderOut(
        id=str(folder.id),
        name=folder.name,
        owner_subject_id=str(folder.owner_subject_id),
        parent_folder_id=str(folder.parent_folder_id) if folder.parent_folder_id else None,
        patient_id=str(folder.patient_id) if folder.patient_id else None,
        created_at=folder.created_at.isoformat(),
        clinical_date=folder.clinical_date.isoformat() if folder.clinical_date else None,
        description=folder.description,
        narrative_md=folder.narrative_md,
        is_root=folder.is_root,
    )


async def _load_owned_folder(
    db: AsyncSession, folder_id: uuid.UUID, user: User, request: Request
) -> Folder:
    folder = (await db.execute(select(Folder).where(Folder.id == folder_id))).scalar_one_or_none()
    if folder is None or not (user.is_admin or folder.owner_subject_id == user.subject_id):
        raise HTTPException(status_code=404, detail="folder not found")
    # Agent token scope: when the folder is patient-scoped, refuse
    # the operation if the agent's token is bound to a different
    # patient. No-op for human callers and for user-owned folders
    # (folder.patient_id is None).
    enforce_agent_patient_scope(request, folder.patient_id, scope="patient:read")
    return folder


@router.post("", response_model=FolderOut, status_code=status.HTTP_201_CREATED)
async def create_folder(
    request: Request,
    body: FolderCreateIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
) -> FolderOut:
    # patient_id propagates top-down through the folder tree. The
    # subtree root declares the scope; every descendant inherits it.
    # Without this rule a UI/agent could create a subfolder under a
    # patient-scoped parent without setting patient_id, leaving an
    # orphan branch that is invisible to _folders_in_patient_tree
    # (404 on _resolve_path) and hides any documents underneath.
    effective_patient_id = body.patient_id
    if body.parent_folder_id is not None:
        parent = (
            await db.execute(select(Folder).where(Folder.id == body.parent_folder_id))
        ).scalar_one_or_none()
        if parent is None:
            raise HTTPException(status_code=404, detail="parent folder not found")
        # Visibility on the parent comes from EITHER personal
        # ownership (user-workspace folder) OR access to the patient
        # the parent is scoped to (fascicolo folder). The patient
        # access itself is enforced just below by the
        # ``managed_by_subject_id``/``self_user`` check; here we only
        # need to refuse callers who have neither lens on the parent.
        if not (
            user.is_admin
            or parent.owner_subject_id == user.subject_id
            or parent.patient_id is not None
        ):
            raise HTTPException(status_code=404, detail="parent folder not found")
        if body.patient_id is None:
            effective_patient_id = parent.patient_id
        elif body.patient_id != parent.patient_id:
            raise HTTPException(
                status_code=400,
                detail=(
                    "subfolder patient_id must match parent: parent="
                    f"{parent.patient_id}, requested={body.patient_id}"
                ),
            )

    # When creating inside a patient fascicolo, the caller must either
    # manage the patient or be the patient themselves. Admins bypass.
    if effective_patient_id is not None:
        patient = (
            await db.execute(select(Patient).where(Patient.id == effective_patient_id))
        ).scalar_one_or_none()
        if patient is None:
            raise HTTPException(status_code=404, detail="patient not found")
        # Agent token scope: refuse cross-patient folder creation via
        # a token bound to a different patient.
        enforce_agent_patient_scope(request, patient.id)
        if not (
            user.is_admin
            or patient.managed_by_subject_id == user.subject_id
            or patient.self_user_subject_id == user.subject_id
        ):
            raise HTTPException(status_code=403, detail="cannot create folder for this patient")
    folder = Folder(
        name=body.name,
        owner_subject_id=user.subject_id,
        parent_folder_id=body.parent_folder_id,
        patient_id=effective_patient_id,
        description=(body.description or None),
        narrative_md=(body.narrative_md or None),
        clinical_date=body.clinical_date,
    )
    db.add(folder)
    await db.commit()
    await db.refresh(folder)
    return _folder_out(folder)


@router.get("", response_model=list[FolderOut])
async def list_folders(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    patient_id: uuid.UUID | None = None,
) -> list[FolderOut]:
    # Refuse listing folders of a patient outside the agent token's
    # scope before touching the DB. When ``patient_id`` is None the
    # query is restricted to the caller's personal workspace so no
    # fascicolo enumeration is possible.
    enforce_agent_patient_scope(request, patient_id, scope="patient:read")
    stmt = select(Folder).where(Folder.owner_subject_id == user.subject_id)
    if patient_id is not None:
        stmt = stmt.where(Folder.patient_id == patient_id)
    stmt = stmt.order_by(Folder.name)
    rows = (await db.execute(stmt)).scalars().all()
    return [_folder_out(f) for f in rows]


@router.get("/{folder_id}", response_model=FolderDetailOut)
async def get_folder(
    request: Request,
    folder_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
) -> FolderDetailOut:
    folder = await _load_owned_folder(db, folder_id, user, request)
    items = (
        (await db.execute(select(FolderItem).where(FolderItem.folder_id == folder.id)))
        .scalars()
        .all()
    )
    base = _folder_out(folder)
    return FolderDetailOut(
        **base.model_dump(),
        items=[
            {
                "resource_kind": i.resource_kind,
                "resource_id": str(i.resource_id),
                "added_at": i.added_at.isoformat(),
            }
            for i in items
        ],
    )


class FolderExportItemOut(BaseModel):
    """Enriched folder item for the export-picker UI.

    The bare FolderDetail.items returns only ``(kind, id, added_at)``
    — not enough to render a checkbox list with sizes / modalities /
    titles. This endpoint resolves each item to a display-ready
    summary so the dialog can show "TC torace 2024-12-01 · TC ·
    420 MB" or "DVD-DICOM Mamma 2025 · 4.7 GB" with one round-trip.
    Read-only; no side effects.
    """

    resource_kind: str
    resource_id: str
    name: str | None = None
    size_bytes: int | None = None
    file_count: int | None = None
    modality: str | None = None
    document_type: str | None = None
    document_date: str | None = None
    study_date: str | None = None


@router.get("/{folder_id}/export-items", response_model=list[FolderExportItemOut])
async def list_folder_export_items(
    request: Request,
    folder_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
) -> list[FolderExportItemOut]:
    """Per-item display data so the export dialog can render a
    deselectable checkbox list. Exposes only the fields the UI needs
    (name / size / kind hints), not the full study/document records.
    """
    from sqlalchemy import func as _func

    from bvphoenix.db.models import Document, DocumentFile, ImagingStudy
    from bvphoenix.db.models import Instance as _Instance

    folder = await _load_owned_folder(db, folder_id, user, request)
    items = (
        (await db.execute(select(FolderItem).where(FolderItem.folder_id == folder.id)))
        .scalars()
        .all()
    )

    study_ids = [i.resource_id for i in items if i.resource_kind == "study"]
    document_ids = [i.resource_id for i in items if i.resource_kind == "document"]

    studies_by_id: dict[uuid.UUID, ImagingStudy] = {}
    if study_ids:
        rows = (
            (await db.execute(select(ImagingStudy).where(ImagingStudy.id.in_(study_ids))))
            .scalars()
            .all()
        )
        studies_by_id = {s.id: s for s in rows}
    # Per-study size/file-count via SUM of Instance.size_bytes — same
    # query as the share-link landing tile so two displays stay
    # consistent.
    study_size: dict[uuid.UUID, tuple[int, int]] = {}
    if study_ids:
        # Two-hop join: Instance → Series → ImagingStudy. Instance has
        # no direct study_id column (memory: same trap caught a bug
        # in /info that 500'd freshly-issued share links).
        from bvphoenix.db.models.dicom import Series as _Series

        agg_rows = (
            await db.execute(
                select(
                    _Series.study_id,
                    _func.count(_Instance.id),
                    _func.coalesce(_func.sum(_Instance.size_bytes), 0),
                )
                .join(_Series, _Series.id == _Instance.series_id)
                .where(_Series.study_id.in_(study_ids))
                .group_by(_Series.study_id)
            )
        ).all()
        for sid, n, total in agg_rows:
            study_size[sid] = (int(n), int(total))

    docs_by_id: dict[uuid.UUID, Document] = {}
    if document_ids:
        rows = (
            (await db.execute(select(Document).where(Document.id.in_(document_ids))))
            .scalars()
            .all()
        )
        docs_by_id = {d.id: d for d in rows}
    # Per-document size = SUM over DocumentFile rows. Documents may
    # be inline-text only (no file rows; size 0) or hold multiple
    # files (DICOM ISOs split into segments, scanned-paper PDF +
    # cover image). The dialog needs the aggregate to render
    # "DVD-DICOM Mamma · 4.7 GB" so the user knows what to skip.
    doc_size: dict[uuid.UUID, int] = {}
    if document_ids:
        agg_rows = (
            await db.execute(
                select(
                    DocumentFile.document_id,
                    _func.coalesce(_func.sum(DocumentFile.size_bytes), 0),
                )
                .where(DocumentFile.document_id.in_(document_ids))
                .group_by(DocumentFile.document_id)
            )
        ).all()
        for did, total in agg_rows:
            doc_size[did] = int(total)

    out: list[FolderExportItemOut] = []
    for item in items:
        if item.resource_kind == "study":
            study = studies_by_id.get(item.resource_id)
            if study is None:
                continue
            n, total = study_size.get(study.id, (0, 0))
            out.append(
                FolderExportItemOut(
                    resource_kind="study",
                    resource_id=str(study.id),
                    name=study.study_description or None,
                    size_bytes=total,
                    file_count=n,
                    modality=", ".join(study.modalities or []) or None,
                    study_date=str(study.study_date) if study.study_date else None,
                )
            )
        elif item.resource_kind == "document":
            doc = docs_by_id.get(item.resource_id)
            if doc is None:
                continue
            out.append(
                FolderExportItemOut(
                    resource_kind="document",
                    resource_id=str(doc.id),
                    name=doc.title or None,
                    size_bytes=doc_size.get(doc.id, 0),
                    # ``kind_id`` is the v3 LOINC-aligned classifier
                    # (e.g. "dicom_iso", "imaging_report"); the FE
                    # uses it to render a chip + flag heavyweight
                    # categories (ISO ≈ multi-GB) the user may want
                    # to deselect first.
                    document_type=doc.kind_id,
                    document_date=str(doc.document_date) if doc.document_date else None,
                )
            )
        # subfolder / report / annotation / consultation: not
        # selectable in the export picker (their files are emitted
        # implicitly as part of the parent study). Skip.
    # Stable order: studies first by study_date desc, then documents
    # by document_date desc, with name as tiebreaker. Doctors expect
    # "most recent on top".
    out.sort(
        key=lambda r: (
            0 if r.resource_kind == "study" else 1,
            -(int((r.study_date or r.document_date or "0").replace("-", "")) or 0),
            r.name or "",
        )
    )
    return out


@router.patch("/{folder_id}", response_model=FolderOut)
async def update_folder(
    request: Request,
    folder_id: uuid.UUID,
    body: FolderUpdateIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
) -> FolderOut:
    folder = await _load_owned_folder(db, folder_id, user, request)
    update_data = body.model_dump(exclude_unset=True)
    # The patient root is the materialised default container; renaming
    # or reparenting it would either break the no-orphan invariant
    # (parent move) or surface an empty/odd label to every patient
    # whose root the user touched (rename). Both are rejected with
    # 409 ``root_folder_protected``.
    _block_root_mutation(folder, update_data)
    if "name" in update_data and update_data["name"] is not None:
        folder.name = update_data["name"]
    if "parent_folder_id" in update_data and update_data["parent_folder_id"] is not None:
        new_parent_id = update_data["parent_folder_id"]
        if new_parent_id == folder.id:
            raise HTTPException(status_code=400, detail="cannot nest folder in itself")
        new_parent = (
            await db.execute(select(Folder).where(Folder.id == new_parent_id))
        ).scalar_one_or_none()
        if new_parent is None:
            raise HTTPException(status_code=404, detail="parent folder not found")
        # Same lens as create_folder: ownership OR patient-scope.
        if not (
            user.is_admin
            or new_parent.owner_subject_id == user.subject_id
            or new_parent.patient_id is not None
        ):
            raise HTTPException(status_code=404, detail="parent folder not found")
        # patient_id is the subtree's scope and must match across the
        # parent edge. A move that crosses scopes (None ↔ X, X ↔ Y)
        # would either orphan the moved branch from the fascicolo or
        # silently graft it onto a different patient — both states
        # the rest of the system cannot represent safely. Forbid.
        if new_parent.patient_id != folder.patient_id:
            raise HTTPException(
                status_code=400,
                detail=(
                    "cross-patient folder move forbidden: folder.patient_id="
                    f"{folder.patient_id}, new parent.patient_id={new_parent.patient_id}"
                ),
            )
        folder.parent_folder_id = new_parent_id
    if "description" in update_data:
        # Empty string and explicit null both clear the field; a
        # non-empty trimmed string sets it. The CHECK in Pydantic
        # already enforced the 500-char cap.
        raw = update_data["description"]
        folder.description = (raw or "").strip() or None
    if "narrative_md" in update_data:
        # Same exclude_unset convention; no length cap.
        raw = update_data["narrative_md"]
        folder.narrative_md = (raw or "").strip() or None
    if "clinical_date" in update_data:
        # Explicit ``null`` clears (FE / tree falls back to
        # ``created_at`` for display); a real ``YYYY-MM-DD`` sets it.
        # Omit the key to leave alone.
        folder.clinical_date = update_data["clinical_date"]
    await db.commit()
    await db.refresh(folder)
    return _folder_out(folder)


@router.post("/{folder_id}/items", status_code=status.HTTP_201_CREATED)
async def add_item_to_folder(
    request: Request,
    folder_id: uuid.UUID,
    body: FolderItemIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
) -> dict:
    folder = await _load_owned_folder(db, folder_id, user, request)
    # Idempotent: filing an already-filed resource into the same folder
    # is a no-op, mirroring ``remove_item_from_folder`` which 204s when
    # the link is already absent. Avoids a 500 on the common
    # double-click / retry path.
    inserted = await link_resource_to_folder(
        db,
        folder_id=folder.id,
        resource_kind=body.resource_kind,
        resource_id=body.resource_id,
    )
    await db.commit()
    return {"status": "added" if inserted else "already_present"}


@router.delete(
    "/{folder_id}/items/{resource_kind}/{resource_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def remove_item_from_folder(
    request: Request,
    folder_id: uuid.UUID,
    resource_kind: str,
    resource_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
) -> None:
    """Smart-delete: rimuove la riga ``folder_items`` per la coppia
    (folder, resource). Per i documenti applica due regole specifiche:

    * se dopo l'unlink il documento ha ancora ≥ 1 folder_items
      (hardlink in altre cartelle), si limita a rimuovere la riga e
      ritorna 204; il documento sopravvive.
    * se era l'ultima copia (``folder_count`` scende a 0), il backend
      esegue un soft-delete del documento nella stessa transazione.
      Se ci sono reference cliniche attive, l'intera transazione fa
      rollback e il client riceve 409 con ``blocking_references`` per
      orchestrare la pulizia preliminare.

    Per kinds non-document (study/series/...) il comportamento è
    invariato: rimuove la riga e basta.
    """
    await _load_owned_folder(db, folder_id, user, request)
    item = (
        await db.execute(
            select(FolderItem).where(
                FolderItem.folder_id == folder_id,
                FolderItem.resource_kind == resource_kind,
                FolderItem.resource_id == resource_id,
            )
        )
    ).scalar_one_or_none()
    if item is None:
        return None

    if resource_kind != "document":
        await db.delete(item)
        await db.commit()
        return None

    # Document path: need to know if this is the last hardlink.
    await db.delete(item)
    await db.flush()
    new_folder_count = (
        await db.execute(
            select(func.count(FolderItem.folder_id)).where(
                FolderItem.resource_kind == "document",
                FolderItem.resource_id == resource_id,
            )
        )
    ).scalar_one()
    if new_folder_count >= 1:
        await db.commit()
        return None

    # Last hardlink → trigger soft-delete with reference guard.
    from bvphoenix.services.documents.references import collect_blocking_references

    blocking = await collect_blocking_references(db, resource_id)
    if blocking:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail={
                "code": "document_has_active_references",
                "message": (
                    "questa è l'ultima copia del documento e non può essere "
                    "rimossa finché esistono reference cliniche attive"
                ),
                "blocking_references": blocking,
            },
        )

    doc = (
        await db.execute(select(Document).where(Document.id == resource_id))
    ).scalar_one_or_none()
    if doc is None or doc.deleted_at is not None:
        # Race: someone else already tombstoned. Commit the unlink.
        await db.commit()
        return None
    doc.deleted_at = datetime.now(UTC)
    doc.purge_after = None  # git-like: never auto-purge
    doc.delete_reason = "last_hardlink_removed"
    await db.commit()
    return None


@router.delete("/{folder_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_folder(
    request: Request,
    folder_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
) -> None:
    folder = await _load_owned_folder(db, folder_id, user, request)
    if folder.is_root:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "root_folder_protected",
                "message": "patient root folder cannot be deleted",
            },
        )
    await db.delete(folder)
    await db.commit()


@router.post(
    "/{folder_id}/share",
    response_model=FolderShareOut,
    status_code=status.HTTP_201_CREATED,
)
async def share_folder(
    request: Request,
    folder_id: uuid.UUID,
    body: FolderShareIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
) -> FolderShareOut:
    """Share a folder — the grant cascades to every contained item.

    One grant is created on the folder itself (for enumeration) and
    one per item whose ``resource_kind`` maps to a grant-level
    resource. Sub-folders are NOT walked recursively: the child
    folder's own grant — if the grantor wants it — must be issued
    separately. This keeps the cascade explicit and auditable.
    """
    folder = await _load_owned_folder(db, folder_id, user, request)

    valid_until = None
    if body.expires_in_hours is not None:
        valid_until = datetime.now(UTC) + timedelta(hours=body.expires_in_hours)
    perms = level_to_permissions(body.access_level, download=body.download)
    purpose = body.label or f"{body.access_level} access via folder {folder.name}"

    # 1) Folder-level grant — lets the grantee list the folder.
    folder_grant = Grant(
        resource_kind="folder",
        resource_id=folder.id,
        grantor_subject_id=user.subject_id,
        grantee_subject_id=body.grantee_subject_id,
        permissions=perms,
        conditions={"scope": "folder"},
        valid_until=valid_until,
        purpose=purpose,
    )
    db.add(folder_grant)
    await db.flush()

    # 2) One cascaded grant per item whose kind has a first-class
    # grant target. ``parent_grant_id`` links each child grant to
    # the folder grant for audit and bulk-revocation by parent id.
    # Kinds without a grant-level target (report / document /
    # consultation) rely on ``can_access_folder`` at the item API
    # level — their endpoints must consult the folder grant
    # explicitly until those resource kinds are added to the
    # grants CHECK constraint.
    items = (
        (await db.execute(select(FolderItem).where(FolderItem.folder_id == folder.id)))
        .scalars()
        .all()
    )
    cascaded: list[Grant] = [
        Grant(
            resource_kind=grant_kind,
            resource_id=item.resource_id,
            grantor_subject_id=user.subject_id,
            grantee_subject_id=body.grantee_subject_id,
            parent_grant_id=folder_grant.id,
            permissions=perms,
            conditions={"scope": "folder", "folder_id": str(folder.id)},
            valid_until=valid_until,
            purpose=purpose,
        )
        for item in items
        if (grant_kind := _ITEM_KIND_TO_GRANT_KIND.get(item.resource_kind)) is not None
    ]
    db.add_all(cascaded)
    await db.commit()
    await db.refresh(folder_grant)

    return FolderShareOut(
        folder_grant_id=str(folder_grant.id),
        cascaded_grant_ids=[str(g.id) for g in cascaded],
        permissions=perms,
        expires_at=folder_grant.valid_until.isoformat() if folder_grant.valid_until else None,
    )
