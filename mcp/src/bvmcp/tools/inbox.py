"""MCP tools for the patient inbound inbox (GUI parity, fbbf5270 §12).

Mirrors ``/patients/{id}/inbox/*``: capability-address management,
trusted-sender configuration and the review queue (list / inspect /
accept / reject). The decision tools ride the ``agent_capable`` gate of
the ``patient_inbox`` review profile — an authorised assistant may
triage its patients' inboxes, with ``author_kind='agent'`` provenance
stamped server-side on every transition.

Concurrency: every mutation takes the item/address ``etag`` (the
backend enforces If-Match 428/412); the response carries the fresh
etag so chained calls never need a re-fetch. ``dry_run`` on accept
previews the promotion split without transitioning anything.
"""

from __future__ import annotations

import json

from mcp.types import Tool

from bvmcp.tools.client import api_delete, api_get, api_patch, api_post_with_headers

_PATIENT = {
    "type": "string",
    "description": "Patient UUID",
}

TOOLS = [
    Tool(
        name="list_patient_inbox_addresses",
        description=(
            "List a patient's inbound-email capability addresses (active and "
            "revoked), each with its full deliverable form code+patient@domain, "
            "label and etag. The code IS the secret: share an address only with "
            "the intended sender."
        ),
        inputSchema={
            "type": "object",
            "properties": {"patient_id": _PATIENT},
            "required": ["patient_id"],
        },
    ),
    Tool(
        name="create_inbox_address",
        description=(
            "Mint a new inbound-email capability address for the patient "
            "(label it with the intended correspondent, e.g. 'Laboratorio "
            "Rossi'). Each correspondent should get their own address so one "
            "can be revoked without burning the others."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "patient_id": _PATIENT,
                "label": {"type": "string", "description": "Who this address is for"},
            },
            "required": ["patient_id"],
        },
    ),
    Tool(
        name="set_inbox_address_label",
        description="Relabel an inbox address (etag-guarded).",
        inputSchema={
            "type": "object",
            "properties": {
                "patient_id": _PATIENT,
                "address_id": {"type": "string"},
                "label": {"type": "string"},
                "etag": {"type": "string", "description": "Current address etag (If-Match)"},
            },
            "required": ["patient_id", "address_id", "etag"],
        },
    ),
    Tool(
        name="revoke_inbox_address",
        description=(
            "Permanently revoke an inbox address: mail to it bounces from the "
            "next message on. Irreversible (mint a new address instead)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "patient_id": _PATIENT,
                "address_id": {"type": "string"},
                "reason": {"type": "string"},
                "etag": {"type": "string", "description": "Current address etag (If-Match)"},
            },
            "required": ["patient_id", "address_id", "etag"],
        },
    ),
    Tool(
        name="configure_trusted_senders",
        description=(
            "Manage the patient's auto-accept allowlist. action='list' shows "
            "active entries; action='add' trusts a sender address (auto-accept "
            "still requires SPF/DKIM alignment and a fully clean auto-check "
            "pass); action='remove' revokes an entry by id. Trusting a sender "
            "means their mail can enter the record without review — use "
            "deliberately."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "patient_id": _PATIENT,
                "action": {"type": "string", "enum": ["list", "add", "remove"]},
                "sender_email": {
                    "type": "string",
                    "description": "Sender address (required for action='add')",
                },
                "entry_id": {
                    "type": "string",
                    "description": "Allowlist entry id (required for action='remove')",
                },
            },
            "required": ["patient_id", "action"],
        },
    ),
    Tool(
        name="list_inbox_items",
        description=(
            "List the patient's inbox review queue (newest first). Each item is "
            "one received lot (an email's attachments, or a held upload) with "
            "its status, auto-check verdict and component manifest. Filter with "
            "status (e.g. needs_review) and page with limit/offset."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "patient_id": _PATIENT,
                "status": {
                    "type": "string",
                    "enum": [
                        "received",
                        "processing",
                        "needs_review",
                        "blocked",
                        "accepted",
                        "promoting",
                        "promoted",
                        "rejected",
                        "expired",
                        "failed",
                    ],
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                "offset": {"type": "integer", "minimum": 0},
            },
            "required": ["patient_id"],
        },
    ),
    Tool(
        name="get_inbox_item",
        description=(
            "Read one inbox item in full: component manifest (filenames, sizes, "
            "hashes), per-check auto-check results, sender authentication "
            "signals and the current etag needed to decide on it."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "patient_id": _PATIENT,
                "item_id": {"type": "string"},
            },
            "required": ["patient_id", "item_id"],
        },
    ),
    Tool(
        name="accept_inbox_item",
        description=(
            "Accept an inbox item into the fascicolo: DICOM components become "
            "studies, documents are filed (root folder unless folder_id is "
            "given). Components any auto-check hard-blocked are always skipped; "
            "exclude more by name with excluded_components. include_body=true "
            "additionally files the e-mail body text as a document. The ingest "
            "runs asynchronously (status accepted → promoting → promoted). "
            "Set dry_run=true to preview what would be promoted/skipped."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "patient_id": _PATIENT,
                "item_id": {"type": "string"},
                "etag": {"type": "string", "description": "Current item etag (If-Match)"},
                "folder_id": {
                    "type": "string",
                    "description": "Target folder (defaults to the patient root)",
                },
                "excluded_components": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Component names to leave out of the promotion",
                },
                "include_body": {"type": "boolean"},
                "note": {"type": "string", "description": "Review note"},
                "dry_run": {"type": "boolean"},
            },
            "required": ["patient_id", "item_id", "etag"],
        },
    ),
    Tool(
        name="reject_inbox_item",
        description=(
            "Reject an inbox item: the staged blobs are purged and nothing "
            "enters the fascicolo. Give a reason for the audit trail."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "patient_id": _PATIENT,
                "item_id": {"type": "string"},
                "etag": {"type": "string", "description": "Current item etag (If-Match)"},
                "reason": {"type": "string"},
                "dry_run": {"type": "boolean"},
            },
            "required": ["patient_id", "item_id", "etag"],
        },
    ),
]


