"""Folder model. Organisational axis: Drive-style container for documents and links, no clinical semantics, no place on the timeline.

A folder groups heterogeneous medical items (studies, series,
report_contents, markers, clinical_notes, documents, consultations,
sub-folders) belonging either to a platform user's personal workspace
or, when ``patient_id`` is set, to a patient's fascicolo (clinical
dossier).

Grants on a folder cascade to every item it contains. The cascade is
materialised in the grants table when a folder is shared, so the
normal permission-resolution path (``services.permissions``) keeps
working without special-casing folder membership at read time.

Root folder: every patient has a *materialised* root folder, marked
by ``folders.is_root = TRUE`` and uniquely indexed per ``patient_id``
(``uq_folders_root_per_patient``, partial unique). The root folder
is created in the same transaction as the patient and is non-deletable
and non-renamable. Documents that the user has not filed under any
specific folder live under the root, so every live document has at
least one folder containment row by construction (enforced by
``trg_folder_items_no_orphan_doc`` + ``trg_documents_restore_no_orphan``,
both ``DEFERRABLE INITIALLY DEFERRED`` to allow the service layer to
stage unlink + soft-delete in a single transaction).

Conceptual placement: see ``docs/data-model.md §0`` (three-axis
model) and ``docs/fascicolo-drive-ux.md``. Folder is orthogonal to
``ClinicalEvent`` (temporal, atomic) and ``CarePhase`` (temporal,
grouping); ``Tag`` labels imaging targets and is orthogonal to all
three.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from bvphoenix.db.base import Base
from bvphoenix.db.models._common import UpdatedAtMixin, uuid_pk

# Polymorphic item kinds allowed inside a folder. ``subfolder`` is the
# self-referential case (a folder containing another folder); it's
# kept alongside the other kinds for uniform cascade handling.
FOLDER_ITEM_KINDS: tuple[str, ...] = (
    "study",
    "series",
    "report",
    "annotation",
    "document",
    "consultation",
    "subfolder",
)


class Folder(UpdatedAtMixin, Base):
    __tablename__ = "folders"

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # Short navigation aid (≤ 500 chars), rendered in the grid hover
    # preview alongside per-kind item counts. For longer clinical
    # commentary use ``narrative_md`` below.
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Free-form Markdown commentary on the folder's clinical context
    # (synthesis of the bundled items + outcome + correlations across
    # the fascicolo). No length cap. Separate from ``description`` so
    # the short-tile + long-narrative split stays clean: the FE shows
    # ``description`` in the hover preview and ``narrative_md`` in a
    # dedicated detail panel.
    narrative_md: Mapped[str | None] = mapped_column(Text, nullable=True)
    owner_subject_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("subjects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    parent_folder_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("folders.id", ondelete="CASCADE"),
        index=True,
    )
    # NULL → user-owned folder (personal workspace).
    # NOT NULL → folder lives inside the patient's fascicolo and
    # inherits grants from the patient-level ACL.
    patient_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("patients.id", ondelete="CASCADE"),
        index=True,
    )
    # Optional clinical / display date — what the folder REPRESENTS in
    # the patient timeline (e.g. ``2024-09-16`` for a folder grouping
    # the studies of that day). Distinct from ``created_at`` (the
    # immutable system audit timestamp). Editable via PATCH; UI sorts
    # and labels by this when set, falling back to ``created_at``.
    clinical_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    # Materialised patient root folder marker. Exactly one folder per
    # patient has ``is_root = TRUE`` (enforced by partial unique index
    # ``uq_folders_root_per_patient``). Service-layer guarantees:
    # the root is created at patient registration, non-renamable, and
    # non-deletable. Newly ingested documents without an explicit
    # ``folder_id`` are auto-attached to it so the no-orphan invariant
    # holds at all times.
    is_root: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=func.false())
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "is_root = FALSE OR (patient_id IS NOT NULL AND parent_folder_id IS NULL)",
            name="ck_folders_root_shape",
        ),
        Index(
            "uq_folders_root_per_patient",
            "patient_id",
            unique=True,
            postgresql_where="is_root",
        ),
    )


class FolderItem(Base):
    __tablename__ = "folder_items"

    folder_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("folders.id", ondelete="CASCADE"),
        primary_key=True,
    )
    resource_kind: Mapped[str] = mapped_column(String(16), primary_key=True)
    resource_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "resource_kind IN (" + ",".join(f"'{k}'" for k in FOLDER_ITEM_KINDS) + ")",
            name="ck_folder_items_kind",
        ),
        Index("ix_folder_items_resource", "resource_kind", "resource_id"),
    )
