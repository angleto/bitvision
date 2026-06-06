"""Tests for the P3 Finding MCP tools (the structured diagnostic surface).

Registry/enum consistency + the create / update / delete / restore /
search / geometry tools forward to the right ``/api/findings`` endpoints
with the right verbs, params, headers and bodies. All requests are
intercepted with ``httpx.MockTransport`` (no backend).
"""

from __future__ import annotations

import json

import httpx

from bvmcp.scopes import TOOL_SCOPE
from bvmcp.tools import findings as findings_tools

from .conftest import TEST_TOKEN, mock_backend


def _json_response(payload: dict | list) -> httpx.Response:
    return httpx.Response(200, json=payload)


def _assert_auth(request: httpx.Request) -> None:
    assert request.headers["authorization"] == f"Bearer {TEST_TOKEN}"


def test_findings_dispatch_tools_and_scopes_consistent() -> None:
    tool_names = {t.name for t in findings_tools.TOOLS}
    for name in findings_tools._DISPATCH:
        assert name in tool_names, f"{name} dispatched but not declared"
        assert name in TOOL_SCOPE, f"{name} dispatched but unscoped"
    for t in findings_tools.TOOLS:
        assert t.name in findings_tools._DISPATCH
    # The full diagnostic loop is exposed.
    assert {
        "get_finding_vocab",
        "search_findings",
        "get_finding",
        "get_finding_revisions",
        "create_finding",
        "update_finding",
        "delete_finding",
        "restore_finding",
        "add_finding_geometry",
    } <= tool_names
    # read vs write scoping
    assert TOOL_SCOPE["search_findings"] == "findings:read"
    assert TOOL_SCOPE["create_finding"] == "findings:write"
    assert TOOL_SCOPE["delete_finding"] == "findings:write"


async def test_get_finding_vocab() -> None:
    payload = {"finding_types": [], "anatomy_sites": [], "morphology_terms": []}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/findings/vocab"
        _assert_auth(request)
        return _json_response(payload)

    with mock_backend(handler):
        result = await findings_tools.handle("get_finding_vocab", {})
    assert json.loads(result) == payload


async def test_create_finding_forwards_body_dry_run_idempotency() -> None:
    pid = "p-1"
    payload = {"id": "f-1", "type": "nodule", "etag": "e1"}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == f"/api/patients/{pid}/findings"
        assert request.url.params["dry_run"] == "true"
        assert request.headers["idempotency-key"] == "idem-1"
        body = json.loads(request.content)
        assert body["type"] == "nodule"
        assert body["study_id"] == "s-1"
        assert body["longest_diameter_mm"] == 14.0
        assert body["morphology"] == ["spiculated"]
        _assert_auth(request)
        return _json_response(payload)

    with mock_backend(handler):
        result = await findings_tools.handle(
            "create_finding",
            {
                "patient_id": pid,
                "study_id": "s-1",
                "type": "nodule",
                "morphology": ["spiculated"],
                "longest_diameter_mm": 14.0,
                "dry_run": True,
                "idempotency_key": "idem-1",
            },
        )
    assert json.loads(result) == payload


async def test_update_finding_partial_body_and_if_match() -> None:
    fid = "f-2"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "PATCH"
        assert request.url.path == f"/api/findings/{fid}"
        assert request.headers["if-match"] == '"old-etag"'
        body = json.loads(request.content)
        # only provided keys are sent (PATCH semantics)
        assert body == {"status": "confirmed", "suv_max": 6.2}
        _assert_auth(request)
        return _json_response({"id": fid, "status": "confirmed"})

    with mock_backend(handler):
        result = await findings_tools.handle(
            "update_finding",
            {"finding_id": fid, "status": "confirmed", "suv_max": 6.2, "if_match": "old-etag"},
        )
    assert json.loads(result)["status"] == "confirmed"


async def test_delete_finding_soft_with_reason_and_if_match() -> None:
    fid = "f-3"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.path == f"/api/findings/{fid}"
        assert request.url.params["reason"] == "duplicate"
        assert request.headers["if-match"] == '"cur"'
        _assert_auth(request)
        return httpx.Response(204)

    with mock_backend(handler):
        result = await findings_tools.handle(
            "delete_finding", {"finding_id": fid, "reason": "duplicate", "if_match": "cur"}
        )
    body = json.loads(result)
    assert body["recoverable"] is True
    assert body["http_status"] == 204


async def test_restore_finding_posts_to_restore() -> None:
    fid = "f-4"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == f"/api/findings/{fid}/restore"
        _assert_auth(request)
        return _json_response({"id": fid, "deleted_at": None})

    with mock_backend(handler):
        result = await findings_tools.handle("restore_finding", {"finding_id": fid})
    assert json.loads(result)["deleted_at"] is None


async def test_search_findings_forwards_structured_filters() -> None:
    pid = "p-9"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == f"/api/patients/{pid}/findings"
        p = request.url.params
        assert p["type"] == "nodule"
        assert p["anatomy"] == "lung_upper_lobe"
        assert p["min_diameter_mm"] == "10"
        assert p["morphology"] == "spiculated"
        _assert_auth(request)
        return _json_response([])

    with mock_backend(handler):
        result = await findings_tools.handle(
            "search_findings",
            {
                "patient_id": pid,
                "type": "nodule",
                "anatomy": "lung_upper_lobe",
                "morphology": ["spiculated"],
                "min_diameter_mm": 10,
            },
        )
    assert json.loads(result) == []


async def test_search_findings_corpus_mode_omits_patient() -> None:
    """No patient_id -> corpus-wide visibility-scoped search at
    /api/findings/search, forwarding scope + structured filters."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/findings/search"
        p = request.url.params
        assert p["type"] == "mass"
        assert p["scope"] == "mine"
        assert p["min_volume_ml"] == "5"
        _assert_auth(request)
        return _json_response([])

    with mock_backend(handler):
        result = await findings_tools.handle(
            "search_findings",
            {"type": "mass", "scope": "mine", "min_volume_ml": 5},
        )
    assert json.loads(result) == []


async def test_add_finding_geometry_posts_link() -> None:
    fid = "f-5"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == f"/api/findings/{fid}/geometry"
        body = json.loads(request.content)
        assert body == {"role": "bbox", "marker_id": "m-1"}
        _assert_auth(request)
        return _json_response({"id": fid, "geometry": [{"id": "g-1", "role": "bbox"}]})

    with mock_backend(handler):
        result = await findings_tools.handle(
            "add_finding_geometry",
            {"finding_id": fid, "role": "bbox", "marker_id": "m-1"},
        )
    assert json.loads(result)["geometry"][0]["role"] == "bbox"
