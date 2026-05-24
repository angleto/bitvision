"""Finite state machine for ``PatientTask.status``.

The lifecycle (migration 0106) is::

    pending     -> in_progress | snoozed | done    | dropped
    in_progress -> snoozed     | done    | dropped
    snoozed     -> pending     | in_progress      (wake)
    done        -> pending                         (reopen)
    dropped     -> pending                         (reopen)

This module is the **single source of truth** for which transitions
are allowed. Every transition sub-resource (``/start``, ``/snooze``,
``/wake``, ``/complete``, ``/drop``, ``/reopen``, ``/reschedule``)
and every MCP tool that mutates ``status`` MUST call
:func:`assert_transition_allowed` before persisting, so the FSM stays
in sync with the DB CHECK constraints from migration 0106.

Why a Python FSM on top of the DB CHECK
---------------------------------------

The DB constraint enforces *shape* (the set of allowed status string
values, "snoozed requires snooze_until"), not *transition sequence*.
Without the FSM a writer could overwrite a ``done`` task back to
``pending`` and silently lose the completed_at timestamp. The FSM
rejects that with a structured 422 ``invalid_transition`` error
before the row hits the DB.

Mirrors the API and shape of
``backend/src/bvphoenix/services/clinical_events_fsm.py`` so callers
in the API/MCP layer can reuse the same idioms verbatim.
"""

from __future__ import annotations

from fastapi import HTTPException

# Status names — keep aligned with ``PATIENT_TASK_STATUSES`` in
# ``db/models/patient_tasks.py``.
PENDING = "pending"
IN_PROGRESS = "in_progress"
SNOOZED = "snoozed"
DONE = "done"
DROPPED = "dropped"

# Allowed forward transitions per source state. Unlike clinical
# events, ``done`` and ``dropped`` are NOT terminal: a user may
# legitimately reopen a task that was marked done by mistake. Reopen
# always lands in ``pending`` (the in_progress / snoozed sub-state is
# lost so the audit chain shows the reopen as a fresh start).
_ALLOWED: dict[str, frozenset[str]] = {
    PENDING: frozenset({IN_PROGRESS, SNOOZED, DONE, DROPPED}),
    IN_PROGRESS: frozenset({SNOOZED, DONE, DROPPED}),
    SNOOZED: frozenset({PENDING, IN_PROGRESS}),
    DONE: frozenset({PENDING}),
    DROPPED: frozenset({PENDING}),
}


def is_transition_allowed(*, from_status: str, to_status: str) -> bool:
    """Pure predicate, no side effects. Useful in tests and for the
    dry_run preview path on transition endpoints."""
    if from_status not in _ALLOWED:
        return False
    return to_status in _ALLOWED[from_status]


def assert_transition_allowed(*, from_status: str, to_status: str) -> None:
    """Raise HTTP 422 with a structured detail if the transition is
    not in the allowed set. Error shape mirrors clinical_events_fsm
    so the MCP layer can dispatch on ``detail.code`` uniformly."""
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
                f"patient_task status transition '{from_status}' -> '{to_status}' "
                f"is not allowed; from '{from_status}' you can go to: "
                f"{sorted(_ALLOWED.get(from_status, frozenset())) or '<terminal>'}"
            ),
        },
    )


def allowed_next(from_status: str) -> frozenset[str]:
    """Inspection helper used by the UI hint layer (e.g. greying out
    action buttons that would 422)."""
    return _ALLOWED.get(from_status, frozenset())


# Mapping from action verb (the URL sub-resource / MCP tool name) to
# the target status the verb produces. Used by the service layer to
# avoid stringly-typed action -> status decoding scattered across
# endpoints. ``reschedule`` does not appear here: it is a compound
# action that drops the current task and creates a new one, the FSM
# transition on the OLD row is ``-> dropped`` and on the NEW row is
# implicitly ``-> pending`` (fresh insert).
ACTION_TO_TARGET_STATUS: dict[str, str] = {
    "start": IN_PROGRESS,
    "snooze": SNOOZED,
    "wake": PENDING,  # default; service may override to IN_PROGRESS if the
    # task was paused mid-flight (snoozed-from-in_progress case)
    "complete": DONE,
    "drop": DROPPED,
    "reopen": PENDING,
}


__all__ = [
    "ACTION_TO_TARGET_STATUS",
    "DONE",
    "DROPPED",
    "IN_PROGRESS",
    "PENDING",
    "SNOOZED",
    "allowed_next",
    "assert_transition_allowed",
    "is_transition_allowed",
]
