"""MCP tools for lesion tracks — longitudinal lesion follow-up.

Where ``create_finding`` records a measured lesion in one study, these
tools follow *one physical lesion across studies* and read its growth:
create a ``LesionTrack``, link a per-study ``Finding`` as a timepoint
(``add_finding_to_track``), and read the derived trajectory
(``get_lesion_trajectory``) — volume/diameter deltas, doubling time,
direction — to answer "has the tumour grown between the baseline CT and
the follow-up?". All wrap the backend ``/api`` surface, which enforces
patient-scoped RBAC, the single-baseline rule, agent provenance and the
cross-patient guard server-side.

Typical loop: ``create_finding`` on each study (baseline + follow-up) →
``create_lesion_track`` (seed with the baseline) → ``add_finding_to_track``
(the follow-up timepoint) → ``get_lesion_trajectory`` (read the growth).
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
    "decline (the request never reached the server), NOT a scope error; "
    "authentic permission errors come back as JSON with "
    '``error: "backend_error"`` and ``http_status`` 401/403.'
)

_RECIST_ROLES = ["target", "non_target", "new", "not_evaluable"]
_LATERALITIES = ["left", "right", "bilateral", "midline"]
_STATUSES = ["active", "resolved", "retracted"]


TOOLS: list[Tool] = [
    Tool(
        name="list_lesion_tracks",
        annotations=ToolAnnotations(
            title="List lesion tracks",
            readOnlyHint=True,
            idempotentHint=True,
            openWorldHint=False,
        ),
        description=(
            "List a patient's lesion tracks (one physical lesion followed "
            "over time). Filter by ``status`` (active|resolved|retracted) or "
            "``recist_role`` (target|non_target|new|not_evaluable). Each "
            "track carries its ordered timepoints (points), each linking a "
            "per-study finding."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "patient_id": {"type": "string"},
                "status": {"type": "string", "enum": _STATUSES},
                "recist_role": {"type": "string", "enum": _RECIST_ROLES},
                "include_deleted": {"type": "boolean"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
            },
            "required": ["patient_id"],
        },
    ),
    Tool(
        name="get_lesion_track",
        annotations=ToolAnnotations(
            title="Get lesion track",
            readOnlyHint=True,
            idempotentHint=True,
            openWorldHint=False,
        ),
        description="Fetch one lesion track with its ordered timepoints.",
        inputSchema={
            "type": "object",
            "properties": {"track_id": {"type": "string"}},
            "required": ["track_id"],
        },
    ),
    Tool(
        name="get_lesion_trajectory",
        annotations=ToolAnnotations(
            title="Get lesion trajectory",
            readOnlyHint=True,
            idempotentHint=True,
            openWorldHint=False,
        ),
        description=(
            "The derived growth of a tracked lesion: per-timepoint volume "
            "and longest-diameter deltas vs the baseline and vs the previous "
            "study, volume doubling time, and an overall direction "
            "(increase|decrease|stable). This is the 'has the tumour grown?' "
            "answer. Pure read — the findings are the source of truth."
        ),
        inputSchema={
            "type": "object",
            "properties": {"track_id": {"type": "string"}},
            "required": ["track_id"],
        },
    ),
    Tool(
        name="get_lesion_track_revisions",
        annotations=ToolAnnotations(
            title="Get lesion track revisions",
            readOnlyHint=True,
            idempotentHint=True,
            openWorldHint=False,
        ),
        description="The full revision history of a lesion track (every create/edit/link act).",
        inputSchema={
            "type": "object",
            "properties": {
                "track_id": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
            },
            "required": ["track_id"],
        },
    ),
    Tool(
        name="create_lesion_track",
        annotations=ToolAnnotations(
            title="Create lesion track",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=False,
        ),
        description=(
            "Create a lesion track for a patient (one physical lesion to "
            "follow over time). Optionally seed the baseline timepoint with "
            "``baseline_finding_id``. ``type`` / ``anatomy`` use the finding "
            "vocabulary (get_finding_vocab). Set ``recist_role='target'`` to "
            "include the lesion in a RECIST response assessment. Pass "
            "``dry_run=true`` to validate without writing. " + _APPROVAL_NOTE
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "patient_id": {"type": "string"},
                "label": {"type": "string", "description": "human name, e.g. 'Nodulo LSD'"},
                "type": {"type": "string", "description": "finding type key (optional)"},
                "anatomy": {"type": "string", "description": "anatomy site key (optional)"},
                "laterality": {"type": "string", "enum": _LATERALITIES},
                "recist_role": {"type": "string", "enum": _RECIST_ROLES},
                "status": {"type": "string", "enum": _STATUSES},
                "description": {"type": "string"},
                "baseline_finding_id": {
                    "type": "string",
                    "description": "seed the baseline timepoint with this finding",
                },
                "dry_run": {"type": "boolean"},
                "idempotency_key": {"type": "string"},
            },
            "required": ["patient_id", "label"],
        },
    ),
    Tool(
        name="update_lesion_track",
        annotations=ToolAnnotations(
            title="Update lesion track",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=False,
        ),
        description=(
            "Patch a lesion track (label, type, anatomy, laterality, "
            "recist_role, status, description). Only the fields you pass "
            "change. Pass ``if_match`` (the track's etag) for optimistic "
            "concurrency. " + _APPROVAL_NOTE
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "track_id": {"type": "string"},
                "label": {"type": "string"},
                "type": {"type": "string"},
                "anatomy": {"type": "string"},
                "laterality": {"type": "string", "enum": _LATERALITIES},
                "recist_role": {"type": "string", "enum": _RECIST_ROLES},
                "status": {"type": "string", "enum": _STATUSES},
                "description": {"type": "string"},
                "if_match": {"type": "string"},
            },
            "required": ["track_id"],
        },
    ),
    Tool(
        name="delete_lesion_track",
        annotations=ToolAnnotations(
            title="Delete lesion track",
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=True,
            openWorldHint=False,
        ),
        description=(
            "Soft-delete a lesion track (recoverable via restore_lesion_track). "
            "Pass ``if_match`` for optimistic concurrency and ``reason`` for "
            "the audit trail. " + _APPROVAL_NOTE
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "track_id": {"type": "string"},
                "reason": {"type": "string"},
                "if_match": {"type": "string"},
            },
            "required": ["track_id"],
        },
    ),
    Tool(
        name="restore_lesion_track",
        annotations=ToolAnnotations(
            title="Restore lesion track",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
        description="Restore a soft-deleted lesion track. " + _APPROVAL_NOTE,
        inputSchema={
            "type": "object",
            "properties": {"track_id": {"type": "string"}},
            "required": ["track_id"],
        },
    ),
    Tool(
        name="add_finding_to_track",
        annotations=ToolAnnotations(
            title="Add finding to lesion track",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=False,
        ),
        description=(
            "Link a finding into a lesion track as a timepoint. The finding "
            "must belong to the same patient (cross-patient is rejected). Set "
            "``is_baseline=true`` for the reference timepoint (at most one per "
            "track). ``registration_id`` records which registration aligned "
            "this timepoint to the baseline frame. " + _APPROVAL_NOTE
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "track_id": {"type": "string"},
                "finding_id": {"type": "string"},
                "is_baseline": {"type": "boolean"},
                "registration_id": {"type": "string"},
                "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
            },
            "required": ["track_id", "finding_id"],
        },
    ),
    Tool(
        name="remove_finding_from_track",
        annotations=ToolAnnotations(
            title="Remove finding from lesion track",
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=True,
            openWorldHint=False,
        ),
        description=(
            "Unlink a timepoint from a lesion track (by point id; the "
            "underlying finding is not deleted). " + _APPROVAL_NOTE
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "track_id": {"type": "string"},
                "point_id": {"type": "string"},
            },
            "required": ["track_id", "point_id"],
        },
    ),
]


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def _list_lesion_tracks(args: dict[str, Any]) -> str:
    params: dict[str, Any] = {}
    for k in ("status", "recist_role", "limit"):
        if args.get(k) is not None:
            params[k] = args[k]
    if args.get("include_deleted"):
        params["include_deleted"] = "true"
    try:
        payload = await api_get(
            f"/api/patients/{args['patient_id']}/lesion-tracks", params=params or None
        )
    except httpx.HTTPStatusError as exc:
        return format_http_error(exc)
    return json.dumps(payload, indent=2, ensure_ascii=False)


async def _get_lesion_track(args: dict[str, Any]) -> str:
    try:
        payload = await api_get(f"/api/lesion-tracks/{args['track_id']}")
    except httpx.HTTPStatusError as exc:
        return format_http_error(exc)
    return json.dumps(payload, indent=2, ensure_ascii=False)


async def _get_lesion_trajectory(args: dict[str, Any]) -> str:
    try:
        payload = await api_get(f"/api/lesion-tracks/{args['track_id']}/trajectory")
    except httpx.HTTPStatusError as exc:
        return format_http_error(exc)
    return json.dumps(payload, indent=2, ensure_ascii=False)


async def _get_lesion_track_revisions(args: dict[str, Any]) -> str:
    params = {"limit": int(args.get("limit", 200))}
    try:
        payload = await api_get(f"/api/lesion-tracks/{args['track_id']}/revisions", params=params)
    except httpx.HTTPStatusError as exc:
        return format_http_error(exc)
    return json.dumps(payload, indent=2, ensure_ascii=False)


_CREATE_BODY_KEYS = (
    "label",
    "type",
    "anatomy",
    "laterality",
    "recist_role",
    "status",
    "description",
    "baseline_finding_id",
)


async def _create_lesion_track(args: dict[str, Any]) -> str:
    body = {k: args[k] for k in _CREATE_BODY_KEYS if k in args}
    params: dict[str, Any] = {}
    if args.get("dry_run"):
        params["dry_run"] = "true"
    try:
        payload, _headers = await api_post_with_headers(
            f"/api/patients/{args['patient_id']}/lesion-tracks",
            json=body,
            params=params or None,
            idempotency_key=args.get("idempotency_key"),
        )
    except httpx.HTTPStatusError as exc:
        return format_http_error(exc)
    return json.dumps(payload, indent=2, ensure_ascii=False)


_UPDATE_BODY_KEYS = (
    "label",
    "type",
    "anatomy",
    "laterality",
    "recist_role",
    "status",
    "description",
)


async def _update_lesion_track(args: dict[str, Any]) -> str:
    body = {k: args[k] for k in _UPDATE_BODY_KEYS if k in args}
    try:
        payload, _headers = await api_patch(
            f"/api/lesion-tracks/{args['track_id']}",
            json=body,
            if_match=args.get("if_match"),
        )
    except httpx.HTTPStatusError as exc:
        return format_http_error(exc)
    return json.dumps(payload, indent=2, ensure_ascii=False)


async def _delete_lesion_track(args: dict[str, Any]) -> str:
    params: dict[str, Any] = {}
    if args.get("reason"):
        params["reason"] = args["reason"]
    try:
        code = await api_delete(
            f"/api/lesion-tracks/{args['track_id']}",
            params=params or None,
            if_match=args.get("if_match"),
        )
    except httpx.HTTPStatusError as exc:
        return format_http_error(exc)
    return json.dumps({"status": "deleted", "recoverable": True, "http_status": code})


async def _restore_lesion_track(args: dict[str, Any]) -> str:
    try:
        payload, _headers = await api_post_with_headers(
            f"/api/lesion-tracks/{args['track_id']}/restore"
        )
    except httpx.HTTPStatusError as exc:
        return format_http_error(exc)
    return json.dumps(payload, indent=2, ensure_ascii=False)


async def _add_finding_to_track(args: dict[str, Any]) -> str:
    body: dict[str, Any] = {"finding_id": args["finding_id"]}
    for k in ("is_baseline", "registration_id", "confidence"):
        if args.get(k) is not None:
            body[k] = args[k]
    try:
        payload, _headers = await api_post_with_headers(
            f"/api/lesion-tracks/{args['track_id']}/points", json=body
        )
    except httpx.HTTPStatusError as exc:
        return format_http_error(exc)
    return json.dumps(payload, indent=2, ensure_ascii=False)


async def _remove_finding_from_track(args: dict[str, Any]) -> str:
    try:
        code = await api_delete(f"/api/lesion-tracks/{args['track_id']}/points/{args['point_id']}")
    except httpx.HTTPStatusError as exc:
        return format_http_error(exc)
    return json.dumps({"status": "removed", "http_status": code})


_DISPATCH = {
    "list_lesion_tracks": _list_lesion_tracks,
    "get_lesion_track": _get_lesion_track,
    "get_lesion_trajectory": _get_lesion_trajectory,
    "get_lesion_track_revisions": _get_lesion_track_revisions,
    "create_lesion_track": _create_lesion_track,
    "update_lesion_track": _update_lesion_track,
    "delete_lesion_track": _delete_lesion_track,
    "restore_lesion_track": _restore_lesion_track,
    "add_finding_to_track": _add_finding_to_track,
    "remove_finding_from_track": _remove_finding_from_track,
}


async def handle(name: str, arguments: dict[str, Any]) -> str:
    handler = _DISPATCH.get(name)
    if handler is None:
        return f"Error: unknown tool '{name}' in lesion_tracks module"
    return await handler(arguments)
