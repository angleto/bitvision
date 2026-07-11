"""Verified-clean pixel blob pointers on instances (accept→publish loop).

When a human accepts a public-contribution submission, the redacted bytes the
reviewer actually saw are stamped (``BurnedInAnnotation=NO`` + CID 7050
``113101``/``113102``) and published. For an in-place tier flip (t3 training
pool) the ORIGINAL instance row gains a pointer to that verified-clean blob so
the egress paths (training-cohort export, public serve) can substitute it for
the raw high-risk bytes instead of excluding the instance forever.

``pixel_deid_status='approved'`` (already in the 0034 CHECK, previously never
written) marks the human decision; these two columns carry WHERE the approved
bytes live. Public clones (t4) are clean at rest, so they get ``approved`` with
NULL pointers (their own ``s3_key`` IS the clean blob).

Purely additive (nullable columns, no data rewrite).

Revision ID: 0044_pixel_clean_blobs
Revises: 0043_submission_gt_boxes
Create Date: 2026-07-06
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0044_pixel_clean_blobs"
down_revision = "0043_submission_gt_boxes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("instances", sa.Column("pixel_clean_s3_bucket", sa.String(length=128)))
    op.add_column("instances", sa.Column("pixel_clean_s3_key", sa.String(length=1024)))


def downgrade() -> None:
    op.drop_column("instances", "pixel_clean_s3_key")
    op.drop_column("instances", "pixel_clean_s3_bucket")
