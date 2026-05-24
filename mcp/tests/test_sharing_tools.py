"""Smoke + contract tests for the sharing MCP tool family.

Each test mocks the backend at the HTTP level and asserts the tool
emits the right method / path / body / params. The point is to catch
drift between the tool's input schema and the wire format the backend
expects, plus the dry_run plumbing (a path that was untested when the
tool family landed).
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from bvmcp.scopes import SCOPE_BY_ID, scope_for_tool
from bvmcp.server import call_tool
from bvmcp.tools import sharing as sharing_tools

from .conftest import TEST_TOKEN, mock_backend

SHARING_TOOL_NAMES = {
    "create_study_share_link",
    "create_folder_share_link",
    "list_share_links",
    "update_share_link",
    "revoke_share_link",
}


# --------------------------------------------------------------------- #
# Registration + scope catalogue                                        #
# --------------------------------------------------------------------- #


def test_sharing_tools_registered_in_module() -> None:
    names = {t.name for t in sharing_tools.TOOLS}
    assert names == SHARING_TOOL_NAMES


def test_sharing_scope_is_sensitive() -> None:
    """``sharing:write`` must be marked sensitive so a leaked token
    without explicit operator consent cannot mint a share-link."""
    sd = SCOPE_BY_ID["sharing:write"]
    assert sd.sensitive is True
    assert sd.human_only is False


def test_every_sharing_tool_maps_to_sharing_write() -> None:
    for name in SHARING_TOOL_NAMES:
        assert scope_for_tool(name) == "sharing:write", name


def test_every_sharing_tool_input_schema_is_object() -> None:
    for tool in sharing_tools.TOOLS:
        assert isinstance(tool.inputSchema, dict)
        assert tool.inputSchema["type"] == "object"
        # Closed superset checks: every ``required`` field must be in
        # ``properties`` so the JSON-schema is self-consistent.
        props = tool.inputSchema.get("properties", {})
        for req in tool.inputSchema.get("required", []):
            assert req in props, f"{tool.name}: required field {req!r} missing from properties"


# --------------------------------------------------------------------- #
# Helpers                                                               #
# --------------------------------------------------------------------- #


def _assert_auth(request: httpx.Request) -> None:
    assert request.headers["authorization"] == f"Bearer {TEST_TOKEN}"


def _share_link_out(**overrides: Any) -> dict[str, Any]:
    base = {
        "id": "link-1",
        "token": "tok-abc",
        "url": "http://test-frontend/shared/tok-abc/info",
        "label": None,
        "permissions": ["read_metadata", "read_pixels"],
        "expires_at": None,
        "revoked": False,
        "use_count": 0,
        "max_uses": None,
        "requires_password": False,
        "created_at": "2026-05-13T12:00:00+00:00",
        "mode": "claim",
        "recipient_name": None,
        "recipient_email": None,
        "recipient_phone": None,
        "generated_password": None,
        "deidentify": True,
        "received_at": None,
        "download_count": 0,
        "resource_kind": "study",
        "resource_id": "study-uuid",
        "grantor_subject_id": "subj-1",
        "prepared_job_id": None,
        "prepared_status": None,
        "prepared_progress_done": None,
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------------- #
# create_study_share_link                                               #
# --------------------------------------------------------------------- #


async def test_create_study_share_link_posts_to_share_endpoint() -> None:
    study_id = "11111111-1111-1111-1111-111111111111"
    payload = _share_link_out(resource_id=study_id)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == f"/api/studies/{study_id}/share"
        # No dry_run query param when caller didn't opt in
        assert "dry_run" not in dict(request.url.params)
        body = json.loads(request.content)
        assert body["target"] == {"kind": "link_public"}
        assert body["access_level"] == "viewer"
        assert body["download"] is False
        _assert_auth(request)
        return httpx.Response(201, json=payload)

    with mock_backend(handler):
        result = await call_tool(
            "create_study_share_link",
            {
                "study_id": study_id,
                "target": {"kind": "link_public"},
                "access_level": "viewer",
                "download": False,
            },
        )

    assert len(result) == 1
    parsed = json.loads(result[0].text)
    assert parsed["id"] == "link-1"
    assert parsed["resource_id"] == study_id


async def test_create_study_share_link_dry_run_propagates_query_param() -> None:
    """The dry_run kwarg must become ``?dry_run=true`` on the wire so
    the backend's dry-run branch fires. The synthetic response is
    handed back to the agent unmodified."""
    study_id = "11111111-1111-1111-1111-111111111111"
    synthetic = _share_link_out(
        id="dry-run",
        token="dry-run",
        url="(dry-run — no persistence)",
        resource_id=study_id,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["dry_run"] == "true"
        return httpx.Response(201, json=synthetic)

    with mock_backend(handler):
        result = await call_tool(
            "create_study_share_link",
            {
                "study_id": study_id,
                "target": {"kind": "link_public"},
                "dry_run": True,
            },
        )

    parsed = json.loads(result[0].text)
    assert parsed["id"] == "dry-run"
    assert parsed["token"] == "dry-run"


async def test_create_study_share_link_propagates_idempotency_key_header() -> None:
    """The ``idempotency_key`` arg must become an ``Idempotency-Key``
    HTTP header so the backend's dedup cache (24h) can collapse retries
    from a flaky autonomous agent into a single visible share-link.
    Without this header a network blip between mint-attempt-1 and
    mint-attempt-2 leaves two live shares in the listing.
    """
    study_id = "11111111-1111-1111-1111-111111111111"
    synthetic = _share_link_out(resource_id=study_id)

    def handler(request: httpx.Request) -> httpx.Response:
        # Header is case-insensitive; httpx normalises to title-case.
        assert request.headers.get("Idempotency-Key") == "agent-batch-42"
        return httpx.Response(201, json=synthetic)

    with mock_backend(handler):
        result = await call_tool(
            "create_study_share_link",
            {
                "study_id": study_id,
                "target": {"kind": "link_public"},
                "idempotency_key": "agent-batch-42",
            },
        )
    assert json.loads(result[0].text)["resource_id"] == study_id


async def test_create_folder_share_link_propagates_idempotency_key_header() -> None:
    folder_id = "22222222-2222-2222-2222-222222222222"
    synthetic = _share_link_out(resource_id=folder_id, resource_kind="folder")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/api/folders/{folder_id}/share-link"
        assert request.headers.get("Idempotency-Key") == "agent-fold-99"
        return httpx.Response(201, json=synthetic)

    with mock_backend(handler):
        result = await call_tool(
            "create_folder_share_link",
            {
                "folder_id": folder_id,
                "target": {"kind": "link_public"},
                "idempotency_key": "agent-fold-99",
            },
        )
    assert json.loads(result[0].text)["resource_id"] == folder_id


async def test_create_study_share_link_no_idempotency_key_means_no_header() -> None:
    """Omitting ``idempotency_key`` must not send a stale or empty
    Idempotency-Key header — the backend would treat empty-string as a
    real key and dedup against a phantom row."""
    study_id = "11111111-1111-1111-1111-111111111111"

    def handler(request: httpx.Request) -> httpx.Response:
        assert "Idempotency-Key" not in request.headers
        return httpx.Response(201, json=_share_link_out(resource_id=study_id))

    with mock_backend(handler):
        await call_tool(
            "create_study_share_link",
            {
                "study_id": study_id,
                "target": {"kind": "link_public"},
            },
        )


async def test_create_study_share_link_forwards_autogen_password_in_body() -> None:
    """The autogen_password flag must round-trip into the JSON body so
    the backend can mint the high-entropy password."""
    study_id = "11111111-1111-1111-1111-111111111111"

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["autogen_password"] is True
        # generated_password is filled by the backend on the response,
        # not by the caller.
        assert "generated_password" not in body
        return httpx.Response(
            201,
            json=_share_link_out(generated_password="x-24-char-secret-..."),
        )

    with mock_backend(handler):
        result = await call_tool(
            "create_study_share_link",
            {
                "study_id": study_id,
                "target": {"kind": "link_public"},
                "autogen_password": True,
            },
        )

    parsed = json.loads(result[0].text)
    assert parsed["generated_password"] == "x-24-char-secret-..."


async def test_create_study_share_link_surfaces_403_with_structured_detail() -> None:
    """A 403 from the backend (owner check fail) must propagate as the
    structured backend_error payload thanks to the ``format_http_error``
    fence in ``server.py``."""
    study_id = "11111111-1111-1111-1111-111111111111"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"detail": "only the owner can share"})

    with mock_backend(handler):
        result = await call_tool(
            "create_study_share_link",
            {"study_id": study_id, "target": {"kind": "link_public"}},
        )

    parsed = json.loads(result[0].text)
    assert parsed["error"] == "backend_error"
    assert parsed["http_status"] == 403
    assert parsed["detail"] == {"detail": "only the owner can share"}


# --------------------------------------------------------------------- #
# create_folder_share_link                                              #
# --------------------------------------------------------------------- #


async def test_create_folder_share_link_posts_to_folder_endpoint() -> None:
    folder_id = "22222222-2222-2222-2222-222222222222"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == f"/api/folders/{folder_id}/share-link"
        body = json.loads(request.content)
        assert body["target"] == {"kind": "email", "email": "rad@example.com"}
        # Caller intentionally did not pass ``access_level``; the
        # backend default (viewer) applies, so the body should NOT
        # carry it. This pins the "only forward provided fields"
        # contract of ``_share_body``.
        assert "access_level" not in body
        return httpx.Response(
            201,
            json=_share_link_out(resource_kind="folder", resource_id=folder_id),
        )

    with mock_backend(handler):
        result = await call_tool(
            "create_folder_share_link",
            {
                "folder_id": folder_id,
                "target": {"kind": "email", "email": "rad@example.com"},
            },
        )

    parsed = json.loads(result[0].text)
    assert parsed["resource_kind"] == "folder"
    assert parsed["resource_id"] == folder_id


# --------------------------------------------------------------------- #
# list_share_links                                                      #
# --------------------------------------------------------------------- #


async def test_list_share_links_flat_uses_share_links_endpoint() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/share-links"
        assert request.url.params["include_revoked"] == "true"
        assert request.url.params["limit"] == "50"
        return httpx.Response(200, json={"items": []})

    with mock_backend(handler):
        result = await call_tool(
            "list_share_links",
            {"include_revoked": True, "limit": 50},
        )

    assert json.loads(result[0].text) == {"items": []}


async def test_list_share_links_study_scope_uses_study_shares_endpoint() -> None:
    """When study_id is set the tool must call the dedicated per-study
    listing endpoint (not the flat one with a filter), so the agent
    avoids cross-patient enumeration via the flat listing path."""
    study_id = "33333333-3333-3333-3333-333333333333"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == f"/api/studies/{study_id}/shares"
        # The patient_id / include_revoked filters of the flat listing
        # do not exist on the study-scoped endpoint and must NOT leak
        # into its query string.
        assert "patient_id" not in dict(request.url.params)
        assert "include_revoked" not in dict(request.url.params)
        return httpx.Response(200, json=[_share_link_out(resource_id=study_id)])

    with mock_backend(handler):
        result = await call_tool(
            "list_share_links",
            {
                "study_id": study_id,
                # These should be ignored — the tool dispatches to the
                # per-study endpoint when study_id is present.
                "patient_id": "ignored",
                "include_revoked": True,
            },
        )

    parsed = json.loads(result[0].text)
    assert isinstance(parsed, list)
    assert parsed[0]["resource_id"] == study_id


# --------------------------------------------------------------------- #
# update_share_link                                                     #
# --------------------------------------------------------------------- #


async def test_update_share_link_patches_with_changed_fields_only() -> None:
    link_id = "44444444-4444-4444-4444-444444444444"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "PATCH"
        assert request.url.path == f"/api/share-links/{link_id}"
        body = json.loads(request.content)
        # Only the fields the caller passed should appear in the body.
        assert body == {"label": "renamed", "expires_in_hours": 48}
        return httpx.Response(200, json=_share_link_out(label="renamed"))

    with mock_backend(handler):
        result = await call_tool(
            "update_share_link",
            {"link_id": link_id, "label": "renamed", "expires_in_hours": 48},
        )

    parsed = json.loads(result[0].text)
    assert parsed["label"] == "renamed"


async def test_update_share_link_409_on_revoked_surfaces_structured_error() -> None:
    link_id = "44444444-4444-4444-4444-444444444444"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409,
            json={"detail": "share link is revoked; create a new one instead"},
        )

    with mock_backend(handler):
        result = await call_tool(
            "update_share_link",
            {"link_id": link_id, "label": "x"},
        )

    parsed = json.loads(result[0].text)
    assert parsed["error"] == "backend_error"
    assert parsed["http_status"] == 409


# --------------------------------------------------------------------- #
# revoke_share_link                                                     #
# --------------------------------------------------------------------- #


async def test_revoke_share_link_soft_delete_default() -> None:
    link_id = "55555555-5555-5555-5555-555555555555"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.path == f"/api/share-links/{link_id}"
        # Without purge=true the path carries no purge query string
        assert "purge" not in dict(request.url.params)
        return httpx.Response(204)

    with mock_backend(handler):
        result = await call_tool("revoke_share_link", {"link_id": link_id})

    parsed = json.loads(result[0].text)
    assert parsed["status"] == "revoked"
    assert parsed["http_status"] == 204


async def test_revoke_share_link_purge_appends_query_flag() -> None:
    link_id = "55555555-5555-5555-5555-555555555555"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.path == f"/api/share-links/{link_id}"
        assert request.url.params["purge"] == "true"
        return httpx.Response(204)

    with mock_backend(handler):
        result = await call_tool(
            "revoke_share_link",
            {"link_id": link_id, "purge": True},
        )

    parsed = json.loads(result[0].text)
    assert parsed["status"] == "purged"
