"""Lesion tracks — a physical lesion followed across timepoints.

The longitudinal pivot for tumour-comparison (baseline CT vs follow-up):
a ``LesionTrack`` is *one physical lesion* tracked over time, and a
``LesionTrackPoint`` links a per-study ``Finding`` (the measured node at
one timepoint) into that track. Growth ("has the tumour grown?") is a
*derived* computation over the ordered points — see
``services/lesion_tracks.py``. Patient-level RECIST / volumetric response
aggregates several tracks (``response_assessments``).

A track is the strictly-more-general abstraction than a pairwise
baseline↔follow-up link: it generalises to 3+ surveillance timepoints
and carries the lesion's identity (label, anatomy, RECIST role) once.

Cross-patient is impossible by construction (mirrors ``care_phases``):

* ``LesionTrack`` carries ``UNIQUE (patient_id, id)``.
* ``LesionTrackPoint`` joins track and finding through **composite**
  foreign keys ``(patient_id, lesion_track_id) → lesion_tracks
  (patient_id, id)`` and ``(patient_id, finding_id) → findings
  (patient_id, id)``. PostgreSQL therefore rejects any cross-patient
  link at the DDL level — a follow-up point can never reference a
  finding from another patient.

Tracking mirrors ``Finding`` / ``Marker``: per-row ``etag`` for If-Match,
soft-delete, and a ``lesion_track_revision`` snapshot table.

See ``backend/alembic/versions/0027_lesion_tracks.py`` for the schema.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from bvphoenix.db.base import Base
from bvphoenix.db.models._common import uuid_pk

# Lifecycle of a tracked lesion.
LESION_TRACK_STATUSES: tuple[str, ...] = ("active", "resolved", "retracted")

# RECIST 1.1 role of the lesion in a response assessment (nullable when
# the track is not part of a formal assessment).
LESION_TRACK_RECIST_ROLES: tuple[str, ...] = (
    "target",
    "non_target",
    "new",
    "not_evaluable",
)

# Acting-author kinds (shared with Finding / Marker).
LESION_TRACK_AUTHOR_KINDS: tuple[str, ...] = ("human", "agent", "system")

# How a timepoint link was established.
LESION_TRACK_LINK_KINDS: tuple[str, ...] = ("human", "agent", "system")

# Revision change kinds.
LESION_TRACK_REVISION_CHANGE_KINDS: tuple[str, ...] = (
    "create",
    "update",
    "add_point",
    "remove_point",
    "delete",
    "restore",
)

# Laterality (shared vocabulary with Finding).
LESION_TRACK_LATERALITIES: tuple[str, ...] = ("left", "right", "bilateral", "midline")


class LesionTrack(Base):
    __tablename__ = "lesion_tracks"

    id: Mapped[uuid.UUID] = uuid_pk()

    # Scope + cascade. Denormalised ``patient_id`` (like Finding) for
    # patient-scoped queries and the composite cross-patient guard.
    patient_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("patients.id", ondelete="CASCADE"),
        nullable=False,
    )

    # Human-readable identity of the lesion (e.g. "Nodulo LSD",
    # "Lesione epatica S7").
    label: Mapped[str] = mapped_column(String(255), nullable=False)

    # Coded semantics (optional context; reuse the Finding vocabularies).
    anatomy_site_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("anatomy_sites.id", ondelete="SET NULL"),
    )
    laterality: Mapped[str | None] = mapped_column(String(8))
    finding_type_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("finding_types.id", ondelete="SET NULL"),
    )

    # RECIST 1.1 designation (target / non-target / new). NULL when the
    # lesion is not part of a formal response assessment.
    recist_role: Mapped[str | None] = mapped_column(String(16))

    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="active")
    description: Mapped[str | None] = mapped_column(Text)

    # Provenance (same shape as Finding / Marker).
    author_subject_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("subjects.id", ondelete="SET NULL")
    )
    author_kind: Mapped[str] = mapped_column(String(16), nullable=False, server_default="human")
    model_id: Mapped[str | None] = mapped_column(Text)
    provider: Mapped[str | None] = mapped_column(Text)
    agent_token_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("agent_tokens.id", ondelete="SET NULL")
    )

    # Tracking (mirrors Finding).
    etag: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False, server_default=text("gen_random_uuid()")
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    purge_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delete_reason: Mapped[str | None] = mapped_column(String(255))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "status IN (" + ",".join(f"'{s}'" for s in LESION_TRACK_STATUSES) + ")",
            name="ck_lesion_tracks_status",
        ),
        CheckConstraint(
            "author_kind IN (" + ",".join(f"'{a}'" for a in LESION_TRACK_AUTHOR_KINDS) + ")",
            name="ck_lesion_tracks_author_kind",
        ),
        CheckConstraint(
            "laterality IS NULL OR laterality IN ("
            + ",".join(f"'{lat}'" for lat in LESION_TRACK_LATERALITIES)
            + ")",
            name="ck_lesion_tracks_laterality",
        ),
        CheckConstraint(
            "recist_role IS NULL OR recist_role IN ("
            + ",".join(f"'{r}'" for r in LESION_TRACK_RECIST_ROLES)
            + ")",
            name="ck_lesion_tracks_recist_role",
        ),
        # Required by the composite FK from lesion_track_points.
        UniqueConstraint("patient_id", "id", name="uq_lesion_tracks_patient_id"),
        Index("ix_lesion_tracks_patient", "patient_id"),
        Index(
            "ix_lesion_tracks_live",
            "patient_id",
            "status",
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )


class LesionTrackPoint(Base):
    """A timepoint on a lesion track: links one ``Finding`` (the measured
    node in a study) into the track. The composite FKs make a
    cross-patient link inexpressible."""

    __tablename__ = "lesion_track_points"

    id: Mapped[uuid.UUID] = uuid_pk()
    lesion_track_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    finding_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    patient_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)

    # Exactly one baseline per track (partial-unique below).
    is_baseline: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))

    # Denormalised acquisition date of the underlying study, for ordering
    # the trajectory (cache; the study's study_date is the source).
    timepoint_date: Mapped[date | None] = mapped_column(Date)

    # The registration that aligned this timepoint to the baseline frame
    # (NULL when the point shares the baseline's frame of reference, or
    # was linked manually without registration).
    registration_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))

    linked_by_kind: Mapped[str] = mapped_column(String(16), nullable=False, server_default="human")
    confidence: Mapped[float | None] = mapped_column(Float)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        # Composite FK → lesion_tracks: enforces same-patient at the DDL
        # level (cross-patient inexpressible).
        ForeignKeyConstraint(
            ["patient_id", "lesion_track_id"],
            ["lesion_tracks.patient_id", "lesion_tracks.id"],
            name="fk_lesion_track_points_track",
            ondelete="CASCADE",
        ),
        # Composite FK → findings: the finding must belong to the same
        # patient as the track.
        ForeignKeyConstraint(
            ["patient_id", "finding_id"],
            ["findings.patient_id", "findings.id"],
            name="fk_lesion_track_points_finding",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["registration_id"],
            ["registrations.id"],
            name="fk_lesion_track_points_registration",
            ondelete="SET NULL",
        ),
        CheckConstraint(
            "linked_by_kind IN (" + ",".join(f"'{k}'" for k in LESION_TRACK_LINK_KINDS) + ")",
            name="ck_lesion_track_points_link_kind",
        ),
        # A finding belongs to at most one track.
        UniqueConstraint("finding_id", name="uq_lesion_track_points_finding"),
        # At most one baseline per track.
        Index(
            "uq_lesion_track_points_one_baseline",
            "lesion_track_id",
            unique=True,
            postgresql_where=text("is_baseline"),
        ),
        Index("ix_lesion_track_points_track", "lesion_track_id"),
        Index("ix_lesion_track_points_patient", "patient_id"),
        Index("ix_lesion_track_points_registration", "registration_id"),
    )


class LesionTrackRevision(Base):
    """Immutable snapshot of a lesion track at each act (mirrors
    ``finding_revision`` / ``care_phase_revision``)."""

    __tablename__ = "lesion_track_revision"

    id: Mapped[uuid.UUID] = uuid_pk()
    lesion_track_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    patient_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    revision_no: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    change_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    author_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    diff_summary: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["lesion_track_id"],
            ["lesion_tracks.id"],
            name="fk_lesion_track_revision_track",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "change_kind IN ("
            + ",".join(f"'{k}'" for k in LESION_TRACK_REVISION_CHANGE_KINDS)
            + ")",
            name="ck_lesion_track_revision_kind",
        ),
        CheckConstraint(
            "author_kind IN (" + ",".join(f"'{a}'" for a in LESION_TRACK_AUTHOR_KINDS) + ")",
            name="ck_lesion_track_revision_author_kind",
        ),
        CheckConstraint("revision_no >= 1", name="ck_lesion_track_revision_no_pos"),
        UniqueConstraint(
            "lesion_track_id", "revision_no", name="uq_lesion_track_revision_track_no"
        ),
        Index("ix_lesion_track_revision_track", "lesion_track_id", "revision_no"),
        Index("ix_lesion_track_revision_patient", "patient_id"),
    )


__all__ = [
    "LESION_TRACK_AUTHOR_KINDS",
    "LESION_TRACK_LATERALITIES",
    "LESION_TRACK_LINK_KINDS",
    "LESION_TRACK_RECIST_ROLES",
    "LESION_TRACK_REVISION_CHANGE_KINDS",
    "LESION_TRACK_STATUSES",
    "LesionTrack",
    "LesionTrackPoint",
    "LesionTrackRevision",
]
