"""Shared review-queue primitives: ``review_status`` enum + provenance CHECKs.

The staging/review engine (``services/review_queue``) is consumed by two
stores with opposite PHI postures — ``inbox_items`` (patient inbound
inbox, fbbf5270) and ``submissions`` (public contribution, 133349a9).
This migration ships only what both share, so the consumer migrations
can land independently:

* the ``review_status`` Postgres ENUM the mixin's ``status`` column
  uses (``create_type=False`` ORM-side: this is the only place the type
  is created);
* the widened ``provenance_events`` CHECK constraints — the engine
  stamps one ``transition.<to_status>`` provenance row per lifecycle
  edge against target kinds ``inbox_item`` / ``submission``.

The CHECK lists are imported from the ORM tuples (single source of
truth, same anchoring the model constraints use) rather than re-typed
here; the import is stable because migrations run inside the backend
package.

Revision ID: 0024_review_queue_shared
Revises: 0023_text_model_routing
Create Date: 2026-06-12
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

from bvphoenix.db.models.provenance_events import (
    PROVENANCE_ACTIVITIES,
    PROVENANCE_TARGET_KINDS,
)
from bvphoenix.services.review_queue.states import REVIEW_STATUSES

revision = "0024_review_queue_shared"
down_revision = "0023_text_model_routing"
branch_labels = None
depends_on = None

_REVIEW_TARGET_KINDS = ("inbox_item", "submission")
_REVIEW_ACTIVITIES = tuple(
    a
    for a in PROVENANCE_ACTIVITIES
    if a.startswith("transition.") and a.split(".", 1)[1] in REVIEW_STATUSES
)


def _in_list(values: tuple[str, ...]) -> str:
    return ",".join(f"'{v}'" for v in values)


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        text(
            "DO $$ BEGIN "
            f"CREATE TYPE review_status AS ENUM ({_in_list(REVIEW_STATUSES)}); "
            "EXCEPTION WHEN duplicate_object THEN NULL; END $$;"
        )
    )
    conn.execute(
        text("ALTER TABLE provenance_events DROP CONSTRAINT ck_provenance_events_target_kind")
    )
    conn.execute(
        text(
            "ALTER TABLE provenance_events ADD CONSTRAINT ck_provenance_events_target_kind "
            f"CHECK (target_kind IN ({_in_list(PROVENANCE_TARGET_KINDS)}))"
        )
    )
    conn.execute(
        text("ALTER TABLE provenance_events DROP CONSTRAINT ck_provenance_events_activity")
    )
    conn.execute(
        text(
            "ALTER TABLE provenance_events ADD CONSTRAINT ck_provenance_events_activity "
            f"CHECK (activity IN ({_in_list(PROVENANCE_ACTIVITIES)}))"
        )
    )


def downgrade() -> None:
    conn = op.get_bind()
    prev_targets = tuple(k for k in PROVENANCE_TARGET_KINDS if k not in _REVIEW_TARGET_KINDS)
    prev_activities = tuple(a for a in PROVENANCE_ACTIVITIES if a not in _REVIEW_ACTIVITIES)
    conn.execute(
        text("ALTER TABLE provenance_events DROP CONSTRAINT ck_provenance_events_activity")
    )
    conn.execute(
        text(
            "ALTER TABLE provenance_events ADD CONSTRAINT ck_provenance_events_activity "
            f"CHECK (activity IN ({_in_list(prev_activities)}))"
        )
    )
    conn.execute(
        text("ALTER TABLE provenance_events DROP CONSTRAINT ck_provenance_events_target_kind")
    )
    conn.execute(
        text(
            "ALTER TABLE provenance_events ADD CONSTRAINT ck_provenance_events_target_kind "
            f"CHECK (target_kind IN ({_in_list(prev_targets)}))"
        )
    )
    conn.execute(text("DROP TYPE IF EXISTS review_status"))
