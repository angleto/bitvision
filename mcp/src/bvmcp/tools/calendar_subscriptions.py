"""MCP tools — public iCal calendar-subscription handles.

Parity surface for the GUI at ``/settings/calendar``: every action a
human can take there (mint a public feed URL, list the active ones,
revoke one) is reachable from MCP too, so an operator can run the
platform from the assistant alone (memory ``feedback_mcp_must_be_gui_superset``).

A subscription is a revocable, non-expiring handle whose URL embeds an
HMAC token binding (subscription_id, patient_id). The feed it points at
is the ONLY anonymous-allowed calendar surface — handing the URL out is
exactly as sensitive as a share-link, hence the dedicated
``calendar:subscribe`` scope (sensitive) for the writes; the listing
rides the existing ``calendar:read``.

Backend contract mirrored here:

* ``create_calendar_subscription`` — ``POST
  /api/patients/{pid}/calendar/subscriptions``. Requires an
  ``Idempotency-Key`` (auto-generated per call unless the caller pins
  one to make a retry safe) and supports ``dry_run=true`` (returns the
  would-be handle, persists nothing, no audit).
* ``list_calendar_subscriptions`` — ``GET`` the same collection;
  ``include_revoked`` surfaces killed handles for audit.
* ``revoke_calendar_subscription`` — ``DELETE``; ``purge=true``
  hard-deletes an already-revoked handle. Idempotent (a missing /
  already-revoked handle still returns success).

The backend enforces ``enforce_agent_patient_scope`` + the scope gate
before any mutation, so a leaked token cannot mint a feed for a
patient it was not granted.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
from mcp.types import Tool

from bvmcp.tools.client import (
    api_delete,
    api_get,
    api_post_with_headers,
    format_http_error,
)

_HINT = (
    "a calendar subscription URL is public and revocable but "
    "non-expiring: anyone who holds it can poll the patient calendar "
    "until you revoke it. Pass dry_run=true first to preview the "
    "handle (RBAC + patient scope) without minting a leakable URL."
)


TOOLS = [
    Tool(
        name="create_calendar_subscription",
        description=(
            "Mint a PUBLIC, revocable iCal subscription URL for a "
            "patient so an external calendar app (Google / Apple "
            "Calendar 'add by URL') can poll it with no BitVision "
            "login. Non-expiring by design (calendar apps stop syncing "
            "silently if the feed 404s); kill it with "
            "revoke_calendar_subscription. Returns the handle including "
            "``feed_url`` / ``feed_path``. Use dry_run=true to preview "
            "without minting."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "patient_id": {
                    "type": "string",
                    "description": "UUID of the patient.",
                },
                "label": {
                    "type": "string",
                    "description": (
                        "Optional free-text note shown only to the "
                        "owner (e.g. 'Caregiver — sister'). Never "
                        "appears in the feed itself."
                    ),
                },
                "dry_run": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Preview the would-be handle without persisting it or writing audit."
                    ),
                },
                "idempotency_key": {
                    "type": "string",
                    "description": (
                        "Optional. Pin a key to make a retry of THIS "
                        "exact create safe (same key + body replays the "
                        "first response). Auto-generated when omitted."
                    ),
                },
            },
            "required": ["patient_id"],
        },
    ),
    Tool(
        name="list_calendar_subscriptions",
        description=(
            "List the iCal subscription handles for a patient (active "
            "by default). Pass include_revoked=true to also see killed "
            "ones for audit. Use this before creating a new one to "
            "avoid minting a duplicate public URL."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "patient_id": {
                    "type": "string",
                    "description": "UUID of the patient.",
                },
                "include_revoked": {
                    "type": "boolean",
                    "default": False,
                    "description": "Include revoked handles in the listing.",
                },
            },
            "required": ["patient_id"],
        },
    ),
    Tool(
        name="revoke_calendar_subscription",
        description=(
            "Revoke a patient's iCal subscription handle. Soft by "
            "default (the URL stops working immediately, the row "
            "survives for audit); pass purge=true to hard-delete an "
            "already-revoked handle. Idempotent: revoking a missing / "
            "already-revoked handle still succeeds."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "patient_id": {
                    "type": "string",
                    "description": "UUID of the patient.",
                },
                "subscription_id": {
                    "type": "string",
                    "description": "UUID of the subscription handle to revoke.",
                },
                "purge": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Hard-delete the row instead of soft-revoking. "
                        "Use only on an already-revoked handle."
                    ),
                },
            },
            "required": ["patient_id", "subscription_id"],
        },
    ),
]


async def _create_calendar_subscription(args: dict[str, Any]) -> str:
    patient_id = args["patient_id"]
    body: dict[str, Any] = {}
    if args.get("label") is not None:
        body["label"] = args["label"]
    params: dict[str, Any] = {}
    if args.get("dry_run"):
        params["dry_run"] = "true"
    # The backend mandates Idempotency-Key on this create; auto-mint one
    # per call unless the caller pinned a key to make a retry safe.
    idem = args.get("idempotency_key") or str(uuid.uuid4())
    try:
        payload, _headers = await api_post_with_headers(
            f"/api/patients/{patient_id}/calendar/subscriptions",
            json=body,
            params=params or None,
            idempotency_key=idem,
        )
    except httpx.HTTPStatusError as exc:
        return format_http_error(exc, hint=_HINT)
    return json.dumps(payload, indent=2, ensure_ascii=False)


async def _list_calendar_subscriptions(args: dict[str, Any]) -> str:
    patient_id = args["patient_id"]
    params: dict[str, Any] = {}
    if args.get("include_revoked"):
        params["include_revoked"] = "true"
    try:
        payload = await api_get(
            f"/api/patients/{patient_id}/calendar/subscriptions",
            params=params or None,
        )
    except httpx.HTTPStatusError as exc:
        return format_http_error(exc, hint=_HINT)
    return json.dumps(payload, indent=2, ensure_ascii=False)


async def _revoke_calendar_subscription(args: dict[str, Any]) -> str:
    patient_id = args["patient_id"]
    subscription_id = args["subscription_id"]
    purge = bool(args.get("purge", False))
    path = f"/api/patients/{patient_id}/calendar/subscriptions/{subscription_id}"
    if purge:
        path += "?purge=true"
    try:
        code = await api_delete(path)
    except httpx.HTTPStatusError as exc:
        return format_http_error(exc, hint=_HINT)
    return json.dumps(
        {
            "status": "purged" if purge else "revoked",
            "subscription_id": subscription_id,
            "http_status": code,
        }
    )


_DISPATCH = {
    "create_calendar_subscription": _create_calendar_subscription,
    "list_calendar_subscriptions": _list_calendar_subscriptions,
    "revoke_calendar_subscription": _revoke_calendar_subscription,
}


async def handle(name: str, arguments: dict) -> str:
    fn = _DISPATCH.get(name)
    if fn is None:
        raise ValueError(f"unknown tool: {name}")
    return await fn(arguments or {})


__all__ = ["TOOLS", "handle"]
