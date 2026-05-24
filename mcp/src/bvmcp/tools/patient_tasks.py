"""MCP tools for patient_tasks — operational checklist on the fascicolo.

A ``PatientTask`` is a private operational to-do attached to a
patient (book the TAC, ask the GP for the impegnativa, buy the
medication). Distinct from clinical events: not part of the medical
record, not exported to FSE/HL7, has its own FSM
(``pending → in_progress → done | dropped | snoozed``).

Tools mirror the REST surface 1:1 (``/api/patient-tasks/...`` and
``/api/patients/{id}/tasks``) — see
``backend/src/bvphoenix/api/patient_tasks.py``. Writes require
``tasks:write`` scope and follow the project-wide write conventions:
``Idempotency-Key`` on create + every transition, ``If-Match`` on
update / delete / transition, ``dry_run`` on transitions for
preview-without-persist.

Provenance: every write through these tools is stamped with
``author_kind='agent'`` and the assistant's identity, so the
timeline UI can render an "AI" badge on tasks that the LLM drafted
(memoria ``ai_provenance_must_be_visible``).
"""

from __future__ import annotations

import json
from typing import Any

from mcp.types import Tool

from bvmcp.tools.client import (
    api_delete,
    api_get,
    api_patch,
    api_post_with_headers,
)

# ---------------------------------------------------------------------------
# Shared schema fragments (kept inline; avoids the indirection cost of
# a separate ``schemas.py`` for a moderate amount of duplication).
# ---------------------------------------------------------------------------

_CATEGORY_ENUM = [
    "admin",
    "pharmacy",
    "appointment_prep",
    "transport",
    "communication",
    "personal",
    "other",
]
_PRIORITY_ENUM = ["low", "normal", "high", "urgent"]
_STATUS_ENUM = ["pending", "in_progress", "snoozed", "done", "dropped"]


