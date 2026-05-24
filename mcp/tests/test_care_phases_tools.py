"""Smoke tests for the care_phases MCP tool family.

These tests do not need a live backend — they verify:

* the 13 tool names are registered (12 user-facing tools per the spec
  plus the SVG alias counted separately);
* every tool's ``inputSchema`` requires ``patient_id``;
* the scope mapping (read / propose / write) is correct for each tool;
* a couple of read paths actually hit the documented backend URL when
  invoked through the dispatcher (httpx.MockTransport).
"""

from __future__ import annotations

import json

import httpx

from bvmcp.scopes import scope_for_tool
from bvmcp.server import call_tool, list_tools
from bvmcp.tools import care_phases as care_phases_tools

from .conftest import mock_backend

# 19 tools = 13 of spec section 7.1 + 6 GUI-superset tools added when
# we audited the MCP layer against the GUI (memory:
# feedback_mcp_must_be_gui_superset). The user-facing count is 19
# registered tools.
EXPECTED_TOOL_NAMES = {
    "get_care_timeline",
    "render_care_timeline_svg",
    "get_care_phase",
    "list_care_phase_material",
    "list_care_phase_revisions",
    "propose_care_phases",
    "apply_phase_proposal",
    "create_care_phase",
    "update_care_phase",
    "assign_event_to_phase",
    "unassign_event_from_phase",
    "reorder_care_phases",
    "restore_care_phase_revision",
    # GUI-superset additions (delete, health, list, exports, scopes).
    "list_care_phases",
    "get_care_timeline_health",
    "delete_care_phase",
    "export_care_timeline_ics",
    "export_care_timeline_pdf",
    "get_my_scopes",
}

# ``get_my_scopes`` is the only patient-unscoped tool (it asks "who am
# I and what scopes do I hold") so it is excluded from the
# patient_id-mandatory check below.
NON_PATIENT_SCOPED_TOOLS = {"get_my_scopes"}

READ_TOOLS = {
    "get_care_timeline",
    "render_care_timeline_svg",
    "get_care_phase",
    "list_care_phase_material",
    "list_care_phase_revisions",
    "list_care_phases",
    "get_care_timeline_health",
    "export_care_timeline_ics",
    "export_care_timeline_pdf",
}
PROPOSE_TOOLS = {"propose_care_phases"}
WRITE_TOOLS = {
    "apply_phase_proposal",
    "create_care_phase",
    "update_care_phase",
    "delete_care_phase",
    "assign_event_to_phase",
    "unassign_event_from_phase",
    "reorder_care_phases",
    "restore_care_phase_revision",
}
INTROSPECTION_TOOLS = {"get_my_scopes"}


def test_all_care_phase_tools_registered_in_module() -> None:
    names = {t.name for t in care_phases_tools.TOOLS}
    assert names == EXPECTED_TOOL_NAMES


async def test_all_care_phase_tools_visible_via_list_tools() -> None:
    tools = await list_tools()
    names = {t.name for t in tools}
    missing = EXPECTED_TOOL_NAMES - names
    assert not missing, f"care_phases tools missing from server: {missing}"


def test_every_care_phase_tool_requires_patient_id() -> None:
    for tool in care_phases_tools.TOOLS:
        if tool.name in NON_PATIENT_SCOPED_TOOLS:
            # Caller-introspection tools (e.g. get_my_scopes) describe
            # the calling identity, not a patient — by design they
            # take no patient_id.
            continue
        schema = tool.inputSchema
        assert isinstance(schema, dict), tool.name
        assert schema.get("type") == "object", tool.name
        props = schema.get("properties") or {}
        assert "patient_id" in props, f"{tool.name}: patient_id missing from inputSchema.properties"
        assert props["patient_id"].get("type") == "string", tool.name
        required = schema.get("required") or []
        assert "patient_id" in required, (
            f"{tool.name}: patient_id missing from inputSchema.required "
            "— cross-patient invariant requires it as a mandatory first input"
        )


