"""DB-level invariants for OpenData tier + pathology PHI label.

Two CHECK constraints pin a pair of policy decisions that were until
now enforced only at the Python layer:

  * ``ck_pathology_slides_label_redacted_when_present`` —
    ``s3_label_key IS NULL OR label_redacted = TRUE``. The printed
    slide label typically carries patient name + DOB; the operator
    workflow guarantees a redaction pass before the upload, but a bug
    in the ingest CLI used to be able to write ``s3_label_key`` while
    leaving ``label_redacted=false``. With the CHECK in place the
    insert fails outright; the application sees the error and stops
    instead of silently storing un-redacted PHI.

  * ``ck_imaging_studies_public_tier_t4`` —
    ``is_public = FALSE OR contribution_tier = 't4'``. The OpenData
    library invariant: a study can only be world-visible if it is
    tagged T4 (creative-commons-friendly tier). Pre-2026-05-21 this
    was enforced only by the application; a manual ``UPDATE ... SET
    is_public=true`` outside the standard flow could leak a T1/T2
    private study to anonymous traffic.

Both constraints are validated against existing rows before
installation; a non-compliant row will fail the migration. The
production data inventory was audited at migration time (35 OpenData
patients / 154 studies, all T4 with license_spdx set; pathology table
is empty so the label CHECK is a no-op on existing rows). If the
audit finds offenders, the operator must fix them before running
``alembic upgrade head``.

Revision ID: 0007_opendata_pathology_constraints
Revises: 0006_agent_assistants_revoked_at
Create Date: 2026-05-21
"""

from __future__ import annotations

from alembic import op

revision = "0007_opendata_pathology_constraints"
down_revision = "0006_agent_assistants_revoked_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Pathology PHI label: if we stored a label image, we must have
    # marked it redacted.
    op.create_check_constraint(
        "ck_pathology_slides_label_redacted_when_present",
        "pathology_slides",
        "s3_label_key IS NULL OR label_redacted = TRUE",
    )

    # OpenData tier isolation: a publicly-visible study must live in
    # the T4 (CC) tier. Private and Marketplace tiers (T1/T2/T3) are
    # never world-readable.
    op.create_check_constraint(
        "ck_imaging_studies_public_tier_t4",
        "imaging_studies",
        "is_public = FALSE OR contribution_tier = 't4'",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_imaging_studies_public_tier_t4",
        "imaging_studies",
        type_="check",
    )
    op.drop_constraint(
        "ck_pathology_slides_label_redacted_when_present",
        "pathology_slides",
        type_="check",
    )
