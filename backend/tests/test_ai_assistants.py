"""Pure-function tests for the AI assistant authorisation gate.

The end-to-end CRUD path (create / share / rotate) is covered by the
integration suite once it runs against a live API stack — these
focused tests pin the smaller invariants that are easy to break in
a refactor and don't require a DB:

* Permission whitelist — unknown verbs must 400 *before* a row is
  written, so a typo can't silently mint a token that the auth layer
  later refuses (or worse, accepts under a wider scope).
* Patient scope enforcement — once an agent token is resolved, the
  per-request set on ``request.state.agent_patient_ids`` is the
  single source of truth for which patients the token may touch.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from bvphoenix.api.ai_assistants import _validate_permissions
from bvphoenix.auth.deps import enforce_agent_patient_scope


def test_validate_permissions_accepts_whitelist() -> None:
    # All four verbs in the documented vocabulary must pass.
    _validate_permissions(
        ["patient:read", "patient:images", "consultation:read", "consultation:write"]
    )
    # Empty set is also fine — there is no "non-empty" rule at this
    # layer; the Pydantic model handles min_length on the route input.
    _validate_permissions([])


def test_validate_permissions_rejects_unknown_verb() -> None:
    with pytest.raises(HTTPException) as exc:
        _validate_permissions(["patient:exfiltrate"])
    assert exc.value.status_code == 400
    assert "patient:exfiltrate" in str(exc.value.detail)


def _fake_request(state: dict) -> SimpleNamespace:
    """Tiny stand-in for ``starlette.requests.Request`` — only
    ``request.state`` is read by the gate."""
    return SimpleNamespace(state=SimpleNamespace(**state))


def test_enforce_agent_scope_no_op_for_human_user() -> None:
    request = _fake_request({"is_agent": False})
    # Should not raise even with a fully-populated patient set.
    enforce_agent_patient_scope(request, uuid.uuid4())


def test_enforce_agent_scope_no_op_when_no_patient_target() -> None:
    request = _fake_request({"is_agent": True, "agent_patient_ids": {uuid.uuid4()}})
    enforce_agent_patient_scope(request, None)


def test_enforce_agent_scope_allows_listed_patient() -> None:
    pid = uuid.uuid4()
    request = _fake_request({"is_agent": True, "agent_patient_ids": {pid, uuid.uuid4()}})
    enforce_agent_patient_scope(request, pid)  # must not raise


def test_enforce_agent_scope_blocks_unlisted_patient() -> None:
    request = _fake_request({"is_agent": True, "agent_patient_ids": {uuid.uuid4()}})
    with pytest.raises(HTTPException) as exc:
        enforce_agent_patient_scope(request, uuid.uuid4())
    assert exc.value.status_code == 403


def test_enforce_agent_scope_blocks_when_set_missing() -> None:
    """A token resolved without an allowed-patients set (legacy /
    misconfigured) must fail closed, not open."""
    request = _fake_request({"is_agent": True})
    with pytest.raises(HTTPException) as exc:
        enforce_agent_patient_scope(request, uuid.uuid4())
    assert exc.value.status_code == 403
