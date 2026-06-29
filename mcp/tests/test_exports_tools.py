"""Contract tests for the exports MCP tool family.

Mocks the backend at the HTTP level and asserts each tool emits the
right method / path / body, plus the OTP->curl plumbing of
``issue_download_token`` (the one piece with logic beyond a passthrough).
"""

from __future__ import annotations

import json

import httpx

from bvmcp.scopes import SCOPE_BY_ID, scope_for_tool
from bvmcp.server import call_tool
from bvmcp.tools import exports as exports_tools

from .conftest import TEST_TOKEN, mock_backend

EXPORTS_TOOL_NAMES = {
    "export_fascicolo",
    "export_study",
    "export_folder",
    "bulk_download",
    "export_health_record_bundle",
    "get_job",
    "issue_download_token",
}

_PATIENT = "0882c1d6-bddc-4daa-bce7-2990ddc56793"
_JOB = "11111111-2222-3333-4444-555555555555"


def _job(**overrides: object) -> dict:
    base = {
        "id": _JOB,
        "kind": "fascicolo_export",
        "owner_subject_id": "subj-1",
        "status": "queued",
        "progress_total": None,
        "progress_done": 0,
        "stage": None,
        "result_uri": None,
        "result_download_url": None,
        "display_label": "Fascicolo: Francesca",
        "error": None,
        "created_at": "2026-06-24T12:00:00+00:00",
        "updated_at": "2026-06-24T12:00:00+00:00",
        "expires_at": "2026-06-26T12:00:00+00:00",
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------------- #
# Registration + scope catalogue                                        #
# --------------------------------------------------------------------- #


def test_exports_tools_registered_in_module() -> None:
    assert {t.name for t in exports_tools.TOOLS} == EXPORTS_TOOL_NAMES


def test_fascicolo_export_scope_is_sensitive() -> None:
    sd = SCOPE_BY_ID["fascicolo:export"]
    assert sd.sensitive is True
    assert sd.human_only is False


def test_export_tools_map_to_fascicolo_export() -> None:
    for name in ("export_fascicolo", "export_study", "export_folder", "bulk_download"):
        assert scope_for_tool(name) == "fascicolo:export", name
    assert scope_for_tool("issue_download_token") == "fascicolo:export"
    # get_job is a read-only status poll, lowest-privilege scope.
    assert scope_for_tool("get_job") == "patients:read"


def test_export_tool_schemas_are_self_consistent() -> None:
    for tool in exports_tools.TOOLS:
        assert isinstance(tool.inputSchema, dict)
        assert tool.inputSchema["type"] == "object"
        props = tool.inputSchema.get("properties", {})
        for req in tool.inputSchema.get("required", []):
            assert req in props, f"{tool.name}: required {req!r} missing from properties"


# --------------------------------------------------------------------- #
# Dispatch contracts                                                    #
# --------------------------------------------------------------------- #


async def test_export_fascicolo_defaults_to_full_tree() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == f"/api/patients/{_PATIENT}/export"
        body = json.loads(request.content)
        # Default: whole fascicolo INCLUDING dicom, navigable tree.
        assert body["include"] == "studies,reports,documents,annotations,dicom"
        assert body["layout"] == "tree"
        assert request.headers["authorization"] == f"Bearer {TEST_TOKEN}"
        return httpx.Response(202, json=_job())

    with mock_backend(handler):
        result = await call_tool("export_fascicolo", {"patient_id": _PATIENT})
    assert json.loads(result[0].text)["id"] == _JOB


async def test_export_fascicolo_honours_explicit_include_layout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["include"] == "documents,reports"
        assert body["layout"] == "flat"
        return httpx.Response(202, json=_job())

    with mock_backend(handler):
        await call_tool(
            "export_fascicolo",
            {"patient_id": _PATIENT, "include": "documents,reports", "layout": "flat"},
        )


async def test_export_study_sends_layout() -> None:
    study = "22222222-2222-2222-2222-222222222222"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/api/studies/{study}/export"
        assert json.loads(request.content)["layout"] == "tree"
        return httpx.Response(202, json=_job(kind="study_export"))

    with mock_backend(handler):
        await call_tool("export_study", {"study_id": study})


async def test_export_folder_forwards_scope_filters() -> None:
    folder = "33333333-3333-3333-3333-333333333333"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/api/folders/{folder}/export"
        body = json.loads(request.content)
        assert body["include_study_ids"] == ["s1"]
        assert body["layout"] == "tree"
        return httpx.Response(202, json=_job())

    with mock_backend(handler):
        await call_tool("export_folder", {"folder_id": folder, "include_study_ids": ["s1"]})


async def test_bulk_download_posts_items() -> None:
    items = [{"id": "s1", "kind": "study"}, {"id": "d1", "kind": "document"}]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/bulk/download"
        assert json.loads(request.content)["items"] == items
        return httpx.Response(202, json=_job())

    with mock_backend(handler):
        await call_tool("bulk_download", {"items": items})


async def test_get_job_reads_jobs_endpoint() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == f"/api/jobs/{_JOB}"
        return httpx.Response(200, json=_job(status="succeeded"))

    with mock_backend(handler):
        result = await call_tool("get_job", {"job_id": _JOB})
    assert json.loads(result[0].text)["status"] == "succeeded"


# --------------------------------------------------------------------- #
# issue_download_token — the OTP -> curl primitive                      #
# --------------------------------------------------------------------- #


async def test_issue_download_token_builds_curl_for_job() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/auth/download-token"
        body = json.loads(request.content)
        assert body == {"resource_kind": "job_result", "resource_id": _JOB}
        return httpx.Response(200, json={"token": "OTP123", "expires_in": 300})

    with mock_backend(handler):
        result = await call_tool("issue_download_token", {"job_id": _JOB})
    out = json.loads(result[0].text)
    assert out["token"] == "OTP123"
    assert out["expires_in_seconds"] == 300
    expected_url = f"https://bitvision.xeno.garden/api/jobs/{_JOB}/result_download?dt=OTP123"
    assert out["download_url"] == expected_url
    assert out["curl"] == f'curl -L -J -O "{expected_url}"'


async def test_issue_download_token_honours_public_base_and_document() -> None:
    doc = "44444444-4444-4444-4444-444444444444"

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body == {"resource_kind": "document", "resource_id": doc}
        return httpx.Response(200, json={"token": "T", "expires_in": 300})

    with mock_backend(handler):
        result = await call_tool(
            "issue_download_token",
            {
                "resource_kind": "document",
                "resource_id": doc,
                "public_base_url": "https://staging.example/",
            },
        )
    out = json.loads(result[0].text)
    assert out["download_url"] == f"https://staging.example/api/documents/{doc}/download?dt=T"


async def test_issue_download_token_requires_a_resource() -> None:
    result = await call_tool("issue_download_token", {})
    assert json.loads(result[0].text)["error"] == "bad_request"
