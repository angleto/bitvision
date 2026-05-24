"""Marker model — server-side persistence for in-viewer ephemera.

A ``Marker`` is anything the radiologist places on or anchors to a
study while reading: a 2D measurement (distance / angle / area / …),
a 3D fiducial point, a text label drawn on the image, or a bookmark
into a clinical note. The radiology viewer sidebar surfaces them in
a single list with click-to-jump, delete, and JSON / DICOM SR export.

The ``Marker`` is **not** F12-versioned: the mid-reading scratchpad
is not a clinical event, and committing every "ruler dragged then
released" to the patient's git-like history would flood the audit
log with noise. Standard create/update/delete entries in
``audit_log`` are sufficient. Final clinical acts (``ClinicalNote``,
``Consultation`` = report) remain F12-versioned in their own tables.

Storage shape — chosen to be compact and forward-compatible:

* ``geometry`` (JSONB): geometric data, kind-specific. Examples:
    {"axis": "axial", "points": [[x,y,z], [x',y',z']]}   # measurement
    {"axis": "axial", "anchor": [x,y,z]}                 # text-overlay
    {"point": [x,y,z]}                                   # fiducial
    null                                                  # pure-text note
* ``computed`` (JSONB): derived value (length / angle / area / probe
  HU value, with unit). NULL for non-measurement kinds.
* ``body``: free-text payload for note-like markers.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from bvphoenix.db.base import Base
from bvphoenix.db.models._common import uuid_pk

# Authoritative list — keep in sync with the CHECK constraint in
# alembic/versions/0038_markers_app_settings.py. Extending the list
# requires a follow-up migration to widen the CHECK.
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
    )
