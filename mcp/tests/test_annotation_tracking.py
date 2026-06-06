"""Tests for the P0 annotation-tracking MCP surface.

Covers the changes that make the in-viewer annotation tools a safe,
tracked diagnostic surface for an agent:

* registry/enum consistency (catches the ``bbox.exclusion`` /
  ``measurement.sphere`` enum drift that previously let other tools
  reference a kind ``write_annotation`` could not emit);
* ``update_annotation`` / ``delete_annotation`` forward ``If-Match`` for
  optimistic concurrency;
* ``delete_annotation`` is a recoverable soft-delete carrying ``reason``;
* ``restore_annotation`` + ``get_annotation_revisions`` are wired to the
  new backend endpoints;
* ``get_annotations`` can surface tombstones via ``include_deleted``.

All requests are intercepted with ``httpx.MockTransport`` (no backend).
"""

from __future__ import annotations

import json

import httpx

from bvmcp.scopes import TOOL_SCOPE
from bvmcp.tools import annotations as annotations_tools
from bvmcp.tools import imaging as imaging_tools

from .conftest import TEST_TOKEN, mock_backend


def _json_response(payload: dict | list) -> httpx.Response:
    return httpx.Response(200, json=payload)


def _assert_auth(request: httpx.Request) -> None:
    assert request.headers["authorization"] == f"Bearer {TEST_TOKEN}"


# --------------------------------------------------------------------------- #
# registry / enum consistency (pure unit)
# --------------------------------------------------------------------------- #


def test_imaging_dispatch_tools_and_scopes_are_consistent() -> None:
    """Every dispatchable imaging tool must be declared, scoped, and
    vice-versa — a new handler without a Tool or scope is a latent
    fail-closed bug."""
    tool_names = {t.name for t in imaging_tools.TOOLS}
    for name in imaging_tools._DISPATCH:
        assert name in tool_names, f"{name} dispatched but not declared in TOOLS"
        assert name in TOOL_SCOPE, f"{name} dispatched but has no scope"
    for t in imaging_tools.TOOLS:
        assert t.name in imaging_tools._DISPATCH, f"{t.name} declared but not dispatched"


def test_new_tracking_tools_registered_with_scopes() -> None:
    tool_names = {t.name for t in imaging_tools.TOOLS}
    assert {"restore_annotation", "get_annotation_revisions"} <= tool_names
    assert TOOL_SCOPE["restore_annotation"] == "annotations:write"
    assert TOOL_SCOPE["get_annotation_revisions"] == "annotations:read"


def test_write_annotation_enum_covers_exclusion_and_sphere() -> None:
    """``find_hot_spots`` / ``compute_roi_stats`` consume
    ``kind='bbox.exclusion'`` and the viewer emits ``measurement.sphere``;
    ``write_annotation`` must be able to create both."""
    wa = next(t for t in imaging_tools.TOOLS if t.name == "write_annotation")
    enum = wa.inputSchema["properties"]["kind"]["enum"]
    assert "bbox.exclusion" in enum
    assert "measurement.sphere" in enum


# --------------------------------------------------------------------------- #
# update / delete forward If-Match; delete is a recoverable soft-delete
# --------------------------------------------------------------------------- #


async def test_update_annotation_forwards_if_match_and_idempotency() -> None:
    marker_id = "m-1"
    payload = {"id": marker_id, "etag": "new-etag", "kind": "bbox.lesion"}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "PATCH"
        assert request.url.path == f"/api/markers/{marker_id}"
        assert request.headers["if-match"] == '"old-etag"'
        assert request.headers["idempotency-key"] == "idem-1"
        assert json.loads(request.content) == {"body": "spiculated nodule"}
        _assert_auth(request)
        return _json_response(payload)

    with mock_backend(handler):
        result = await imaging_tools.handle(
            "update_annotation",
            {
                "marker_id": marker_id,
                "body": "spiculated nodule",
                "if_match": "old-etag",
                "idempotency_key": "idem-1",
            },
        )

    assert json.loads(result) == payload


async def test_delete_annotation_soft_with_reason_and_if_match() -> None:
    marker_id = "m-2"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.path == f"/api/markers/{marker_id}"
        assert request.url.params["reason"] == "misplaced"
        assert request.headers["if-match"] == '"cur-etag"'
        _assert_auth(request)
        return httpx.Response(204)

    with mock_backend(handler):
        result = await imaging_tools.handle(
            "delete_annotation",
            {"marker_id": marker_id, "reason": "misplaced", "if_match": "cur-etag"},
        )

    body = json.loads(result)
    assert body["status"] == "deleted"
    assert body["recoverable"] is True
    assert body["http_status"] == 204


# --------------------------------------------------------------------------- #
# restore + revision history are wired to the new endpoints
# --------------------------------------------------------------------------- #


async def test_restore_annotation_posts_to_restore_endpoint() -> None:
    marker_id = "m-3"
    payload = {"id": marker_id, "deleted_at": None, "etag": "restored-etag"}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == f"/api/markers/{marker_id}/restore"
        _assert_auth(request)
        return _json_response(payload)

    with mock_backend(handler):
        result = await imaging_tools.handle("restore_annotation", {"marker_id": marker_id})

    assert json.loads(result) == payload


async def test_get_annotation_revisions_gets_history() -> None:
    marker_id = "m-4"
    payload = [
        {"revision_no": 2, "change_kind": "delete", "author_kind": "agent"},
        {"revision_no": 1, "change_kind": "create", "author_kind": "agent"},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == f"/api/markers/{marker_id}/revisions"
        assert request.url.params["limit"] == "50"
        _assert_auth(request)
        return _json_response(payload)

    with mock_backend(handler):
        result = await imaging_tools.handle(
            "get_annotation_revisions", {"marker_id": marker_id, "limit": 50}
        )

    assert json.loads(result) == payload


# --------------------------------------------------------------------------- #
# get_annotations surfaces tombstones on request
# --------------------------------------------------------------------------- #


async def test_get_annotations_include_deleted_forwards_flag() -> None:
    patient_id = "p-1"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == f"/api/patients/{patient_id}/markers"
        assert request.url.params["include_deleted"] == "true"
        _assert_auth(request)
        return _json_response([])

    with mock_backend(handler):
        result = await annotations_tools.handle(
            "get_annotations",
            {
                "patient_id": patient_id,
                "target_kind": "study",
                "target_id": "s-1",
                "include_deleted": True,
            },
        )

    assert json.loads(result)["markers"] == []
