"""Clinical-event time amendments: new transition verb + total date derivation.

Four structural changes, no data backfill.

1. ``clinical_event_transitions.action`` gains ``amend_time``. Correcting
   the recorded date/time of an event is not an FSM move (the status does
   not change), but it IS an amendment of a clinical record and must land
   in the same audit table as the transitions, with actor, snapshots and
   an optional reason. The existing CHECK only knew the five FSM verbs,
   so the amendment row would have failed to insert.

2. ``fn_ce_derive_event_date()`` becomes total over the six statuses.
   Before this migration ``cancelled`` matched neither branch, so a
   cancelled row's derived ``event_date`` was frozen while every other
   status re-derived: one status behaved differently for no reason, and
   that special case had to be mirrored in the API layer. ``cancelled``
   is anchored on ``planned_start_at`` like the rest of the
   not-yet-realised family.

   The function's old comment claimed it only derived "when the API
   hasn't supplied an explicit event_date"; the body never implemented
   such a guard and the comment is removed rather than the behaviour: it
   is the behaviour that is correct. ``event_date`` is a DERIVED
   projection of the status anchor, and the API now enforces the same
   rule (``POST /clinical-events/{id}/amend-time`` accepts a direct
   ``event_date`` only for rows whose anchor is NULL, i.e. the DICOM
   ``StudyDate`` / legacy imports that genuinely have no time).

3. ``provenance_events.activity`` gains ``transition.amend_time``. The
   amendment stamps the parent event like every other transition verb,
   and the CHECK is rebuilt from the model tuple exactly as migrations
   0024 and 0025 did.

4. ``notification_dispatches.idempotency_key`` uniqueness becomes
   partial (``WHERE status <> 'cancelled'``). Every re-scheduling path
   cancels the pending reminders and then re-materialises them; with a
   global UNIQUE the cancelled row still owned the key, the fresh insert
   hit ON CONFLICT DO NOTHING and the patient silently stopped getting
   any reminder for the moved appointment. Paired with the anchor
   joining the key hash in ``services/notifications/scheduling.py``.
   Existing rows are untouched: the old constraint guaranteed global
   uniqueness, so a weaker partial index always builds.

No row is rewritten. Existing ``cancelled`` rows already hold
``planned_start_at::date`` (derived while they were planned/confirmed),
so re-derivation on their next update is a no-op; asserting a value for
rows where the anchor is NULL would be guesswork on patient data.

Revision ID: 0047_clinical_event_amend_time
Revises: 0046_email_deliveries
Create Date: 2026-08-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from bvphoenix.db.models.provenance_events import PROVENANCE_ACTIVITIES

revision = "0047_clinical_event_amend_time"
down_revision = "0046_email_deliveries"
branch_labels = None
depends_on = None

_ACTIONS_AFTER = ("confirm", "reschedule", "complete", "cancel", "mark_missed", "amend_time")
_ACTIONS_BEFORE = ("confirm", "reschedule", "complete", "cancel", "mark_missed")

_DERIVE_FN_AFTER = """
CREATE OR REPLACE FUNCTION public.fn_ce_derive_event_date() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        DECLARE
            tz text := COALESCE(NEW.timezone, 'UTC');
        BEGIN
            -- event_date is a DERIVED projection of the status anchor,
            -- kept for the legacy DATE-only queries and indices. It is
            -- overwritten unconditionally whenever an anchor exists: a
            -- hand-written event_date must never survive a move of the
            -- timestamp it is supposed to project.
            --
            -- Rows with a NULL anchor (DICOM StudyDate imports, document
            -- backfills, other date-only history) keep their standalone
            -- event_date; that is the only case in which the API lets a
            -- caller write it directly.
            IF NEW.event_status IN ('planned','confirmed','rescheduled','cancelled')
               AND NEW.planned_start_at IS NOT NULL THEN
                NEW.event_date := ((NEW.planned_start_at AT TIME ZONE tz))::date;
            ELSIF NEW.event_status IN ('completed','missed')
                  AND NEW.actual_start_at IS NOT NULL THEN
                NEW.event_date := ((NEW.actual_start_at AT TIME ZONE tz))::date;
            END IF;
            RETURN NEW;
        END $$;
"""

_DERIVE_FN_BEFORE = """
CREATE OR REPLACE FUNCTION public.fn_ce_derive_event_date() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        DECLARE
            tz text := COALESCE(NEW.timezone, 'UTC');
        BEGIN
            -- Only derive when the API hasn't supplied an explicit
            -- event_date (back-compat path: legacy callers set it
            -- directly without touching the timestamps).
            IF NEW.event_status IN ('planned','confirmed','rescheduled')
               AND NEW.planned_start_at IS NOT NULL THEN
                NEW.event_date := ((NEW.planned_start_at AT TIME ZONE tz))::date;
            ELSIF NEW.event_status IN ('completed','missed')
                  AND NEW.actual_start_at IS NOT NULL THEN
                NEW.event_date := ((NEW.actual_start_at AT TIME ZONE tz))::date;
            END IF;
            RETURN NEW;
        END $$;
