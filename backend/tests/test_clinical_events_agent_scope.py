"""Agent-token hardening for clinical-events endpoints.

Pins the cross-patient invariant on every write endpoint of
``backend/src/bvphoenix/api/clinical_events.py``:

* ``create_clinical_event`` / ``update_clinical_event`` /
  ``delete_clinical_event``
* ``confirm_event`` / ``reschedule_event`` / ``complete_event`` /
  ``cancel_event`` / ``mark_event_missed`` (FSM transitions,
  routed through ``_load_event_for_transition``)

Before this commit the helpers only ran ``can_patient`` (human RBAC).
An agent token sitting under a broadly-privileged user could therefore
mutate events for fascicoli outside its ``agent_patient_ids`` scope.
The fix adds ``enforce_agent_patient_scope(request, patient.id)`` to
both inline checks and the FSM helper.

Stub-only: route functions called directly. No HTTP, no DB.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

import bvphoenix.api.clinical_events as ce_module
from bvphoenix.api.clinical_events import (
    _load_event_for_transition,
    create_clinical_event,
)

_OWNER = uuid.uuid4()
_PATIENT_IN_SCOPE = uuid.uuid4()
_PATIENT_OUT_OF_SCOPE = uuid.uuid4()


@dataclass
class _StubPatient:
    id: uuid.UUID
    managed_by_subject_id: uuid.UUID = field(default_factory=lambda: _OWNER)
    display_name: str = "Test Patient"


@dataclass
class _StubEvent:
    id: uuid.UUID
    patient_id: uuid.UUID
    event_status: str = "planned"
    kind: str = "outpatient_visit"
    etag: uuid.UUID = field(default_factory=uuid.uuid4)


@dataclass
class _StubUser:
    subject_id: uuid.UUID = field(default_factory=lambda: _OWNER)
    is_admin: bool = False


class _StubScalar:
    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar_one_or_none(self) -> Any:
        return self._value


class _StubSession:
    """Returns the canned objects in the order the handlers expect:

    * ``_load_event_with_imaging`` issues SELECT(ClinicalEvent) +
      SELECT(ImagingStudy) — the latter returns None.
    * The handler then SELECT(Patient).
    """

    def __init__(
        self,
        *,
        event: _StubEvent | None,
        patient: _StubPatient | None,
    ) -> None:
        self._event = event
        self._patient = patient
        self._call = 0

    async def execute(self, _stmt: Any) -> Any:
        self._call += 1
        if self._call == 1:
            return _StubScalar(self._event)
        if self._call == 2:
            # imaging study lookup — always None for these tests
            return _StubScalar(None)
        # 3rd call (and beyond): patient lookup
        return _StubScalar(self._patient)


class _StubSessionForCreate:
    """``create_clinical_event`` first SELECT is on Patient (the event
    doesn't exist yet)."""

    def __init__(self, patient: _StubPatient | None) -> None:
        self._patient = patient

    async def execute(self, _stmt: Any) -> Any:
        return _StubScalar(self._patient)


class _Audit:
    async def log(self, **_: Any) -> None:
        return None


def _request(*, is_agent: bool, allowed: set[uuid.UUID] | None) -> SimpleNamespace:
    state_kwargs: dict[str, Any] = {"is_agent": is_agent}
    if allowed is not None:
        state_kwargs["agent_patient_ids"] = allowed
    return SimpleNamespace(state=SimpleNamespace(**state_kwargs))


@pytest.fixture(autouse=True)
def _patch_can_patient(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _allow(*_a: Any, **_kw: Any) -> bool:
        return True

    monkeypatch.setattr(ce_module, "can_patient", _allow)


# --------------------------------------------------------------------- #
# _load_event_for_transition (used by 5 FSM endpoints)                  #
# --------------------------------------------------------------------- #


async def test_transition_helper_refuses_agent_outside_patient_scope() -> None:
    ev = _StubEvent(id=uuid.uuid4(), patient_id=_PATIENT_OUT_OF_SCOPE)
    patient = _StubPatient(id=_PATIENT_OUT_OF_SCOPE)
    request = _request(is_agent=True, allowed={_PATIENT_IN_SCOPE})
    db = _StubSession(event=ev, patient=patient)

    with pytest.raises(HTTPException) as exc_info:
        await _load_event_for_transition(
            db,  # type: ignore[arg-type]
            request=request,  # type: ignore[arg-type]
            event_id=ev.id,
            user=_StubUser(is_admin=True),  # type: ignore[arg-type]
        )
    assert exc_info.value.status_code == 403
    assert "not authorised" in exc_info.value.detail


async def test_transition_helper_allows_agent_in_patient_scope() -> None:
    ev = _StubEvent(id=uuid.uuid4(), patient_id=_PATIENT_IN_SCOPE)
    patient = _StubPatient(id=_PATIENT_IN_SCOPE)
    request = _request(is_agent=True, allowed={_PATIENT_IN_SCOPE})
    db = _StubSession(event=ev, patient=patient)

    out = await _load_event_for_transition(
        db,  # type: ignore[arg-type]
        request=request,  # type: ignore[arg-type]
        event_id=ev.id,
        user=_StubUser(),  # type: ignore[arg-type]
    )
    assert out is ev


async def test_transition_helper_human_caller_skips_agent_gate() -> None:
    ev = _StubEvent(id=uuid.uuid4(), patient_id=_PATIENT_OUT_OF_SCOPE)
    patient = _StubPatient(id=_PATIENT_OUT_OF_SCOPE)
    # is_agent=False -> agent gate is a no-op, can_patient is monkeypatched
    # to allow, so the helper should return the event regardless of patient
    # scope. Pins that the new gate doesn't accidentally affect humans.
    request = _request(is_agent=False, allowed=None)
    db = _StubSession(event=ev, patient=patient)

    out = await _load_event_for_transition(
        db,  # type: ignore[arg-type]
        request=request,  # type: ignore[arg-type]
        event_id=ev.id,
        user=_StubUser(),  # type: ignore[arg-type]
    )
    assert out is ev


async def test_transition_helper_404_on_missing_event_runs_before_agent_gate() -> None:
    """The 404 on missing event must fire before the agent-scope
    refusal, otherwise an agent token could probe event existence via
    a 403-vs-404 oracle (same pattern as study/folder share)."""
    request = _request(is_agent=True, allowed={_PATIENT_IN_SCOPE})
    db = _StubSession(event=None, patient=None)

    with pytest.raises(HTTPException) as exc_info:
        await _load_event_for_transition(
            db,  # type: ignore[arg-type]
            request=request,  # type: ignore[arg-type]
            event_id=uuid.uuid4(),
            user=_StubUser(),  # type: ignore[arg-type]
        )
    assert exc_info.value.status_code == 404


# --------------------------------------------------------------------- #
# create_clinical_event — uses inline can_patient, NOT the helper       #
# --------------------------------------------------------------------- #


async def test_create_clinical_event_refuses_agent_outside_patient_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patient = _StubPatient(id=_PATIENT_OUT_OF_SCOPE)
    request = _request(is_agent=True, allowed={_PATIENT_IN_SCOPE})
    db = _StubSessionForCreate(patient)

    # Block any DB write from reaching its mocks — if the gate fails
    # closed, none of these run.
    monkeypatch.setattr(ce_module, "validate_mentions_or_raise", AsyncMock())
    monkeypatch.setattr(ce_module, "ensure_planned_phase", AsyncMock())

    body = ce_module.ClinicalEventCreateIn(
        patient_id=_PATIENT_OUT_OF_SCOPE,
        kind="outpatient_visit",
        title="visit",
    )

    with pytest.raises(HTTPException) as exc_info:
        await create_clinical_event(
            body=body,
            request=request,  # type: ignore[arg-type]
            user=_StubUser(is_admin=True),  # type: ignore[arg-type]
            db=db,  # type: ignore[arg-type]
            audit=_Audit(),  # type: ignore[arg-type]
        )
    assert exc_info.value.status_code == 403
    assert "not authorised" in exc_info.value.detail
