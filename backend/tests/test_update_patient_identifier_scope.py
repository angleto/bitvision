"""Tests for the ``update_patient`` PATCH identifier-scope split.

Background: an LLM via MCP called ``update_patient`` with ``tax_id`` and
``external_id`` set; the server happily returned 200 but the values
were silently dropped — they are not columns on ``patients`` (v3
moved them into the ``external_identifiers`` JSONB array) and the
naive ``setattr`` loop just no-op'd. The fix:

* tax_id / external_id in the body are extracted before the column
  loop and routed to a JSONB upsert on ``external_identifiers``;
* the upsert requires the granular ``patients:identify`` scope (the
  legacy singular ``patient:identify`` is honoured for backward
  compat) — broader ``patient:write`` is no longer enough;
* a missing scope returns 403 instead of silently dropping the
  identifiers.

These tests cover the scope helper directly + assert the endpoint
source contains the new gate so a future refactor can't regress to
the silent-drop behaviour.
"""

from __future__ import annotations

import inspect

import pytest
from fastapi import HTTPException

from bvphoenix.api.patients import update_patient
from bvphoenix.auth.deps import enforce_agent_scope


class _Req:
    def __init__(self, *, is_agent: bool, scopes: list[str] | None) -> None:
        self.state = type("S", (), {})()
        self.state.is_agent = is_agent
        self.state.agent_scope = scopes


def test_canonical_patients_identify_unlocks_identifier_writes() -> None:
    """An agent token carrying the canonical ``patients:identify`` scope
    must be allowed to PATCH ``tax_id`` / ``external_id``."""
    enforce_agent_scope(
        _Req(is_agent=True, scopes=["patient:write", "patients:identify"]),
        "patients:identify",
        "patient:identify",
    )


def test_legacy_patient_identify_alias_still_unlocks() -> None:
    """Tokens minted under the pre-Sprint 6 catalog used the singular
    ``patient:identify`` form. The endpoint accepts either spelling so
    legacy assistants don't need a forced re-grant."""
    enforce_agent_scope(
        _Req(is_agent=True, scopes=["patient:write", "patient:identify"]),
        "patients:identify",
        "patient:identify",
    )


def test_patient_write_alone_is_403_for_identifier_writes() -> None:
    """Without ``patients:identify`` (or its alias), the dedicated gate
    refuses the call. Previously the field was silently dropped — the
    new behaviour surfaces a 403 so the agent can react explicitly."""
    with pytest.raises(HTTPException) as exc:
        enforce_agent_scope(
            _Req(is_agent=True, scopes=["patient:write"]),
            "patients:identify",
            "patient:identify",
        )
    assert exc.value.status_code == 403
    detail = exc.value.detail
    assert "patients:identify" in detail
    assert "patient:identify" in detail


def test_endpoint_routes_legacy_identifiers_through_dedicated_scope() -> None:
    """Lock in the wiring: the PATCH handler pops tax_id / external_id
    out of the column-update map and gates them with the identifier
    scope. Source-level smoke is enough — the runtime path is exercised
    by the integration suite once Postgres is available.
    """
    src = inspect.getsource(update_patient)
    # The legacy fields must be intercepted, not setattr'd onto the row.
    assert "legacy_identifier_updates" in src
    assert 'updates.pop("tax_id")' in src
    assert 'updates.pop("external_id")' in src
    # The dedicated scope check fires before the JSONB upsert applies.
    assert "patients:identify" in src
    assert "patient:identify" in src
