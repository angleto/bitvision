"""Marker model — server-side persistence for in-viewer annotations.

A ``Marker`` is anything the radiologist (or an agent) places on or
anchors to a study while reading: a 2D measurement (distance / angle /
area / …), a 3D fiducial point, a text label drawn on the image, or a
bookmark into a clinical note. The radiology viewer sidebar surfaces
them in a single list with click-to-jump, delete, and JSON / DICOM SR
export.

The ``Marker`` is **not** F12-versioned (it does not live on the
patient git-like DAG: a measurement is not a clinical commit). It does,
however, carry full *tracking* so that a diagnosis authored via the MCP
surface — create / edit / delete — is always attributable and
recoverable (ADR: agents must never leave an untracked mutation):

* per-row ``etag`` (rotated on every mutation) → optimistic concurrency
  via ``If-Match`` so a concurrent agent / human edit cannot silently
  clobber the other (mirrors ``CarePhase``);
* soft-delete (``deleted_at`` / ``purge_after`` / ``delete_reason``) so
  a removed annotation is recoverable, not destroyed;
* a companion ``marker_revision`` snapshot table (mirrors
  ``care_phase_revision``) recording every create / update / delete /
  restore with its ``author_kind`` + actor, so prior values can be
  inspected or restored.

The high-frequency viewer measurement-sync path
(``api/measurements.py``, keyed on ``client_id``) deliberately does
*not* append a revision per drag — only the explicit marker CRUD
endpoints do, keeping the history at the same granularity as the
``audit_log`` (one row per deliberate act, not per ruler-drag frame).

Storage shape — chosen to be compact and forward-compatible:

* ``geometry`` (JSONB): geometric data, kind-specific. Examples:
    {"axis": "axial", "points": [[i,j,k], ...], "world_points": [...],
     "frame_of_reference_uid": "..."}                    # measurement
    {"axis": "axial", "anchor": [i,j,k]}                 # text-overlay
    {"point": [i,j,k]}                                   # fiducial
    null                                                  # pure-text note
  ``world_points`` (patient/LPS coords) + ``frame_of_reference_uid``
  are persisted alongside the voxel ``points`` so a marker survives a
  re-pack and can be compared across series/modalities (the voxel
  ``points`` alone are meaningless without their exact packed volume).
* ``computed`` (JSONB): derived value (length / angle / area / probe
  HU value, with unit). NULL for non-measurement kinds.
* ``body``: free-text payload for note-like markers.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
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

# Authoritative list — keep in sync with the CHECK constraint, last
# widened by alembic/versions/0019_marker_tracking.py. Extending the
# list requires a follow-up migration to widen the CHECK.
MARKER_KINDS: tuple[str, ...] = (
    "measurement.distance",
    "measurement.angle",
    "measurement.area",
    "measurement.ellipse",
    "measurement.freehand",
    "measurement.arrow",
    "measurement.text",
    "measurement.probe",
    "measurement.bbox",
    # ``measurement.sphere`` — a 2D circle ROI (Cornerstone ``CircleROI``)
    # that the backend treats as the equator of a 3D sphere for PERCIST
    # SUVpeak (1 cm³). A distinct kind so the panel / report-composer can
    # label it "Sphere ROI (PERCIST)" instead of collapsing it to a flat
    # distance — previously mis-persisted as ``measurement.distance``.
    "measurement.sphere",
    # ``bbox.lesion`` — 3D axis-aligned bounding box anchored to a
    # candidate lesion, used to round-trip the output of
    # ``find_hot_spots`` (``bbox_min_ijk`` / ``bbox_max_ijk``) into a
    # persistent annotation. Geometry shape:
    #   {"min_ijk": [i, j, k], "max_ijk": [i', j', k']}
    # When the source is a discovery tool, callers SHOULD populate
    # ``computed`` with the relevant metrics (e.g. ``suv_max``,
    # ``volume_ml``) so the viewer can surface them next to the box.
    "bbox.lesion",
    # ``bbox.exclusion`` — same geometry shape as ``bbox.lesion`` but
    # marks a region the operator wants ROI stats and hot-spot search
    # to ignore (kidneys, bladder on PET when an automatic
    # segmentation mask is unavailable). Consumed by
    # ``services.exclusion_masks.build_exclusion_mask``.
    "bbox.exclusion",
    "fiducial",
    "reading-note",
    "text-overlay",
)

# Change kinds recorded in ``marker_revision`` — one row per deliberate
# CRUD act on a marker (mirrors ``care_phase_revision``).
MARKER_REVISION_CHANGE_KINDS: tuple[str, ...] = (
    "create",
    "update",
    "delete",
    "restore",
)


class Marker(Base):
    __tablename__ = "markers"

    id: Mapped[uuid.UUID] = uuid_pk()

    patient_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("patients.id", ondelete="CASCADE"),
        nullable=False,
    )

    # Polymorphic anchor: the entity (study / series / instance) the
    # marker is attached to. Decouples the marker from a specific
    # presentation: a measurement on a series can survive series
    # re-ingestion as long as the (kind, id) reference is preserved.
    target_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    target_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)

    # Hierarchical kind, dot-namespaced. See MARKER_KINDS.
    kind: Mapped[str] = mapped_column(String(48), nullable=False)

    # Geometry, computed value, body — see module docstring.
    geometry: Mapped[dict | None] = mapped_column(JSONB)
    body: Mapped[str | None] = mapped_column(Text)
    computed: Mapped[dict | None] = mapped_column(JSONB)

    # Provenance — same shape as ClinicalNote / Consultation so the
    # unified UI panel can render an author badge consistently.
    author_subject_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("subjects.id", ondelete="SET NULL"),
    )
    author_kind: Mapped[str] = mapped_column(String(16), nullable=False, server_default="human")
    model_id: Mapped[str | None] = mapped_column(Text)
    provider: Mapped[str | None] = mapped_column(Text)
    agent_token_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("agent_tokens.id", ondelete="SET NULL"),
    )

    # Optimistic-concurrency token. Rotated on every mutation; callers
    # guard PATCH / DELETE / restore with ``If-Match`` (see services/etag).
    etag: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=False,
        server_default=text("gen_random_uuid()"),
    )

    # Soft-delete (mirrors Document). A removed annotation is recoverable
    # via the restore endpoint until ``purge_after`` elapses; nothing
    # hard-deletes it automatically.
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
            "target_kind IN ('study','series','instance')",
            name="ck_markers_target_kind",
        ),
        CheckConstraint(
            "kind IN (" + ",".join(f"'{k}'" for k in MARKER_KINDS) + ")",
            name="ck_markers_kind",
        ),
        CheckConstraint(
            "author_kind IN ('human','agent','system')",
            name="ck_markers_author_kind",
        ),
        Index(
            "ix_markers_patient_target",
            "patient_id",
            "target_kind",
            "target_id",
        ),
        Index("ix_markers_kind", "kind"),
        # Partial index over live rows — the default list filters out
        # soft-deleted markers.
        Index(
            "ix_markers_live",
            "patient_id",
            "target_kind",
            "target_id",
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )


class MarkerRevision(Base):
    """Immutable snapshot of a marker at each deliberate CRUD act.

    One row per create / update / delete / restore on a ``Marker``,
    carrying the full pre/post snapshot plus the acting ``author_kind``
    so an agent-made edit is always distinguishable and a prior value
    can be restored. Mirrors ``care_phase_revision``.
    """

    __tablename__ = "marker_revision"

    id: Mapped[uuid.UUID] = uuid_pk()
    marker_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
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
            ["marker_id"],
            ["markers.id"],
            name="fk_marker_revision_marker",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "change_kind IN (" + ",".join(f"'{k}'" for k in MARKER_REVISION_CHANGE_KINDS) + ")",
            name="ck_marker_revision_kind",
        ),
        CheckConstraint(
            "author_kind IN ('human','agent','system')",
            name="ck_marker_revision_author_kind",
        ),
        CheckConstraint("revision_no >= 1", name="ck_marker_revision_no_pos"),
        UniqueConstraint("marker_id", "revision_no", name="uq_marker_revision_marker_no"),
        Index("ix_marker_revision_marker", "marker_id", "revision_no"),
        Index("ix_marker_revision_patient", "patient_id"),
    )