def _base(patient_id: str) -> str:
    return f"/api/patients/{patient_id}/inbox"


async def handle(name: str, arguments: dict) -> str:
    if name == "list_patient_inbox_addresses":
        result = await api_get(f"{_base(arguments['patient_id'])}/addresses")
        return json.dumps(result, indent=2)

    if name == "create_inbox_address":
        body, _ = await api_post_with_headers(
            f"{_base(arguments['patient_id'])}/addresses",
            json={"label": arguments.get("label")},
        )
        return json.dumps(body, indent=2)

    if name == "set_inbox_address_label":
        body, _ = await api_patch(
            f"{_base(arguments['patient_id'])}/addresses/{arguments['address_id']}",
            json={"label": arguments.get("label")},
            if_match=arguments["etag"],
        )
        return json.dumps(body, indent=2)

    if name == "revoke_inbox_address":
        body, _ = await api_post_with_headers(
            f"{_base(arguments['patient_id'])}/addresses/{arguments['address_id']}/revoke",
            json={"reason": arguments.get("reason")},
            if_match=arguments["etag"],
        )
        return json.dumps(body, indent=2)

    if name == "configure_trusted_senders":
        base = f"{_base(arguments['patient_id'])}/trusted-senders"
        action = arguments["action"]
        if action == "list":
            return json.dumps(await api_get(base), indent=2)
        if action == "add":
            sender = arguments.get("sender_email")
            if not sender:
                raise ValueError("sender_email is required for action='add'")
            body, _ = await api_post_with_headers(base, json={"sender_email": sender})
            return json.dumps(body, indent=2)
        if action == "remove":
            entry_id = arguments.get("entry_id")
            if not entry_id:
                raise ValueError("entry_id is required for action='remove'")
            code = await api_delete(f"{base}/{entry_id}")
            return json.dumps({"removed": code == 204}, indent=2)
        raise ValueError(f"unknown action: {action}")

    if name == "list_inbox_items":
        params: dict = {}
        for key in ("status", "limit", "offset"):
            if arguments.get(key) is not None:
                params[key] = arguments[key]
        result = await api_get(f"{_base(arguments['patient_id'])}/items", params=params or None)
        return json.dumps(result, indent=2)

    if name == "get_inbox_item":
        result = await api_get(f"{_base(arguments['patient_id'])}/items/{arguments['item_id']}")
        return json.dumps(result, indent=2)

    if name == "accept_inbox_item":
        payload = {
            "folder_id": arguments.get("folder_id"),
            "excluded_components": arguments.get("excluded_components") or [],
            "include_body": bool(arguments.get("include_body", False)),
            "note": arguments.get("note"),
        }
        body, _ = await api_post_with_headers(
            f"{_base(arguments['patient_id'])}/items/{arguments['item_id']}/accept",
            json=payload,
            params={"dry_run": bool(arguments.get("dry_run", False))},
            if_match=arguments["etag"],
        )
        return json.dumps(body, indent=2)

    if name == "reject_inbox_item":
        body, _ = await api_post_with_headers(
            f"{_base(arguments['patient_id'])}/items/{arguments['item_id']}/reject",
            json={"reason": arguments.get("reason")},
            params={"dry_run": bool(arguments.get("dry_run", False))},
            if_match=arguments["etag"],
        )
        return json.dumps(body, indent=2)

    raise ValueError(f"unknown tool: {name}")
