"""Integration tests for the A2A (Agent-to-Agent) protocol surface.

Covers:
- Agent Card discovery at /.well-known/agent-card.json (shape + 5 skills).
- JSON-RPC 2.0 dispatch at /api/a2a: error codes for unknown method,
  malformed JSON, and missing method.
- Task lifecycle: agent/sendMessage, agent/getTask, agent/listTasks,
  agent/cancelTask — happy paths.

Authentication note: /api/a2a currently uses ``optional_user`` so
anonymous callers are accepted. These tests lock that behavior in.
If the endpoint ever switches to ``require_user``, the anonymous
dispatch cases below will fail and force a deliberate update.

The endpoint depends on a DB session via ``optional_user`` and the
routing helper. We override ``get_db`` with a stub so these tests do
not need a running Postgres. The DB is only consulted when the router
actually executes a search; our default-routed messages intentionally
avoid search keywords.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from bvphoenix.api.a2a import AGENT_CARD
from bvphoenix.db.session import get_db
from bvphoenix.main import app
from bvphoenix.services.a2a_store import get_store


class _StubSession:
    """Minimal async session stand-in. The handlers only touch this if a
    search keyword is present in the user message; our lifecycle tests use
    non-search text so the stub is never actually invoked."""

    async def execute(self, *_: Any, **__: Any) -> Any:  # pragma: no cover - defensive
        raise AssertionError("DB should not be touched by these tests")

    async def close(self) -> None:
        return None


async def _override_get_db() -> AsyncIterator[_StubSession]:
    yield _StubSession()


@pytest.fixture(autouse=True)
def _stub_db() -> Iterator[None]:
    """Install the get_db override only for this module's tests, then
    remove it. Module-level installation leaked the stub into every
    other test that imported through collection, surfacing as random
    AssertionError(DB should not be touched) in unrelated integration
    tests when the file happened to be collected before them."""
    app.dependency_overrides[get_db] = _override_get_db
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_db, None)


client = TestClient(app)


@pytest.fixture(autouse=True)
def _reset_tasks() -> Iterator[None]:
    """Reset the task store between tests for isolation.

    Pytest-asyncio creates a fresh event loop per test; the singleton
    Redis client cached on the store would be bound to the *previous*
    loop and trigger "Event loop is closed" on reuse. Reset the client
    ref too so each test re-initialises."""
    store = get_store()
    store._memory.clear()
    store._memory_contexts.clear()
    store._client = None
    # Force in-memory path so tests never try to reach Redis. Redis is
    # covered separately by unit tests on a2a_store itself.
    store._use_memory = True
    yield
    store._memory.clear()
    store._memory_contexts.clear()
    store._client = None


def _rpc(method: str, params: dict | None = None, req_id: int | str | None = 1) -> dict:
    body: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        body["params"] = params
    response = client.post("/api/a2a", json=body)
    assert response.status_code == 200, response.text
    return response.json()


# ---- Agent Card ----


def test_agent_card_endpoint_ok() -> None:
    response = client.get("/.well-known/agent-card.json")
    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "bitvision-phoenix"
    assert body["url"] == "/api/a2a"
    assert body["version"]


def test_agent_card_has_required_top_level_fields() -> None:
    response = client.get("/.well-known/agent-card.json")
    body = response.json()
    for field in (
        "name",
        "description",
        "version",
        "url",
        "supported_interfaces",
        "capabilities",
        "default_input_modes",
        "default_output_modes",
        "skills",
        "security_schemes",
    ):
        assert field in body, f"missing field: {field}"


def test_agent_card_exposes_exactly_six_skills() -> None:
    response = client.get("/.well-known/agent-card.json")
    skills = response.json()["skills"]
    assert len(skills) == 6
    ids = {s["id"] for s in skills}
    assert ids == {
        "dicom-search",
        "similarity-search",
        "image-analysis",
        "patient-fascicolo",
        "radiology-consultation",
        "fascicolo-executive-summary",
    }


def test_agent_card_skills_have_required_fields() -> None:
    for skill in AGENT_CARD["skills"]:
        assert skill["id"]
        assert skill["name"]
        assert skill["description"]
        assert isinstance(skill["tags"], list) and skill["tags"]
        assert isinstance(skill["examples"], list) and skill["examples"]


def test_agent_card_advertises_jsonrpc_binding() -> None:
    body = client.get("/.well-known/agent-card.json").json()
    bindings = body["supported_interfaces"]
    assert any(
        b.get("protocol_binding") == "JSONRPC" and b.get("url") == "/api/a2a" for b in bindings
    )


# ---- JSON-RPC error dispatch ----


def test_jsonrpc_unknown_method_returns_minus_32601() -> None:
    body = _rpc("agent/nonsense")
    assert body["jsonrpc"] == "2.0"
    assert body["id"] == 1
    assert body["error"]["code"] == -32601
    assert "not found" in body["error"]["message"].lower()


def test_jsonrpc_malformed_body_returns_minus_32700() -> None:
    response = client.post(
        "/api/a2a",
        content=b"{not valid json",
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["error"]["code"] == -32700
    assert body["id"] is None


def test_jsonrpc_missing_method_returns_minus_32600() -> None:
    response = client.post("/api/a2a", json={"jsonrpc": "2.0", "id": 7})
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == 7
    assert body["error"]["code"] == -32600
    assert "method" in body["error"]["message"].lower()


def test_a2a_endpoint_accepts_anonymous_caller() -> None:
    """Locks in the current contract: /api/a2a uses optional_user, so
    unauthenticated callers get a normal JSON-RPC response (not 401)."""
    response = client.post(
        "/api/a2a", json={"jsonrpc": "2.0", "id": 1, "method": "agent/getAgentCard"}
    )
    assert response.status_code == 200
    assert "result" in response.json()


# ---- agent/getAgentCard over JSON-RPC ----


def test_get_agent_card_via_jsonrpc() -> None:
    body = _rpc("agent/getAgentCard")
    assert body["result"]["name"] == "bitvision-phoenix"
    assert len(body["result"]["skills"]) == 6


# ---- agent/sendMessage ----


def _text_message(text: str) -> dict:
    return {
        "role": "user",
        "parts": [{"type": "text", "text": text}],
    }


def test_send_message_creates_task_with_contextid() -> None:
    """A message with text creates a task with id + contextId. The
    final state depends on routing (completed for simple dispatch,
    input-required when the skill needs a UUID we didn't provide); we
    assert only the lifecycle fields the protocol guarantees."""
    body = _rpc(
        "agent/sendMessage",
        {"message": _text_message("find similar cases")},
    )
    task = body["result"]
    assert task["id"]
    assert task["contextId"]
    assert task["status"]["state"] in {"completed", "input-required", "failed"}


def test_send_message_without_text_requires_input() -> None:
    body = _rpc(
        "agent/sendMessage",
        {"message": {"role": "user", "parts": []}},
    )
    task = body["result"]
    assert task["status"]["state"] == "input-required"


def test_send_message_preserves_context_id() -> None:
    body = _rpc(
        "agent/sendMessage",
        {"message": _text_message("hello"), "contextId": "ctx-abc"},
    )
    assert body["result"]["contextId"] == "ctx-abc"


def test_send_message_continues_existing_task() -> None:
    first = _rpc("agent/sendMessage", {"message": _text_message("hello")})
    task_id = first["result"]["id"]

    second = _rpc(
        "agent/sendMessage",
        {"message": _text_message("follow up"), "taskId": task_id},
    )
    assert second["result"]["id"] == task_id
    # two user messages should now be on the task
    assert len(second["result"]["messages"]) == 2


# ---- agent/getTask ----


def test_get_task_returns_task_by_id() -> None:
    created = _rpc("agent/sendMessage", {"message": _text_message("hello")})
    task_id = created["result"]["id"]

    body = _rpc("agent/getTask", {"taskId": task_id})
    assert body["result"]["id"] == task_id
    # We assert the protocol surface (task is retrievable), not the
    # terminal state — that depends on skill routing which evolves.
    assert "state" in body["result"]["status"]


def test_get_task_unknown_id_returns_minus_32602() -> None:
    body = _rpc("agent/getTask", {"taskId": "does-not-exist"})
    assert body["error"]["code"] == -32602


def test_get_task_missing_param_returns_minus_32602() -> None:
    body = _rpc("agent/getTask", {})
    assert body["error"]["code"] == -32602


# ---- agent/listTasks ----


def test_list_tasks_returns_all_when_no_context() -> None:
    _rpc("agent/sendMessage", {"message": _text_message("hello"), "contextId": "ctx-1"})
    _rpc("agent/sendMessage", {"message": _text_message("hello"), "contextId": "ctx-2"})

    body = _rpc("agent/listTasks", {})
    assert len(body["result"]["tasks"]) == 2


def test_list_tasks_filters_by_context_id() -> None:
    _rpc("agent/sendMessage", {"message": _text_message("hello"), "contextId": "ctx-1"})
    _rpc("agent/sendMessage", {"message": _text_message("hello"), "contextId": "ctx-2"})
    _rpc("agent/sendMessage", {"message": _text_message("hello"), "contextId": "ctx-1"})

    body = _rpc("agent/listTasks", {"contextId": "ctx-1"})
    tasks = body["result"]["tasks"]
    assert len(tasks) == 2
    assert all(t["contextId"] == "ctx-1" for t in tasks)


# ---- agent/cancelTask ----


def test_cancel_task_transitions_to_canceled() -> None:
    created = _rpc("agent/sendMessage", {"message": _text_message("hello")})
    task_id = created["result"]["id"]

    body = _rpc("agent/cancelTask", {"taskId": task_id})
    assert body["result"]["status"]["state"] == "canceled"

    # getTask should now also reflect the canceled state
    follow_up = _rpc("agent/getTask", {"taskId": task_id})
    assert follow_up["result"]["status"]["state"] == "canceled"


def test_cancel_task_unknown_id_returns_minus_32602() -> None:
    body = _rpc("agent/cancelTask", {"taskId": "does-not-exist"})
    assert body["error"]["code"] == -32602
