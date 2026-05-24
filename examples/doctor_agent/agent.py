"""Minimal doctor-agent A2A client for bitvision phoenix.

Demonstrates the end-to-end A2A v1.0 protocol flow:
  1. Discover capabilities via the Agent Card
  2. Send a task via JSON-RPC (agent/sendMessage)
  3. Poll until terminal state (agent/getTask)
  4. Handle INPUT_REQUIRED by prompting the user on stdin
  5. Pretty-print artifacts

Usage:
    uv run python agent.py --token $JWT --query "search for chest CT"
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from typing import Any

import httpx

# A2A task states (mirrors backend/src/bvphoenix/api/a2a.py TaskState)
TERMINAL_STATES = {"completed", "failed", "canceled"}
INPUT_REQUIRED = "input-required"

# Exit codes
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_CANCELED_OR_TIMEOUT = 2


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Doctor-agent A2A reference client.")
    p.add_argument("--token", required=True, help="JWT bearer token (from POST /api/auth/login).")
    p.add_argument("--backend", default="http://localhost:8000", help="Backend base URL.")
    p.add_argument("--query", required=True, help="Initial natural-language query.")
    p.add_argument("--poll-interval", type=float, default=1.0, help="Seconds between getTask polls.")
    p.add_argument("--timeout", type=float, default=30.0, help="Overall timeout in seconds.")
    return p.parse_args()


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _raise_for_http(resp: httpx.Response) -> None:
    if resp.status_code == 401:
        print("error: authentication failed (401) — token expired or invalid.", file=sys.stderr)
        sys.exit(EXIT_FAILED)
    if 500 <= resp.status_code < 600:
        print(
            f"error: backend returned {resp.status_code} — try again in a moment.",
            file=sys.stderr,
        )
        sys.exit(EXIT_FAILED)
    resp.raise_for_status()


def fetch_agent_card(client: httpx.Client, backend: str) -> dict[str, Any]:
    resp = client.get(f"{backend}/.well-known/agent-card.json")
    _raise_for_http(resp)
    return resp.json()


def _jsonrpc_call(
    client: httpx.Client, backend: str, token: str, method: str, params: dict[str, Any]
) -> dict[str, Any]:
    body = {"jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": method, "params": params}
    resp = client.post(f"{backend}/api/a2a", headers=_headers(token), json=body)
    _raise_for_http(resp)
    payload = resp.json()
    if "error" in payload:
        err = payload["error"]
        print(f"JSON-RPC error {err.get('code')}: {err.get('message')}", file=sys.stderr)
        sys.exit(EXIT_FAILED)
    return payload["result"]


def _text_message(text: str) -> dict[str, Any]:
    return {
        "role": "user",
        "parts": [{"type": "text", "text": text}],
        "messageId": str(uuid.uuid4()),
    }


def send_message(
    client: httpx.Client,
    backend: str,
    token: str,
    text: str,
    *,
    task_id: str | None = None,
    context_id: str | None = None,
) -> dict[str, Any]:
    params: dict[str, Any] = {"message": _text_message(text)}
    if task_id:
        params["taskId"] = task_id
    if context_id:
        params["contextId"] = context_id
    return _jsonrpc_call(client, backend, token, "agent/sendMessage", params)


def get_task(client: httpx.Client, backend: str, token: str, task_id: str) -> dict[str, Any]:
    return _jsonrpc_call(client, backend, token, "agent/getTask", {"taskId": task_id})


def poll_until_terminal(
    client: httpx.Client,
    backend: str,
    token: str,
    task: dict[str, Any],
    poll_interval: float,
    timeout: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    current = task
    while True:
        state = current.get("status", {}).get("state")
        if state in TERMINAL_STATES or state == INPUT_REQUIRED:
            return current
        if time.monotonic() >= deadline:
            print(f"timeout: task still in state '{state}' after {timeout}s.", file=sys.stderr)
            sys.exit(EXIT_CANCELED_OR_TIMEOUT)
        time.sleep(poll_interval)
        current = get_task(client, backend, token, current["id"])


def _extract_text_parts(parts: list[dict[str, Any]] | None) -> str:
    if not parts:
        return ""
    return "\n".join(p.get("text", "") for p in parts if p.get("type") == "text")


def print_agent_card_summary(card: dict[str, Any]) -> None:
    name = card.get("name", "<unknown>")
    version = card.get("version", "?")
    skills = card.get("skills", [])
    skill_ids = ", ".join(s.get("id", "?") for s in skills) or "(none)"
    print(f"Connected to agent: {name} v{version}")
    print(f"  skills: {skill_ids}")
    print()


def print_artifacts(task: dict[str, Any]) -> None:
    artifacts = task.get("artifacts") or []
    if not artifacts:
        print("(no artifacts returned)")
        return
    print("--- Artifacts ---")
    for art in artifacts:
        name = art.get("name", "artifact")
        text = _extract_text_parts(art.get("parts"))
        print(f"[{name}]")
        if text:
            print(text)
        else:
            print(json.dumps(art, indent=2, ensure_ascii=False))
        print()


def _input_required_prompt(task: dict[str, Any]) -> str:
    status = task.get("status", {})
    prompt_msg = status.get("message", {})
    prompt_text = _extract_text_parts(prompt_msg.get("parts")) or "(agent requests input)"
    print(f"\n[agent needs input] {prompt_text}")
    try:
        return input("your reply> ").strip()
    except EOFError:
        return ""


def run(args: argparse.Namespace) -> int:
    backend = args.backend.rstrip("/")
    with httpx.Client(timeout=httpx.Timeout(args.timeout)) as client:
        card = fetch_agent_card(client, backend)
        print_agent_card_summary(card)

        task = send_message(client, backend, args.token, args.query)
        while True:
            task = poll_until_terminal(
                client, backend, args.token, task, args.poll_interval, args.timeout
            )
            state = task.get("status", {}).get("state")
            if state == INPUT_REQUIRED:
                reply = _input_required_prompt(task)
                if not reply:
                    print("no input provided — aborting.", file=sys.stderr)
                    return EXIT_CANCELED_OR_TIMEOUT
                task = send_message(
                    client,
                    backend,
                    args.token,
                    reply,
                    task_id=task["id"],
                    context_id=task.get("contextId"),
                )
                continue
            break

        print(f"Task {task['id']} -> {task['status']['state']}")
        print_artifacts(task)

        final_state = task["status"]["state"]
        if final_state == "completed":
            return EXIT_OK
        if final_state == "failed":
            return EXIT_FAILED
        return EXIT_CANCELED_OR_TIMEOUT  # canceled


def main() -> int:
    args = _parse_args()
    try:
        return run(args)
    except httpx.HTTPError as e:
        print(f"network error: {e}", file=sys.stderr)
        return EXIT_FAILED
    except KeyboardInterrupt:
        print("\ninterrupted.", file=sys.stderr)
        return EXIT_CANCELED_OR_TIMEOUT


if __name__ == "__main__":
    sys.exit(main())
