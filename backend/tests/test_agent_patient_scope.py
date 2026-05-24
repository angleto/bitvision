"""Agent token patient-scope enforcement — ``enforce_agent_patient_scope``.

Unit tests on the helper in isolation. The model used to live on
``AgentToken.patient_id`` (one token = one patient); after the AI
assistants refactor, the allowed patient set is loaded once per
request from the ``agent_assistant_patients`` join table and cached
on ``request.state.agent_patient_ids``. The helper now does an O(1)
membership check against that set.

Integration coverage (fascicolo endpoints wiring the call) is implicit
in the existing test_consultations / test_patients suites; here we
exercise the rule tree directly so regressions surface fast.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from bvphoenix.auth.deps import enforce_agent_patient_scope


def _make_request(*, is_agent: bool, allowed: set[uuid.UUID] | None = None) -> SimpleNamespace:
    """Build a minimal stand-in for ``fastapi.Request``.

    Only ``request.state`` is read by the helper, so a bare
    ``SimpleNamespace`` with that attribute is enough — the real Request
    class would drag in scope / receive / send which are irrelevant here.
    """
    state_kwargs: dict = {"is_agent": is_agent}
    if allowed is not None:
        state_kwargs["agent_patient_ids"] = allowed
    return SimpleNamespace(state=SimpleNamespace(**state_kwargs))


def test_noop_for_human_caller() -> None:
    # Human session: is_agent is False (or missing); no matter what
    # patient_id is requested, the helper must not raise.
    req = _make_request(is_agent=False)
    enforce_agent_patient_scope(req, uuid.uuid4())


def test_noop_when_patient_id_is_none() -> None:
    # Endpoints that do not resolve to a specific patient (e.g. list
    # views) pass ``None``; we cannot enforce what we do not know.
    req = _make_request(is_agent=True, allowed={uuid.uuid4()})
    enforce_agent_patient_scope(req, None)


def test_allows_listed_patient() -> None:
    pid = uuid.uuid4()
    req = _make_request(is_agent=True, allowed={pid, uuid.uuid4()})
    enforce_agent_patient_scope(req, pid)  # should not raise


def test_refuses_unlisted_patient() -> None:
    req = _make_request(is_agent=True, allowed={uuid.uuid4()})
    with pytest.raises(HTTPException) as exc:
        enforce_agent_patient_scope(req, uuid.uuid4())
    assert exc.value.status_code == 403
    assert "not authorised" in exc.value.detail


def test_fails_closed_when_set_missing() -> None:
    """If the agent branch of ``_resolve_user`` ran but the allowed-
    patient set never got populated (legacy / misconfigured),
    we must fail closed rather than open."""
    req = SimpleNamespace(state=SimpleNamespace(is_agent=True))
    with pytest.raises(HTTPException) as exc:
        enforce_agent_patient_scope(req, uuid.uuid4())
    assert exc.value.status_code == 403


def test_fails_closed_when_set_empty() -> None:
    """An assistant with zero shared patients can't reach any record."""
    req = _make_request(is_agent=True, allowed=set())
    with pytest.raises(HTTPException) as exc:
        enforce_agent_patient_scope(req, uuid.uuid4())
    assert exc.value.status_code == 403
