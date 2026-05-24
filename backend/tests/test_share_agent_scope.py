"""Agent-token hardening for ``POST /studies/{id}/share``.

Pins the security invariants the MCP ``create_study_share_link`` tool
relies on:

* ``enforce_agent_patient_scope`` runs BEFORE the owner check. An
  agent token whose ``agent_patient_ids`` does not include the
  study's patient must be refused with 403 *and* the error wording
  must NOT reveal whether the caller is the study owner — otherwise a
  leaked token could enumerate ownership across patients via a
  403-vs-different-403 oracle.
* The owner check still fires for agent tokens whose patient IS in
  scope but whose underlying user does not own the study.
* The happy path (agent in scope + owner user) reaches the dry-run
  branch and returns the synthetic ``ShareLinkOut``.

These tests call the route function ``create_share_link`` directly
with bare stubs for ``request`` / ``db`` / ``user`` / ``audit`` to
avoid the asyncpg event-loop-sharing flakes documented in
``memory/backend_test_isolation_pre_existing.md``. No HTTP server, no
real DB.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from bvphoenix.api.sharing import (
    ShareCreateIn,
    ShareTarget,
    create_share_link,
)

_OWNER_SUBJECT_ID = uuid.uuid4()
_NON_OWNER_SUBJECT_ID = uuid.uuid4()
_PATIENT_IN_SCOPE = uuid.uuid4()
_PATIENT_OUT_OF_SCOPE = uuid.uuid4()


@dataclass
class _StubStudy:
    id: uuid.UUID
    patient_id: uuid.UUID
    owner_subject_id: uuid.UUID = field(default_factory=lambda: _OWNER_SUBJECT_ID)


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
    """Routes the single ``select(ImagingStudy).where(id == :study_id)``
    issued by ``create_share_link`` up to the agent scope check.

    The agent-scope failure path raises BEFORE the next DB call, and
    the owner-check failure path raises BEFORE the grantee resolution
    DB call. The happy path lands on the ``dry_run`` branch we already
    cover in ``test_share_dry_run.py``, so a single ``execute``
    implementation is enough for all three scenarios.
    """

    def __init__(self, study: _StubStudy | None) -> None:
        self._study = study

    async def execute(self, _stmt: Any) -> Any:
        return _StubScalar(self._study)


class _Audit:
    async def log(self, **_: Any) -> None:
        return None


def _request(*, is_agent: bool, allowed: set[uuid.UUID] | None) -> SimpleNamespace:
    """Bare stand-in for ``fastapi.Request`` carrying only the state the
    handler reads. Mirrors the pattern in ``test_agent_patient_scope.py``."""
    state_kwargs: dict[str, Any] = {"is_agent": is_agent}
    if allowed is not None:
        state_kwargs["agent_patient_ids"] = allowed
    return SimpleNamespace(state=SimpleNamespace(**state_kwargs))


def _body() -> ShareCreateIn:
    return ShareCreateIn(
        access_level="viewer",
        target=ShareTarget(kind="link_public"),
        mode="claim",
    )


# --------------------------------------------------------------------- #
# Agent-scope refusal (the security invariant the MCP layer relies on)  #
# --------------------------------------------------------------------- #


async def test_agent_token_outside_patient_scope_is_refused_403() -> None:
    """A token bound to patient X must not mint a share for a study
    whose patient is Y. The 403 carries the "not authorised for this
    patient" wording, distinct from the owner-check 403, so a leaked
    token cannot enumerate ownership via the response."""
    study = _StubStudy(id=uuid.uuid4(), patient_id=_PATIENT_OUT_OF_SCOPE)
    request = _request(is_agent=True, allowed={_PATIENT_IN_SCOPE})
    db = _StubSession(study)

    with pytest.raises(HTTPException) as exc_info:
        await create_share_link(
            request=request,  # type: ignore[arg-type]
            study_id=study.id,
            body=_body(),
            db=db,  # type: ignore[arg-type]
            user=_StubUser(),  # type: ignore[arg-type]
            audit=_Audit(),  # type: ignore[arg-type]
            dry_run=True,
        )

    assert exc_info.value.status_code == 403
    assert "not authorised" in exc_info.value.detail
    # Crucial negative assertion: the agent-scope failure must NOT
    # leak the ownership reason. Otherwise an out-of-scope agent token
    # could enumerate which patient-study pairs are owned by its
    # underlying user.
    assert "only the owner" not in str(exc_info.value.detail).lower()


async def test_agent_token_with_empty_scope_fails_closed() -> None:
    """An assistant linked to zero patients cannot mint any share —
    the empty-set case must NOT be interpreted as ``allow all``."""
    study = _StubStudy(id=uuid.uuid4(), patient_id=_PATIENT_IN_SCOPE)
    request = _request(is_agent=True, allowed=set())
    db = _StubSession(study)

    with pytest.raises(HTTPException) as exc_info:
        await create_share_link(
            request=request,  # type: ignore[arg-type]
            study_id=study.id,
            body=_body(),
            db=db,  # type: ignore[arg-type]
            user=_StubUser(),  # type: ignore[arg-type]
            audit=_Audit(),  # type: ignore[arg-type]
            dry_run=True,
        )
    assert exc_info.value.status_code == 403


async def test_agent_token_missing_patient_set_fails_closed() -> None:
    """If the agent branch ran but ``agent_patient_ids`` never landed
    on ``request.state`` (legacy bearer / middleware misorder), the
    sharing endpoint must still refuse rather than open up."""
    study = _StubStudy(id=uuid.uuid4(), patient_id=_PATIENT_IN_SCOPE)
    # is_agent=True but no allowed set on state — same shape as
    # ``test_agent_patient_scope.test_fails_closed_when_set_missing``.
    request = SimpleNamespace(state=SimpleNamespace(is_agent=True))
    db = _StubSession(study)

    with pytest.raises(HTTPException) as exc_info:
        await create_share_link(
            request=request,  # type: ignore[arg-type]
            study_id=study.id,
            body=_body(),
            db=db,  # type: ignore[arg-type]
            user=_StubUser(),  # type: ignore[arg-type]
            audit=_Audit(),  # type: ignore[arg-type]
            dry_run=True,
        )
    assert exc_info.value.status_code == 403


# --------------------------------------------------------------------- #
# Owner check applies independently of agent vs human                   #
# --------------------------------------------------------------------- #


async def test_agent_in_scope_but_non_owner_user_is_refused_with_owner_error() -> None:
    """When the patient IS in scope, the agent passes the patient-scope
    gate and falls through to the owner check. A non-owner user (even
    one whose agent token authorises the patient) must be refused with
    the owner-specific 403."""
    study = _StubStudy(
        id=uuid.uuid4(),
        patient_id=_PATIENT_IN_SCOPE,
        owner_subject_id=_OWNER_SUBJECT_ID,
    )
    request = _request(is_agent=True, allowed={_PATIENT_IN_SCOPE})
    db = _StubSession(study)
    non_owner = _StubUser(subject_id=_NON_OWNER_SUBJECT_ID, is_admin=False)

    with pytest.raises(HTTPException) as exc_info:
        await create_share_link(
            request=request,  # type: ignore[arg-type]
            study_id=study.id,
            body=_body(),
            db=db,  # type: ignore[arg-type]
            user=non_owner,  # type: ignore[arg-type]
            audit=_Audit(),  # type: ignore[arg-type]
            dry_run=True,
        )

    assert exc_info.value.status_code == 403
    assert "only the owner" in exc_info.value.detail


async def test_human_caller_skips_agent_scope_check_entirely() -> None:
    """``is_agent=False`` short-circuits ``enforce_agent_patient_scope``
    so the human path is unaffected by the agent-only invariant. The
    owner check still fires — pinning that a human non-owner is
    refused the same way an agent non-owner is."""
    study = _StubStudy(
        id=uuid.uuid4(),
        # Patient deliberately set to ``_PATIENT_OUT_OF_SCOPE``: the
        # ``agent_patient_ids`` set is irrelevant for humans, the
        # helper returns immediately.
        patient_id=_PATIENT_OUT_OF_SCOPE,
        owner_subject_id=_OWNER_SUBJECT_ID,
    )
    request = _request(is_agent=False, allowed=None)
    db = _StubSession(study)
    non_owner_human = _StubUser(subject_id=_NON_OWNER_SUBJECT_ID, is_admin=False)

    with pytest.raises(HTTPException) as exc_info:
        await create_share_link(
            request=request,  # type: ignore[arg-type]
            study_id=study.id,
            body=_body(),
            db=db,  # type: ignore[arg-type]
            user=non_owner_human,  # type: ignore[arg-type]
            audit=_Audit(),  # type: ignore[arg-type]
            dry_run=True,
        )

    assert exc_info.value.status_code == 403
    assert "only the owner" in exc_info.value.detail


# --------------------------------------------------------------------- #
# Happy path: agent in scope + owner user reaches the dry-run synthetic #
# --------------------------------------------------------------------- #


async def test_agent_in_scope_owner_user_reaches_dry_run_branch() -> None:
    """The dry_run branch is the safest one to exercise here: it
    validates RBAC + patient scope + owner check and bails before any
    DB write or grantee resolution that would need a richer stub. A
    real mint integration is covered separately in
    ``test_share_hardening.py`` + ``test_share_download_security.py``."""
    study = _StubStudy(
        id=uuid.uuid4(),
        patient_id=_PATIENT_IN_SCOPE,
        owner_subject_id=_OWNER_SUBJECT_ID,
    )
    request = _request(is_agent=True, allowed={_PATIENT_IN_SCOPE})
    db = _StubSession(study)

    out = await create_share_link(
        request=request,  # type: ignore[arg-type]
        study_id=study.id,
        body=_body(),
        db=db,  # type: ignore[arg-type]
        user=_StubUser(subject_id=_OWNER_SUBJECT_ID),  # type: ignore[arg-type]
        audit=_Audit(),  # type: ignore[arg-type]
        dry_run=True,
    )

    assert out.id == "dry-run"
    assert out.token == "dry-run"
    assert out.resource_id == str(study.id)
    assert out.resource_kind == "study"


async def test_admin_user_bypasses_owner_check_for_agent_in_scope() -> None:
    """Admins (``is_admin=True``) skip the owner check by design. The
    agent-scope check still applies before this; admin bypass only
    kicks in once the patient gate has cleared."""
    study = _StubStudy(
        id=uuid.uuid4(),
        patient_id=_PATIENT_IN_SCOPE,
        owner_subject_id=_OWNER_SUBJECT_ID,
    )
    request = _request(is_agent=True, allowed={_PATIENT_IN_SCOPE})
    db = _StubSession(study)
    admin_non_owner = _StubUser(subject_id=_NON_OWNER_SUBJECT_ID, is_admin=True)

    out = await create_share_link(
        request=request,  # type: ignore[arg-type]
        study_id=study.id,
        body=_body(),
        db=db,  # type: ignore[arg-type]
        user=admin_non_owner,  # type: ignore[arg-type]
        audit=_Audit(),  # type: ignore[arg-type]
        dry_run=True,
    )
    assert out.id == "dry-run"


async def test_missing_study_404_runs_before_agent_scope_check() -> None:
    """A 404 on the study lookup must fire BEFORE the agent-scope
    check so a leaked token cannot probe which study UUIDs exist
    across patients via a 403-vs-404 oracle. (404 means ``not in this
    namespace`` everywhere in the API.)"""
    request = _request(is_agent=True, allowed={_PATIENT_IN_SCOPE})
    db = _StubSession(None)  # study not found

    with pytest.raises(HTTPException) as exc_info:
        await create_share_link(
            request=request,  # type: ignore[arg-type]
            study_id=uuid.uuid4(),
            body=_body(),
            db=db,  # type: ignore[arg-type]
            user=_StubUser(),  # type: ignore[arg-type]
            audit=_Audit(),  # type: ignore[arg-type]
            dry_run=True,
        )
    assert exc_info.value.status_code == 404
