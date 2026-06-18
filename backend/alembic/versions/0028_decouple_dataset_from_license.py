"""Decouple the training Dataset from the License (Flow task a5c3f73e, Option 3).

A ``licensed_datasets`` row was tied to exactly one ``training_licenses`` row
(``license_id`` NOT NULL). That conflated "the frozen, anonymized cohort"
with "one licensee's grant over it", forcing a re-stream + duplicate ledger
per licensee and leaving no place for the open/unsigned datasets the
revoke-propagation story needs.

Make the dataset standalone and reusable; the grant moves onto the license:

* ``licensed_datasets`` drops ``license_id`` (Postgres cascades its FK +
  ``ix_licensed_datasets_license``) and gains ``status``
  (``open`` | ``frozen`` | ``stale``): a dataset is ``open`` until a license
  over it is signed (``frozen``), or a contributing consent is revoked while
  still open (``stale`` → must be rebuilt before it can be licensed).
* ``training_licenses`` gains ``dataset_id`` → ``licensed_datasets.id``
  (RESTRICT): the cohort a license grants.

The table has never had a producer, so it is empty in every environment —
no data migration. ``assemble_payouts`` now reaches the dataset via
``license.dataset_id``.

Revision ID: 0028_decouple_dataset_from_license
Revises: 0027_lesion_tracks
Create Date: 2026-06-18
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PGUUID

revision = "0028_decouple_dataset_from_license"
down_revision = "0027_lesion_tracks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # DROP COLUMN cascades to the license_id FK and ix_licensed_datasets_license.
    op.drop_column("licensed_datasets", "license_id")
    op.add_column(
        "licensed_datasets",
        sa.Column("status", sa.String(16), nullable=False, server_default="open"),
    )
    op.create_check_constraint(
        "ck_licensed_datasets_status",
        "licensed_datasets",
        "status IN ('open','frozen','stale')",
    )
    op.add_column(
        "training_licenses",
        sa.Column("dataset_id", PGUUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_training_licenses_dataset",
        "training_licenses",
        "licensed_datasets",
        ["dataset_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_training_licenses_dataset", "training_licenses", ["dataset_id"])


def downgrade() -> None:
    op.drop_index("ix_training_licenses_dataset", table_name="training_licenses")
    op.drop_constraint("fk_training_licenses_dataset", "training_licenses", type_="foreignkey")
    op.drop_column("training_licenses", "dataset_id")
    op.drop_constraint("ck_licensed_datasets_status", "licensed_datasets", type_="check")
    op.drop_column("licensed_datasets", "status")
    op.add_column(
        "licensed_datasets",
        sa.Column("license_id", PGUUID(as_uuid=True), nullable=False),
    )
    op.create_foreign_key(
        "fk_licensed_datasets_license",
        "licensed_datasets",
        "training_licenses",
        ["license_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index("ix_licensed_datasets_license", "licensed_datasets", ["license_id"])
