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
    # Record amendment: corrects the recorded clinical time without
    # moving event_status (the only writer of clinical time after
    # creation, and the only way to re-date a terminal row).
    "amend_event_time",
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


def test_amend_event_time_scope_is_events_write() -> None:
    assert scope_for_tool("amend_event_time") == "events:write"


def test_update_patch_schema_matches_backend_updatable_fields() -> None:
    """``event_date`` and the planned_* / timezone anchors are derived
    from the status anchor by a DB trigger, so PATCH is not their
    writer: the backend refuses them with 422 ``use_amend_time``.
    Advertising them here would invite the LLM into a guaranteed 422."""
    schema = next(t for t in events_tools.TOOLS if t.name == "update_clinical_event").inputSchema
    patch_schema = (schema or {}).get("properties", {}).get("patch", {})
    keys = set(patch_schema.get("properties") or {})
    assert keys == {
        "kind",
        "title",
        "body_part",
        "code_loinc",
        "code_snomed",
        "narrative",
        "location_struct",
        "recurrence_rule",
        "recurrence_exdates",
        "reminder_offsets_minutes",
        "meeting_url",
        "links",
    }


def test_amend_tool_requires_etag_and_idempotency_key() -> None:
    schema = next(t for t in events_tools.TOOLS if t.name == "amend_event_time").inputSchema
    required = set((schema or {}).get("required") or [])
    assert required == {"event_id", "etag", "idempotency_key"}
    props = (schema or {}).get("properties") or {}
    # Clearing an end timestamp must be expressible, so the two end
    # anchors accept an explicit null.
    for field in ("planned_end_at", "actual_end_at"):
        assert "null" in props[field]["type"], f"{field} must accept an explicit null"
    # The START anchors are NOT clearable (backend: 422
    # ``anchor_not_clearable``), so the schema must not advertise null.
    for field in ("planned_start_at", "actual_start_at"):
        assert props[field]["type"] == "string", f"{field} must not accept null"


def test_amend_tool_documents_the_status_keyed_reason_rule() -> None:
    """``reason`` is mandatory for every amendment of a completed /
    missed row (timezone-only included) and for any ``event_date``
    write, optional for the planned family. The tool text must not
    describe it as keyed on which field was sent, and must list the two
    codes the rewrite introduced."""
    tool = next(t for t in events_tools.TOOLS if t.name == "amend_event_time")
    text = tool.description or ""
    assert "anchor_not_clearable" in text
    assert "invalid_timezone" in text
    # Replaced by ``anchor_not_clearable``; the backend has no such code.
    assert "planned_start_required_by_status" not in text
    assert "timezone-only" in text


def test_update_patch_kind_enum_excludes_imaging_study() -> None:
    """PATCH may reclassify a row, but the DICOM pipeline owns
    ``imaging_study``: promoting into it is 422 and leaving it with a
    live projection is 409. Advertising a bare string invited both."""
    schema = next(t for t in events_tools.TOOLS if t.name == "update_clinical_event").inputSchema
    kind = (schema or {})["properties"]["patch"]["properties"]["kind"]
    assert "imaging_study" not in kind["enum"]
    assert "radiology_appointment" in kind["enum"]
    assert "409" in kind["description"]


def test_create_tool_does_not_promise_a_noon_fallback() -> None:
    """There is no ``event_date`` AT 12:00 UTC fallback in
    ``POST /api/clinical-events``: only ``event_date`` leaves both
    anchors NULL."""
    tool = next(t for t in events_tools.TOOLS if t.name == "create_clinical_event")
    props = (tool.inputSchema or {})["properties"]
    text = props["actual_start_at"]["description"]
    assert "12:00" not in text
    assert "event_date_conflicts_with_anchor" in text


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


async def test_amend_event_time_dispatches_post_with_headers() -> None:
    """The amendment rides the same envelope as the FSM transitions
    (If-Match + Idempotency-Key) but hits ``/amend-time`` and carries
    the mandatory ``reason`` for a realised fact."""
    captured_body: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/clinical-events/ev-1/amend-time"
        assert request.headers.get("if-match") in ('"etag-1"', "etag-1")
        assert request.headers.get("idempotency-key") == "amend-key-1"
        # No dry_run argument was passed, so no query string is sent.
        assert request.url.params.get("dry_run") is None
        captured_body.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "ev-1",
                "patient_id": "pat-1",
                "kind": "outpatient_visit",
                "event_date": "2026-03-02",
                "title": "Visita oncologica",
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
            "amend_event_time",
            {
                "event_id": "ev-1",
                "etag": "etag-1",
                "actual_start_at": "2026-03-02T09:30:00+01:00",
                "reason": "data di inserimento, non data della visita",
                "idempotency_key": "amend-key-1",
            },
        )

    assert captured_body == {
        "actual_start_at": "2026-03-02T09:30:00+01:00",
        "reason": "data di inserimento, non data della visita",
    }
    payload = json.loads(result[0].text)
    assert payload["event_date"] == "2026-03-02"
    assert payload["_etag_header"] == '"etag-2"'


async def test_amend_event_time_forwards_explicit_null_end() -> None:
    """The backend reads the amendment body with ``exclude_unset``, so an
    explicit null is how an end timestamp gets cleared. A truthiness
    filter in the dispatch loop would swallow it and make clearing
    unexpressible from MCP."""
    captured_body: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/clinical-events/ev-1/amend-time"
        assert request.url.params.get("dry_run") == "true"
        captured_body.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "ev-1",
                "patient_id": "pat-1",
                "kind": "outpatient_visit",
                "event_date": "2026-06-01",
                "title": "Controllo",
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
        await call_tool(
            "amend_event_time",
            {
                "event_id": "ev-1",
                "etag": "etag-1",
                "planned_start_at": "2026-06-01T08:00:00+02:00",
                "planned_end_at": None,
                "idempotency_key": "amend-key-2",
                "dry_run": True,
            },
        )

    assert captured_body == {
        "planned_start_at": "2026-06-01T08:00:00+02:00",
        "planned_end_at": None,
    }


async def test_amend_event_time_refuses_to_clear_a_start_anchor() -> None:
    """A null START anchor is a caller bug the backend answers with 422
    ``anchor_not_clearable``. The dispatch layer must never put it on
    the wire, and must say so in the same vocabulary."""

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("no request should reach the backend")

    with mock_backend(handler):
        result = await call_tool(
            "amend_event_time",
            {
                "event_id": "ev-1",
                "etag": "etag-1",
                "planned_start_at": None,
                "idempotency_key": "amend-key-4",
            },
        )

    payload = json.loads(result[0].text)
    assert payload["error"] == "anchor_not_clearable"
    assert payload["field"] == "planned_start_at"


async def test_amend_event_time_propagates_backend_error_body() -> None:
    """``raise_for_status`` loses the response body; the transport-level
    handler must render the 422 detail so the LLM learns which anchor
    family the row expects instead of seeing '422 Client Error'."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422,
            json={
                "detail": {
                    "code": "wrong_anchor_for_status",
                    "event_status": "completed",
                    "expected_prefix": "actual_",
                }
            },
        )

    with mock_backend(handler):
        result = await call_tool(
            "amend_event_time",
            {
                "event_id": "ev-1",
                "etag": "etag-1",
                "planned_start_at": "2026-06-01T08:00:00+02:00",
                "idempotency_key": "amend-key-3",
            },
        )

    payload = json.loads(result[0].text)
    assert payload["http_status"] == 422
    assert payload["detail"]["detail"]["code"] == "wrong_anchor_for_status"