def test_no_tool_takes_two_patient_ids() -> None:
    """Defence in depth: cross-patient should be unrepresentable."""
    for tool in care_phases_tools.TOOLS:
        props = (tool.inputSchema or {}).get("properties") or {}
        # No second ``*patient*`` key sneaks in.
        patient_keys = [k for k in props if "patient" in k and k != "patient_id"]
        assert not patient_keys, (
            f"{tool.name}: extra patient-scoped key {patient_keys!r} would "
            "allow cross-patient combinations"
        )


def test_scope_mapping_read_tools() -> None:
    for name in READ_TOOLS:
        assert scope_for_tool(name) == "phases:read", name


def test_scope_mapping_propose_tools() -> None:
    for name in PROPOSE_TOOLS:
        assert scope_for_tool(name) == "phases:propose", name


def test_scope_mapping_write_tools() -> None:
    for name in WRITE_TOOLS:
        assert scope_for_tool(name) == "phases:write", name


def test_scope_partition_is_complete_and_disjoint() -> None:
    """Every registered tool maps to exactly one of the four scope groups."""
    union = READ_TOOLS | PROPOSE_TOOLS | WRITE_TOOLS | INTROSPECTION_TOOLS
    assert union == EXPECTED_TOOL_NAMES
    assert READ_TOOLS.isdisjoint(PROPOSE_TOOLS)
    assert READ_TOOLS.isdisjoint(WRITE_TOOLS)
    assert PROPOSE_TOOLS.isdisjoint(WRITE_TOOLS)


# --------------------------------------------------------------------------- #
# Light end-to-end smoke through the dispatcher: confirm a few tools
# hit the documented backend paths. Heavy coverage lives in the
# backend test suite.
# --------------------------------------------------------------------------- #


def _json(payload: dict | list) -> httpx.Response:
    return httpx.Response(200, json=payload)


async def test_get_care_timeline_json_hits_documented_path() -> None:
    pid = "patient-uuid"
    payload = {"phases": [], "events": []}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == f"/api/patients/{pid}/care-timeline"
        assert request.url.params["lang"] == "it"
        assert request.url.params["format"] == "json"
        return _json(payload)

    with mock_backend(handler):
        result = await call_tool("get_care_timeline", {"patient_id": pid})

    assert len(result) == 1
    assert result[0].type == "text"
    assert json.loads(result[0].text) == payload


