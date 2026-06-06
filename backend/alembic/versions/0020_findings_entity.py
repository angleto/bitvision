"""Finding entity: coded, measured, queryable clinical reperto.

P2 of the annotation-layer overhaul (Flow note 76949928), the keystone.
Adds the structured representation a diagnosis is expressed in, so the
corpus becomes queryable by type / site / morphology / size / SUV and
assemblable into a training set:

* controlled vocabularies ``finding_types`` / ``anatomy_sites`` /
  ``morphology_terms`` (code-system-agnostic: optional ``code_system`` /
  ``code`` for RadLex / SNOMED / ICD-O mapping, seeded NULL);
* ``findings`` — coded type + site + laterality + morphology slugs +
  TYPED, INDEXED measurements (longest_diameter_mm / volume_ml / suv_*),
  world-space bbox, provenance + soft-delete + etag;
* ``finding_geometry`` — links a finding to its Marker(s) / Segmentation
  mask(s) without duplicating geometry;
* ``finding_revision`` — per-act snapshot history (mirrors
  ``marker_revision``).

Vocabulary is seeded with a starter set; ``code`` is intentionally left
NULL (mapping onto an external code system is a curated step, not a
guess).

Revision ID: 0020_findings_entity
Revises: 0019_marker_tracking
Create Date: 2026-06-06
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0020_findings_entity"
down_revision = "0019_marker_tracking"
branch_labels = None
depends_on = None


_FINDING_TYPE_CATEGORIES = (
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
_FINDING_STATUSES = ("candidate", "confirmed", "retracted")
_AUTHOR_KINDS = ("human", "agent", "system")
_LATERALITIES = ("left", "right", "bilateral", "midline")
_GEOMETRY_ROLES = ("measurement", "bbox", "mask", "fiducial")
_REVISION_CHANGE_KINDS = ("create", "update", "delete", "restore")


def _in_check(col: str, values: tuple[str, ...]) -> str:
    arr = ", ".join(f"'{v}'::character varying" for v in values)
    return f"({col})::text = ANY ((ARRAY[{arr}])::text[])"


# Starter vocabulary. ``code`` deliberately omitted (NULL) — mapping onto
# RadLex / SNOMED / ICD-O is a curated follow-up, not a fabricated guess.
_FINDING_TYPES = [
    ("nodule", "Nodule", "lesion"),
    ("mass", "Mass", "mass"),
    ("cyst", "Cyst", "lesion"),
    ("lymph_node", "Lymph node", "lesion"),
    ("consolidation", "Consolidation", "inflammation"),
    ("ground_glass_opacity", "Ground-glass opacity", "inflammation"),
    ("effusion", "Effusion", "fluid"),
    ("edema", "Edema", "fluid"),
    ("fracture", "Fracture", "fracture"),
    ("calcification", "Calcification", "calcification"),
    ("hemorrhage", "Hemorrhage", "hemorrhage"),
    ("infarct", "Infarct", "vascular"),
    ("aneurysm", "Aneurysm", "vascular"),
    ("stenosis", "Stenosis", "vascular"),
    ("lesion", "Lesion (unspecified)", "lesion"),
    ("other", "Other", "other"),
]

_ANATOMY_SITES = [
    ("liver", "Liver", False),
    ("spleen", "Spleen", False),
    ("pancreas", "Pancreas", False),
    ("kidney", "Kidney", True),
    ("adrenal", "Adrenal gland", True),
    ("lung", "Lung", True),
    ("lung_upper_lobe", "Lung, upper lobe", True),
    ("lung_middle_lobe", "Lung, middle lobe", False),
    ("lung_lower_lobe", "Lung, lower lobe", True),
    ("mediastinum", "Mediastinum", False),
    ("breast", "Breast", True),
    ("prostate", "Prostate", False),
    ("brain", "Brain", False),
    ("bone", "Bone", False),
    ("lymph_node_region", "Lymph node region", False),
    ("bladder", "Bladder", False),
    ("thyroid", "Thyroid", True),
    ("bowel", "Bowel", False),
]

_MORPHOLOGY_TERMS = [
    ("spiculated", "Spiculated"),
    ("lobulated", "Lobulated"),
    ("circumscribed", "Circumscribed"),
    ("irregular", "Irregular"),
    ("smooth", "Smooth"),
    ("well_defined", "Well-defined"),
    ("ill_defined", "Ill-defined"),
    ("cavitary", "Cavitary"),
    ("solid", "Solid"),
    ("part_solid", "Part-solid"),
    ("ground_glass", "Ground-glass"),
    ("calcified", "Calcified"),
    ("necrotic", "Necrotic"),
]


def upgrade() -> None:
    # --- vocab: finding_types ------------------------------------------
    op.create_table(
        "finding_types",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("display", sa.String(length=128), nullable=False),
        sa.Column(
            "display_i18n",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.Column("category", sa.String(length=32), server_default="other", nullable=False),
        sa.Column("code_system", sa.String(length=32), nullable=True),
        sa.Column("code", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_finding_types"),
        sa.UniqueConstraint("key", name="uq_finding_types_key"),
        sa.CheckConstraint(
            _in_check("category", _FINDING_TYPE_CATEGORIES), name="ck_finding_types_category"
        ),
    )
    op.create_index("ix_finding_types_category", "finding_types", ["category"])
    op.create_index("ix_finding_types_code", "finding_types", ["code_system", "code"])

    # --- vocab: anatomy_sites ------------------------------------------
    op.create_table(
        "anatomy_sites",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("display", sa.String(length=128), nullable=False),
        sa.Column(
            "display_i18n",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.Column("parent_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "laterality_applicable",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("code_system", sa.String(length=32), nullable=True),
        sa.Column("code", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_anatomy_sites"),
        sa.UniqueConstraint("key", name="uq_anatomy_sites_key"),
        sa.ForeignKeyConstraint(
            ["parent_id"],
            ["anatomy_sites.id"],
            name="fk_anatomy_sites_parent",
            ondelete="SET NULL",
        ),
    )
    op.create_index("ix_anatomy_sites_parent", "anatomy_sites", ["parent_id"])
    op.create_index("ix_anatomy_sites_code", "anatomy_sites", ["code_system", "code"])

    # --- vocab: morphology_terms ---------------------------------------
    op.create_table(
        "morphology_terms",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("display", sa.String(length=128), nullable=False),
        sa.Column(
            "display_i18n",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.Column("code_system", sa.String(length=32), nullable=True),
        sa.Column("code", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_morphology_terms"),
        sa.UniqueConstraint("key", name="uq_morphology_terms_key"),
    )
    op.create_index("ix_morphology_terms_code", "morphology_terms", ["code_system", "code"])

    # --- findings ------------------------------------------------------
    op.create_table(
        "findings",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("patient_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("study_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("series_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("frame_of_reference_uid", sa.String(length=128), nullable=True),
        sa.Column("finding_type_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("anatomy_site_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("laterality", sa.String(length=8), nullable=True),
        sa.Column(
            "morphology_keys",
            postgresql.ARRAY(sa.String(length=64)),
            server_default="{}",
            nullable=False,
        ),
        sa.Column("longest_diameter_mm", sa.Float(), nullable=True),
        sa.Column("short_axis_mm", sa.Float(), nullable=True),
        sa.Column("volume_ml", sa.Float(), nullable=True),
        sa.Column("suv_max", sa.Float(), nullable=True),
        sa.Column("suv_peak", sa.Float(), nullable=True),
        sa.Column("suv_mean", sa.Float(), nullable=True),
        sa.Column("hu_mean", sa.Float(), nullable=True),
        sa.Column("hu_std", sa.Float(), nullable=True),
        sa.Column("bbox_lps", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("status", sa.String(length=16), server_default="candidate", nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("author_subject_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("author_kind", sa.String(length=16), server_default="human", nullable=False),
        sa.Column("model_id", sa.Text(), nullable=True),
        sa.Column("provider", sa.Text(), nullable=True),
        sa.Column("agent_token_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "etag",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("purge_after", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delete_reason", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_findings"),
        sa.ForeignKeyConstraint(
            ["patient_id"], ["patients.id"], name="fk_findings_patient", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["study_id"], ["imaging_studies.id"], name="fk_findings_study", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["series_id"], ["series.id"], name="fk_findings_series", ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["finding_type_id"],
            ["finding_types.id"],
            name="fk_findings_type",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["anatomy_site_id"],
            ["anatomy_sites.id"],
            name="fk_findings_anatomy",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["author_subject_id"],
            ["subjects.id"],
            name="fk_findings_author",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["agent_token_id"],
            ["agent_tokens.id"],
            name="fk_findings_agent_token",
            ondelete="SET NULL",
        ),
        sa.CheckConstraint(_in_check("status", _FINDING_STATUSES), name="ck_findings_status"),
        sa.CheckConstraint(_in_check("author_kind", _AUTHOR_KINDS), name="ck_findings_author_kind"),
        sa.CheckConstraint(
            "laterality IS NULL OR " + _in_check("laterality", _LATERALITIES),
            name="ck_findings_laterality",
        ),
    )
    op.create_index("ix_findings_patient", "findings", ["patient_id"])
    op.create_index("ix_findings_study", "findings", ["study_id"])
    op.create_index("ix_findings_type", "findings", ["finding_type_id"])
    op.create_index("ix_findings_anatomy", "findings", ["anatomy_site_id"])
    op.create_index("ix_findings_status", "findings", ["status"])
    op.create_index("ix_findings_longest_diameter", "findings", ["longest_diameter_mm"])
    op.create_index("ix_findings_volume", "findings", ["volume_ml"])
    op.create_index("ix_findings_suv_max", "findings", ["suv_max"])
    op.create_index(
        "ix_findings_morphology", "findings", ["morphology_keys"], postgresql_using="gin"
    )
    op.create_index(
        "ix_findings_live",
        "findings",
        ["patient_id", "study_id"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )

    # --- finding_geometry ----------------------------------------------
    op.create_table(
        "finding_geometry",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("finding_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("marker_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("segmentation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_finding_geometry"),
        sa.ForeignKeyConstraint(
            ["finding_id"], ["findings.id"], name="fk_finding_geometry_finding", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["marker_id"], ["markers.id"], name="fk_finding_geometry_marker", ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["segmentation_id"],
            ["segmentations.id"],
            name="fk_finding_geometry_segmentation",
            ondelete="SET NULL",
        ),
        sa.CheckConstraint(_in_check("role", _GEOMETRY_ROLES), name="ck_finding_geometry_role"),
        sa.CheckConstraint(
            "marker_id IS NOT NULL OR segmentation_id IS NOT NULL",
            name="ck_finding_geometry_target_present",
        ),
    )
    op.create_index("ix_finding_geometry_finding", "finding_geometry", ["finding_id"])
    op.create_index("ix_finding_geometry_marker", "finding_geometry", ["marker_id"])
    op.create_index("ix_finding_geometry_segmentation", "finding_geometry", ["segmentation_id"])

    # --- finding_revision ----------------------------------------------
    op.create_table(
        "finding_revision",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("finding_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("patient_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("revision_no", sa.Integer(), nullable=False),
        sa.Column("snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("change_kind", sa.String(length=16), nullable=False),
        sa.Column("author_kind", sa.String(length=16), nullable=False),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("diff_summary", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_finding_revision"),
        sa.ForeignKeyConstraint(
            ["finding_id"], ["findings.id"], name="fk_finding_revision_finding", ondelete="CASCADE"
        ),
        sa.CheckConstraint(
            _in_check("change_kind", _REVISION_CHANGE_KINDS), name="ck_finding_revision_kind"
        ),
        sa.CheckConstraint(
            _in_check("author_kind", _AUTHOR_KINDS), name="ck_finding_revision_author_kind"
        ),
        sa.CheckConstraint("revision_no >= 1", name="ck_finding_revision_no_pos"),
        sa.UniqueConstraint("finding_id", "revision_no", name="uq_finding_revision_finding_no"),
    )
    op.create_index(
        "ix_finding_revision_finding", "finding_revision", ["finding_id", "revision_no"]
    )
    op.create_index("ix_finding_revision_patient", "finding_revision", ["patient_id"])

    # --- seed vocabularies (code left NULL — curated mapping later) ----
    op.bulk_insert(
        sa.table(
            "finding_types",
            sa.column("key", sa.String),
            sa.column("display", sa.String),
            sa.column("category", sa.String),
        ),
        [{"key": k, "display": d, "category": c} for (k, d, c) in _FINDING_TYPES],
    )
    op.bulk_insert(
        sa.table(
            "anatomy_sites",
            sa.column("key", sa.String),
            sa.column("display", sa.String),
            sa.column("laterality_applicable", sa.Boolean),
        ),
        [{"key": k, "display": d, "laterality_applicable": lat} for (k, d, lat) in _ANATOMY_SITES],
    )
    op.bulk_insert(
        sa.table(
            "morphology_terms",
            sa.column("key", sa.String),
            sa.column("display", sa.String),
        ),
        [{"key": k, "display": d} for (k, d) in _MORPHOLOGY_TERMS],
    )


def downgrade() -> None:
    op.drop_table("finding_revision")
    op.drop_table("finding_geometry")
    op.drop_table("findings")
    op.drop_table("morphology_terms")
    op.drop_table("anatomy_sites")
    op.drop_table("finding_types")
