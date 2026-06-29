"""MCP tools — Folder navigation + tree reshape.

Folders are the **organisational axis**: a Drive-style container for
documents and links, no clinical semantics, no place on the timeline.
A folder groups studies, series, report_contents, markers,
clinical_notes, documents, consultations, and sub-folders together.
The backend's ``/api/folders`` REST endpoints already cover the full
CRUD; this module exposes them to LLM agents as MCP tools so the
assistant can read the existing tree and reshape it (rename, move,
create, delete, attach / detach items).

Sharing is intentionally **out of scope** here: ``POST
/api/folders/:id/share`` mints cross-patient grants and is
structurally human-only; the agent should never decide who else gets
access to a folder of clinical records.

Scope mapping (declared in :mod:`bvmcp.scopes`):

* ``folders:read``  → ``list_folders``, ``get_folder``
* ``folders:write`` → ``create_folder``, ``update_folder``,
  ``delete_folder``, ``add_item_to_folder``, ``remove_item_from_folder``

The agent's bearer is forwarded to the backend; the backend already
enforces ``enforce_agent_patient_scope`` so a token bound to one
patient cannot touch another patient's folders even when this layer
passes the call through.

Conceptual placement: see ``docs/data-model.md §0`` (three-axis
model). For UX details see ``docs/fascicolo-drive-ux.md``; for the
temporal axis (events / phases) see ``docs/care-timeline-phases.md``.
"""

from __future__ import annotations

import json
from typing import Any

from mcp.types import TextContent, Tool

from bvmcp.tools.client import (
    api_delete,
    api_get,
    api_patch,
    api_post_with_headers,
)

_FOLDER_ITEM_KINDS = (
    "study",
    "series",
    "report",
    "annotation",
    "document",
    "consultation",
    "subfolder",
)


TOOLS: list[Tool] = [
    Tool(
        name="list_folders",
        description=(
            "List folders the calling assistant can see. Pass "
            "``patient_id`` to enumerate folders inside a specific "
            "fascicolo (the agent's own token must be bound to that "
            "patient — cross-patient enumeration is refused server-side). "
            "Omit ``patient_id`` to list the user-owned personal "
            "workspace folders. Returns each folder with its "
            "``parent_folder_id`` so the caller can rebuild the tree."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "patient_id": {"type": "string"},
            },
        },
    ),
    Tool(
        name="get_folder",
        description=(
            "Read a single folder plus the polymorphic items it contains "
            "(``study`` / ``series`` / ``report`` / ``annotation`` / "
            "``document`` / ``consultation`` / ``subfolder``). The "
            "response carries ``parent_folder_id`` for navigation and "
            "the ``items`` array with ``(resource_kind, resource_id, "
            "added_at)`` triples."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "folder_id": {"type": "string"},
            },
            "required": ["folder_id"],
        },
    ),
    Tool(
        name="create_folder",
        description=(
            "Create a new folder. Pass ``patient_id`` to drop the folder "
            "inside a fascicolo (agent token must be bound to that "
            "patient); omit it to create a user-owned folder in the "
            "personal workspace. ``parent_folder_id`` nests under "
            "another folder. ``description`` is a free-form navigation "
            "aid (no clinical role).\n"
            "\n"
            "NOT to be confused with: ``create_clinical_event`` (an "
            "event in the patient timeline, e.g. visit, surgery) or "
            "``create_care_phase`` (a semantic grouping of events, "
            "e.g. 'Follow-up post-op'). A folder is just a Drive-style "
            "container for documents and links, with no clinical "
            "semantics and no place on the timeline."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "minLength": 1, "maxLength": 255},
                "patient_id": {"type": "string"},
                "parent_folder_id": {"type": "string"},
                "description": {
                    "type": "string",
                    "maxLength": 500,
                    "description": (
                        "Short hover-preview text (≤ 500 chars). For "
                        "longer clinical commentary use ``narrative_md``."
                    ),
                },
                "narrative_md": {
                    "type": "string",
                    "description": (
                        "Free-form Markdown commentary on the folder's "
                        "clinical context (synthesis of the bundled "
                        "items + outcome + correlations across the "
                        "fascicolo). No length cap. Rendered in the "
                        "folder detail panel; ``description`` stays "
                        "the short tile-hover text."
                    ),
                },
                "clinical_date": {
                    "type": "string",
                    "format": "date",
                    "description": (
                        "Optional ISO-8601 date (YYYY-MM-DD) the folder "
                        "represents in the patient timeline (e.g. "
                        "``2024-09-16`` for a folder grouping the day's "
                        "studies). Distinct from ``created_at`` (the "
                        "system audit timestamp, immutable). The FE "
                        "renders this as the folder's display date when "
                        "set, falling back to ``created_at`` otherwise."
                    ),
                },
            },
            "required": ["name"],
        },
    ),
    Tool(
        name="update_folder",
        description=(
            "Rename, re-parent, re-describe, or re-date a folder. Send "
            "only the fields you want to change (``exclude_unset`` "
            "semantics). Setting ``parent_folder_id`` to itself is "
            'rejected. Pass ``description=""`` or ``null`` to clear '
            "the description. Pass ``clinical_date=null`` to clear the "
            "display date (the FE falls back to ``created_at``). "
            "``created_at`` itself is system audit and remains "
            "immutable — to change the folder's displayed date use "
            "``clinical_date``, not ``created_at``."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "folder_id": {"type": "string"},
                "name": {"type": "string", "minLength": 1, "maxLength": 255},
                "parent_folder_id": {"type": "string"},
                "description": {
                    "type": ["string", "null"],
                    "maxLength": 500,
                    "description": (
                        "Short hover-preview text. ``null`` or empty "
                        "string clears it. Omit to leave alone."
                    ),
                },
                "narrative_md": {
                    "type": ["string", "null"],
                    "description": (
                        "Extended Markdown clinical commentary, no "
                        "length cap. ``null`` or empty clears. Omit "
                        "to leave alone."
                    ),
                },
                "clinical_date": {
                    "type": ["string", "null"],
                    "format": "date",
                    "description": (
                        "ISO-8601 date (YYYY-MM-DD) the folder "
                        "represents in the patient timeline. ``null`` "
                        "clears (FE falls back to ``created_at``). "
                        "Omit the key entirely to leave the field "
                        "alone."
                    ),
                },
            },
            "required": ["folder_id"],
        },
    ),
    Tool(
        name="delete_folder",
        description=(
            "Delete a folder. The cascade drops every ``folder_items`` "
            "row plus any sub-folder (``ON DELETE CASCADE`` on the "
            "self-referential FK). The contained resources themselves "
            "are NOT deleted — only their membership in this folder."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "folder_id": {"type": "string"},
            },
            "required": ["folder_id"],
        },
    ),
    Tool(
        name="add_item_to_folder",
        description=(
            "Attach a resource to a folder. ``resource_kind`` must be "
            "one of ``study`` / ``series`` / ``report`` / ``annotation`` "
            "/ ``document`` / ``consultation`` / ``subfolder``. Idempotent "
            "from the agent's perspective: re-adding the same triple "
            "returns 201 once and is a no-op afterwards."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "folder_id": {"type": "string"},
                "resource_kind": {
                    "type": "string",
                    "enum": list(_FOLDER_ITEM_KINDS),
                },
                "resource_id": {"type": "string"},
            },
            "required": ["folder_id", "resource_kind", "resource_id"],
        },
    ),
    Tool(
        name="remove_item_from_folder",
        description=(
            "Detach a resource from a folder. The resource itself is "
            "untouched — only the membership row is dropped. Idempotent: "
            "removing an absent membership returns 204 without raising."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "folder_id": {"type": "string"},
                "resource_kind": {
                    "type": "string",
                    "enum": list(_FOLDER_ITEM_KINDS),
                },
                "resource_id": {"type": "string"},
            },
            "required": ["folder_id", "resource_kind", "resource_id"],
        },
    ),
]


