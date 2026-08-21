"""The MCP surface must describe the same PATCH contract the API enforces.

``memoria: feedback_mcp_must_be_gui_superset`` — every action available
in the UI has an MCP equivalent. The corollary this file guards is the
narrower one: where a tool mirrors a server-side allow-list, the two
must not drift. ``update_clinical_event`` advertises a ``patch`` object
whose properties are a copy of ``_UPDATABLE_FIELDS``; if the API drops a
field (the temporal ones just moved out) and the tool schema keeps it,
an agent sends a field the server answers with 422 and the failure
surfaces to the user as an unexplained refusal.

DB-free and import-free on the MCP side: the tool module is parsed with
:mod:`ast` rather than imported, so the backend test suite does not take
a dependency on the ``mcp`` package being installed in this venv.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from bvphoenix.api.clinical_events import _AMEND_ONLY_FIELDS, _UPDATABLE_FIELDS

_MCP_TOOLS = Path(__file__).resolve().parents[2] / "mcp/src/bvmcp/tools/clinical_events.py"


def _tool_calls(tree: ast.AST) -> dict[str, ast.Call]:
    """Every ``Tool(name="...", ...)`` constructor, keyed by tool name."""
    out: dict[str, ast.Call] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if name != "Tool":
            continue
        for kw in node.keywords:
            if kw.arg == "name" and isinstance(kw.value, ast.Constant):
                out[kw.value.value] = node
    return out


def _keyword(call: ast.Call, arg: str) -> ast.expr | None:
    for kw in call.keywords:
        if kw.arg == arg:
            return kw.value
    return None


def _dict_get(node: ast.expr | None, key: str) -> ast.expr | None:
    """Fetch a literal-keyed entry out of an ``ast.Dict`` literal."""
    if not isinstance(node, ast.Dict):
        return None
    for k, v in zip(node.keys, node.values, strict=False):
        if isinstance(k, ast.Constant) and k.value == key:
            return v
    return None


def _dict_keys(node: ast.expr | None) -> set[str]:
    if not isinstance(node, ast.Dict):
        return set()
    return {k.value for k in node.keys if isinstance(k, ast.Constant)}


@pytest.fixture(scope="module")
def tools() -> dict[str, ast.Call]:
    assert _MCP_TOOLS.exists(), f"MCP tool module not found at {_MCP_TOOLS}"
    return _tool_calls(ast.parse(_MCP_TOOLS.read_text(encoding="utf-8"), filename=str(_MCP_TOOLS)))


def test_update_tool_patch_matches_updatable_fields(tools) -> None:
    """Set-equality, not containment: a field missing from the tool is
    an agent capability silently dropped, and an extra one is an agent
    request the API will refuse."""
    update = tools.get("update_clinical_event")
    assert update is not None, "update_clinical_event tool missing from the MCP surface"

    schema = _keyword(update, "inputSchema")
    patch = _dict_get(_dict_get(schema, "properties"), "patch")
    advertised = _dict_keys(_dict_get(patch, "properties"))
    assert advertised, "update_clinical_event.patch declares no properties"

    assert advertised == set(_UPDATABLE_FIELDS), (
        "MCP update_clinical_event.patch drifted from "
        "bvphoenix.api.clinical_events._UPDATABLE_FIELDS; "
        f"only in MCP: {sorted(advertised - set(_UPDATABLE_FIELDS))}, "
        f"only in API: {sorted(set(_UPDATABLE_FIELDS) - advertised)}"
    )


def test_update_tool_does_not_advertise_temporal_fields(tools) -> None:
    """Explicit restatement of the bug's contract: an agent must not be
    invited to send a temporal field on the metadata patch, because the
    DB trigger owns ``event_date`` and would revert it."""
    update = tools["update_clinical_event"]
    patch = _dict_get(_dict_get(_keyword(update, "inputSchema"), "properties"), "patch")
    advertised = _dict_keys(_dict_get(patch, "properties"))
    leaked = advertised & set(_AMEND_ONLY_FIELDS)
    assert not leaked, (
        f"temporal fields still advertised on update_clinical_event: {sorted(leaked)}"
    )


def test_amend_event_time_tool_exists(tools) -> None:
    """The replacement path has to exist, or the 422 from PATCH points
    an agent at a tool it cannot call."""
    amend = tools.get("amend_event_time")
    assert amend is not None, (
        "amend_event_time tool missing: PATCH refuses temporal fields with "
        "{'code': 'use_amend_time'} and MCP callers would have no way to correct a date"
    )
    props = _dict_keys(_dict_get(_keyword(amend, "inputSchema"), "properties"))
    # The four anchors plus the date-only escape hatch, the concurrency
    # token, the idempotency token, the audit reason and the preview
    # switch: the full contract of the endpoint, nothing more.
    #
    # Set-equality in BOTH directions, like the ``update_clinical_event``
    # assertion above. A subset check only catches half the drift: a
    # property the endpoint does not accept is just as broken as a
    # missing one, because the agent sends it, the server ignores or
    # refuses it, and the user is told the amendment did nothing.
    expected = {
        "event_id",
        "etag",
        "idempotency_key",
        "dry_run",
        "planned_start_at",
        "planned_end_at",
        "actual_start_at",
        "actual_end_at",
        "event_date",
        "timezone",
        "reason",
    }
    assert props == expected, (
        "amend_event_time drifted from POST /api/clinical-events/{id}/amend-time; "
        f"only in MCP: {sorted(props - expected)}, only in the endpoint: "
        f"{sorted(expected - props)}"
    )


def _type_names(node: ast.expr | None) -> set[str]:
    """The ``type`` of a JSON-schema property, as a set.

    JSON Schema allows either a bare string or a list of them, and the
    difference IS the contract here: ``"string"`` forbids ``null``,
    ``["string", "null"]`` allows it.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, ast.List):
        return {e.value for e in node.elts if isinstance(e, ast.Constant)}
    return set()


def test_amend_event_time_starts_move_and_ends_clear(tools) -> None:
    """``anchor_not_clearable``, expressed in the schema.

    The endpoint refuses a ``null`` START anchor whatever the status
    (the value the row's date is derived from cannot be removed) and
    accepts a ``null`` END anchor ("we do not know when it finished" is
    a legitimate state). A schema that types all four the same way
    invites the agent into a guaranteed 422 on two of them, or hides a
    legal clear on the other two.
    """
    schema = _keyword(tools["amend_event_time"], "inputSchema")
    props = _dict_get(schema, "properties")
    for field in ("planned_start_at", "actual_start_at"):
        types = _type_names(_dict_get(_dict_get(props, field), "type"))
        assert "null" not in types, (
            f"{field} advertises a nullable type, but clearing it is refused "
            "with 422 anchor_not_clearable"
        )
    for field in ("planned_end_at", "actual_end_at"):
        types = _type_names(_dict_get(_dict_get(props, field), "type"))
        assert "null" in types, (
            f"{field} is clearable through the endpoint; the schema must accept null"
        )
