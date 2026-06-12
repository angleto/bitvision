"""Unit tests for the patient-inbox MCP tools (GUI parity, fbbf5270 §12).

Hermetic via ``httpx.MockTransport`` (see conftest). Asserts the
path/params/header contract of every tool plus the scope posture:
reads on ``inbox:read``, the capability-address surface on the
sensitive ``inbox:manage``, decisions on the sensitive
``inbox:review`` (agent-capable: ``human_only`` stays False — the
backend profile gates with RBAC + provenance, not a blanket refusal).
"""

from __future__ import annotations

import json

import httpx

from bvmcp.scopes import SCOPE_BY_ID, TOOL_SCOPE
from bvmcp.server import ALL_TOOLS, call_tool
from bvmcp.tools import inbox as inbox_tools

from .conftest import TEST_TOKEN, mock_backend

_PATIENT = "11111111-1111-1111-1111-111111111111"
_ITEM = "22222222-2222-2222-2222-222222222222"
_ADDRESS = "33333333-3333-3333-3333-333333333333"
_ETAG = "44444444-4444-4444-4444-444444444444"

_EXPECTED_SCOPES = {
    "list_inbox_items": "inbox:read",
    "get_inbox_item": "inbox:read",
    "list_patient_inbox_addresses": "inbox:manage",
    "create_inbox_address": "inbox:manage",
    "set_inbox_address_label": "inbox:manage",
    "revoke_inbox_address": "inbox:manage",
    "configure_trusted_senders": "inbox:manage",
    "accept_inbox_item": "inbox:review",
    "reject_inbox_item": "inbox:review",
}


def _json_response(payload: dict | list) -> httpx.Response:
    return httpx.Response(200, json=payload)


def _assert_auth(request: httpx.Request) -> None:
    assert request.headers["authorization"] == f"Bearer {TEST_TOKEN}"


def test_tools_are_registered_and_scoped() -> None:
    advertised = {t.name for t in ALL_TOOLS}
    assert advertised >= set(_EXPECTED_SCOPES)
    assert {t.name for t in inbox_tools.TOOLS} == set(_EXPECTED_SCOPES)
    for name, scope in _EXPECTED_SCOPES.items():
        assert TOOL_SCOPE[name] == scope, name
    assert SCOPE_BY_ID["inbox:read"].sensitive is False
    assert SCOPE_BY_ID["inbox:manage"].sensitive is True
    assert SCOPE_BY_ID["inbox:review"].sensitive is True
    # agent_capable by design: the backend profile gates decisions, the
    # MCP layer must not blanket-refuse agents.
    assert SCOPE_BY_ID["inbox:review"].human_only is False


async def test_list_inbox_items_forwards_filters() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == f"/api/patients/{_PATIENT}/inbox/items"
        assert request.url.params["status"] == "needs_review"
        assert request.url.params["limit"] == "10"
        _assert_auth(request)
        return _json_response([])

    with mock_backend(handler):
        result = await call_tool(
            "list_inbox_items",
            {"patient_id": _PATIENT, "status": "needs_review", "limit": 10},
        )
    assert json.loads(result[0].text) == []


async def test_create_and_revoke_address() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        _assert_auth(request)
        if request.url.path.endswith("/revoke"):
            assert request.method == "POST"
            assert request.headers["if-match"] == f'"{_ETAG}"'
            assert json.loads(request.content)["reason"] == "compromised"
            return _json_response({"id": _ADDRESS, "active": False})
        assert request.method == "POST"
        assert request.url.path == f"/api/patients/{_PATIENT}/inbox/addresses"
        assert json.loads(request.content)["label"] == "Lab Rossi"
        return _json_response({"id": _ADDRESS, "address": "code+patient@x", "active": True})

    with mock_backend(handler):
        created = await call_tool(
            "create_inbox_address", {"patient_id": _PATIENT, "label": "Lab Rossi"}
        )
        revoked = await call_tool(
            "revoke_inbox_address",
            {
                "patient_id": _PATIENT,
                "address_id": _ADDRESS,
                "etag": _ETAG,
                "reason": "compromised",
            },
        )
    assert json.loads(created[0].text)["active"] is True
    assert json.loads(revoked[0].text)["active"] is False


async def test_accept_item_sends_options_etag_and_dry_run() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == f"/api/patients/{_PATIENT}/inbox/items/{_ITEM}/accept"
        assert request.url.params["dry_run"] == "true"
        assert request.headers["if-match"] == f'"{_ETAG}"'
        body = json.loads(request.content)
        assert body["excluded_components"] == ["bad.zip"]
        assert body["include_body"] is True
        _assert_auth(request)
        return _json_response({"item": {"id": _ITEM, "status": "needs_review"}, "dry_run": True})

    with mock_backend(handler):
        result = await call_tool(
            "accept_inbox_item",
            {
                "patient_id": _PATIENT,
                "item_id": _ITEM,
                "etag": _ETAG,
                "excluded_components": ["bad.zip"],
                "include_body": True,
                "dry_run": True,
            },
        )
    assert json.loads(result[0].text)["dry_run"] is True


async def test_reject_item_posts_reason() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == f"/api/patients/{_PATIENT}/inbox/items/{_ITEM}/reject"
        assert request.headers["if-match"] == f'"{_ETAG}"'
        assert json.loads(request.content)["reason"] == "spam"
        _assert_auth(request)
        return _json_response({"item": {"id": _ITEM, "status": "rejected"}})

    with mock_backend(handler):
        result = await call_tool(
            "reject_inbox_item",
            {"patient_id": _PATIENT, "item_id": _ITEM, "etag": _ETAG, "reason": "spam"},
        )
    assert json.loads(result[0].text)["item"]["status"] == "rejected"


async def test_configure_trusted_senders_actions() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        _assert_auth(request)
        calls.append(f"{request.method} {request.url.path}")
        if request.method == "GET":
            return _json_response([{"id": "e1", "sender_email": "lab@x.example"}])
        if request.method == "POST":
            assert json.loads(request.content)["sender_email"] == "lab@x.example"
            return _json_response({"id": "e1", "sender_email": "lab@x.example"})
        assert request.method == "DELETE"
        return httpx.Response(204)

    with mock_backend(handler):
        listed = await call_tool(
            "configure_trusted_senders", {"patient_id": _PATIENT, "action": "list"}
        )
        await call_tool(
            "configure_trusted_senders",
            {"patient_id": _PATIENT, "action": "add", "sender_email": "lab@x.example"},
        )
        removed = await call_tool(
            "configure_trusted_senders",
            {"patient_id": _PATIENT, "action": "remove", "entry_id": "e1"},
        )
    assert json.loads(listed[0].text)[0]["sender_email"] == "lab@x.example"
    assert json.loads(removed[0].text)["removed"] is True
    base = f"/api/patients/{_PATIENT}/inbox/trusted-senders"
    assert calls == [f"GET {base}", f"POST {base}", f"DELETE {base}/e1"]
