"""Agent-token hardening for external-identifier writes.

``POST /patients/{id}/external-identifiers`` and the matching DELETE
both ran only ``can_patient`` before this commit. An agent token
sitting under a broadly-privileged user could attach / detach external
identifiers (codice fiscale, NHS number, …) on fascicoli outside its
``agent_patient_ids`` — silently rewiring the cross-system patient
identity layer. Fix adds ``enforce_agent_patient_scope`` after
``can_patient``.

Stub-only tests: route functions called directly, no HTTP/DB.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

import bvphoenix.api.external_identifiers as ext_module
from bvphoenix.api.external_identifiers import (
    ExternalIdentifier,
    add_external_identifier,
    remove_external_identifier,
)

_OWNER = uuid.uuid4()
_PATIENT_IN_SCOPE = uuid.uuid4()
_PATIENT_OUT_OF_SCOPE = uuid.uuid4()


@dataclass
class _StubPatient:
    id: uuid.UUID
    external_identifiers: list[dict] = field(default_factory=list)


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
    def __init__(self, patient: _StubPatient | None) -> None:
        self._patient = patient

    async def execute(self, _stmt: Any) -> Any:
        return _StubScalar(self._patient)

    async def commit(self) -> None:
        return None

    async def refresh(self, _obj: Any) -> None:
        return None


class _Audit:
    async def log(self, **_: Any) -> None:
        return None


def _request(*, is_agent: bool, allowed: set[uuid.UUID] | None) -> SimpleNamespace:
    state_kwargs: dict[str, Any] = {"is_agent": is_agent}
    if allowed is not None:
        state_kwargs["agent_patient_ids"] = allowed
    return SimpleNamespace(state=SimpleNamespace(**state_kwargs))


@pytest.fixture(autouse=True)
def _patch_can_patient_and_provenance(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _allow(*_a: Any, **_kw: Any) -> bool:
        return True

    monkeypatch.setattr(ext_module, "can_patient", _allow)
    # record_provenance commits its own DB ops; bypass.
    monkeypatch.setattr(ext_module, "record_provenance", lambda *a, **kw: None)


# --------------------------------------------------------------------- #
# add_external_identifier                                               #
# --------------------------------------------------------------------- #


async def test_add_external_identifier_refuses_agent_outside_patient_scope() -> None:
    patient = _StubPatient(id=_PATIENT_OUT_OF_SCOPE)
    request = _request(is_agent=True, allowed={_PATIENT_IN_SCOPE})
    db = _StubSession(patient)
    body = ExternalIdentifier(
        system="https://www.example.org/cf", value="ABCDEF80A01H501Z", type="codice_fiscale"
    )

    with pytest.raises(HTTPException) as exc_info:
        await add_external_identifier(
            patient_id=patient.id,
            body=body,
            request=request,  # type: ignore[arg-type]
            user=_StubUser(is_admin=True),  # type: ignore[arg-type]
            db=db,  # type: ignore[arg-type]
            audit=_Audit(),  # type: ignore[arg-type]
        )
    assert exc_info.value.status_code == 403
    # Pin that no identifier was written (the patient stub list stays empty).
    assert patient.external_identifiers == []


async def test_add_external_identifier_allows_agent_in_patient_scope() -> None:
    patient = _StubPatient(id=_PATIENT_IN_SCOPE)
    request = _request(is_agent=True, allowed={_PATIENT_IN_SCOPE})
    db = _StubSession(patient)
    body = ExternalIdentifier(
        system="https://www.example.org/cf", value="ABCDEF80A01H501Z", type="codice_fiscale"
    )

    out = await add_external_identifier(
        patient_id=patient.id,
        body=body,
        request=request,  # type: ignore[arg-type]
        user=_StubUser(),  # type: ignore[arg-type]
        db=db,  # type: ignore[arg-type]
        audit=_Audit(),  # type: ignore[arg-type]
    )
    assert any(ident.system == body.system and ident.value == body.value for ident in out)


# --------------------------------------------------------------------- #
# remove_external_identifier                                            #
# --------------------------------------------------------------------- #


async def test_remove_external_identifier_refuses_agent_outside_patient_scope() -> None:
    patient = _StubPatient(
        id=_PATIENT_OUT_OF_SCOPE,
        external_identifiers=[
            {"system": "https://www.example.org/cf", "value": "X", "type": "codice_fiscale"}
        ],
    )
    request = _request(is_agent=True, allowed={_PATIENT_IN_SCOPE})
    db = _StubSession(patient)

    with pytest.raises(HTTPException) as exc_info:
        await remove_external_identifier(
            patient_id=patient.id,
            request=request,  # type: ignore[arg-type]
            user=_StubUser(is_admin=True),  # type: ignore[arg-type]
            db=db,  # type: ignore[arg-type]
            audit=_Audit(),  # type: ignore[arg-type]
            system="https://www.example.org/cf",
            value="X",
        )
    assert exc_info.value.status_code == 403
    # Pin that the identifier survived.
    assert len(patient.external_identifiers) == 1
