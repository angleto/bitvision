"""Drift + shape tests for the AI assistant scope catalog.

The catalog is now derived from ``bvmcp.scopes.SCOPE_CATALOG`` (single
source of truth) so the UI exposes every scope the MCP gate enforces.
``_ALLOWED_PERMISSIONS`` additionally accepts pre-Sprint 6 legacy
spellings so existing assistant rows keep validating on PATCH; the
runtime gate maps them via ``bvmcp.auth._SCOPE_ALIASES``.
"""

from __future__ import annotations

from bvmcp.scopes import SCOPE_CATALOG

from bvphoenix.api.ai_assistants import (
    _ALLOWED_PERMISSIONS,
    _GRANTABLE_SCOPES,
    _LEGACY_SCOPES,
    _SCOPE_CATALOG,
)


def test_catalog_keys_are_a_subset_of_allowed_permissions() -> None:
    """Every scope the UI offers as a checkbox must validate at the
    PATCH boundary. The reverse inclusion is intentionally NOT
    asserted: ``_ALLOWED_PERMISSIONS`` carries legacy keys that the UI
    no longer surfaces but old rows still legitimately use."""
    catalog_keys = {entry["key"] for entry in _SCOPE_CATALOG}
    assert catalog_keys <= _ALLOWED_PERMISSIONS


def test_grantable_scopes_match_canonical_mcp_catalog() -> None:
    """``_GRANTABLE_SCOPES`` MUST equal the MCP catalog minus the
    structurally human-only entries — the test fails fast if a new MCP
    scope is added but the backend forgot to surface it (the original
    drift that broke ``documents:write`` for agents)."""
    expected = {s.id for s in SCOPE_CATALOG if not s.human_only}
    assert expected == _GRANTABLE_SCOPES


def test_legacy_scopes_only_validate_do_not_appear_in_catalog() -> None:
    """Legacy keys are accepted by the validator (so existing rows
    don't break on PATCH) but are not advertised in the UI catalog.
    A new assistant created today should never receive a legacy key."""
    catalog_keys = {entry["key"] for entry in _SCOPE_CATALOG}
    assert _LEGACY_SCOPES.isdisjoint(catalog_keys)
    assert _LEGACY_SCOPES <= _ALLOWED_PERMISSIONS


def test_every_entry_has_required_fields() -> None:
    required = {"key", "category", "label", "description", "dangerous", "enforced"}
    for entry in _SCOPE_CATALOG:
        missing = required - set(entry.keys())
        assert not missing, f"entry {entry.get('key')!r} missing fields: {missing}"


def test_all_scopes_are_enforced() -> None:
    """Every scope in the catalog must be backed by a backend gate.
    UI theatre is forbidden: any scope advertised to the operator
    must have a real refusal path on agent-token requests."""
    not_enforced = [e["key"] for e in _SCOPE_CATALOG if not e["enforced"]]
    assert not not_enforced, f"these catalog scopes are not enforced anywhere: {not_enforced}"


def test_categories_are_known() -> None:
    allowed = {"read", "write", "danger"}
    for entry in _SCOPE_CATALOG:
        assert entry["category"] in allowed, (
            f"entry {entry['key']!r} has unknown category {entry['category']!r}"
        )


def test_danger_implies_dangerous_flag() -> None:
    """``category=danger`` rows must set ``dangerous=True`` so the UI
    cannot accidentally render a danger scope without the warning."""
    for entry in _SCOPE_CATALOG:
        if entry["category"] == "danger":
            assert entry["dangerous"] is True, (
                f"{entry['key']!r} category=danger but dangerous=False"
            )


def test_lookup_external_is_dangerous() -> None:
    """Cross-patient lookup is the canonical sensitive scope (PHI
    leakage risk). The marker has to flow through the ``sensitive``
    flag on ``ScopeDef`` → ``dangerous=True`` in the UI; if the wiring
    breaks we want the drift detected here, not in production."""
    lookup = next(e for e in _SCOPE_CATALOG if e["key"] == "lookup:external")
    assert lookup["dangerous"] is True
    assert lookup["category"] == "danger"


def test_human_only_scope_is_filtered_out() -> None:
    """``synthesis:sign`` is structurally ungrantable (the backend
    rejects agent tokens regardless of grant). The UI must NOT render
    a checkbox for it — that would mislead the operator into thinking
    the scope means anything when granted."""
    catalog_keys = {entry["key"] for entry in _SCOPE_CATALOG}
    assert "synthesis:sign" not in catalog_keys


def test_folders_scopes_are_present() -> None:
    """Folder navigation + reshape are required by product spec: the
    LLM must be able to read and reorganise the fascicolo tree.
    Regression guard against future refactors."""
    catalog_keys = {entry["key"] for entry in _SCOPE_CATALOG}
    assert "folders:read" in catalog_keys
    assert "folders:write" in catalog_keys


def test_documents_write_is_grantable() -> None:
    """The original bug: ``documents:write`` was missing from the
    backend grant catalog, so even ``abilita tutti`` produced tokens
    that returned 403 on every document write. Lock it in."""
    catalog_keys = {entry["key"] for entry in _SCOPE_CATALOG}
    assert "documents:write" in catalog_keys
    assert "documents:write" in _ALLOWED_PERMISSIONS
