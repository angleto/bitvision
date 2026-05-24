"""MCP tools for clinical notes (free-text annotation channel).

The note channel is the prose half of the unified annotation surface
(markers carry geometry, notes carry text). Both channels share the
same target taxonomy (``study`` / ``series`` / ``instance`` plus the
non-imaging targets the Notes API supports). For agents these tools
are the write side of ``get_annotations(include_notes=True)``.
"""

from __future__ import annotations

import json
from typing import Any

from mcp.types import Tool

from bvmcp.tools.client import api_delete, api_patch, api_post_with_headers

TOOLS = [
    Tool(
        name="write_clinical_note",
        description=(
            "Create a free-text clinical note anchored to a target "
            "(study / series / instance / event / phase / patient). The "
            "backend forces ``author_kind=agent`` when the call is "
            "agent-authenticated; ``model_id`` and ``provider`` are "
            "stored as provenance. Use this for findings prose that "
            "doesn't fit a structured marker (impressions, deltas vs "
            "prior, follow-up suggestions). Cross-patient mentions in "
            "the body (``@kind:UUID`` DSL) are rejected by the backend."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "patient_id": {"type": "string"},
                "target_kind": {
                    "type": "string",
                    "description": (
                        "Target taxonomy. Includes the imaging triad "
                        "(study / series / instance) plus event / phase "
                        "/ patient — see CLINICAL_NOTE_TARGET_KINDS."
                    ),
                },
                "target_id": {"type": "string"},
                "body": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 8000,
                },
                "pinned": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Pinned notes float to the top of the per-target "
                        "view; reserve for clinically important context."
                    ),
                },
                "anchor": {
                    "type": "object",
                    "description": (
                        "Optional viewer-pinned anchor (slice index, "
                        "voxel coords). Stored as JSONB; same shape as "
                        "the NoteAnchor schema in the backend."
                    ),
                },
                "model_id": {"type": "string"},
                "provider": {"type": "string"},
                "consultation_id": {
                    "type": "string",
                    "description": (
                        "When set, the write is routed to the named "
                        "consultation's branch instead of main. Required "
                        "for non-owner writers."
                    ),
                },
                "idempotency_key": {"type": "string"},
            },
            "required": ["patient_id", "target_kind", "target_id", "body"],
        },
    ),
    Tool(
        name="update_clinical_note",
        description=(
            "Edit a clinical note (body / pinned / anchor). The backend "
            "rejects edits from a caller who is neither the author nor "
            "the patient owner with 403."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "patient_id": {"type": "string"},
                "note_id": {"type": "string"},
                "body": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 8000,
                },
                "pinned": {"type": "boolean"},
                "anchor": {
                    "type": ["object", "null"],
                    "description": (
                        "Replace the anchor (object) or clear it (null). "
                        "Omit the field to leave the existing anchor "
                        "untouched."
                    ),
                },
                "consultation_id": {"type": "string"},
                "idempotency_key": {"type": "string"},
            },
            "required": ["patient_id", "note_id"],
        },
    ),
    Tool(
        name="delete_clinical_note",
        description=(
            "Soft-delete a clinical note. Same authorship gate as "
            "update; non-author non-owners receive 403."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "patient_id": {"type": "string"},
                "note_id": {"type": "string"},
                "consultation_id": {"type": "string"},
            },
            "required": ["patient_id", "note_id"],
        },
    ),
]


async def handle(name: str, arguments: dict) -> str:
    if name == "write_clinical_note":
        return await _write(arguments)
    if name == "update_clinical_note":
        return await _update(arguments)
    if name == "delete_clinical_note":
        return await _delete(arguments)
    raise ValueError(f"unknown tool: {name}")


def _consultation_params(args: dict) -> dict[str, Any] | None:
    cid = args.get("consultation_id")
    return {"consultation": cid} if cid else None


async def _write(args: dict) -> str:
    patient_id = args["patient_id"]
    body: dict[str, Any] = {
        "target_kind": args["target_kind"],
        "target_id": args["target_id"],
        "body": args["body"],
        "pinned": bool(args.get("pinned", False)),
    }
    for k in ("anchor", "model_id", "provider"):
        if k in args:
            body[k] = args[k]
    payload, _h = await api_post_with_headers(
        f"/api/patients/{patient_id}/notes",
        json=body,
        params=_consultation_params(args),
        idempotency_key=args.get("idempotency_key"),
    )
    return json.dumps(payload, indent=2, ensure_ascii=False)


async def _update(args: dict) -> str:
    patient_id = args["patient_id"]
    note_id = args["note_id"]
    body: dict[str, Any] = {}
    for k in ("body", "pinned"):
        if k in args:
            body[k] = args[k]
    if "anchor" in args:
        body["anchor"] = args["anchor"]
    payload, _h = await api_patch(
        f"/api/patients/{patient_id}/notes/{note_id}",
        json=body,
        params=_consultation_params(args),
        idempotency_key=args.get("idempotency_key"),
    )
    return json.dumps(payload, indent=2, ensure_ascii=False)


async def _delete(args: dict) -> str:
    patient_id = args["patient_id"]
    note_id = args["note_id"]
    cid = args.get("consultation_id")
    path = f"/api/patients/{patient_id}/notes/{note_id}"
    if cid:
        path = f"{path}?consultation={cid}"
    code = await api_delete(path)
    return json.dumps({"status": "deleted", "http_status": code})