TOOLS = [
    # ----- Discovery / read -----------------------------------------
    Tool(
        name="list_patient_tasks",
        description=(
            "List operational tasks on a patient's checklist. Tasks are "
            "private to-dos (book the TAC, ask the GP, pharmacy run) "
            "separate from clinical events. Filter by ``statuses`` "
            "(multi: pending / in_progress / snoozed / done / dropped), "
            "by ``category`` (admin / pharmacy / appointment_prep / "
            "transport / communication / personal / other), by ``priority`` "
            "(low / normal / high / urgent), or by a ``due_at`` window. "
            "Default ordering is by ``due_at`` ascending (closest deadline "
            "first), then by creation time. Soft-deleted tasks are hidden "
            "unless ``include_deleted=true``."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "patient_id": {"type": "string"},
                "statuses": {
                    "type": "array",
                    "items": {"type": "string", "enum": _STATUS_ENUM},
                },
                "category": {"type": "string", "enum": _CATEGORY_ENUM},
                "priority": {"type": "string", "enum": _PRIORITY_ENUM},
                "due_from": {"type": "string", "format": "date-time"},
                "due_to": {"type": "string", "format": "date-time"},
                "include_deleted": {"type": "boolean", "default": False},
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 500,
                    "default": 100,
                },
                "offset": {"type": "integer", "minimum": 0, "default": 0},
            },
            "required": ["patient_id"],
        },
    ),
    Tool(
        name="get_patient_task",
        description=(
            "Read one task by id. Returns full metadata: status, due_at, "
            "snooze_until / completed_at when relevant, assigned contact, "
            "phase grouping, related event / document anchors, "
            "reminder_offsets_minutes, author_kind (human vs agent), and "
            "the ``etag`` required for subsequent mutations."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "include_deleted": {"type": "boolean", "default": False},
            },
            "required": ["task_id"],
        },
    ),
    Tool(
        name="find_overdue_tasks",
        description=(
            "Convenience filter: tasks still in pending/in_progress whose "
            "``due_at`` is in the past. Use to prompt the user for a "
            "decision (complete? snooze? drop?). Returns a list ordered "
            "by oldest overdue first."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "patient_id": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 100},
            },
            "required": ["patient_id"],
        },
    ),
    Tool(
        name="find_tasks_due_today",
        description=(
            "Convenience filter: tasks in pending/in_progress whose "
            "``due_at`` falls in the current day (server-local UTC range). "
            "Useful for a daily morning briefing."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "patient_id": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 100},
            },
            "required": ["patient_id"],
        },
    ),
    # ----- Create / update / delete --------------------------------
    Tool(
        name="create_patient_task",
        description=(
            "Create a new operational task for a patient. The new row "
            "lands in ``status='pending'`` regardless of inputs. Use "
            "``due_at`` (ISO-8601 datetime) for a deadline, ``timezone`` "
            "for IANA name, ``priority`` for ordering, ``category`` for "
            "the UI chip. Optional anchors: ``phase_id`` to group under "
            "an existing CarePhase, ``related_event_id`` to tie to a "
            "ClinicalEvent (same-patient, composite FK enforced at the "
            "DB), ``related_document_id`` to anchor on a Document, "
            "``assigned_to_contact_id`` to dispatch reminders to a "
            "PatientContact (caregiver, family). ``reminder_offsets_minutes`` "
            "is a list of negative integers (minutes before due_at) used "
            "by the dispatcher; max 5 entries. ``idempotency_key`` is "
            "mandatory: same key returns the previously-created row."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "patient_id": {"type": "string"},
                "title": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 255,
                    "description": (
                        "Short label, shown on the task chip "
                        "(e.g. 'Prenotare TAC torace', 'Chiedere impegnativa MMG')."
                    ),
                },
                "description": {"type": "string"},
                "category": {
                    "type": "string",
                    "enum": _CATEGORY_ENUM,
                    "default": "other",
                },
                "priority": {
                    "type": "string",
                    "enum": _PRIORITY_ENUM,
                    "default": "normal",
                },
                "due_at": {"type": "string", "format": "date-time"},
                "timezone": {"type": "string", "maxLength": 64},
                "phase_id": {"type": "string"},
                "recurrence_rule": {
                    "type": "string",
                    "maxLength": 512,
                    "description": (
                        "RFC 5545 RRULE for recurring tasks. Stored but "
                        "NOT expanded server-side in this version — a "
                        "follow-up worker materialises occurrences."
                    ),
                },
                "assigned_to_contact_id": {"type": "string"},
                "related_event_id": {"type": "string"},
                "related_document_id": {"type": "string"},
                "labels": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Free-form chip tags (e.g. ['urgente', 'CUP'])."
                    ),
                },
                "links": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "label": {"type": "string"},
                            "url": {"type": "string"},
                        },
                        "required": ["url"],
                    },
                },
                "reminder_offsets_minutes": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "maxItems": 5,
                    "description": (
                        "Negative integers (minutes before due_at). Example: "
                        "[-1440, -60] = 1 day and 1 hour before. Capped at 5 "
                        "entries to bound dispatcher fan-out."
                    ),
                },
                "idempotency_key": {"type": "string", "minLength": 1},
            },
            "required": ["patient_id", "title", "idempotency_key"],
        },
    ),
    Tool(
        name="update_patient_task",
        description=(
            "Patch mutable task metadata. ``status`` and ``patient_id`` "
            "are immutable (use the transition tools to move status). "
            "``etag`` is sent as ``If-Match`` and must match the latest "
            "GET; 412 on mismatch means a concurrent writer landed in "
            "between and the caller should re-read + retry."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "etag": {"type": "string"},
                "patch": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "title": {"type": "string", "minLength": 1, "maxLength": 255},
                        "description": {"type": "string"},
                        "category": {"type": "string", "enum": _CATEGORY_ENUM},
                        "priority": {"type": "string", "enum": _PRIORITY_ENUM},
                        "due_at": {"type": "string", "format": "date-time"},
                        "timezone": {"type": "string", "maxLength": 64},
                        "phase_id": {"type": "string"},
                        "recurrence_rule": {"type": "string", "maxLength": 512},
                        "assigned_to_contact_id": {"type": "string"},
                        "related_event_id": {"type": "string"},
                        "related_document_id": {"type": "string"},
                        "labels": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "links": {
                            "type": "array",
                            "items": {"type": "object"},
                        },
                        "reminder_offsets_minutes": {
                            "type": "array",
                            "items": {"type": "integer"},
                            "maxItems": 5,
                        },
                    },
                },
            },
            "required": ["task_id", "etag", "patch"],
        },
    ),
    Tool(
        name="assign_task_to_contact",
        description=(
            "Convenience write: assign the task to a PatientContact "
            "(caregiver, family, GP). Same-patient enforced at the DB "
            "via composite FK; cross-patient assignment is rejected "
            "with 422 ``cross_patient_reference``. Pass ``contact_id=null`` "
            "to clear the assignment."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "etag": {"type": "string"},
                "contact_id": {"type": ["string", "null"]},
            },
            "required": ["task_id", "etag", "contact_id"],
        },
    ),
    Tool(
        name="delete_patient_task",
        description=(
            "Soft-delete a task (tombstone). Idempotent: deleting an "
            "already-deleted task is a 204 no-op. ``restore_patient_task`` "
            "brings it back. ``etag`` -> ``If-Match``."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "etag": {"type": "string"},
            },
            "required": ["task_id", "etag"],
        },
    ),
    Tool(
        name="restore_patient_task",
        description=(
            "Clear the tombstone on a soft-deleted task. Returns the "
            "restored row. ``etag`` -> ``If-Match`` (use the etag from "
            "a GET with ``include_deleted=true``)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "etag": {"type": "string"},
            },
            "required": ["task_id", "etag"],
        },
    ),
    # ----- FSM transitions -----------------------------------------
    Tool(
        name="start_task",
        description=(
            "``pending`` → ``in_progress``. Mark the task as actively "
            "being worked on. Empty body; the FSM rejects from any other "
            "starting status with 422 ``invalid_transition``."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "etag": {"type": "string"},
                "idempotency_key": {"type": "string", "minLength": 1},
                "dry_run": {"type": "boolean", "default": False},
            },
            "required": ["task_id", "etag", "idempotency_key"],
        },
    ),
    Tool(
        name="snooze_task",
        description=(
            "``pending``/``in_progress`` → ``snoozed``. Requires "
            "``snooze_until`` (ISO-8601 datetime). The task is hidden "
            "from active lists until ``wake_task`` (or until a future "
            "dispatcher worker auto-wakes it; not implemented yet)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "etag": {"type": "string"},
                "snooze_until": {"type": "string", "format": "date-time"},
                "reason": {"type": "string", "maxLength": 255},
                "idempotency_key": {"type": "string", "minLength": 1},
                "dry_run": {"type": "boolean", "default": False},
            },
            "required": ["task_id", "etag", "snooze_until", "idempotency_key"],
        },
    ),
    Tool(
        name="wake_task",
        description=(
            "``snoozed`` → ``pending`` (default) or ``in_progress`` if "
            "``resume_in_progress=true`` (the snooze paused a task that "
            "was already underway)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "etag": {"type": "string"},
                "resume_in_progress": {"type": "boolean", "default": False},
                "idempotency_key": {"type": "string", "minLength": 1},
                "dry_run": {"type": "boolean", "default": False},
            },
            "required": ["task_id", "etag", "idempotency_key"],
        },
    ),
    Tool(
        name="complete_task",
        description=(
            "``pending``/``in_progress`` → ``done``. Records the finish "
            "timestamp (defaults to server now) and an optional note."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "etag": {"type": "string"},
                "completed_at": {"type": "string", "format": "date-time"},
                "note": {"type": "string", "maxLength": 255},
                "idempotency_key": {"type": "string", "minLength": 1},
                "dry_run": {"type": "boolean", "default": False},
            },
            "required": ["task_id", "etag", "idempotency_key"],
        },
    ),
    Tool(
        name="drop_task",
        description=(
            "``pending``/``in_progress`` → ``dropped`` (won't do). "
            "``reason`` is mandatory so the audit chain is informative. "
            "Not terminal: ``reopen_task`` lifts back to ``pending``."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "etag": {"type": "string"},
                "reason": {"type": "string", "minLength": 1, "maxLength": 255},
                "idempotency_key": {"type": "string", "minLength": 1},
                "dry_run": {"type": "boolean", "default": False},
            },
            "required": ["task_id", "etag", "reason", "idempotency_key"],
        },
    ),
    Tool(
        name="reopen_task",
        description=(
            "``done``/``dropped`` → ``pending``. The original "
            "``completed_at`` stays as historical evidence; only the "
            "status moves. Use when a task was prematurely closed."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "etag": {"type": "string"},
                "reason": {"type": "string", "maxLength": 255},
                "idempotency_key": {"type": "string", "minLength": 1},
                "dry_run": {"type": "boolean", "default": False},
            },
            "required": ["task_id", "etag", "idempotency_key"],
        },
    ),
    Tool(
        name="export_task_ics",
        description=(
            "Export a single patient task as a standalone .ics file. "
            "VALARM blocks are emitted by default from the task's "
            "``reminder_offsets_minutes`` so the recipient's calendar "
            "fires local notifications without any server-side push. "
            "Tasks without ``due_at`` (and without ``completed_at`` "
            "for done tasks) cannot be calendarised — the call returns "
            "422 ``task_has_no_anchor`` so the caller can show a "
            "useful error instead of an empty file. Capped at 5 "
            "VALARMs per task. Use this when the agent needs to "
            "deliver a single reminder via email attachment or "
            "calendar invite."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "lang": {
                    "type": "string",
                    "enum": ["it", "en"],
                    "default": "it",
                },
                "with_valarm": {
                    "type": "boolean",
                    "default": True,
                    "description": (
                        "When true (default), emit one VALARM block per offset "
                        "in ``reminder_offsets_minutes``. Set false to ship a "
                        "calendar invite without local alarms."
                    ),
                },
            },
            "required": ["task_id"],
        },
    ),
]


