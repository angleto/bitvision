"""MCP tools for the public-contribution review queue (GUI parity).

Mirrors ``/contributions/*``: list + inspect submissions offered to the OpenData
library and reject them. **Accept is deliberately NOT exposed** — publishing
PHI-bearing imaging to the public web is irreversible and human-only; the review
profile's gate refuses agent actors server-side, so an agent can triage and
reject (or flag) but a human must approve in the GUI. This keeps the MCP a
superset of the GUI's *information* surface while the irreversible release stays
with a person.

Every submission carries its ``auto_checks`` — the per-instance header-de-id,
burned-in-pixel-risk, malware and CSAM verdicts — so an agent can describe why
an item needs review without fetching pixels.
"""

from __future__ import annotations

import json

from mcp.types import Tool

from bvmcp.tools.client import api_get, api_post_with_headers

TOOLS = [
    Tool(
        name="list_contribution_queue",
        description=(
            "List public-contribution submissions to the OpenData library with "
            "their review status and auto-check verdicts (header de-id, "
            "burned-in-pixel risk, malware, CSAM). Filter by status."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": [
                        "received",
                        "processing",
                        "needs_review",
                        "blocked",
                        "promoted",
                        "rejected",
                        "failed",
                    ],
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                "offset": {"type": "integer", "minimum": 0},
            },
        },
    ),
    Tool(
        name="get_contribution",
        description=(
            "Read one public-contribution submission: status, target tier, "
            "instance count, and the full per-check auto_checks (the burned-in "
            "pixel risk + de-id verdicts per instance) so you can explain why it "
            "needs human review."
        ),
        inputSchema={
            "type": "object",
            "properties": {"submission_id": {"type": "string", "description": "Submission UUID"}},
            "required": ["submission_id"],
        },
    ),
    Tool(
        name="reject_contribution",
        description=(
            "Reject a public-contribution submission (it will NOT be published; "
            "staged blobs are purged). Requires the submission etag (If-Match) "
            "and a reason. NOTE: accepting/publishing is human-only and must be "
            "done in the GUI — there is no agent-accept tool by design."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "submission_id": {"type": "string", "description": "Submission UUID"},
                "etag": {"type": "string", "description": "Current submission etag (If-Match)"},
                "reason": {"type": "string", "description": "Why it is rejected (audited)"},
            },
            "required": ["submission_id", "etag", "reason"],
        },
    ),
]


async def handle(name: str, arguments: dict) -> str:
    if name == "list_contribution_queue":
        params: dict = {}
        for key in ("status", "limit", "offset"):
            if arguments.get(key) is not None:
                params[key] = arguments[key]
        return json.dumps(await api_get("/contributions/queue", params=params or None), indent=2)

    if name == "get_contribution":
        return json.dumps(await api_get(f"/contributions/{arguments['submission_id']}"), indent=2)

    if name == "reject_contribution":
        body, _ = await api_post_with_headers(
            f"/contributions/{arguments['submission_id']}/reject",
            json={"reason": arguments["reason"]},
            if_match=arguments["etag"],
        )
        return json.dumps(body, indent=2)

    raise ValueError(f"unknown tool: {name}")


__all__ = ["TOOLS", "handle"]
