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

from bvmcp.tools.client import api_get, api_post_with_headers, api_put

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
    Tool(
        name="get_contribution_gt_boxes",
        description=(
            "Read the reviewer's ground-truth burned-in-PHI boxes for one staged "
            "instance (the answer key the automatic pixel redaction is scored "
            "against). Boxes are {x, y, w, h, text, category} in intrinsic pixels."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "submission_id": {"type": "string", "description": "Submission UUID"},
                "instance_id": {"type": "string", "description": "SOPInstanceUID of the instance"},
            },
            "required": ["submission_id", "instance_id"],
        },
    ),
    Tool(
        name="save_contribution_gt_boxes",
        description=(
            "Set the ground-truth PHI boxes for one staged instance (replaces the "
            "existing set). Requires the submission etag (If-Match); the write "
            "bumps it. Boxes are clipped to the image bounds; category is one of "
            "name|codice_fiscale|date|address|phone|email|mrn|other|unknown. This "
            "is curation, NOT publishing — accepting a contribution stays "
            "human-only in the GUI."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "submission_id": {"type": "string", "description": "Submission UUID"},
                "instance_id": {"type": "string", "description": "SOPInstanceUID of the instance"},
                "etag": {"type": "string", "description": "Current submission etag (If-Match)"},
                "boxes": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "x": {"type": "integer", "minimum": 0},
                            "y": {"type": "integer", "minimum": 0},
                            "w": {"type": "integer", "minimum": 1},
                            "h": {"type": "integer", "minimum": 1},
                            "text": {"type": "string"},
                            "category": {"type": "string"},
                        },
                        "required": ["x", "y", "w", "h"],
                    },
                },
            },
            "required": ["submission_id", "instance_id", "etag", "boxes"],
        },
    ),
    Tool(
        name="score_contribution_gt",
        description=(
            "Recall of the automatic pixel redaction against the reviewer's GT "
            "boxes for one instance: the fraction of GT PHI boxes the auto-masks "
            "cover by >= 80%. A miss means residual PHI the pipeline would ship."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "submission_id": {"type": "string", "description": "Submission UUID"},
                "instance_id": {"type": "string", "description": "SOPInstanceUID of the instance"},
            },
            "required": ["submission_id", "instance_id"],
        },
    ),
    Tool(
        name="get_deid_recall_runs",
        description=(
            "Burned-in-pixel redaction recall over time: the persisted "
            "bvphoenix-deid-recall runs (recall/covered/total per corpus + engine "
            "fingerprint) — the tracked-over-time counterpart to the per-instance "
            "gt-score. Filter by corpus_kind (synthetic/public/curated). The PHI-"
            "bearing 'missed' texts are omitted unless include_missed=true."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "corpus_kind": {
                    "type": "string",
                    "enum": ["synthetic", "public", "curated"],
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 500},
                "include_missed": {"type": "boolean"},
            },
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

    if name == "get_contribution_gt_boxes":
        sid, iid = arguments["submission_id"], arguments["instance_id"]
        return json.dumps(await api_get(f"/contributions/{sid}/instances/{iid}/gt-boxes"), indent=2)

    if name == "save_contribution_gt_boxes":
        sid, iid = arguments["submission_id"], arguments["instance_id"]
        body, _ = await api_put(
            f"/contributions/{sid}/instances/{iid}/gt-boxes",
            json={"boxes": arguments["boxes"]},
            if_match=arguments["etag"],
        )
        return json.dumps(body, indent=2)

    if name == "score_contribution_gt":
        sid, iid = arguments["submission_id"], arguments["instance_id"]
        return json.dumps(await api_get(f"/contributions/{sid}/instances/{iid}/gt-score"), indent=2)

    if name == "get_deid_recall_runs":
        params = {
            k: arguments[k]
            for k in ("corpus_kind", "limit", "include_missed")
            if arguments.get(k) is not None
        }
        return json.dumps(
            await api_get("/contributions/recall-runs", params=params or None), indent=2
        )

    raise ValueError(f"unknown tool: {name}")


__all__ = ["TOOLS", "handle"]
