"""Unit tests for the new ``enforce_agent_scope`` helper.

The helper is the OR-gated counterpart of ``enforce_agent_patient_scope``:
no-op for human users, scope check for agent tokens.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from bvphoenix.auth.deps import enforce_agent_scope


class _Req:
    def __init__(self, *, is_agent: bool, scopes: list[str] | None) -> None:
        self.state = type("S", (), {})()
        self.state.is_agent = is_agent
        self.state.agent_scope = scopes


def test_human_user_is_noop() -> None:
    enforce_agent_scope(_Req(is_agent=False, scopes=None), "tags:write")
    enforce_agent_scope(_Req(is_agent=False, scopes=[]), "tags:write")


def test_agent_with_scope_passes() -> None:
    enforce_agent_scope(
        _Req(is_agent=True, scopes=["tags:write", "patient:read"]),
        "tags:write",
    )


def test_agent_without_scope_403() -> None:
    with pytest.raises(HTTPException) as exc:
        enforce_agent_scope(
            _Req(is_agent=True, scopes=["patient:read"]),
            "tags:write",
        )
    assert exc.value.status_code == 403
    assert "tags:write" in exc.value.detail


def test_agent_with_any_of_or_set_passes() -> None:
    enforce_agent_scope(
        _Req(is_agent=True, scopes=["studies:write_metadata"]),
        "studies:write_metadata",
        "patient:full",
    )


def test_agent_with_none_of_or_set_403() -> None:
    with pytest.raises(HTTPException) as exc:
        enforce_agent_scope(
            _Req(is_agent=True, scopes=["consultation:read"]),
            "studies:write_metadata",
            "patient:full",
        )
    assert exc.value.status_code == 403
    detail = exc.value.detail
    assert "studies:write_metadata" in detail
    assert "patient:full" in detail


def test_agent_with_no_scope_claim_403() -> None:
    with pytest.raises(HTTPException):
        enforce_agent_scope(_Req(is_agent=True, scopes=None), "tags:write")
