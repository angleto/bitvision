"""Add ``geometry`` JSONB to ``derivatives`` for true volume patient-space.

The viewer used to build its Cornerstone volume from ``volume.raw`` with a
fabricated identity frame (ImageOrientationPatient ``[1,0,0,0,1,0]``,
origin ``[0,0,0]``), because the packed blob's 32-byte header is frozen
for backward compatibility and carries no orientation/position tags. That
made on-image L/R / A/P orientation markers an assumption rather than
data, defeated Cornerstone's FrameOfReference-mismatch safety check, and
made cross-study spatial sync impossible.

This column stores the real geometry computed at pack time from the same
sorted datasets used to write the scalars (origin = IPP of voxel (0,0,0),
direction = row/column/slice cosines, FrameOfReferenceUID). The
``volume.raw`` endpoint serves it back as ``X-Volume-*`` response headers
on both the fresh-pack and cache-hit paths.

Nullable + no backfill: existing cached derivatives keep NULL and the
viewer falls back to the identity frame until the series is re-packed.

Revision ID: 0009_derivative_geometry
Revises: 0008_search_indexes
Create Date: 2026-05-29
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0009_derivative_geometry"
down_revision = "0008_search_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "derivatives",
        sa.Column("geometry", JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("derivatives", "geometry")