# ---------------------------------------------------------------------------
# Handlers — each maps to one REST endpoint exactly. The dispatcher in
# ``server*.py`` looks up the handler by tool name.
# ---------------------------------------------------------------------------


async def handle(name: str, arguments: dict) -> str:
    if name == "list_patient_tasks":
        params: dict[str, Any] = {}
        for k in ("statuses", "category", "priority", "due_from", "due_to"):
            v = arguments.get(k)
            if v is not None:
                params[k] = v
        params["include_deleted"] = arguments.get("include_deleted", False)
        params["limit"] = arguments.get("limit", 100)
        params["offset"] = arguments.get("offset", 0)
        result = await api_get(
            f"/api/patients/{arguments['patient_id']}/tasks",
            params=params,
        )
        return json.dumps(result, indent=2)

    if name == "get_patient_task":
        params = {"include_deleted": arguments.get("include_deleted", False)}
        result = await api_get(
            f"/api/patient-tasks/{arguments['task_id']}",
            params=params,
        )
        return json.dumps(result, indent=2)

    if name == "find_overdue_tasks":
        # Done client-side: ask the backend for pending+in_progress and
        # let httpx repeat the ``statuses`` key. The backend orders by
        # due_at ASC NULLS LAST, so overdue items appear first naturally.
        # We add a server-side due_to=now to keep the result tight.
        from datetime import UTC, datetime

        params = {
            "statuses": ["pending", "in_progress"],
            "due_to": datetime.now(UTC).isoformat(),
            "limit": arguments.get("limit", 100),
        }
        result = await api_get(
            f"/api/patients/{arguments['patient_id']}/tasks",
            params=params,
        )
        return json.dumps(result, indent=2)

    if name == "find_tasks_due_today":
        from datetime import UTC, datetime, timedelta

        today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        params = {
            "statuses": ["pending", "in_progress"],
            "due_from": today.isoformat(),
            "due_to": (today + timedelta(days=1)).isoformat(),
            "limit": arguments.get("limit", 100),
        }
        result = await api_get(
            f"/api/patients/{arguments['patient_id']}/tasks",
            params=params,
        )
        return json.dumps(result, indent=2)

    if name == "create_patient_task":
        body: dict[str, Any] = {
            "patient_id": arguments["patient_id"],
            "title": arguments["title"],
        }
        for k in (
            "description",
            "category",
            "priority",
            "due_at",
            "timezone",
            "phase_id",
            "recurrence_rule",
            "assigned_to_contact_id",
            "related_event_id",
            "related_document_id",
            "labels",
            "links",
            "reminder_offsets_minutes",
        ):
            v = arguments.get(k)
            if v is not None:
                body[k] = v
        payload, _hdrs = await api_post_with_headers(
            "/api/patient-tasks",
            json=body,
            idempotency_key=arguments["idempotency_key"],
        )
        return json.dumps(payload, indent=2, ensure_ascii=False)

    if name == "update_patient_task":
        payload, hdrs = await api_patch(
            f"/api/patient-tasks/{arguments['task_id']}",
            json=arguments["patch"],
            if_match=arguments["etag"],
        )
        if isinstance(payload, dict):
            new_etag = hdrs.get("etag") or hdrs.get("ETag")
            if new_etag:
                payload = {**payload, "_etag_header": new_etag}
        return json.dumps(payload, indent=2, ensure_ascii=False)

    if name == "assign_task_to_contact":
        # Thin wrapper over update_patient_task with a single-key patch.
        payload, hdrs = await api_patch(
            f"/api/patient-tasks/{arguments['task_id']}",
            json={"assigned_to_contact_id": arguments["contact_id"]},
            if_match=arguments["etag"],
        )
        if isinstance(payload, dict):
            new_etag = hdrs.get("etag") or hdrs.get("ETag")
            if new_etag:
                payload = {**payload, "_etag_header": new_etag}
        return json.dumps(payload, indent=2, ensure_ascii=False)

    if name == "delete_patient_task":
        await api_delete(
            f"/api/patient-tasks/{arguments['task_id']}",
            if_match=arguments["etag"],
        )
        return json.dumps({"deleted": True, "task_id": arguments["task_id"]})

    if name == "restore_patient_task":
        payload, hdrs = await api_post_with_headers(
            f"/api/patient-tasks/{arguments['task_id']}/restore",
            json={},
            if_match=arguments["etag"],
        )
        if isinstance(payload, dict):
            new_etag = hdrs.get("etag") or hdrs.get("ETag")
            if new_etag:
                payload = {**payload, "_etag_header": new_etag}
        return json.dumps(payload, indent=2, ensure_ascii=False)

    # ----- FSM transition handlers -----------------------------------
    # Each verb posts to ``/patient-tasks/{id}/{verb}`` with Idempotency-Key
    # + If-Match + optional dry_run; the response body is the new state of
    # the task (or the dry-run preview).
    transition_verbs = {
        "start_task": "start",
        "snooze_task": "snooze",
        "wake_task": "wake",
        "complete_task": "complete",
        "drop_task": "drop",
        "reopen_task": "reopen",
    }
    if name in transition_verbs:
        verb = transition_verbs[name]
        body: dict[str, Any] = {}
        if name == "snooze_task":
            body["snooze_until"] = arguments["snooze_until"]
            if arguments.get("reason"):
                body["reason"] = arguments["reason"]
        elif name == "wake_task":
            body["resume_in_progress"] = arguments.get("resume_in_progress", False)
        elif name == "complete_task":
            if arguments.get("completed_at"):
                body["completed_at"] = arguments["completed_at"]
            if arguments.get("note"):
                body["note"] = arguments["note"]
        elif name == "drop_task":
            body["reason"] = arguments["reason"]
        elif name == "reopen_task":
            if arguments.get("reason"):
                body["reason"] = arguments["reason"]
        url = f"/api/patient-tasks/{arguments['task_id']}/{verb}"
        if arguments.get("dry_run"):
            url += "?dry_run=true"
        payload, hdrs = await api_post_with_headers(
            url,
            json=body,
            idempotency_key=arguments["idempotency_key"],
            if_match=arguments["etag"],
        )
        if isinstance(payload, dict):
            new_etag = hdrs.get("etag") or hdrs.get("ETag")
            if new_etag:
                payload = {**payload, "_etag_header": new_etag}
        return json.dumps(payload, indent=2, ensure_ascii=False)

    if name == "export_task_ics":
        from bvmcp.tools.client import api_get_bytes

        params: dict[str, Any] = {
            "lang": arguments.get("lang", "it"),
            "with_valarm": "true" if arguments.get("with_valarm", True) else "false",
        }
        ics_bytes, _ctype = await api_get_bytes(
            f"/api/patient-tasks/{arguments['task_id']}/calendar.ics",
            params=params,
        )
        return ics_bytes.decode("utf-8", errors="replace")

    raise ValueError(f"Unknown patient_tasks tool: {name}")


__all__ = ["TOOLS", "handle"]
