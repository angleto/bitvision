"""Finding model — the structured, coded, measured clinical reperto.

P2 of the annotation-layer overhaul (Flow note 76949928). A ``Finding``
is the first-class clinical entity a diagnosis is expressed in: a coded
*type* (nodule / mass / fracture / …), an *anatomical site* (+ laterality),
*morphology* descriptors (spiculated / lobulated / …), and *typed,
indexed* measurements (longest diameter, volume, SUVmax/peak/mean, HU
stats). It is the perno that makes the corpus queryable ("all studies
with a spiculated nodule > 1 cm in the RUL") and assemblable into a
training set — neither expressible against the free-form ``Marker`` blob.

A Finding does not duplicate geometry: it *references* 1..N geometries
(``Marker`` rows and/or ``Segmentation`` masks) through
``finding_geometry``. The voxel/world geometry lives on those; the
Finding carries the semantics + measured scalars + a materialized
world-space bbox for spatial context.

Vocabularies are controlled but code-system-agnostic: ``finding_types``,
``anatomy_sites`` and ``morphology_terms`` each carry optional
``code_system`` / ``code`` columns so the internal slugs can be mapped
onto RadLex / SNOMED CT / ICD-O progressively without a schema change
(the codes are deliberately seeded NULL — mapping is a curated step, not
a guess).

Tracking mirrors ``Marker`` (see ``markers.py``): per-row ``etag`` for
If-Match, soft-delete, and a ``finding_revision`` snapshot table — a
diagnosis authored via MCP (create / edit / delete) is always
attributable and reversible.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
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
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from bvphoenix.db.base import Base
from bvphoenix.db.models._common import uuid_pk

# Diagnostic lifecycle of a finding.
FINDING_STATUSES: tuple[str, ...] = ("candidate", "confirmed", "retracted")

# Acting-author kinds (shared with Marker).
FINDING_AUTHOR_KINDS: tuple[str, ...] = ("human", "agent", "system")

# Revision change kinds (mirrors marker_revision).
FINDING_REVISION_CHANGE_KINDS: tuple[str, ...] = ("create", "update", "delete", "restore")

# Laterality of an anatomical site.
LATERALITIES: tuple[str, ...] = ("left", "right", "bilateral", "midline")

# Role a linked geometry plays for a finding.
FINDING_GEOMETRY_ROLES: tuple[str, ...] = ("measurement", "bbox", "mask", "fiducial")

# Coarse grouping of finding types, for faceting.
FINDING_TYPE_CATEGORIES: tuple[str, ...] = (
    "lesion",
    "mass",
    "fluid",
    "fracture",
    "inflammation",
    "vascular",
    "calcification",
    "hemorrhage",
    "other",
)


# ---------------------------------------------------------------------------
# Controlled vocabularies
# ---------------------------------------------------------------------------


class FindingType(Base):
    """Controlled vocabulary of finding types (nodule, mass, cyst, …)."""

    __tablename__ = "finding_types"

    id: Mapped[uuid.UUID] = uuid_pk()
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    display: Mapped[str] = mapped_column(String(128), nullable=False)
    display_i18n: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    category: Mapped[str] = mapped_column(String(32), nullable=False, server_default="other")
    # Optional mapping onto an external code system (RadLex / SNOMED / ICD-O).
    code_system: Mapped[str | None] = mapped_column(String(32))
    code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("key", name="uq_finding_types_key"),
        CheckConstraint(
            "category IN (" + ",".join(f"'{c}'" for c in FINDING_TYPE_CATEGORIES) + ")",
            name="ck_finding_types_category",
        ),
        Index("ix_finding_types_category", "category"),
        Index("ix_finding_types_code", "code_system", "code"),
    )


class AnatomySite(Base):
    """Controlled vocabulary of anatomical sites, optionally hierarchical."""

    __tablename__ = "anatomy_sites"

    id: Mapped[uuid.UUID] = uuid_pk()
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    display: Mapped[str] = mapped_column(String(128), nullable=False)
    display_i18n: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    parent_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    laterality_applicable: Mapped[bool] = mapped_column(
        nullable=False, server_default=text("false")
    )
    code_system: Mapped[str | None] = mapped_column(String(32))
    code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("key", name="uq_anatomy_sites_key"),
        ForeignKeyConstraint(
            ["parent_id"], ["anatomy_sites.id"], name="fk_anatomy_sites_parent", ondelete="SET NULL"
        ),
        Index("ix_anatomy_sites_parent", "parent_id"),
        Index("ix_anatomy_sites_code", "code_system", "code"),
    )


class MorphologyTerm(Base):
    """Controlled vocabulary of morphology / shape descriptors."""

    __tablename__ = "morphology_terms"

    id: Mapped[uuid.UUID] = uuid_pk()
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    display: Mapped[str] = mapped_column(String(128), nullable=False)
    display_i18n: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    code_system: Mapped[str | None] = mapped_column(String(32))
    code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("key", name="uq_morphology_terms_key"),
        Index("ix_morphology_terms_code", "code_system", "code"),
    )


# ---------------------------------------------------------------------------
# Finding
# ---------------------------------------------------------------------------


class Finding(Base):
    __tablename__ = "findings"

    id: Mapped[uuid.UUID] = uuid_pk()

    # Scope + cascade. ``patient_id`` is denormalized (like Marker) for
    # patient-scoped queries and cascade.
    patient_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("patients.id", ondelete="CASCADE"),
        nullable=False,
    )
    study_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("imaging_studies.id", ondelete="CASCADE"),
        nullable=False,
    )
    series_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("series.id", ondelete="SET NULL"),
    )
    # The frame of reference the (optional) world-space bbox lives in.
    frame_of_reference_uid: Mapped[str | None] = mapped_column(String(128))

    # Coded semantics. ``finding_type_id`` is required (a finding's whole
    # point is to be classified); seed an 'other' type as the escape
    # hatch. ``anatomy_site_id`` is recommended but nullable.
    finding_type_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("finding_types.id", ondelete="RESTRICT"),
        nullable=False,
    )
    anatomy_site_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("anatomy_sites.id", ondelete="SET NULL"),
    )
    laterality: Mapped[str | None] = mapped_column(String(8))
    # Morphology descriptors as validated slugs (each must exist in
    # ``morphology_terms``; enforced at the service layer). GIN-indexed so
    # ``morphology_keys @> ARRAY['spiculated']`` is fast.
    morphology_keys: Mapped[list[str]] = mapped_column(
        ARRAY(String(64)), nullable=False, server_default="{}"
    )

    # Typed, indexed measurements (RECIST / PERCIST ready). NULL when not
    # measured. These are the columns that make size/SUV range queries
    # expressible without a JSONB scan.
    longest_diameter_mm: Mapped[float | None] = mapped_column(Float)
    short_axis_mm: Mapped[float | None] = mapped_column(Float)
    volume_ml: Mapped[float | None] = mapped_column(Float)
    suv_max: Mapped[float | None] = mapped_column(Float)
    suv_peak: Mapped[float | None] = mapped_column(Float)
    suv_mean: Mapped[float | None] = mapped_column(Float)
    hu_mean: Mapped[float | None] = mapped_column(Float)
    hu_std: Mapped[float | None] = mapped_column(Float)

    # Materialized world-space (LPS) bounding box for spatial context /
    # cross-series comparison: {"min": [x,y,z], "max": [x,y,z]}.
    bbox_lps: Mapped[dict | None] = mapped_column(JSONB)

    # Diagnostic state + free-text elaboration beyond the structured fields.
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="candidate")
    confidence: Mapped[float | None] = mapped_column(Float)
    description: Mapped[str | None] = mapped_column(Text)

    # Provenance (same shape as Marker / ClinicalNote).
    author_subject_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("subjects.id", ondelete="SET NULL")
    )
    author_kind: Mapped[str] = mapped_column(String(16), nullable=False, server_default="human")
    model_id: Mapped[str | None] = mapped_column(Text)
    provider: Mapped[str | None] = mapped_column(Text)
    agent_token_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("agent_tokens.id", ondelete="SET NULL")
    )

    # Tracking (mirrors Marker).
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
            "status IN (" + ",".join(f"'{s}'" for s in FINDING_STATUSES) + ")",
            name="ck_findings_status",
        ),
        CheckConstraint(
            "author_kind IN (" + ",".join(f"'{a}'" for a in FINDING_AUTHOR_KINDS) + ")",
            name="ck_findings_author_kind",
        ),
        CheckConstraint(
            "laterality IS NULL OR laterality IN ("
            + ",".join(f"'{lat}'" for lat in LATERALITIES)
            + ")",
            name="ck_findings_laterality",
        ),
        # Composite-FK target for lesion_track_points(patient_id, finding_id):
        # guarantees a track point can only reference a finding of the same
        # patient (cross-patient link inexpressible at the DDL level).
        UniqueConstraint("patient_id", "id", name="uq_findings_patient_id"),
        Index("ix_findings_patient", "patient_id"),
        Index("ix_findings_study", "study_id"),
        Index("ix_findings_type", "finding_type_id"),
        Index("ix_findings_anatomy", "anatomy_site_id"),
        Index("ix_findings_status", "status"),
        # B-tree on the measurements that drive range queries.
        Index("ix_findings_longest_diameter", "longest_diameter_mm"),
        Index("ix_findings_volume", "volume_ml"),
        Index("ix_findings_suv_max", "suv_max"),
        # GIN over morphology slugs for containment queries.
        Index("ix_findings_morphology", "morphology_keys", postgresql_using="gin"),
        # Partial index over live rows (default list hides tombstones).
        Index(
            "ix_findings_live",
            "patient_id",
            "study_id",
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )


class FindingGeometry(Base):
    """Link between a Finding and its underlying geometry (a Marker
    and/or a Segmentation mask). A Finding aggregates 1..N of these."""

    __tablename__ = "finding_geometry"

    id: Mapped[uuid.UUID] = uuid_pk()
    finding_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    marker_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    segmentation_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["finding_id"], ["findings.id"], name="fk_finding_geometry_finding", ondelete="CASCADE"
        ),
        ForeignKeyConstraint(
            ["marker_id"], ["markers.id"], name="fk_finding_geometry_marker", ondelete="SET NULL"
        ),
        ForeignKeyConstraint(
            ["segmentation_id"],
            ["segmentations.id"],
            name="fk_finding_geometry_segmentation",
            ondelete="SET NULL",
        ),
        CheckConstraint(
            "role IN (" + ",".join(f"'{r}'" for r in FINDING_GEOMETRY_ROLES) + ")",
            name="ck_finding_geometry_role",
        ),
        # At least one geometry pointer must be set.
        CheckConstraint(
            "marker_id IS NOT NULL OR segmentation_id IS NOT NULL",
            name="ck_finding_geometry_target_present",
        ),
        Index("ix_finding_geometry_finding", "finding_id"),
        Index("ix_finding_geometry_marker", "marker_id"),
        Index("ix_finding_geometry_segmentation", "segmentation_id"),
    )


class FindingRevision(Base):
    """Immutable snapshot of a finding at each CRUD act (mirrors
    ``marker_revision`` / ``care_phase_revision``)."""

    __tablename__ = "finding_revision"

    id: Mapped[uuid.UUID] = uuid_pk()
    finding_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
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
            ["finding_id"], ["findings.id"], name="fk_finding_revision_finding", ondelete="CASCADE"
        ),
        CheckConstraint(
            "change_kind IN (" + ",".join(f"'{k}'" for k in FINDING_REVISION_CHANGE_KINDS) + ")",
            name="ck_finding_revision_kind",
        ),
        CheckConstraint(
            "author_kind IN (" + ",".join(f"'{a}'" for a in FINDING_AUTHOR_KINDS) + ")",
            name="ck_finding_revision_author_kind",
        ),
        CheckConstraint("revision_no >= 1", name="ck_finding_revision_no_pos"),
        UniqueConstraint("finding_id", "revision_no", name="uq_finding_revision_finding_no"),
        Index("ix_finding_revision_finding", "finding_id", "revision_no"),
        Index("ix_finding_revision_patient", "patient_id"),
    )
