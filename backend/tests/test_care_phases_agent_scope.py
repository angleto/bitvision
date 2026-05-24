"""Agent-token hardening on the care-phases endpoints.

Pins two security bugs discovered during the MCP write-tool audit and
fixed in the same commit:

1. **Cross-patient bypass (A)**: ``_ensure_patient_access`` was missing
   the ``enforce_agent_patient_scope(request, patient_id)`` call. An
   agent token whose underlying user holds broad ``can_patient``
   permissions could create / update / delete / assign care phases for
   patients OUTSIDE the assistant's ``agent_patient_ids`` set —
   violating the cross-patient invariant
   (memoria ``cross_patient_links_forbidden``).
2. **Provenance forgery (B)**: ``create_phase`` / ``patch_phase`` /
   ``assign_event`` hardcoded ``author_kind="human"`` even for agent
   callers, falsifying the audit trail. The "AI" badge on the GUI and
   the provenance log must reflect the actual author
   (memoria ``feedback_ai_provenance_must_be_visible``).

Stub-only tests: route function called directly, no HTTP, no DB.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

import bvphoenix.api.care_phases as care_phases_module
from bvphoenix.api.care_phases import (
    _author_kind,
    assign_event,
    create_phase,
    patch_phase,
)
from bvphoenix.services.care_phase_schemas import (
    AssignPhaseIn,
    CarePhaseCreateIn,
    CarePhaseUpdateIn,
)

_OWNER_SUBJECT_ID = uuid.uuid4()
_PATIENT_IN_SCOPE = uuid.uuid4()
_PATIENT_OUT_OF_SCOPE = uuid.uuid4()


@dataclass
class _StubPatient:
    id: uuid.UUID
    managed_by_subject_id: uuid.UUID = field(default_factory=lambda: _OWNER_SUBJECT_ID)
    display_name: str = "Test Patient"


@dataclass
class _StubUser:
    subject_id: uuid.UUID = field(default_factory=lambda: _OWNER_SUBJECT_ID)
    is_admin: bool = False


class _StubScalar:
    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar_one_or_none(self) -> Any:
        return self._value


class _StubSession:
    """The patient SELECT is the only DB call ``_ensure_patient_access``
    issues before the gate decision. All hardening assertions raise here,
    so a single ``execute`` impl covers the failure paths."""

    def __init__(self, patient: _StubPatient | None) -> None:
        self._patient = patient

    async def execute(self, _stmt: Any) -> Any:
        return _StubScalar(self._patient)


class _Audit:
    async def log(self, **_: Any) -> None:
        return None


def _request(
    *,
    is_agent: bool,
    allowed: set[uuid.UUID] | None,
) -> SimpleNamespace:
    state_kwargs: dict[str, Any] = {"is_agent": is_agent}
    if allowed is not None:
        state_kwargs["agent_patient_ids"] = allowed
    return SimpleNamespace(state=SimpleNamespace(**state_kwargs))


@pytest.fixture(autouse=True)
def _patch_can_patient(monkeypatch: pytest.MonkeyPatch) -> None:
    """``can_patient`` reads org_membership / share_grants from the DB.
    For the failure-path tests we want the human RBAC to grant access
    so the agent scope is the only thing standing between the caller
    and the resource — that's the gap we are pinning."""

    async def _allow(*_a: Any, **_kw: Any) -> bool:
        return True

    monkeypatch.setattr(care_phases_module, "can_patient", _allow)


# --------------------------------------------------------------------- #
# _author_kind helper                                                   #
# --------------------------------------------------------------------- #


def test_author_kind_returns_agent_for_agent_request() -> None:
    req = _request(is_agent=True, allowed={_PATIENT_IN_SCOPE})
    assert _author_kind(req) == "agent"  # type: ignore[arg-type]


def test_author_kind_returns_human_for_non_agent_request() -> None:
    req = _request(is_agent=False, allowed=None)
    assert _author_kind(req) == "human"  # type: ignore[arg-type]


