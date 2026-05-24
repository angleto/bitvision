"""Agent-token hardening for ``POST /folders/{id}/share-link``.

Sibling of ``test_share_agent_scope.py`` but tailored to the *folder*
share-link endpoint, whose gate model differs from the study one:

* Existence and ownership are conflated into a single **404**
  ("folder not found") so a non-owner cannot distinguish "folder
  exists but not yours" from "folder doesn't exist". Studies, by
  contrast, return 403 on non-owner.
* The agent capability check requires ``patient:read`` on top of
  the patient-id membership (``enforce_agent_patient_scope(request,
  folder.patient_id, scope="patient:read")``). An agent token with
  only ``patients:read`` written in the catalog still passes through
  the legacy alias (``patient:read`` → ``patients:read``).
* The agent gate fires **after** the owner 404, so an out-of-scope
  agent on a folder it does not own sees 404, not 403. This is the
  anti-enumeration property and the test surface pins it.

Stub-only tests: route function called directly, no HTTP, no DB.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

import bvphoenix.api.sharing as sharing_module
from bvphoenix.api.sharing import (
    ShareCreateIn,
    ShareTarget,
    create_folder_share_link,
)

_OWNER_SUBJECT_ID = uuid.uuid4()
_NON_OWNER_SUBJECT_ID = uuid.uuid4()
_PATIENT_IN_SCOPE = uuid.uuid4()
_PATIENT_OUT_OF_SCOPE = uuid.uuid4()


@dataclass
class _StubFolder:
    id: uuid.UUID
    patient_id: uuid.UUID | None
    owner_subject_id: uuid.UUID = field(default_factory=lambda: _OWNER_SUBJECT_ID)
    name: str = "test-folder"


@dataclass
class _StubPatient:
    id: uuid.UUID
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
    """Stubs only the first DB call ``_load_owned_folder`` issues:
    ``select(Folder).where(id == :folder_id)``. The failure paths
    raise here, so no further queries are exercised. The happy-path
    test monkeypatches ``_resolve_folder_scope`` /
    ``resolve_deidentify_default`` to bypass the rest of the DB."""

    def __init__(self, folder: _StubFolder | None) -> None:
        self._folder = folder

    async def execute(self, _stmt: Any) -> Any:
        return _StubScalar(self._folder)


class _Audit:
    async def log(self, **_: Any) -> None:
        return None


def _request(
    *,
    is_agent: bool,
    allowed: set[uuid.UUID] | None,
    agent_scope: list[str] | None = None,
) -> SimpleNamespace:
    """Bare ``Request`` stand-in. ``agent_scope`` defaults to a list
    containing ``patient:read`` for happy-path tests so the capability
    check inside ``enforce_agent_patient_scope`` passes."""
    state_kwargs: dict[str, Any] = {"is_agent": is_agent}
    if allowed is not None:
        state_kwargs["agent_patient_ids"] = allowed
    if agent_scope is not None:
        state_kwargs["agent_scope"] = agent_scope
    return SimpleNamespace(state=SimpleNamespace(**state_kwargs))


def _body() -> ShareCreateIn:
    return ShareCreateIn(
        access_level="viewer",
        target=ShareTarget(kind="link_public"),
        mode="claim",
    )


# --------------------------------------------------------------------- #
# Anti-enumeration: non-owner always sees 404 (regardless of agent vs   #
# human) so the agent-gate cannot serve as an existence oracle.         #
# --------------------------------------------------------------------- #


async def test_non_owner_human_sees_404_not_403() -> None:
    folder = _StubFolder(id=uuid.uuid4(), patient_id=_PATIENT_IN_SCOPE)
    request = _request(is_agent=False, allowed=None)
    db = _StubSession(folder)
    non_owner = _StubUser(subject_id=_NON_OWNER_SUBJECT_ID, is_admin=False)

    with pytest.raises(HTTPException) as exc_info:
        await create_folder_share_link(
            request=request,  # type: ignore[arg-type]
            folder_id=folder.id,
            body=_body(),
            db=db,  # type: ignore[arg-type]
            user=non_owner,  # type: ignore[arg-type]
            audit=_Audit(),  # type: ignore[arg-type]
            dry_run=True,
        )

    assert exc_info.value.status_code == 404
    assert "folder not found" in exc_info.value.detail


async def test_non_owner_agent_in_patient_scope_still_sees_404() -> None:
    """The ownership check is independent of the agent token's patient
    scope. An agent that holds ``patient:read`` on the right patient
    but whose user is not the folder owner gets 404 — the same shape
    a missing folder produces."""
    folder = _StubFolder(id=uuid.uuid4(), patient_id=_PATIENT_IN_SCOPE)
    request = _request(
        is_agent=True,
        allowed={_PATIENT_IN_SCOPE},
        agent_scope=["patient:read"],
    )
    db = _StubSession(folder)
    non_owner = _StubUser(subject_id=_NON_OWNER_SUBJECT_ID, is_admin=False)

    with pytest.raises(HTTPException) as exc_info:
        await create_folder_share_link(
            request=request,  # type: ignore[arg-type]
            folder_id=folder.id,
            body=_body(),
            db=db,  # type: ignore[arg-type]
            user=non_owner,  # type: ignore[arg-type]
            audit=_Audit(),  # type: ignore[arg-type]
            dry_run=True,
        )
    assert exc_info.value.status_code == 404


async def test_missing_folder_returns_404() -> None:
    request = _request(is_agent=True, allowed={_PATIENT_IN_SCOPE})
    db = _StubSession(None)  # folder lookup returns nothing

    with pytest.raises(HTTPException) as exc_info:
        await create_folder_share_link(
            request=request,  # type: ignore[arg-type]
            folder_id=uuid.uuid4(),
            body=_body(),
            db=db,  # type: ignore[arg-type]
            user=_StubUser(),  # type: ignore[arg-type]
            audit=_Audit(),  # type: ignore[arg-type]
            dry_run=True,
        )
    assert exc_info.value.status_code == 404


# --------------------------------------------------------------------- #
# Agent patient-scope refusal — applies only after the owner gate       #
# clears, i.e. only owners can learn about patient-scope outcomes.      #
# --------------------------------------------------------------------- #


async def test_owner_agent_with_patient_out_of_scope_gets_403() -> None:
    folder = _StubFolder(id=uuid.uuid4(), patient_id=_PATIENT_OUT_OF_SCOPE)
    request = _request(
        is_agent=True,
        allowed={_PATIENT_IN_SCOPE},
        agent_scope=["patient:read"],
    )
    db = _StubSession(folder)

    with pytest.raises(HTTPException) as exc_info:
        await create_folder_share_link(
            request=request,  # type: ignore[arg-type]
            folder_id=folder.id,
            body=_body(),
            db=db,  # type: ignore[arg-type]
            user=_StubUser(),  # type: ignore[arg-type]
            audit=_Audit(),  # type: ignore[arg-type]
            dry_run=True,
        )
    assert exc_info.value.status_code == 403
    assert "not authorised" in exc_info.value.detail


async def test_owner_agent_with_empty_patient_set_fails_closed() -> None:
    folder = _StubFolder(id=uuid.uuid4(), patient_id=_PATIENT_IN_SCOPE)
    request = _request(is_agent=True, allowed=set(), agent_scope=["patient:read"])
    db = _StubSession(folder)

    with pytest.raises(HTTPException) as exc_info:
        await create_folder_share_link(
            request=request,  # type: ignore[arg-type]
            folder_id=folder.id,
            body=_body(),
            db=db,  # type: ignore[arg-type]
            user=_StubUser(),  # type: ignore[arg-type]
            audit=_Audit(),  # type: ignore[arg-type]
            dry_run=True,
        )
    assert exc_info.value.status_code == 403


async def test_owner_agent_missing_patient_set_fails_closed() -> None:
    folder = _StubFolder(id=uuid.uuid4(), patient_id=_PATIENT_IN_SCOPE)
    # is_agent=True with no agent_patient_ids on state.
    request = SimpleNamespace(state=SimpleNamespace(is_agent=True))
    db = _StubSession(folder)

    with pytest.raises(HTTPException) as exc_info:
        await create_folder_share_link(
            request=request,  # type: ignore[arg-type]
            folder_id=folder.id,
            body=_body(),
            db=db,  # type: ignore[arg-type]
            user=_StubUser(),  # type: ignore[arg-type]
            audit=_Audit(),  # type: ignore[arg-type]
            dry_run=True,
        )
    assert exc_info.value.status_code == 403


async def test_owner_agent_missing_patient_read_capability_is_refused() -> None:
    """Patient is in agent_patient_ids but agent_scope omits
    ``patient:read`` (and its canonical alias). The capability check
    inside ``enforce_agent_patient_scope`` must raise even though the
    patient-membership check passed."""
    folder = _StubFolder(id=uuid.uuid4(), patient_id=_PATIENT_IN_SCOPE)
    request = _request(
        is_agent=True,
        allowed={_PATIENT_IN_SCOPE},
        # Deliberately not 'patient:read' nor 'patients:read'.
        agent_scope=["sharing:write"],
    )
    db = _StubSession(folder)

    with pytest.raises(HTTPException) as exc_info:
        await create_folder_share_link(
            request=request,  # type: ignore[arg-type]
            folder_id=folder.id,
            body=_body(),
            db=db,  # type: ignore[arg-type]
            user=_StubUser(),  # type: ignore[arg-type]
            audit=_Audit(),  # type: ignore[arg-type]
            dry_run=True,
        )
    assert exc_info.value.status_code == 403
    assert "missing required scope" in exc_info.value.detail


# --------------------------------------------------------------------- #
# Admin bypass on the 404 still subject to the agent gate when running  #
# under an agent token (admin can't shortcut the patient-scope gate).   #
# --------------------------------------------------------------------- #


async def test_admin_bypasses_owner_404_but_still_subject_to_agent_scope() -> None:
    """``is_admin=True`` bypasses the ownership 404, but if the call
    arrives under an agent token whose patient set excludes the
    folder's patient, the gate still fires. This pins the rule
    ``admin > owner gate, admin < patient-scope gate`` so a
    compromised admin's agent token cannot mint shares for fascicoli
    outside its consented scope."""
    folder = _StubFolder(
        id=uuid.uuid4(),
        patient_id=_PATIENT_OUT_OF_SCOPE,
        # NOT owned by the admin
        owner_subject_id=_OWNER_SUBJECT_ID,
    )
    request = _request(
        is_agent=True,
        allowed={_PATIENT_IN_SCOPE},
        agent_scope=["patient:read"],
    )
    db = _StubSession(folder)
    admin_non_owner = _StubUser(subject_id=_NON_OWNER_SUBJECT_ID, is_admin=True)

    with pytest.raises(HTTPException) as exc_info:
        await create_folder_share_link(
            request=request,  # type: ignore[arg-type]
            folder_id=folder.id,
            body=_body(),
            db=db,  # type: ignore[arg-type]
            user=admin_non_owner,  # type: ignore[arg-type]
            audit=_Audit(),  # type: ignore[arg-type]
            dry_run=True,
        )
    assert exc_info.value.status_code == 403
    assert "not authorised" in exc_info.value.detail


# --------------------------------------------------------------------- #
# Happy path: agent owner + in scope + correct capability reaches the   #
# dry-run synthetic. ``_resolve_folder_scope`` /                         #
# ``resolve_deidentify_default`` are monkeypatched away because the     #
# focus of this test is the GATE, not the body of the endpoint.         #
# --------------------------------------------------------------------- #


async def test_agent_owner_in_scope_with_capability_reaches_dry_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    folder = _StubFolder(id=uuid.uuid4(), patient_id=_PATIENT_IN_SCOPE)
    patient = _StubPatient(id=_PATIENT_IN_SCOPE)

    async def _stub_resolve_folder_scope(_db: Any, _folder_id: uuid.UUID, **_: Any):
        return patient, set(), set()

    async def _stub_resolve_deidentify_default(*_args: Any, **_kw: Any) -> bool:
        return True

    monkeypatch.setattr(
        "bvphoenix.api.patient_export._resolve_folder_scope",
        _stub_resolve_folder_scope,
    )
    monkeypatch.setattr(
        sharing_module, "resolve_deidentify_default", _stub_resolve_deidentify_default
    )

    request = _request(
        is_agent=True,
        allowed={_PATIENT_IN_SCOPE},
        agent_scope=["patient:read"],
    )
    db = _StubSession(folder)

    out = await create_folder_share_link(
        request=request,  # type: ignore[arg-type]
        folder_id=folder.id,
        body=_body(),
        db=db,  # type: ignore[arg-type]
        user=_StubUser(subject_id=_OWNER_SUBJECT_ID),  # type: ignore[arg-type]
        audit=_Audit(),  # type: ignore[arg-type]
        dry_run=True,
    )

    assert out.id == "dry-run"
    assert out.token == "dry-run"
    assert out.resource_kind == "folder"
    assert out.resource_id == str(folder.id)
