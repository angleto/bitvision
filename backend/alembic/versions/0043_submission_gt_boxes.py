"""Ground-truth PHI boxes on submissions (M6c review-UI box-labeling).

Adds a per-instance ground-truth annotation store to ``submissions`` so a
reviewer can draw the burned-in-PHI boxes on a staged instance (the answer key
against which the automatic pixel redaction's recall is scored). Shape:
``{instance_id: [{x, y, w, h, text, category}]}`` — the exact ``GtBox`` schema
of ``services.pixel_deid_eval`` (intrinsic pixel XYWH, top-left origin), so the
labels round-trip to ``answer_key.json`` and feed ``score_redaction``.

Purely additive (nullable JSONB column, no data rewrite).

Revision ID: 0043_submission_gt_boxes
Revises: 0042_bge_m3_aux_target_kinds
Create Date: 2026-07-02
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0043_submission_gt_boxes"
down_revision = "0042_bge_m3_aux_target_kinds"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("submissions", sa.Column("gt_boxes", JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("submissions", "gt_boxes")