async def test_get_care_timeline_markdown_groups_events_by_phase() -> None:
    pid = "patient-uuid"
    payload = {
        "phases": [
            {
                "id": "ph1",
                "slug": "diagnosi",
                "name_i18n": {"it": "Diagnosi", "en": "Diagnosis"},
            }
        ],
        "events": [
            {
                "id": "e1",
                "phase_id": "ph1",
                "event_date": "2024-05-01",
                "title": "TC torace",
                "target": {"kind": "study", "id": "s1"},
            },
            {
                "id": "e2",
                "phase_id": None,
                "event_date": "2024-06-01",
                "title": "Visita follow-up",
                "target": {"kind": "consultation", "id": "c2"},
            },
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["format"] == "json"
        return _json(payload)

    with mock_backend(handler):
        result = await call_tool(
            "get_care_timeline",
            {"patient_id": pid, "format": "markdown", "lang": "it"},
        )

    text = result[0].text
    assert "## Diagnosi" in text
    assert "mcp://study/s1" in text
    assert "mcp://consultation/c2" in text
    assert "## Non assegnati" in text


async def test_get_care_phase_passes_include_param() -> None:
    pid, phid = "p1", "ph1"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == f"/api/patients/{pid}/care-phases/{phid}"
        # FastAPI accepts repeated keys; httpx serializes a list to repeats.
        assert request.url.params.get_list("include") == ["events", "material"]
        return _json({"id": phid})

    with mock_backend(handler):
        await call_tool(
            "get_care_phase",
            {"patient_id": pid, "phase_id": phid, "include": ["events", "material"]},
        )


async def test_propose_care_phases_posts_to_propose_endpoint() -> None:
    pid = "p1"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == f"/api/patients/{pid}/care-phases:propose"
        body = json.loads(request.content)
        assert body == {"lang": "it", "dry_run": True}
        return _json({"proposal_id": "prop-1"})

    with mock_backend(handler):
        result = await call_tool("propose_care_phases", {"patient_id": pid})

    assert json.loads(result[0].text)["proposal_id"] == "prop-1"


async def test_apply_phase_proposal_sets_idempotency_header() -> None:
    pid = "p1"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.headers["idempotency-key"] == "key-abc"
        body = json.loads(request.content)
        assert body["proposal_id"] == "prop-1"
        assert body["accept_phases"] == ["diagnosi"]
        assert body["accept_assignments"] == ["a1"]
        return _json({"applied": 1})

    with mock_backend(handler):
        await call_tool(
            "apply_phase_proposal",
            {
                "patient_id": pid,
                "proposal_id": "prop-1",
                "accept_phases": ["diagnosi"],
                "accept_assignments": ["a1"],
                "idempotency_key": "key-abc",
            },
        )


async def test_update_care_phase_sets_if_match_header() -> None:
    pid, phid = "p1", "ph1"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "PATCH"
        # Strong ETag — quoted form
        assert request.headers["if-match"] == '"etag-value"'
        body = json.loads(request.content)
        assert body == {"name": "Nuova fase"}
        return httpx.Response(200, json={"id": phid}, headers={"etag": '"etag-value-2"'})

    with mock_backend(handler):
        await call_tool(
            "update_care_phase",
            {
                "patient_id": pid,
                "phase_id": phid,
                "etag": "etag-value",
                "patch": {"name": "Nuova fase"},
            },
        )


async def test_assign_event_to_phase_resolves_slug_then_puts() -> None:
    pid, slug, eid = "p1", "diagnosi", "e1"
    phid = "ph1"
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "GET" and request.url.path == f"/api/patients/{pid}/care-phases":
            return _json([{"id": phid, "slug": slug}])
        if (
            request.method == "PUT"
            and request.url.path == f"/api/patients/{pid}/care-phases/{phid}/events/{eid}"
        ):
            body = json.loads(request.content)
            assert body == {"confidence": 0.9}
            return _json({"status": "assigned"})
        return httpx.Response(404, json={"detail": "unexpected"})

    with mock_backend(handler):
        await call_tool(
            "assign_event_to_phase",
            {
                "patient_id": pid,
                "phase_slug": slug,
                "event_id": eid,
                "confidence": 0.9,
            },
        )

    assert ("GET", f"/api/patients/{pid}/care-phases") in calls
    assert (
        "PUT",
        f"/api/patients/{pid}/care-phases/{phid}/events/{eid}",
    ) in calls


async def test_unassign_event_reads_event_then_deletes() -> None:
    pid, eid, phid = "p1", "e1", "ph1"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == f"/api/clinical-events/{eid}":
            return _json({"id": eid, "patient_id": pid, "phase_id": phid})
        if (
            request.method == "DELETE"
            and request.url.path == f"/api/patients/{pid}/care-phases/{phid}/events/{eid}"
        ):
            return httpx.Response(204)
        return httpx.Response(404, json={"detail": "unexpected"})

    with mock_backend(handler):
        result = await call_tool(
            "unassign_event_from_phase",
            {"patient_id": pid, "event_id": eid},
        )

    assert json.loads(result[0].text)["status"] == "unassigned"


async def test_unassign_event_rejects_cross_patient_event() -> None:
    """Cross-patient short-circuit — bubbles up as a clean MCP error."""
    pid, eid = "p1", "e1"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return _json({"id": eid, "patient_id": "OTHER", "phase_id": "ph1"})
        return httpx.Response(500, json={"detail": "should not be called"})

    with mock_backend(handler):
        result = await call_tool(
            "unassign_event_from_phase",
            {"patient_id": pid, "event_id": eid},
        )

    assert len(result) == 1
    assert result[0].type == "text"
    assert result[0].text.startswith("Error:")
    assert "does not belong" in result[0].text
