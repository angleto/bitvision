"""License / provenance columns on imaging_studies (public dataset support).

Adds the columns needed by the new admin-only public-dataset importer
(``services.public_dataset.import_public_dataset``). T4 (CC public) studies
land with the upstream collection name, the original source subject id,
the SPDX license code, the canonical license URL, and the attribution
string the license requires.

Idempotency invariant: ``(source_collection, source_subject_id, study_instance_uid)``
is UNIQUE among rows that *have* a ``source_collection``. Re-running the
importer against the same TCIA / IDC / OsiriX subject is a no-op rather
than a duplicate.

Integrity invariant: ``contribution_tier='t4'`` requires both
``license_spdx`` and ``source_collection`` to be set. The CHECK lets
existing private rows stay NULL-everything; only T4 carries the
attribution payload.

Revision ID: 0004_imaging_studies_provenance
Revises: 0003_calendar_subscriptions
Create Date: 2026-05-20
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004_imaging_studies_provenance"
down_revision = "0003_calendar_subscriptions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("imaging_studies", sa.Column("source_collection", sa.Text(), nullable=True))
    op.add_column("imaging_studies", sa.Column("source_subject_id", sa.Text(), nullable=True))
    op.add_column("imaging_studies", sa.Column("license_spdx", sa.Text(), nullable=True))
    op.add_column("imaging_studies", sa.Column("license_url", sa.Text(), nullable=True))
    op.add_column(
        "imaging_studies",
        sa.Column(
            "citation_required",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column("imaging_studies", sa.Column("citation_text", sa.Text(), nullable=True))

    # Partial UNIQUE: only enforced when source_collection IS NOT NULL,
    # so the millions of private/uploaded studies (NULL on these new
    # columns) are unaffected. Includes study_instance_uid because a
    # single source subject can legitimately have several studies
    # (longitudinal CT, paired CT+PET) under the same subject id.
    op.create_index(
        "uq_imaging_studies_source",
        "imaging_studies",
        ["source_collection", "source_subject_id", "study_instance_uid"],
        unique=True,
        postgresql_where=sa.text("source_collection IS NOT NULL"),
    )

    # T4 (CC public) is the only tier that *must* carry license+provenance.
    # Lower tiers are private/shared user uploads; they have no license
    # to advertise. The CHECK is loose by design: T1..T3 can leave the
    # new columns NULL, T4 must populate the two essentials.
    op.create_check_constraint(
        "ck_imaging_studies_t4_license",
        "imaging_studies",
        "contribution_tier <> 't4' OR (license_spdx IS NOT NULL AND source_collection IS NOT NULL)",
    )


def downgrade() -> None:
    op.drop_constraint("ck_imaging_studies_t4_license", "imaging_studies", type_="check")
    op.drop_index("uq_imaging_studies_source", table_name="imaging_studies")
    op.drop_column("imaging_studies", "citation_text")
    op.drop_column("imaging_studies", "citation_required")
    op.drop_column("imaging_studies", "license_url")
    op.drop_column("imaging_studies", "license_spdx")
    op.drop_column("imaging_studies", "source_subject_id")
    op.drop_column("imaging_studies", "source_collection")
