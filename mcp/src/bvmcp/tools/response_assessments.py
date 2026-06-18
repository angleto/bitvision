"""MCP tools for response assessments — patient-level RECIST / volumetric.

The oncology roll-up above lesion tracks: aggregate the patient's target
lesions at a follow-up study into an overall response (CR/PR/SD/PD). The
category is computed server-side from the findings; a human/agent can edit
it; recompute re-derives it after measurements change. Wraps
``/api/.../response-assessments``.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
from mcp.types import Tool, ToolAnnotations

from bvmcp.tools.client import (
    api_delete,
    api_get,
    api_patch,
    api_post_with_headers,
    format_http_error,
)

_APPROVAL_NOTE = (
    "APPROVAL FLOW: the MCP host prompts the human for approval before "
    'this write runs. A bare "No approval received" is a host-side '
    "decline, NOT a scope error."
)
_CRITERIA = ["recist_1_1", "volumetric", "percist"]
_CATEGORIES = ["CR", "PR", "SD", "PD", "NE"]
_NON_TARGET = ["CR", "non_CR_non_PD", "PD", "NE"]


TOOLS: list[Tool] = [
    Tool(
        name="list_response_assessments",
        annotations=ToolAnnotations(
            title="List response assessments",
            readOnlyHint=True,
            idempotentHint=True,
            openWorldHint=False,
        ),
        description="List a patient's response assessments (newest first).",
        inputSchema={
            "type": "object",
            "properties": {
                "patient_id": {"type": "string"},
                "include_deleted": {"type": "boolean"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
            },
            "required": ["patient_id"],
        },
    ),
    Tool(
        name="get_response_assessment",
        annotations=ToolAnnotations(
            title="Get response assessment",
            readOnlyHint=True,
            idempotentHint=True,
            openWorldHint=False,
        ),
        description="Fetch one response assessment (category + sums + auditable basis).",
        inputSchema={
            "type": "object",
            "properties": {"assessment_id": {"type": "string"}},
            "required": ["assessment_id"],
        },
    ),
    Tool(
        name="get_response_assessment_revisions",
        annotations=ToolAnnotations(
            title="Get response assessment revisions",
            readOnlyHint=True,
            idempotentHint=True,
            openWorldHint=False,
        ),
        description="The revision history of a response assessment.",
        inputSchema={
            "type": "object",
            "properties": {
                "assessment_id": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
            },
            "required": ["assessment_id"],
        },
    ),
    Tool(
        name="compute_response_assessment",
        annotations=ToolAnnotations(
            title="Compute response assessment",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=False,
        ),
        description=(
            "Compute and record the patient's overall tumour response at a "
            "follow-up study by aggregating the target lesion tracks (those "
            "with recist_role='target'): sum of longest diameters vs baseline "
            "and nadir -> CR/PR/SD/PD. ``criterion`` selects recist_1_1 "
            "(default), volumetric (research thresholds) or percist. Omit "
            "``baseline_study_id`` to infer the earliest target timepoint. "
            "``dry_run=true`` computes without persisting. " + _APPROVAL_NOTE
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "patient_id": {"type": "string"},
                "current_study_id": {"type": "string"},
                "baseline_study_id": {"type": "string"},
                "criterion": {"type": "string", "enum": _CRITERIA},
                "notes": {"type": "string"},
                "dry_run": {"type": "boolean"},
                "idempotency_key": {"type": "string"},
            },
            "required": ["patient_id", "current_study_id"],
        },
    ),
    Tool(
        name="recompute_response_assessment",
        annotations=ToolAnnotations(
            title="Recompute response assessment",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
        description=(
            "Re-derive the category over the current findings (e.g. after a "
            "lesion was propagated or re-measured) and update the record. " + _APPROVAL_NOTE
        ),
        inputSchema={
            "type": "object",
            "properties": {"assessment_id": {"type": "string"}},
            "required": ["assessment_id"],
        },
    ),
    Tool(
        name="update_response_assessment",
        annotations=ToolAnnotations(
            title="Update response assessment",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=False,
        ),
        description=(
            "Edit the human-authored fields of an assessment: ``category`` "
            "override, ``non_target_status``, ``notes``. Pass ``if_match``. " + _APPROVAL_NOTE
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "assessment_id": {"type": "string"},
                "category": {"type": "string", "enum": _CATEGORIES},
                "non_target_status": {"type": "string", "enum": _NON_TARGET},
                "notes": {"type": "string"},
                "if_match": {"type": "string"},
            },
            "required": ["assessment_id"],
        },
    ),
    Tool(
        name="delete_response_assessment",
        annotations=ToolAnnotations(
            title="Delete response assessment",
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=True,
            openWorldHint=False,
        ),
        description="Soft-delete a response assessment (recoverable). " + _APPROVAL_NOTE,
        inputSchema={
            "type": "object",
            "properties": {
                "assessment_id": {"type": "string"},
                "reason": {"type": "string"},
                "if_match": {"type": "string"},
            },
            "required": ["assessment_id"],
        },
    ),
    Tool(
        name="restore_response_assessment",
        annotations=ToolAnnotations(
            title="Restore response assessment",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
        description="Restore a soft-deleted response assessment. " + _APPROVAL_NOTE,
        inputSchema={
            "type": "object",
            "properties": {"assessment_id": {"type": "string"}},
            "required": ["assessment_id"],
        },
    ),
]


async def _list_response_assessments(args: dict[str, Any]) -> str:
    params: dict[str, Any] = {}
    if args.get("include_deleted"):
        params["include_deleted"] = "true"
    if args.get("limit") is not None:
        params["limit"] = args["limit"]
    try:
        payload = await api_get(
            f"/api/patients/{args['patient_id']}/response-assessments", params=params or None
        )
    except httpx.HTTPStatusError as exc:
        return format_http_error(exc)
    return json.dumps(payload, indent=2, ensure_ascii=False)


async def _get_response_assessment(args: dict[str, Any]) -> str:
    try:
        payload = await api_get(f"/api/response-assessments/{args['assessment_id']}")
    except httpx.HTTPStatusError as exc:
        return format_http_error(exc)
    return json.dumps(payload, indent=2, ensure_ascii=False)


async def _get_response_assessment_revisions(args: dict[str, Any]) -> str:
    params = {"limit": int(args.get("limit", 200))}
    try:
        payload = await api_get(
            f"/api/response-assessments/{args['assessment_id']}/revisions", params=params
        )
    except httpx.HTTPStatusError as exc:
        return format_http_error(exc)
    return json.dumps(payload, indent=2, ensure_ascii=False)


async def _compute_response_assessment(args: dict[str, Any]) -> str:
    body: dict[str, Any] = {"current_study_id": args["current_study_id"]}
    for k in ("baseline_study_id", "criterion", "notes"):
        if args.get(k) is not None:
            body[k] = args[k]
    params: dict[str, Any] = {}
    if args.get("dry_run"):
        params["dry_run"] = "true"
    try:
        payload, _headers = await api_post_with_headers(
            f"/api/patients/{args['patient_id']}/response-assessments",
            json=body,
            params=params or None,
            idempotency_key=args.get("idempotency_key"),
        )
    except httpx.HTTPStatusError as exc:
        return format_http_error(exc)
    return json.dumps(payload, indent=2, ensure_ascii=False)


async def _recompute_response_assessment(args: dict[str, Any]) -> str:
    try:
        payload, _headers = await api_post_with_headers(
            f"/api/response-assessments/{args['assessment_id']}/recompute"
        )
    except httpx.HTTPStatusError as exc:
        return format_http_error(exc)
    return json.dumps(payload, indent=2, ensure_ascii=False)


async def _update_response_assessment(args: dict[str, Any]) -> str:
    body = {k: args[k] for k in ("category", "non_target_status", "notes") if k in args}
    try:
        payload, _headers = await api_patch(
            f"/api/response-assessments/{args['assessment_id']}",
            json=body,
            if_match=args.get("if_match"),
        )
    except httpx.HTTPStatusError as exc:
        return format_http_error(exc)
    return json.dumps(payload, indent=2, ensure_ascii=False)


async def _delete_response_assessment(args: dict[str, Any]) -> str:
    params: dict[str, Any] = {}
    if args.get("reason"):
        params["reason"] = args["reason"]
    try:
        code = await api_delete(
            f"/api/response-assessments/{args['assessment_id']}",
            params=params or None,
            if_match=args.get("if_match"),
        )
    except httpx.HTTPStatusError as exc:
        return format_http_error(exc)
    return json.dumps({"status": "deleted", "recoverable": True, "http_status": code})


async def _restore_response_assessment(args: dict[str, Any]) -> str:
    try:
        payload, _headers = await api_post_with_headers(
            f"/api/response-assessments/{args['assessment_id']}/restore"
        )
    except httpx.HTTPStatusError as exc:
        return format_http_error(exc)
    return json.dumps(payload, indent=2, ensure_ascii=False)


_DISPATCH = {
    "list_response_assessments": _list_response_assessments,
    "get_response_assessment": _get_response_assessment,
    "get_response_assessment_revisions": _get_response_assessment_revisions,
    "compute_response_assessment": _compute_response_assessment,
    "recompute_response_assessment": _recompute_response_assessment,
    "update_response_assessment": _update_response_assessment,
    "delete_response_assessment": _delete_response_assessment,
    "restore_response_assessment": _restore_response_assessment,
}


async def handle(name: str, arguments: dict[str, Any]) -> str:
    handler = _DISPATCH.get(name)
    if handler is None:
        return f"Error: unknown tool '{name}' in response_assessments module"
    return await handler(arguments)
