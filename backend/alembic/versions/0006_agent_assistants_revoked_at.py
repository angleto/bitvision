"""Per-assistant explicit revocation column.

Adds ``revoked_at`` to ``agent_assistants`` so a leaked client_secret
can be retired with an audit-friendly timestamp, separately from the
existing ``is_active`` flag (which is overloaded: today it doubles as
"soft delete" and "compromised — do not honour"). The auth path now
considers both columns:

  WHERE client_secret_hash = :h
    AND is_active = TRUE
    AND revoked_at IS NULL

The column is nullable (no backfill needed): existing rows are
treated as not revoked.

Revision ID: 0006_agent_assistants_revoked_at
Revises: 0005_pathology_slides
Create Date: 2026-05-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006_agent_assistants_revoked_at"
down_revision = "0005_pathology_slides"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agent_assistants",
        sa.Column(
            "revoked_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    # Partial index — only the small set of revoked rows is indexed,
    # so the auth-path lookup pays no extra cost on the hot
    # ``revoked_at IS NULL`` filter.
    op.create_index(
        "ix_agent_assistants_revoked_at",
        "agent_assistants",
        ["revoked_at"],
        postgresql_where=sa.text("revoked_at IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_agent_assistants_revoked_at", table_name="agent_assistants")
    op.drop_column("agent_assistants", "revoked_at")
