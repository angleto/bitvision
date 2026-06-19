"""De-identification stamp on imaging_studies (services/deid engine).

Adds ``deidentified_at`` + ``deid_method_version`` so the reindex worker can
mark a study as de-identified at rest and the share-link / T3 download path can
serve the stored scrubbed bytes directly when the version matches — retiring the
lazy re-scrub-on-every-download. Both nullable + additive; no backfill.

Revision ID: 0035_deid_stamp
Revises: 0034_pixel_phi_risk
Create Date: 2026-06-19
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0035_deid_stamp"
down_revision = "0034_pixel_phi_risk"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "imaging_studies",
        sa.Column("deidentified_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "imaging_studies",
        sa.Column("deid_method_version", sa.String(length=32), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("imaging_studies", "deid_method_version")
    op.drop_column("imaging_studies", "deidentified_at")
