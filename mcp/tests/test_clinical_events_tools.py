"""Smoke + scope tests for the clinical_events MCP tool family.

Pre-2026-05-03 the MCP layer exposed only read operations on
ClinicalEvent (find_clinical_events, get_event) plus link helpers
(propose / confirm). The agent had no way to *create* a non-imaging
event, despite the backend exposing ``POST /api/clinical-events`` and
the scope ``events:write`` being defined in the catalog. This was the
primary symptom of "MCP must be GUI superset" being violated for
events. These tests pin the new surface and prevent a regression.
"""

from __future__ import annotations

import json

import httpx

from bvmcp.scopes import scope_for_tool
from bvmcp.server import call_tool
from bvmcp.tools import clinical_events as events_tools

from .conftest import mock_backend

EVENT_TOOL_NAMES = {
    "find_clinical_events",
    "get_event",
    "create_clinical_event",
    "update_clinical_event",
    "delete_clinical_event",
    "propose_event_link",
    "confirm_event_link",
    # FSM-checked calendar transitions on a ClinicalEvent (added so
    # the agent can drive the same state machine the viewer drives).
    "confirm_event",
    "reschedule_event",
    "complete_event",
    "cancel_event",
    "mark_event_missed",
    # Calendar discovery feeds + ICS export.
    "find_upcoming_events",
    "find_overdue_events",
    "find_events_by_date_range",
    "export_calendar_ics",
    "export_event_ics",
}


def test_event_tools_registered_in_module() -> None:
    names = {t.name for t in events_tools.TOOLS}
    assert names == EVENT_TOOL_NAMES


def test_create_clinical_event_scope_is_events_write() -> None:
    assert scope_for_tool("create_clinical_event") == "events:write"


def test_update_clinical_event_scope_is_events_write() -> None:
    assert scope_for_tool("update_clinical_event") == "events:write"


def test_delete_clinical_event_scope_is_events_write() -> None:
    assert scope_for_tool("delete_clinical_event") == "events:write"


def test_create_tool_has_idempotency_key_required() -> None:
    """Every write tool in the project must enforce idempotency at the
    schema level so the LLM cannot accidentally double-insert. The
    backend echoes the same row when the same key is replayed."""
    schema = next(t for t in events_tools.TOOLS if t.name == "create_clinical_event").inputSchema
    assert "idempotency_key" in (schema or {}).get("properties", {})
    assert "idempotency_key" in (schema or {}).get("required", [])


def test_create_tool_does_not_accept_imaging_study_kind() -> None:
    """Imaging events are owned by the DICOM ingest pipeline; the MCP
    tool must surface this contract at the schema layer so the LLM is
    told ``imaging_study`` is not a valid choice (the backend would
    refuse with 422 anyway, but failing fast at the LLM is cheaper)."""
    schema = next(t for t in events_tools.TOOLS if t.name == "create_clinical_event").inputSchema
    kinds = (schema or {}).get("properties", {}).get("kind", {}).get("enum") or []
    assert "imaging_study" not in kinds, (
        "create_clinical_event must not advertise imaging_study; that path is "
        "owned by the ingestion pipeline"
    )
    # Sanity: at least one non-imaging kind is offered.
    assert "outpatient_visit" in kinds


def test_update_and_delete_tools_require_etag() -> None:
    for tool_name in ("update_clinical_event", "delete_clinical_event"):
        schema = next(t for t in events_tools.TOOLS if t.name == tool_name).inputSchema
        required = (schema or {}).get("required") or []
        assert "etag" in required, f"{tool_name}: etag is mandatory for If-Match"


# --------------------------------------------------------------------------- #
# Light dispatch smoke — confirm the tools hit the documented backend paths.
# --------------------------------------------------------------------------- #


async def test_create_clinical_event_dispatches_post() -> None:
    captured_body: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/clinical-events"
        assert request.headers.get("idempotency-key") == "key-abc"
        captured_body.update(json.loads(request.content))
        return httpx.Response(
            201,
            json={
                "id": "ev-1",
                "patient_id": "pat-1",
                "kind": "outpatient_visit",
                "event_date": "2026-04-30",
                "title": "Visita oncologica",
                "body_part": None,
                "code_loinc": None,
                "code_snomed": None,
                "narrative": None,
                "imaging_study_id": None,
                "etag": "etag-1",
                "created_at": "2026-04-30T10:00:00Z",
                "updated_at": "2026-04-30T10:00:00Z",
            },
        )

    with mock_backend(handler):
        result = await call_tool(
            "create_clinical_event",
            {
                "patient_id": "pat-1",
                "kind": "outpatient_visit",
                "title": "Visita oncologica",
                "event_date": "2026-04-30",
                "idempotency_key": "key-abc",
            },
        )

    assert captured_body["patient_id"] == "pat-1"
    assert captured_body["kind"] == "outpatient_visit"
    assert captured_body["title"] == "Visita oncologica"
    payload_text = result[0].text if hasattr(result, "__getitem__") else str(result)
    payload = json.loads(payload_text)
    assert payload["id"] == "ev-1"


async def test_update_clinical_event_sends_if_match() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "PATCH"
        assert request.url.path == "/api/clinical-events/ev-1"
        # parse_if_match wraps unquoted strings in double quotes.
        assert request.headers.get("if-match") in ('"etag-1"', "etag-1")
        return httpx.Response(
            200,
            json={
                "id": "ev-1",
                "patient_id": "pat-1",
                "kind": "outpatient_visit",
                "event_date": "2026-04-30",
                "title": "Visita oncologica revised",
                "body_part": None,
                "code_loinc": None,
                "code_snomed": None,
                "narrative": None,
                "imaging_study_id": None,
                "etag": "etag-2",
                "created_at": "2026-04-30T10:00:00Z",
                "updated_at": "2026-04-30T11:00:00Z",
            },
            headers={"ETag": '"etag-2"'},
        )

    with mock_backend(handler):
        result = await call_tool(
            "update_clinical_event",
            {
                "event_id": "ev-1",
                "etag": "etag-1",
                "patch": {"title": "Visita oncologica revised"},
            },
        )

    payload_text = result[0].text if hasattr(result, "__getitem__") else str(result)
    payload = json.loads(payload_text)
    assert payload["title"] == "Visita oncologica revised"


async def test_delete_clinical_event_dispatches_delete() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.path == "/api/clinical-events/ev-1"
        assert request.headers.get("if-match") in ('"etag-1"', "etag-1")
        return httpx.Response(204)

    with mock_backend(handler):
        result = await call_tool(
            "delete_clinical_event",
            {"event_id": "ev-1", "etag": "etag-1"},
        )

    payload_text = result[0].text if hasattr(result, "__getitem__") else str(result)
    payload = json.loads(payload_text)
    assert payload["status"] == "deleted"
    assert payload["http_status"] == 204
