"""FSM transition matrix for ClinicalEvent.event_status.

Pure unit tests — no DB. Pins the contract that
``services/clinical_events_fsm.assert_transition_allowed`` enforces,
so a future edit that loosens or tightens the FSM is caught here
before it ships.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from bvphoenix.services import clinical_events_fsm as fsm

# (from_status, to_status, allowed)
MATRIX: list[tuple[str, str, bool]] = [
    # planned ->
    ("planned", "confirmed", True),
    ("planned", "cancelled", True),
    ("planned", "rescheduled", True),
    ("planned", "completed", True),
    ("planned", "missed", True),
    ("planned", "planned", False),
    # confirmed ->
    ("confirmed", "completed", True),
    ("confirmed", "cancelled", True),
    ("confirmed", "rescheduled", True),
    ("confirmed", "missed", True),
    ("confirmed", "planned", False),
    ("confirmed", "confirmed", False),
    # completed (terminal)
    ("completed", "planned", False),
    ("completed", "confirmed", False),
    ("completed", "cancelled", False),
    ("completed", "rescheduled", False),
    ("completed", "missed", False),
    # cancelled (terminal)
    ("cancelled", "planned", False),
    ("cancelled", "confirmed", False),
    ("cancelled", "completed", False),
    ("cancelled", "rescheduled", False),
    ("cancelled", "missed", False),
    # missed -> rescheduled | completed
    ("missed", "rescheduled", True),
    ("missed", "completed", True),
    ("missed", "planned", False),
    ("missed", "confirmed", False),
    ("missed", "cancelled", False),
    # rescheduled (terminal)
    ("rescheduled", "planned", False),
    ("rescheduled", "confirmed", False),
    ("rescheduled", "completed", False),
    ("rescheduled", "cancelled", False),
    ("rescheduled", "missed", False),
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


def test_allowed_next_planned() -> None:
    assert fsm.allowed_next("planned") == frozenset(
        {"confirmed", "cancelled", "rescheduled", "completed", "missed"}
    )


def test_allowed_next_terminal() -> None:
    assert fsm.allowed_next("completed") == frozenset()
    assert fsm.allowed_next("cancelled") == frozenset()
    assert fsm.allowed_next("rescheduled") == frozenset()


def test_allowed_next_unknown_status() -> None:
    # Defensive: an unknown status (typo, future state we don't know
    # about) returns the empty set rather than raising. Callers that
    # care can check ``is_transition_allowed`` directly.
    assert fsm.allowed_next("nonsense") == frozenset()
