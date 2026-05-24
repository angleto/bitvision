"""FSM transition matrix for PatientTask.status.

Pure unit tests — no DB. Pins the contract enforced by
``services/patient_tasks_fsm.assert_transition_allowed`` so a future
edit that loosens or tightens the FSM is caught here before it ships.

The clinical_events FSM has ``done`` and ``cancelled`` as terminal,
but PatientTask intentionally allows ``done → pending`` and
``dropped → pending`` (reopen) because operational items are
frequently closed by mistake or re-emerge.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from bvphoenix.services import patient_tasks_fsm as fsm

# (from_status, to_status, allowed)
MATRIX: list[tuple[str, str, bool]] = [
    # pending -> in_progress | snoozed | done | dropped
    ("pending", "in_progress", True),
    ("pending", "snoozed", True),
    ("pending", "done", True),
    ("pending", "dropped", True),
    ("pending", "pending", False),
    # in_progress -> snoozed | done | dropped
    ("in_progress", "snoozed", True),
    ("in_progress", "done", True),
    ("in_progress", "dropped", True),
    ("in_progress", "pending", False),
    ("in_progress", "in_progress", False),
    # snoozed -> pending | in_progress
    ("snoozed", "pending", True),
    ("snoozed", "in_progress", True),
    ("snoozed", "done", False),
    ("snoozed", "dropped", False),
    ("snoozed", "snoozed", False),
    # done -> pending (reopen only)
    ("done", "pending", True),
    ("done", "in_progress", False),
    ("done", "snoozed", False),
    ("done", "dropped", False),
    ("done", "done", False),
    # dropped -> pending (reopen only)
    ("dropped", "pending", True),
    ("dropped", "in_progress", False),
    ("dropped", "snoozed", False),
    ("dropped", "done", False),
    ("dropped", "dropped", False),
]


@pytest.mark.parametrize("from_status,to_status,allowed", MATRIX)
def test_is_transition_allowed_matrix(from_status: str, to_status: str, allowed: bool) -> None:
    assert fsm.is_transition_allowed(from_status=from_status, to_status=to_status) is allowed


@pytest.mark.parametrize("from_status,to_status,allowed", MATRIX)
def test_assert_transition_allowed_matrix(from_status: str, to_status: str, allowed: bool) -> None:
    if allowed:
        fsm.assert_transition_allowed(from_status=from_status, to_status=to_status)
    else:
        with pytest.raises(HTTPException) as exc_info:
            fsm.assert_transition_allowed(from_status=from_status, to_status=to_status)
        assert exc_info.value.status_code == 422
        assert exc_info.value.detail["code"] == "invalid_transition"
        assert exc_info.value.detail["from"] == from_status
        assert exc_info.value.detail["to"] == to_status


def test_allowed_next_pending() -> None:
    assert fsm.allowed_next("pending") == frozenset({"in_progress", "snoozed", "done", "dropped"})


def test_allowed_next_in_progress() -> None:
    assert fsm.allowed_next("in_progress") == frozenset({"snoozed", "done", "dropped"})


def test_allowed_next_done_reopens_only_to_pending() -> None:
    """Operational tasks can be reopened: a done task lifts back to
    pending (not in_progress) so the status alone tells the story of
    "this was finished, now it isn't"."""
    assert fsm.allowed_next("done") == frozenset({"pending"})


def test_allowed_next_dropped_reopens_only_to_pending() -> None:
    assert fsm.allowed_next("dropped") == frozenset({"pending"})


def test_allowed_next_snoozed_wakes_pending_or_in_progress() -> None:
    assert fsm.allowed_next("snoozed") == frozenset({"pending", "in_progress"})


def test_allowed_next_unknown_status() -> None:
    assert fsm.allowed_next("nonsense") == frozenset()


def test_action_to_target_status_map() -> None:
    """Service-layer convenience map: verb → target status."""
    assert fsm.ACTION_TO_TARGET_STATUS["start"] == fsm.IN_PROGRESS
    assert fsm.ACTION_TO_TARGET_STATUS["complete"] == fsm.DONE
    assert fsm.ACTION_TO_TARGET_STATUS["drop"] == fsm.DROPPED
    assert fsm.ACTION_TO_TARGET_STATUS["reopen"] == fsm.PENDING
    assert fsm.ACTION_TO_TARGET_STATUS["snooze"] == fsm.SNOOZED
    # ``wake`` defaults to PENDING; service may override to IN_PROGRESS
    # when the snooze was applied while the task was already underway.
    assert fsm.ACTION_TO_TARGET_STATUS["wake"] == fsm.PENDING
    # Reschedule is NOT in the map: it's a compound action that drops
    # the old row and inserts a new one, the FSM transition on the old
    # row is ``-> dropped``.
    assert "reschedule" not in fsm.ACTION_TO_TARGET_STATUS