async def _list_folders(args: dict[str, Any]) -> str:
    params: dict[str, Any] = {}
    if pid := args.get("patient_id"):
        params["patient_id"] = pid
    payload = await api_get("/api/folders", params=params or None)
    return json.dumps(payload, indent=2, ensure_ascii=False)


async def _get_folder(args: dict[str, Any]) -> str:
    folder_id = args["folder_id"]
    payload = await api_get(f"/api/folders/{folder_id}")
    return json.dumps(payload, indent=2, ensure_ascii=False)


async def _create_folder(args: dict[str, Any]) -> str:
    body: dict[str, Any] = {"name": args["name"]}
    for k in (
        "patient_id",
        "parent_folder_id",
        "description",
        "narrative_md",
        "clinical_date",
    ):
        if k in args and args[k] is not None:
            body[k] = args[k]
    payload, _headers = await api_post_with_headers("/api/folders", json=body)
    return json.dumps(payload, indent=2, ensure_ascii=False)


async def _update_folder(args: dict[str, Any]) -> str:
    folder_id = args["folder_id"]
    body: dict[str, Any] = {}
    # ``exclude_unset`` is implicit in the agent contract: only forward
    # keys the caller supplied. ``description``, ``narrative_md`` and
    # ``clinical_date`` all accept an explicit ``null`` / empty value
    # to clear the field, so we deliberately preserve that case.
    for k in (
        "name",
        "parent_folder_id",
        "description",
        "narrative_md",
        "clinical_date",
    ):
        if k in args:
            body[k] = args[k]
    payload, _headers = await api_patch(f"/api/folders/{folder_id}", json=body)
    return json.dumps(payload, indent=2, ensure_ascii=False)


async def _delete_folder(args: dict[str, Any]) -> str:
    folder_id = args["folder_id"]
    code = await api_delete(f"/api/folders/{folder_id}")
    return json.dumps({"status": "deleted", "http_status": code})


async def _add_item_to_folder(args: dict[str, Any]) -> str:
    folder_id = args["folder_id"]
    body = {
        "resource_kind": args["resource_kind"],
        "resource_id": args["resource_id"],
    }
    payload, _headers = await api_post_with_headers(f"/api/folders/{folder_id}/items", json=body)
    return json.dumps(payload, indent=2, ensure_ascii=False)


async def _remove_item_from_folder(args: dict[str, Any]) -> str:
    folder_id = args["folder_id"]
    kind = args["resource_kind"]
    rid = args["resource_id"]
    code = await api_delete(f"/api/folders/{folder_id}/items/{kind}/{rid}")
    return json.dumps({"status": "removed", "http_status": code})


_DISPATCH = {
    "list_folders": _list_folders,
    "get_folder": _get_folder,
    "create_folder": _create_folder,
    "update_folder": _update_folder,
    "delete_folder": _delete_folder,
    "add_item_to_folder": _add_item_to_folder,
    "remove_item_from_folder": _remove_item_from_folder,
}


async def handle(name: str, arguments: dict[str, Any]) -> str | list[TextContent]:
    handler = _DISPATCH.get(name)
    if handler is None:
        return f"Error: unknown tool '{name}' in folders module"
    return await handler(arguments)
