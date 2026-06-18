"""Tests for the lesion-track MCP tools (longitudinal lesion follow-up).

Registry/scope consistency + the create / list / link / trajectory / delete
tools forward to the right ``/api/lesion-tracks`` endpoints with the right
verbs, params, headers and bodies. Requests are intercepted with
``httpx.MockTransport`` (no backend).
"""

from __future__ import annotations

import json

import httpx

from bvmcp.scopes import TOOL_SCOPE
from bvmcp.tools import lesion_tracks as lt_tools

from .conftest import TEST_TOKEN, mock_backend


def _json_response(payload: dict | list) -> httpx.Response:
    return httpx.Response(200, json=payload)


def _assert_auth(request: httpx.Request) -> None:
    assert request.headers["authorization"] == f"Bearer {TEST_TOKEN}"


def test_lesion_track_dispatch_tools_and_scopes_consistent() -> None:
    tool_names = {t.name for t in lt_tools.TOOLS}
    for name in lt_tools._DISPATCH:
        assert name in tool_names, f"{name} dispatched but not declared"
        assert name in TOOL_SCOPE, f"{name} dispatched but unscoped"
    for t in lt_tools.TOOLS:
        assert t.name in lt_tools._DISPATCH
    # The full longitudinal loop is exposed.
    assert {
        "list_lesion_tracks",
        "get_lesion_track",
        "get_lesion_trajectory",
        "create_lesion_track",
        "add_finding_to_track",
        "remove_finding_from_track",
    } <= tool_names
    # read vs write scoping
    assert TOOL_SCOPE["get_lesion_trajectory"] == "findings:read"
    assert TOOL_SCOPE["create_lesion_track"] == "findings:write"
    assert TOOL_SCOPE["add_finding_to_track"] == "findings:write"
    assert TOOL_SCOPE["delete_lesion_track"] == "findings:write"


async def test_create_lesion_track_forwards_body_dry_run_idempotency() -> None:
    pid = "p-1"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == f"/api/patients/{pid}/lesion-tracks"
        assert request.url.params.get("dry_run") == "true"
        assert request.headers.get("idempotency-key") == "idem-9"
        body = json.loads(request.content)
        assert body["label"] == "Nodulo LSD"
        assert body["type"] == "nodule"
        assert body["baseline_finding_id"] == "f-base"
        # unset optional fields must not be forwarded
        assert "anatomy" not in body
        _assert_auth(request)
        return _json_response({"id": "dry-run", "points": []})

    with mock_backend(handler):
        result = await lt_tools.handle(
            "create_lesion_track",
            {
                "patient_id": pid,
                "label": "Nodulo LSD",
                "type": "nodule",
                "baseline_finding_id": "f-base",
                "dry_run": True,
                "idempotency_key": "idem-9",
            },
        )
    assert json.loads(result)["id"] == "dry-run"


async def test_add_finding_to_track_forwards_point_body() -> None:
    tid = "t-1"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == f"/api/lesion-tracks/{tid}/points"
        body = json.loads(request.content)
        assert body["finding_id"] == "f-follow"
        assert body["is_baseline"] is False
        assert body["registration_id"] == "reg-1"
        _assert_auth(request)
        return _json_response({"id": tid, "points": [{}, {}]})

    with mock_backend(handler):
        result = await lt_tools.handle(
            "add_finding_to_track",
            {
                "track_id": tid,
                "finding_id": "f-follow",
                "is_baseline": False,
                "registration_id": "reg-1",
            },
        )
    assert len(json.loads(result)["points"]) == 2


async def test_get_lesion_trajectory_is_a_get() -> None:
    tid = "t-1"
    payload = {"summary": {"overall_direction": "increase"}}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == f"/api/lesion-tracks/{tid}/trajectory"
        _assert_auth(request)
        return _json_response(payload)

    with mock_backend(handler):
        result = await lt_tools.handle("get_lesion_trajectory", {"track_id": tid})
    assert json.loads(result) == payload


async def test_delete_lesion_track_sends_if_match_and_reason() -> None:
    tid = "t-1"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.path == f"/api/lesion-tracks/{tid}"
        assert request.url.params.get("reason") == "merged"
        # the client wraps the etag in the quoted If-Match format
        assert request.headers.get("if-match") == '"etag-7"'
        _assert_auth(request)
        return httpx.Response(204)

    with mock_backend(handler):
        result = await lt_tools.handle(
            "delete_lesion_track", {"track_id": tid, "reason": "merged", "if_match": "etag-7"}
        )
    assert json.loads(result)["status"] == "deleted"
