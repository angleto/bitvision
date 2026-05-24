"""Pathology / whole-slide imaging (Step 1 of the spike doc).

Adds the ``pathology_slides`` table — the histology counterpart of
``imaging_studies``. A row represents one scanned vetrino (SVS / NDPI
/ OME-TIFF / DICOM-WSI), linked to a patient and to a clinical event
(``kind = 'pathology_slide'``, an enum value introduced here).

Provenance / license columns mirror migration 0004 so an OpenData
public-pathology library can be wired up later with the same idiom
(partial UNIQUE on ``(source_collection, source_subject_id,
slide_instance_uid)`` for idempotent re-imports, CHECK that
``tier='t4'`` carries license + collection).

Revision ID: 0005_pathology_slides
Revises: 0004_imaging_studies_provenance
Create Date: 2026-05-20
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

revision = "0005_pathology_slides"
down_revision = "0004_imaging_studies_provenance"
branch_labels = None
depends_on = None


_PATHOLOGY_KIND = "pathology_slide"

# Mirrors the Python tuple in ``db.models.clinical_events`` — keep in
# lock-step. The CHECK constraint below allows-lists exactly these.
_NEW_KINDS = (
    "imaging_study",
    "surgical_procedure",
    "outpatient_visit",
    "inpatient_admission",
    "lab_batch",
    "consultation_event",
    "pathology_review",
    "mdt_meeting",
    "cardio_diagnostic",
    "endoscopy",
    "radiology_appointment",
    "other",
    _PATHOLOGY_KIND,
)


def upgrade() -> None:
    # 1. Extend the clinical_events.kind CHECK to allow the new value.
    op.drop_constraint("ck_clinical_events_kind", "clinical_events", type_="check")
    op.create_check_constraint(
        "ck_clinical_events_kind",
        "clinical_events",
        "kind IN (" + ",".join(f"'{k}'" for k in _NEW_KINDS) + ")",
    )

    # 2. pathology_slides table — histology counterpart of
    #    imaging_studies. Owner-scoped, license-aware, and ready to be
    #    cloned to OpenData via the same idempotency keys used by
    #    migration 0004.
    op.create_table(
        "pathology_slides",
        sa.Column(
            "id",
            PG_UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "patient_id",
            PG_UUID(as_uuid=True),
            sa.ForeignKey("patients.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "clinical_event_id",
            PG_UUID(as_uuid=True),
            sa.ForeignKey("clinical_events.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "owner_subject_id",
            PG_UUID(as_uuid=True),
            sa.ForeignKey("subjects.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "owner_org_subject_id",
            PG_UUID(as_uuid=True),
            sa.ForeignKey("subjects.id", ondelete="SET NULL"),
            nullable=True,
        ),
        # DICOM-style UID. For non-DICOM sources (SVS / NDPI / OME-TIFF)
        # we mint a deterministic UUID5 from the source filename + SHA so
        # re-ingesting the same file from a different folder is a
        # no-op via the UNIQUE constraint below.
        sa.Column("slide_instance_uid", sa.String(128), nullable=False),
        # Optional curator-supplied identifiers describing the block /
        # slide on the gross specimen. e.g. block_label = 'A2',
        # slide_label = '3'. The pair is unique within a case at the
        # pathologist's discretion; we do not enforce uniqueness.
        sa.Column("block_label", sa.String(64), nullable=True),
        sa.Column("slide_label", sa.String(64), nullable=True),
        sa.Column("stain", sa.String(64), nullable=True),
        sa.Column("scanner_make", sa.String(64), nullable=True),
        sa.Column("scanner_model", sa.String(64), nullable=True),
        sa.Column("magnification", sa.Float(), nullable=True),
        sa.Column("mpp_x", sa.Float(), nullable=True),  # microns per pixel
        sa.Column("mpp_y", sa.Float(), nullable=True),
        sa.Column("base_width", sa.Integer(), nullable=True),
        sa.Column("base_height", sa.Integer(), nullable=True),
        sa.Column("pyramid_levels", sa.Integer(), nullable=True),
        # Source format string used to pick the reader at viewer time.
        # Free-form on purpose (new formats land without a schema bump);
        # the importer normalises to one of {svs, ndpi, ome-tiff,
        # dicom-wsi, mrxs, scn}.
        sa.Column("source_format", sa.String(16), nullable=False),
        # S3 layout: bucket + key for the original file plus optional
        # derived artefacts. Storage isolation (see memory
        # ``feedback-storage-isolation``): callers never see these
        # values, all reads go through backend endpoints that resolve
        # them server-side.
        sa.Column("s3_bucket", sa.String(255), nullable=False),
        sa.Column("s3_source_key", sa.String(512), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("s3_thumbnail_key", sa.String(512), nullable=True),
        sa.Column("s3_macro_key", sa.String(512), nullable=True),
        sa.Column("s3_label_key", sa.String(512), nullable=True),
        # ``label_redacted=True`` means we either dropped the label
        # image during ingest or never wrote it (the label often
        # carries PHI like patient name + MRN — see spike §6). The
        # column is True by default so a slide with NULL s3_label_key
        # is a deliberate omission, not a missing field.
        sa.Column(
            "label_redacted",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "contribution_tier",
            sa.String(8),
            nullable=False,
            server_default=sa.text("'t1'"),
        ),
        sa.Column(
            "is_public",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "ingestion_complete",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        # License / provenance — same shape as imaging_studies (0004).
        sa.Column("source_collection", sa.Text(), nullable=True),
        sa.Column("source_subject_id", sa.Text(), nullable=True),
        sa.Column("license_spdx", sa.Text(), nullable=True),
        sa.Column("license_url", sa.Text(), nullable=True),
        sa.Column(
            "citation_required",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("citation_text", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "owner_subject_id",
            "slide_instance_uid",
            name="uq_pathology_slides_owner_uid",
        ),
        sa.CheckConstraint(
            "contribution_tier IN ('t1','t2','t3','t4')",
            name="ck_pathology_slides_tier",
        ),
        sa.CheckConstraint(
            "contribution_tier <> 't4' "
            "OR (license_spdx IS NOT NULL AND source_collection IS NOT NULL)",
            name="ck_pathology_slides_t4_license",
        ),
    )

    op.create_index(
        "ix_pathology_slides_patient",
        "pathology_slides",
        ["patient_id"],
    )
    op.create_index(
        "ix_pathology_slides_public",
        "pathology_slides",
        ["is_public"],
    )
    op.create_index(
        "ix_pathology_slides_tier",
        "pathology_slides",
        ["contribution_tier"],
    )
    op.create_index(
        "ix_pathology_slides_event",
        "pathology_slides",
        ["clinical_event_id"],
        postgresql_where=sa.text("clinical_event_id IS NOT NULL"),
    )
    # Partial UNIQUE mirroring imaging_studies (0004): same idempotency
    # key shape so the public-dataset import flow plugs in unchanged
    # when we add a pathology-public connector.
    op.create_index(
        "uq_pathology_slides_source",
        "pathology_slides",
        ["source_collection", "source_subject_id", "slide_instance_uid"],
        unique=True,
        postgresql_where=sa.text("source_collection IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_pathology_slides_source", table_name="pathology_slides")
    op.drop_index("ix_pathology_slides_event", table_name="pathology_slides")
    op.drop_index("ix_pathology_slides_tier", table_name="pathology_slides")
    op.drop_index("ix_pathology_slides_public", table_name="pathology_slides")
    op.drop_index("ix_pathology_slides_patient", table_name="pathology_slides")
    op.drop_table("pathology_slides")

    # Restore the previous CHECK without ``pathology_slide`` — any
    # rows already carrying that kind must be cleaned up before
    # downgrade (the migration does NOT auto-delete them to avoid
    # silent data loss).
    op.drop_constraint("ck_clinical_events_kind", "clinical_events", type_="check")
    _old_kinds = tuple(k for k in _NEW_KINDS if k != _PATHOLOGY_KIND)
    op.create_check_constraint(
        "ck_clinical_events_kind",
        "clinical_events",
        "kind IN (" + ",".join(f"'{k}'" for k in _old_kinds) + ")",
    )