def test_author_kind_returns_human_when_state_missing_is_agent_flag() -> None:
    """Legacy code paths that never set ``is_agent`` must default to
    ``human`` so a missing state attribute can't silently elevate
    provenance from human to agent."""
    req = SimpleNamespace(state=SimpleNamespace())
    assert _author_kind(req) == "human"  # type: ignore[arg-type]


# --------------------------------------------------------------------- #
# create_phase — agent patient scope + provenance                       #
# --------------------------------------------------------------------- #


async def test_create_phase_refuses_agent_outside_patient_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the ``enforce_agent_patient_scope`` call inside
    ``_ensure_patient_access`` (the bug fixed in this commit) an agent
    whose user is admin could mint phases across fascicoli."""
    patient = _StubPatient(id=_PATIENT_OUT_OF_SCOPE)
    request = _request(is_agent=True, allowed={_PATIENT_IN_SCOPE})
    db = _StubSession(patient)

    # If the gate fails closed, the service call below must never run.
    svc_create = AsyncMock()
    monkeypatch.setattr(care_phases_module.svc, "create_phase", svc_create)

    with pytest.raises(HTTPException) as exc_info:
        await create_phase(
            patient_id=patient.id,
            data=CarePhaseCreateIn(slug="diagnosis", name="Diagnosis", kind="diagnosis"),
            user=_StubUser(is_admin=True),  # type: ignore[arg-type]
            db=db,  # type: ignore[arg-type]
            _audit=_Audit(),  # type: ignore[arg-type]
            request=request,  # type: ignore[arg-type]
            response=SimpleNamespace(headers={}),  # type: ignore[arg-type]
        )
    assert exc_info.value.status_code == 403
    assert "not authorised" in exc_info.value.detail
    svc_create.assert_not_awaited()


async def test_create_phase_records_author_kind_agent_for_agent_caller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Provenance bug fix: ``author_kind`` must follow
    ``request.state.is_agent`` rather than being hardcoded to
    ``"human"`` (memoria ``feedback_ai_provenance_must_be_visible``)."""
    patient = _StubPatient(id=_PATIENT_IN_SCOPE)
    request = _request(is_agent=True, allowed={_PATIENT_IN_SCOPE})
    db = _StubSession(patient)

    svc_create = AsyncMock(
        return_value=SimpleNamespace(
            etag=uuid.uuid4(),
            model_dump=lambda: {},
        )
    )
    monkeypatch.setattr(care_phases_module.svc, "create_phase", svc_create)

    await create_phase(
        patient_id=patient.id,
        data=CarePhaseCreateIn(slug="diagnosis", name="Diagnosis", kind="diagnosis"),
        user=_StubUser(),  # type: ignore[arg-type]
        db=db,  # type: ignore[arg-type]
        _audit=_Audit(),  # type: ignore[arg-type]
        request=request,  # type: ignore[arg-type]
        response=SimpleNamespace(headers={}),  # type: ignore[arg-type]
    )

    svc_create.assert_awaited_once()
    kwargs = svc_create.await_args.kwargs
    assert kwargs["author_kind"] == "agent"


async def test_create_phase_records_author_kind_human_for_human_caller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patient = _StubPatient(id=_PATIENT_IN_SCOPE)
    request = _request(is_agent=False, allowed=None)
    db = _StubSession(patient)

    svc_create = AsyncMock(return_value=SimpleNamespace(etag=uuid.uuid4()))
    monkeypatch.setattr(care_phases_module.svc, "create_phase", svc_create)

    await create_phase(
        patient_id=patient.id,
        data=CarePhaseCreateIn(slug="diagnosis", name="Diagnosis", kind="diagnosis"),
        user=_StubUser(),  # type: ignore[arg-type]
        db=db,  # type: ignore[arg-type]
        _audit=_Audit(),  # type: ignore[arg-type]
        request=request,  # type: ignore[arg-type]
        response=SimpleNamespace(headers={}),  # type: ignore[arg-type]
    )
    assert svc_create.await_args.kwargs["author_kind"] == "human"


