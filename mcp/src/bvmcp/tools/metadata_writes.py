"""Sprint 3.5 MCP tools — agent-driven tag + metadata writes.

The platform requires LLM agents to be able to *adjust* classifications
across the corpus, not just read them. This module exposes the
write-side surface for studies / series / tags:

* ``add_tag_to_study`` — single-tag add (uses the existing
  ``POST /api/tags`` endpoint).
* ``remove_tag_from_study`` — delete by tag id.
* ``replace_study_tags`` — bulk manifest. Uses the new
  ``PATCH /api/studies/:sid/tags`` endpoint with ``mode=add|replace|remove``.
* ``update_study_metadata`` — restricted to ``study_description``.
* ``update_series_metadata`` — ``series_description``,
  ``body_part_examined``, ``modality_corrected``.

Granular OAuth scopes (Sprint 3.5 §"Scope catalog extension"):

* ``tags:write`` for the first three tools.
* ``studies:write_metadata`` for ``update_study_metadata`` plus the
  bulk-tag tool when targeting study-level tags.
* ``series:write_metadata`` for ``update_series_metadata``.

Scope enforcement happens backend-side (auth deps + per-endpoint
``require_user``). The MCP layer simply forwards the JWT — see
``concurrent_work_sr_pr.md`` and the parent commit chain.
"""

from __future__ import annotations

import json
from typing import Any

from mcp.types import TextContent, Tool

from bvmcp.tools.client import (
    api_delete,
    api_patch,
    api_post_with_headers,
)

TOOLS: list[Tool] = [
    Tool(
        name="add_tag_to_study",
        description=(
            "Add a single tag to a study, series, or instance. Tags are "
            "(namespace, value) pairs; ``manual`` source is recorded so "
            "the autotag worker won't overwrite the human/agent intent. "
            "Idempotent — re-posting the same triple returns the existing "
            "tag without creating a duplicate.\n"
            "\n"
            "Tags are the cross-cutting axis of the data model: they "
            "label content for search and filtering without claiming a "
            "place on the timeline (that is what ``create_clinical_event`` "
            "is for) or in a folder (that is ``create_folder``). Picking "
            "the right axis: a 2024 study goes onto the ``Folder`` 2024 "
            "(organisational); the surgery on 2024-07-29 is a "
            "``ClinicalEvent`` (temporal); ``anatomy:liver`` is a tag "
            "(searchable). Today the schema accepts tags only on imaging "
            "targets (study, series, instance, dataset). See "
            "``docs/data-model.md §0`` and ``docs/tag-taxonomy.md``."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "target_kind": {
                    "type": "string",
                    "enum": ["study", "series", "instance"],
                    "default": "study",
                },
                "target_id": {"type": "string"},
                "namespace": {"type": "string"},
                "value": {"type": "string"},
            },
            "required": ["target_id", "namespace", "value"],
        },
    ),
    Tool(
        name="remove_tag_from_study",
        description=(
            "Remove a single tag by ``tag_id``. The agent is expected to "
            "first list tags via ``list_tags_for_target`` to discover the "
            "id."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "tag_id": {"type": "string"},
            },
            "required": ["tag_id"],
        },
    ),
    Tool(
        name="replace_study_tags",
        description=(
            "Bulk-update tags on a study. ``mode=add`` upserts every "
            "entry; ``mode=replace`` aligns the study's manual tags to "
            "the manifest (entries missing from the manifest are "
            "removed); ``mode=remove`` removes every entry. Auto / "
            "imported tags are never touched. ``dry_run=true`` returns "
            "the diff without committing."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "study_id": {"type": "string"},
                "items": {
                    "type": "array",
                    "maxItems": 200,
                    "items": {
                        "type": "object",
                        "properties": {
                            "namespace": {"type": "string"},
                            "value": {"type": "string"},
                        },
                        "required": ["namespace", "value"],
                    },
                },
                "mode": {
                    "type": "string",
                    "enum": ["add", "replace", "remove"],
                    "default": "add",
                },
                "dry_run": {"type": "boolean", "default": False},
                "idempotency_key": {"type": "string"},
            },
            "required": ["study_id", "items"],
        },
    ),
    Tool(
        name="update_study_metadata",
        description=(
            "Edit a study's safe descriptive fields. Today: "
            "``study_description`` only. DICOM authoritative fields "
            "(UIDs, modality acquired by the scanner) stay read-only. "
            "Requires ownership of the study."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "study_id": {"type": "string"},
                "study_description": {"type": "string", "maxLength": 2048},
            },
            "required": ["study_id"],
        },
    ),
    Tool(
        name="update_series_metadata",
        description=(
            "Edit a series' descriptive fields. ``series_description`` "
            "(free-form), ``body_part_examined`` (cap 64 chars), "
            "``modality_corrected`` (recorded as a tag in the "
            "``modality_corrected`` namespace — search picks it up "
            "without overwriting the DICOM-authoritative ``modality``)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "series_id": {"type": "string"},
                "series_description": {"type": "string", "maxLength": 2048},
                "body_part_examined": {"type": "string", "maxLength": 64},
                "modality_corrected": {"type": "string", "maxLength": 16},
            },
            "required": ["series_id"],
        },
    ),
]


async def _add_tag_to_study(args: dict[str, Any]) -> str:
    body = {
        "target_kind": args.get("target_kind", "study"),
        "target_id": args["target_id"],
        "namespace": args["namespace"],
        "value": args["value"],
    }
    payload, _headers = await api_post_with_headers("/api/tags", json=body)
    return json.dumps(payload, indent=2, ensure_ascii=False)


async def _remove_tag_from_study(args: dict[str, Any]) -> str:
    tag_id = args["tag_id"]
    code = await api_delete(f"/api/tags/{tag_id}")
    return json.dumps({"status": "deleted", "http_status": code})


async def _replace_study_tags(args: dict[str, Any]) -> str:
    study_id = args["study_id"]
    body = {
        "items": args.get("items") or [],
        "mode": args.get("mode", "add"),
    }
    params: dict[str, Any] = {}
    if args.get("dry_run"):
        params["dry_run"] = "true"
    payload, _headers = await api_patch(
        f"/api/studies/{study_id}/tags",
        json=body,
        params=params or None,
        idempotency_key=args.get("idempotency_key"),
    )
    return json.dumps(payload, indent=2, ensure_ascii=False)


async def _update_study_metadata(args: dict[str, Any]) -> str:
    study_id = args["study_id"]
    body: dict[str, Any] = {}
    for k in ("study_description",):
        if k in args:
            body[k] = args[k]
    payload, _headers = await api_patch(f"/api/studies/{study_id}", json=body)
    return json.dumps(payload, indent=2, ensure_ascii=False)


async def _update_series_metadata(args: dict[str, Any]) -> str:
    series_id = args["series_id"]
    body: dict[str, Any] = {}
    for k in ("series_description", "body_part_examined", "modality_corrected"):
        if k in args:
            body[k] = args[k]
    payload, _headers = await api_patch(f"/api/series/{series_id}", json=body)
    return json.dumps(payload, indent=2, ensure_ascii=False)


_DISPATCH = {
    "add_tag_to_study": _add_tag_to_study,
    "remove_tag_from_study": _remove_tag_from_study,
    "replace_study_tags": _replace_study_tags,
    "update_study_metadata": _update_study_metadata,
    "update_series_metadata": _update_series_metadata,
}


async def handle(name: str, arguments: dict[str, Any]) -> str | list[TextContent]:
    handler = _DISPATCH.get(name)
    if handler is None:
        return f"Error: unknown tool '{name}' in metadata_writes module"
    return await handler(arguments)