"""


def _swap_action_check(actions: tuple[str, ...]) -> None:
    op.execute(
        "ALTER TABLE clinical_event_transitions DROP CONSTRAINT IF EXISTS ck_ce_transitions_action"
    )
    allowed = ",".join(f"'{a}'" for a in actions)
    op.execute(
        "ALTER TABLE clinical_event_transitions "
        "ADD CONSTRAINT ck_ce_transitions_action "
        f"CHECK (action IN ({allowed}))"
    )


def _swap_activity_check(activities: tuple[str, ...]) -> None:
    """Rebuild ``ck_provenance_events_activity``. Same shape as
    migrations 0024 and 0025, which also widened this enum."""
    op.execute(
        "ALTER TABLE provenance_events DROP CONSTRAINT IF EXISTS ck_provenance_events_activity"
    )
    allowed = ",".join(f"'{a}'" for a in activities)
    op.execute(
        "ALTER TABLE provenance_events "
        "ADD CONSTRAINT ck_provenance_events_activity "
        f"CHECK (activity IN ({allowed}))"
    )


def upgrade() -> None:
    _swap_action_check(_ACTIONS_AFTER)
    # The amendment also stamps ``activity='transition.amend_time'`` on
    # provenance_events; without this the whole endpoint dies at commit
    # on the activity CHECK.
    _swap_activity_check(tuple(PROVENANCE_ACTIVITIES))
    op.execute(_DERIVE_FN_AFTER)

    # A cancelled reminder must release its idempotency key.
    #
    # ``materialise_event_dispatches`` inserts with ON CONFLICT DO
    # NOTHING on ``idempotency_key``, and every re-scheduling path
    # (reschedule, amend-time, a reminder-offsets patch) cancels the
    # pending rows first and then re-materialises. With a GLOBAL unique
    # constraint the cancelled row still owned the key, so the fresh row
    # was silently dropped: the appointment moved and the patient
    # stopped getting any reminder at all. Excluding cancelled rows from
    # the uniqueness scope is what makes cancel-then-rebuild work, and
    # it still rejects a genuine duplicate re-run (the live row keeps
    # the key). Paired with the anchor now being part of the key hash
    # (services/notifications/scheduling.py), so a moved anchor is a
    # different reminder rather than the same one rescheduled.
    op.execute(
        "ALTER TABLE notification_dispatches DROP CONSTRAINT IF EXISTS uq_notification_dispatches_idem"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_notification_dispatches_idem "
        "ON notification_dispatches (idempotency_key) WHERE status <> 'cancelled'"
    )


def downgrade() -> None:
    # Refuse rather than silently delete audit rows: an amendment row is
    # a clinical-record correction, not scratch data.
    conn = op.get_bind()
    existing = conn.execute(
        sa.text("SELECT 1 FROM clinical_event_transitions WHERE action = 'amend_time' LIMIT 1")
    ).first()
    if existing is not None:
        raise RuntimeError(
            "clinical_event_transitions contains amend_time rows; downgrading would "
            "require deleting audit records. Remove them deliberately first."
        )
    # Restoring the global unique constraint needs the key namespace to
    # be globally unique again. Refuse rather than delete dispatch rows.
    dup = conn.execute(
        sa.text(
            "SELECT idempotency_key FROM notification_dispatches "
            "GROUP BY idempotency_key HAVING count(*) > 1 LIMIT 1"
        )
    ).first()
    if dup is not None:
        raise RuntimeError(
            "notification_dispatches holds cancelled rows sharing an idempotency_key with a "
            "live one; restoring the global UNIQUE would require deleting dispatch history."
        )
    # Robust in both directions: before 0047 the name belongs to a table
    # CONSTRAINT (whose index cannot be dropped with DROP INDEX), after
    # 0047 it is a bare partial index.
    op.execute(
        "ALTER TABLE notification_dispatches "
        "DROP CONSTRAINT IF EXISTS uq_notification_dispatches_idem"
    )
    op.execute("DROP INDEX IF EXISTS uq_notification_dispatches_idem")
    op.execute(
        "ALTER TABLE notification_dispatches "
        "ADD CONSTRAINT uq_notification_dispatches_idem UNIQUE (idempotency_key)"
    )
    op.execute(_DERIVE_FN_BEFORE)
    _swap_activity_check(tuple(a for a in PROVENANCE_ACTIVITIES if a != "transition.amend_time"))
    _swap_action_check(_ACTIONS_BEFORE)
