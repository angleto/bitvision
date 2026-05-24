"""Finite state machine for ``ClinicalEvent.event_status``.

The schema lifecycle (migration 0098) is::

    planned    -> confirmed | cancelled | rescheduled | completed | missed
    confirmed  -> completed  | cancelled | rescheduled | missed
    completed  -> (terminal: re-open out of scope; admin override only)
    cancelled  -> (terminal)
    missed     -> rescheduled | completed   (patient arrived late)
    rescheduled-> (terminal: a new event row carries the moved slot,
                   the old row stays as audit-trail anchor)

This module is the **single source of truth** for which transitions
are allowed. Every transition sub-resource (``/confirm``,
``/reschedule``, ``/complete``, ``/cancel``, ``/mark-missed``) and
every MCP tool that mutates ``event_status`` MUST call
:func:`assert_transition_allowed` before persisting, so the FSM stays
in sync with the DB CHECK constraints from migration 0098.

Why a Python FSM on top of the DB CHECK: the DB constraint enforces
*shape* (planned needs ``planned_start_at``, etc.), not *transition
sequence*. Without the FSM a writer could overwrite a ``completed``
event back to ``planned`` and silently lose audit context. The FSM
rejects that with a structured 422 ``invalid_transition`` error before
the row hits the DB.

Conceptual placement: see ``docs/data-model.md`` once the calendar
section lands; for now this module is referenced from
``backend/src/bvphoenix/api/clinical_events.py`` and the MCP
transition tools.
"""

from __future__ import annotations

from fastapi import HTTPException

# Status names — keep aligned with ``CLINICAL_EVENT_STATUSES`` in
# ``db/models/clinical_events.py``. We re-declare them here as
# constants so callers do not need to import the model layer just to
# spell a status.
PLANNED = "planned"
CONFIRMED = "confirmed"
COMPLETED = "completed"
CANCELLED = "cancelled"
MISSED = "missed"
RESCHEDULED = "rescheduled"

# Allowed forward transitions per source state. Terminal states map
# to the empty frozenset.
_ALLOWED: dict[str, frozenset[str]] = {
    PLANNED: frozenset({CONFIRMED, CANCELLED, RESCHEDULED, COMPLETED, MISSED}),
    CONFIRMED: frozenset({COMPLETED, CANCELLED, RESCHEDULED, MISSED}),
    COMPLETED: frozenset(),
    CANCELLED: frozenset(),
    MISSED: frozenset({RESCHEDULED, COMPLETED}),
    RESCHEDULED: frozenset(),
}


def is_transition_allowed(*, from_status: str, to_status: str) -> bool:
    """Pure predicate, no side effects. Useful in tests and for the
    dry_run preview path on transition endpoints."""
    if from_status not in _ALLOWED:
        return False
    return to_status in _ALLOWED[from_status]


def assert_transition_allowed(*, from_status: str, to_status: str) -> None:
    """Raise HTTP 422 with a structured detail if the transition is
    not in the allowed set. The error shape is intentionally machine-
    readable so the MCP layer can surface it back to the agent with
    a precise ``detail.code`` and let the LLM decide whether to retry
    a different verb."""
    if is_transition_allowed(from_status=from_status, to_status=to_status):
        return
    raise HTTPException(
        status_code=422,
        detail={
            "code": "invalid_transition",
            "from": from_status,
            "to": to_status,
            "allowed_from_here": sorted(_ALLOWED.get(from_status, frozenset())),
            "message": (
                f"event_status transition '{from_status}' -> '{to_status}' is not "
                f"allowed; from '{from_status}' you can go to: "
                f"{sorted(_ALLOWED.get(from_status, frozenset())) or '<terminal>'}"
            ),
        },
    )


def allowed_next(from_status: str) -> frozenset[str]:
    """Inspection helper used by the UI hint layer (e.g. greying out
    action buttons that would 422)."""
    return _ALLOWED.get(from_status, frozenset())


__all__ = [
    "CANCELLED",
    "COMPLETED",
    "CONFIRMED",
    "MISSED",
    "PLANNED",
    "RESCHEDULED",
    "allowed_next",
    "assert_transition_allowed",
    "is_transition_allowed",
]
