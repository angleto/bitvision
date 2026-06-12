"""Unit tests for the embeddings-admin MCP tools (GUI parity surface).

Hermetic via ``httpx.MockTransport`` (see conftest). Beyond the usual
path/params/auth contract, these assert two postures specific to this
family:

* scope: every tool rides the sensitive ``admin:embeddings`` scope;
* storage isolation: the series-coverage projection strips
  ``last_failures`` (raw worker error strings + cross-patient target
  UUIDs) before the payload reaches the model.
"""

from __future__ import annotations

import json

import httpx

from bvmcp.scopes import SCOPE_BY_ID, TOOL_SCOPE
from bvmcp.server import ALL_TOOLS, call_tool
from bvmcp.tools import embeddings_admin as embeddings_admin_tools

from .conftest import TEST_TOKEN, mock_backend

_TOOL_NAMES = {
    "get_embedding_coverage",
    "get_text_embedding_coverage",
    "retry_failed_embeddings",
    "embed_missing_targets",
    "reembed_text_chunks",
}


def _json_response(payload: dict | list) -> httpx.Response:
    return httpx.Response(200, json=payload)


def _assert_auth(request: httpx.Request) -> None:
    assert request.headers["authorization"] == f"Bearer {TEST_TOKEN}"


def test_tools_are_registered_and_scoped() -> None:
    advertised = {t.name for t in ALL_TOOLS}
    assert advertised >= _TOOL_NAMES
    assert {t.name for t in embeddings_admin_tools.TOOLS} == _TOOL_NAMES
    for name in _TOOL_NAMES:
        assert TOOL_SCOPE[name] == "admin:embeddings"
    scope = SCOPE_BY_ID["admin:embeddings"]
    assert scope.sensitive is True
    assert scope.human_only is False


async def test_get_embedding_coverage_strips_failure_details() -> None:
    payload = {
        "items": [
            {
                "model_id": "biomedclip-v1",
                "target_kind": "series",
                "total": 100,
                "done": 90,
                "failed": 5,
                "pending": 5,
                "percentage": 90.0,
                "last_failures": [
                    {
                        "target_id": "11111111-1111-1111-1111-111111111111",
                        "error_message": "S3 GET s3://secret-bucket/key failed",
                        "error_class": "ClientError",
                        "failed_at": "2026-06-12T00:00:00+00:00",
                        "retry_count": 2,
                    }
                ],
            }
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/embeddings/coverage"
        _assert_auth(request)
        return _json_response(payload)

    with mock_backend(handler):
        result = await call_tool("get_embedding_coverage", {})

    body = json.loads(result[0].text)
    assert body["items"][0]["done"] == 90
    # Storage isolation: no error strings, no cross-patient target ids.
    assert "last_failures" not in body["items"][0]
    assert "secret-bucket" not in result[0].text


async def test_get_text_embedding_coverage_forwards_model() -> None:
    payload = {
        "total_chunks": 10,
        "embedded_chunks": 8,
        "pending_chunks": 2,
        "pct": 80,
        "by_source_kind": [],
        "model_id": "bge-m3-v1",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/embeddings/text-chunks/coverage"
        assert request.url.params["model"] == "bge-m3-v1"
        _assert_auth(request)
        return _json_response(payload)

    with mock_backend(handler):
        result = await call_tool("get_text_embedding_coverage", {"model": "bge-m3-v1"})

    assert json.loads(result[0].text)["model_id"] == "bge-m3-v1"


async def test_retry_failed_embeddings_posts_params() -> None:
    payload = {
        "status": "enqueued",
        "model_id": "biomedclip-v1",
        "target_kind": "series",
        "enqueued": 3,
        "candidates": 3,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/embeddings/retry-failed"
        assert request.url.params["model_id"] == "biomedclip-v1"
        assert request.url.params["target_kind"] == "series"
        assert request.url.params["dry_run"] == "false"
        _assert_auth(request)
        return _json_response(payload)

    with mock_backend(handler):
        result = await call_tool(
            "retry_failed_embeddings",
            {"model_id": "biomedclip-v1", "target_kind": "series"},
        )

    assert json.loads(result[0].text)["enqueued"] == 3


async def test_embed_missing_targets_dry_run() -> None:
    payload = {
        "status": "dry_run",
        "model_id": "biomedclip-v1",
        "target_kind": "series",
        "enqueued": 0,
        "candidates": 12,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/embeddings/embed-missing"
        assert request.url.params["dry_run"] == "true"
        _assert_auth(request)
        return _json_response(payload)

    with mock_backend(handler):
        result = await call_tool(
            "embed_missing_targets",
            {"model_id": "biomedclip-v1", "target_kind": "series", "dry_run": True},
        )

    body = json.loads(result[0].text)
    assert body["status"] == "dry_run"
    assert body["candidates"] == 12


async def test_reembed_text_chunks_full_reembed() -> None:
    payload = {
        "status": "enqueued",
        "model_id": "bge-m3-v1",
        "target_kind": "document_chunk",
        "enqueued": 122,
        "candidates": 122,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/embeddings/text-chunks/embed-missing"
        assert request.url.params["model"] == "bge-m3-v1"
        assert request.url.params["only_missing"] == "false"
        assert request.url.params["dry_run"] == "false"
        _assert_auth(request)
        return _json_response(payload)

    with mock_backend(handler):
        result = await call_tool(
            "reembed_text_chunks",
            {"model": "bge-m3-v1", "only_missing": False},
        )

    assert json.loads(result[0].text)["enqueued"] == 122