# --------------------------------------------------------------------- #
# patch_phase + assign_event — same invariants, smoke checks            #
# --------------------------------------------------------------------- #


async def test_patch_phase_refuses_agent_outside_patient_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patient = _StubPatient(id=_PATIENT_OUT_OF_SCOPE)
    request = _request(is_agent=True, allowed={_PATIENT_IN_SCOPE})
    db = _StubSession(patient)

    svc_update = AsyncMock()
    monkeypatch.setattr(care_phases_module.svc, "update_phase", svc_update)

    with pytest.raises(HTTPException) as exc_info:
        await patch_phase(
            patient_id=patient.id,
            phase_id=uuid.uuid4(),
            data=CarePhaseUpdateIn(name="x"),
            user=_StubUser(is_admin=True),  # type: ignore[arg-type]
            db=db,  # type: ignore[arg-type]
            _audit=_Audit(),  # type: ignore[arg-type]
            request=request,  # type: ignore[arg-type]
            response=SimpleNamespace(headers={}),  # type: ignore[arg-type]
            if_match=None,
        )
    assert exc_info.value.status_code == 403
    svc_update.assert_not_awaited()


async def test_patch_phase_passes_author_kind_agent_to_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patient = _StubPatient(id=_PATIENT_IN_SCOPE)
    request = _request(is_agent=True, allowed={_PATIENT_IN_SCOPE})
    db = _StubSession(patient)

    svc_update = AsyncMock(return_value=SimpleNamespace(etag=uuid.uuid4()))
    monkeypatch.setattr(care_phases_module.svc, "update_phase", svc_update)

    await patch_phase(
        patient_id=patient.id,
        phase_id=uuid.uuid4(),
        data=CarePhaseUpdateIn(name="x"),
        user=_StubUser(),  # type: ignore[arg-type]
        db=db,  # type: ignore[arg-type]
        _audit=_Audit(),  # type: ignore[arg-type]
        request=request,  # type: ignore[arg-type]
        response=SimpleNamespace(headers={}),  # type: ignore[arg-type]
        if_match=None,
    )
    assert svc_update.await_args.kwargs["author_kind"] == "agent"


async def test_assign_event_refuses_agent_outside_patient_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patient = _StubPatient(id=_PATIENT_OUT_OF_SCOPE)
    request = _request(is_agent=True, allowed={_PATIENT_IN_SCOPE})
    db = _StubSession(patient)

    svc_assign = AsyncMock()
    monkeypatch.setattr(care_phases_module.svc, "assign_event", svc_assign)

    with pytest.raises(HTTPException) as exc_info:
        await assign_event(
            patient_id=patient.id,
            phase_id=uuid.uuid4(),
            event_id=uuid.uuid4(),
            data=AssignPhaseIn(confidence=0.9),
            user=_StubUser(is_admin=True),  # type: ignore[arg-type]
            db=db,  # type: ignore[arg-type]
            _audit=_Audit(),  # type: ignore[arg-type]
            request=request,  # type: ignore[arg-type]
        )
    assert exc_info.value.status_code == 403
    svc_assign.assert_not_awaited()


async def test_assign_event_passes_author_kind_agent_to_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patient = _StubPatient(id=_PATIENT_IN_SCOPE)
    request = _request(is_agent=True, allowed={_PATIENT_IN_SCOPE})
    db = _StubSession(patient)

    svc_assign = AsyncMock()
    monkeypatch.setattr(care_phases_module.svc, "assign_event", svc_assign)

    await assign_event(
        patient_id=patient.id,
        phase_id=uuid.uuid4(),
        event_id=uuid.uuid4(),
        data=AssignPhaseIn(confidence=0.9),
        user=_StubUser(),  # type: ignore[arg-type]
        db=db,  # type: ignore[arg-type]
        _audit=_Audit(),  # type: ignore[arg-type]
        request=request,  # type: ignore[arg-type]
    )
    assert svc_assign.await_args.kwargs["author_kind"] == "agent"
