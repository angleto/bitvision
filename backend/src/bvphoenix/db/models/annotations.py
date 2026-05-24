"""Tag, TagAlias models. Cross-cutting axis.

Tags are flat ``namespace:value`` labels on imaging targets and
curated datasets. The schema CHECK constraint
``ck_tags_target_kind`` (line 91) limits ``target_kind`` to
``study``, ``series``, ``instance``, ``dataset``; this is a deliberate
design choice, not a missing feature. Tags fit imaging because the
vocabulary is open (``anatomy:*``, ``finding:*``, ``pathology:*``)
and the source of truth is the underlying artefact (referto, DICOM
tag) from which the autotag worker rederives them.

On every other entity (Documents, ClinicalEvents, CarePhases,
ReportContent rows, Markers, ClinicalNotes, Folders) a tag would
typically duplicate information already held more rigorously
elsewhere (enum column with CHECK, structured field, FK). That
creates a second source of truth that drifts. Before proposing to
widen the constraint, model the need as a structured field or a
dedicated entity (``incident_anchor`` ClinicalEvent kind, ``LegalHold``
table, ``Trial`` + ``TrialEnrollment``, ``ReviewRequest``). See
``docs/tag-taxonomy.md`` for the checklist and ``docs/data-model.md
§0`` for the conceptual placement.

``Annotation`` used to live here too; it was retired in favour of
the unified ``Marker`` (in-viewer ephemera) + ``ClinicalNote``
(human prose) split (migration 0042 dropped the table).

``Report`` (the version-tracked text+file attached to a study) was
retired by the v3 refactor in favour of ``ReportContent``: the
Expression layer carries the same role with a richer authority
ladder, supersede chains, and the n:m link to documents.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from bvphoenix.db.base import Base
from bvphoenix.db.models._common import uuid_pk


class Tag(Base):
    __tablename__ = "tags"

    id: Mapped[uuid.UUID] = uuid_pk()
    target_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    target_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    # Denormalised pointer to the patient that owns the target. Set on
    # study / series / instance tags from the resolved imaging chain;
    # left NULL for ``dataset`` tags (system-wide, no patient owner).
    # The ``GET /api/tags`` autocomplete + tree endpoints filter on
    # this column against the caller's visible-patients set so a
    # PHI-bearing tag value (``cognome_distretto_anno``) is not
    # leaked across patient boundaries via the global tag list.
    patient_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("patients.id", ondelete="CASCADE"),
        nullable=True,
    )
    namespace: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[str] = mapped_column(String(255), nullable=False)
    # ``manual`` = human typed it via the API, ``agent`` = an MCP-
    # authenticated AI assistant wrote it, ``auto`` = worker-inferred,
    # ``imported`` = lifted from an upstream DICOM tag / export.
    # Server default keeps back-compat with older migrations that
    # predate the column.
    source: Mapped[str] = mapped_column(String(16), nullable=False, server_default="manual")
    # Only populated for automated rows. Manual tags stay NULL so the
    # UI can render them without a misleading score.
    confidence: Mapped[float | None] = mapped_column(Float)
    created_by_subject_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("subjects.id", ondelete="SET NULL"),
    )
    # Identifies the AI assistant that wrote the tag when
    # ``source='agent'``. Mirrors the same column on ``commits`` so
    # the UI can render an assistant-specific badge instead of the
    # generic "manual" pill. ON DELETE SET NULL so revoking the
    # assistant does not lose history.
    agent_assistant_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("agent_assistants.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "target_kind",
            "target_id",
            "namespace",
            "value",
            name="uq_tags_target_namespace_value",
        ),
        CheckConstraint(
            "target_kind IN ('study','series','instance','dataset')",
            name="ck_tags_target_kind",
        ),
        CheckConstraint(
            "source IN ('manual','agent','auto','imported')",
            name="ck_tags_source",
        ),
        Index("ix_tags_namespace_value", "namespace", "value"),
        Index("ix_tags_target", "target_kind", "target_id"),
        Index("ix_tags_source", "source"),
        Index("ix_tags_patient_id", "patient_id"),
        Index("ix_tags_agent_assistant_id", "agent_assistant_id"),
    )


class TagAlias(Base):
    """Synonym rewrite table. ``alias_value`` → ``primary_value`` within
    a given namespace. The search / autocomplete layers consult this to
    collapse italian / english spellings onto a single canonical tag
    without duplicating the underlying ``tags`` rows.
    """

    __tablename__ = "tag_aliases"

    id: Mapped[uuid.UUID] = uuid_pk()
    namespace: Mapped[str] = mapped_column(String(64), nullable=False)
    primary_value: Mapped[str] = mapped_column(String(255), nullable=False)
    alias_value: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "namespace",
            "alias_value",
            name="uq_tag_aliases_namespace_alias",
        ),
        Index("ix_tag_aliases_primary", "namespace", "primary_value"),
    )
