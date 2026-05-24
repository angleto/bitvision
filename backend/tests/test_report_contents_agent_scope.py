"""Agent-token hardening for report-contents endpoints.

Every write on a ReportContent goes through ``_load_event_and_check``
(directly, or via ``_load_rc_and_check`` which delegates). Before
this commit those helpers ran only ``can_patient`` — an agent token
sitting under a broadly-privileged user could mutate ReportContent
rows on fascicoli outside its ``agent_patient_ids``. The fix routes
the request to both helpers and runs ``enforce_agent_patient_scope``
after ``can_patient``.

Affected endpoints (all use one of the two helpers):
* ``extract_report_content`` / ``create_report_content``
* ``update_report_content``
* ``cite_source`` / ``link_document``
* ``sign_report_content`` (HUMAN-only by separate gate, but the
  patient-scope rule still applies for parity)
* ``endorse_report_content`` / ``reject_report_content`` /
  ``supersede_report_content``

Stub-only tests: helpers called directly. No HTTP, no DB.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

import bvphoenix.api.report_contents as rc_module
from bvphoenix.api.report_contents import (
    _load_event_and_check,
    _load_rc_and_check,
)

_OWNER = uuid.uuid4()
_PATIENT_IN_SCOPE = uuid.uuid4()
_PATIENT_OUT_OF_SCOPE = uuid.uuid4()


@dataclass
class _StubPatient:
    id: uuid.UUID
    display_name: str = "Test Patient"


@dataclass
class _StubEvent:
    id: uuid.UUID
    patient_id: uuid.UUID


@dataclass
class _StubRC:
    id: uuid.UUID
    clinical_event_id: uuid.UUID


@dataclass
class _StubUser:
    subject_id: uuid.UUID = field(default_factory=lambda: _OWNER)
    is_admin: bool = False


class _StubScalar:
    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar_one_or_none(self) -> Any:
        return self._value


class _StubSessionEvent:
    """Returns the event then the patient for ``_load_event_and_check``."""

    def __init__(self, event: _StubEvent | None, patient: _StubPatient | None) -> None:
        self._event = event
        self._patient = patient
        self._n = 0

    async def execute(self, _stmt: Any) -> Any:
        self._n += 1
        return _StubScalar(self._event if self._n == 1 else self._patient)


class _StubSessionRc:
    """Returns rc, then event, then patient — the chain used by
    ``_load_rc_and_check``."""

    def __init__(
        self,
        rc: _StubRC | None,
        event: _StubEvent | None,
        patient: _StubPatient | None,
    ) -> None:
        self._rc = rc
        self._event = event
        self._patient = patient
        self._n = 0

    async def execute(self, _stmt: Any) -> Any:
        self._n += 1
        if self._n == 1:
            return _StubScalar(self._rc)
        if self._n == 2:
            return _StubScalar(self._event)
        return _StubScalar(self._patient)


def _request(*, is_agent: bool, allowed: set[uuid.UUID] | None) -> SimpleNamespace:
    state_kwargs: dict[str, Any] = {"is_agent": is_agent}
    if allowed is not None:
        state_kwargs["agent_patient_ids"] = allowed
    return SimpleNamespace(state=SimpleNamespace(**state_kwargs))


@pytest.fixture(autouse=True)
def _patch_can_patient(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _allow(*_a: Any, **_kw: Any) -> bool:
        return True

    monkeypatch.setattr(rc_module, "can_patient", _allow)


# --------------------------------------------------------------------- #
# _load_event_and_check                                                 #
# --------------------------------------------------------------------- #


async def test_event_helper_refuses_agent_outside_patient_scope() -> None:
    ev = _StubEvent(id=uuid.uuid4(), patient_id=_PATIENT_OUT_OF_SCOPE)
    patient = _StubPatient(id=_PATIENT_OUT_OF_SCOPE)
    request = _request(is_agent=True, allowed={_PATIENT_IN_SCOPE})
    db = _StubSessionEvent(ev, patient)

    with pytest.raises(HTTPException) as exc_info:
        await _load_event_and_check(
            db,  # type: ignore[arg-type]
            ev.id,
            _StubUser(is_admin=True),  # type: ignore[arg-type]
            "write:report",
            request=request,  # type: ignore[arg-type]
        )
    assert exc_info.value.status_code == 403
    assert "not authorised" in exc_info.value.detail


async def test_event_helper_allows_agent_in_patient_scope() -> None:
    ev = _StubEvent(id=uuid.uuid4(), patient_id=_PATIENT_IN_SCOPE)
    patient = _StubPatient(id=_PATIENT_IN_SCOPE)
    request = _request(is_agent=True, allowed={_PATIENT_IN_SCOPE})
    db = _StubSessionEvent(ev, patient)

    out = await _load_event_and_check(
        db,  # type: ignore[arg-type]
        ev.id,
        _StubUser(),  # type: ignore[arg-type]
        "write:report",
        request=request,  # type: ignore[arg-type]
    )
    assert out is ev


async def test_event_helper_404_runs_before_agent_gate() -> None:
    """A missing event must surface 404 before the 403, so a leaked
    agent token cannot probe event existence via the gate oracle."""
    request = _request(is_agent=True, allowed={_PATIENT_IN_SCOPE})
    db = _StubSessionEvent(None, None)

    with pytest.raises(HTTPException) as exc_info:
        await _load_event_and_check(
            db,  # type: ignore[arg-type]
            uuid.uuid4(),
            _StubUser(),  # type: ignore[arg-type]
            "read:metadata",
            request=request,  # type: ignore[arg-type]
        )
    assert exc_info.value.status_code == 404


# --------------------------------------------------------------------- #
# _load_rc_and_check — delegates to _load_event_and_check               #
# --------------------------------------------------------------------- #


async def test_rc_helper_refuses_agent_outside_patient_scope() -> None:
    rc = _StubRC(id=uuid.uuid4(), clinical_event_id=uuid.uuid4())
    ev = _StubEvent(id=rc.clinical_event_id, patient_id=_PATIENT_OUT_OF_SCOPE)
    patient = _StubPatient(id=_PATIENT_OUT_OF_SCOPE)
    request = _request(is_agent=True, allowed={_PATIENT_IN_SCOPE})
    db = _StubSessionRc(rc, ev, patient)

    with pytest.raises(HTTPException) as exc_info:
        await _load_rc_and_check(
            db,  # type: ignore[arg-type]
            rc.id,
            _StubUser(is_admin=True),  # type: ignore[arg-type]
            "write:report",
            request=request,  # type: ignore[arg-type]
        )
    assert exc_info.value.status_code == 403


async def test_rc_helper_missing_rc_returns_404() -> None:
    request = _request(is_agent=True, allowed={_PATIENT_IN_SCOPE})
    db = _StubSessionRc(None, None, None)
    with pytest.raises(HTTPException) as exc_info:
        await _load_rc_and_check(
            db,  # type: ignore[arg-type]
            uuid.uuid4(),
            _StubUser(),  # type: ignore[arg-type]
            "read:metadata",
            request=request,  # type: ignore[arg-type]
        )
    assert exc_info.value.status_code == 404


async def test_rc_helper_happy_path_returns_pair() -> None:
    rc = _StubRC(id=uuid.uuid4(), clinical_event_id=uuid.uuid4())
    ev = _StubEvent(id=rc.clinical_event_id, patient_id=_PATIENT_IN_SCOPE)
    patient = _StubPatient(id=_PATIENT_IN_SCOPE)
    request = _request(is_agent=True, allowed={_PATIENT_IN_SCOPE})
    db = _StubSessionRc(rc, ev, patient)

    out_rc, out_ev = await _load_rc_and_check(
        db,  # type: ignore[arg-type]
        rc.id,
        _StubUser(),  # type: ignore[arg-type]
        "write:report",
        request=request,  # type: ignore[arg-type]
    )
    assert out_rc is rc
    assert out_ev is ev
