"""MCP tools for outbound notifications.

Five tools matching the most common operator + agent flows:

* ``configure_contact_channel`` — turn on / off a channel for a
  contact + record the channel address (email is already on the
  contact row; this tool covers telegram_chat_id, whatsapp_phone,
  webhook_url) and flip the matching consent flag.
* ``list_notification_dispatches`` — audit list per patient.
* ``cancel_pending_dispatch`` — cancel one queued row.
* ``send_test_notification`` — fire a one-shot reminder to a contact
  for smoke-testing the channel without waiting for a scheduled
  reminder. Goes through the same dispatcher so HMAC / template /
  consent paths are exercised.
* ``revoke_consent`` — administrator-side opt-out (not the recipient
  one-click flow, which is handled by the public ``/opt-out`` URL).

Scopes: ``notifications:read`` for list, ``notifications:write`` for
everything else. The catalog is extended in
``mcp/src/bvmcp/scopes.py``.
"""

from __future__ import annotations

import json
from typing import Any

from mcp.types import Tool

from bvmcp.tools.client import api_get, api_post, api_post_with_headers

TOOLS = [
    Tool(
        name="configure_contact_channel",
        description=(
            "Configure one outbound notification channel for a "
            "PatientContact: set the channel address (telegram_chat_id, "
            "whatsapp_phone, webhook_url) and flip the matching consent "
            "flag. Email and ICS share the ``consent_email`` flag + the "
            "existing ``email`` column on the contact row. Subsequent "
            "scheduled reminders honour the new configuration on the "
            "next dispatcher tick (no immediate re-fire of past rows)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "patient_id": {"type": "string"},
                "contact_id": {"type": "string"},
                "channel": {
                    "type": "string",
                    "enum": [
                        "email",
                        "webhook_telegram",
                        "webhook_whatsapp",
                        "webhook_generic",
                        "ics_attachment",
                    ],
                },
                "enabled": {"type": "boolean", "default": True},
                "channel_address": {
                    "type": "string",
                    "description": (
                        "Address for the channel: email goes to the contact's "
                        "``email`` column (don't pass it here unless changing); "
                        "telegram → chat_id, whatsapp → E.164 phone, "
                        "webhook_generic → HTTPS URL. Pass empty string to clear."
                    ),
                },
                "preferred_locale": {
                    "type": "string",
                    "enum": ["it", "en"],
                    "default": "it",
                },
            },
            "required": ["patient_id", "contact_id", "channel"],
        },
    ),
    Tool(
        name="list_notification_dispatches",
        description=(
            "List scheduled / sent / cancelled notification dispatches "
            "for a patient. Useful for the operator (or her agent) to "
            "see what's queued for an upcoming TAC, audit past delivery "
            "failures, or confirm an opt-out cancellation took effect. "
            "Filter by ``statuses`` (multi). Most recent first."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "patient_id": {"type": "string"},
                "statuses": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": ["pending", "sent", "failed", "dead_letter", "cancelled"],
                    },
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 200},
                "offset": {"type": "integer", "minimum": 0, "default": 0},
            },
            "required": ["patient_id"],
        },
    ),
    Tool(
        name="cancel_pending_dispatch",
        description=(
            "Cancel one queued notification dispatch. Idempotent: a "
            "row already terminal (sent / cancelled / dead_letter) is "
            "returned as-is. Use to revoke a stray reminder that no "
            "longer makes sense after a manual reschedule outside the "
            "calendar (e.g. the patient confirmed by phone)."
        ),
        inputSchema={
            "type": "object",
            "properties": {"dispatch_id": {"type": "string"}},
            "required": ["dispatch_id"],
        },
    ),
    Tool(
        name="send_test_notification",
        description=(
            "Fire a one-shot test notification to a contact, bypassing "
            "the scheduler. Goes through the same dispatcher service "
            "(consent / template / channel registry) so the smoke test "
            "exercises every path a real reminder would touch. The "
            "subject line is prefixed with ``[TEST]`` so the recipient "
            "knows it's not a real appointment reminder. Returns the "
            "newly-created dispatch row so the caller can poll its "
            "status. NOTE: in this sprint the test send is implemented "
            "as a scheduled dispatch at ``now()`` rather than a direct "
            "send, so the cron picks it up within the next 5 minutes."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "patient_id": {"type": "string"},
                "contact_id": {"type": "string"},
                "channel": {
                    "type": "string",
                    "enum": [
                        "email",
                        "webhook_telegram",
                        "webhook_whatsapp",
                        "webhook_generic",
                        "ics_attachment",
                    ],
                },
            },
            "required": ["patient_id", "contact_id", "channel"],
        },
    ),
    Tool(
        name="revoke_consent",
        description=(
            "Administrator-side opt-out: flip the per-channel consent "
            "flag to false for a contact. Different from the public "
            "single-click opt-out URL (which the recipient triggers "
            "from an email footer). Use this when the operator wants "
            "to stop notifications without waiting for the recipient. "
            "``channel='all'`` flips the umbrella ``consent_to_contact`` "
            "and revokes every channel."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "patient_id": {"type": "string"},
                "contact_id": {"type": "string"},
                "channel": {
                    "type": "string",
                    "enum": [
                        "all",
                        "email",
                        "webhook_telegram",
                        "webhook_whatsapp",
                        "webhook_generic",
                        "ics_attachment",
                    ],
                },
            },
            "required": ["patient_id", "contact_id", "channel"],
        },
    ),
    Tool(
        name="start_telegram_link",
        description=(
            "Mint a one-shot linking code for the contact and return "
            "the Telegram deep-link URL ``https://t.me/<bot>?start="
            "<code>``. Surface this to the recipient (button + QR + "
            "plain-text fallback). The recipient opens the link, "
            "sends ``/start <code>`` to the bot, and the bot webhook "
            "binds the resolved chat_id to the contact. Codes are "
            "single-use and expire after 15 minutes."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "patient_id": {"type": "string"},
                "contact_id": {"type": "string"},
            },
            "required": ["patient_id", "contact_id"],
        },
    ),
    Tool(
        name="check_telegram_link",
        description=(
            "Poll the Telegram binding status for a contact. Returns "
            "``status`` = one of ``pending`` (a live code is awaiting "
            "redemption), ``linked`` (chat_id is set, ready to "
            "receive), ``expired`` (last code aged out without "
            "redemption), ``none`` (no link attempt yet). Useful in a "
            "loop after ``start_telegram_link`` to confirm the user "
            "completed the dance."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "patient_id": {"type": "string"},
                "contact_id": {"type": "string"},
            },
            "required": ["patient_id", "contact_id"],
        },
    ),
    Tool(
        name="unlink_telegram",
        description=(
            "Clear the Telegram binding on a contact. Useful when the "
            "recipient changes phone or asks to stop receiving "
            "notifications on Telegram while keeping email. Cancels "
            "any queued telegram dispatches at the same time so stale "
            "chat_ids don't fire."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "patient_id": {"type": "string"},
                "contact_id": {"type": "string"},
            },
            "required": ["patient_id", "contact_id"],
        },
    ),
]


def _consent_field_for(channel: str) -> str:
    if channel in ("email", "ics_attachment"):
        return "consent_email"
    if channel == "webhook_telegram":
        return "consent_telegram"
    if channel == "webhook_whatsapp":
        return "consent_whatsapp"
    if channel == "webhook_generic":
        return "consent_webhook"
    return ""


def _address_field_for(channel: str) -> str | None:
    if channel == "webhook_telegram":
        return "telegram_chat_id"
    if channel == "webhook_whatsapp":
        return "whatsapp_phone"
    if channel == "webhook_generic":
        return "webhook_url"
    return None


async def _patch_contact(patient_id: str, contact_id: str, body: dict) -> dict:
    """PatientContact is patched via the patients API. The route
    ``PATCH /api/patients/{pid}/contacts/{cid}`` covers the column set
    we touch here; if the server-side surface diverges we'll wire a
    dedicated endpoint later."""
    payload, _hdrs = await api_post_with_headers(
        f"/api/patients/{patient_id}/contacts/{contact_id}",
        json=body,
    )
    return payload if isinstance(payload, dict) else {}


async def handle(name: str, arguments: dict) -> str:
    if name == "list_notification_dispatches":
        params: dict[str, Any] = {}
        for k in ("statuses", "limit", "offset"):
            v = arguments.get(k)
            if v is not None:
                params[k] = v
        result = await api_get(
            f"/api/patients/{arguments['patient_id']}/notification-dispatches",
            params=params or None,
        )
        return json.dumps(result, indent=2)

    if name == "cancel_pending_dispatch":
        result = await api_post(
            f"/api/notification-dispatches/{arguments['dispatch_id']}/cancel",
            json={},
        )
        return json.dumps(result, indent=2)

    if name == "configure_contact_channel":
        # The contact patch endpoint accepts the same shape as the
        # underlying PatientContact columns. Build a partial patch
        # touching only the address + consent flag we care about.
        body: dict[str, Any] = {}
        if (locale := arguments.get("preferred_locale")) is not None:
            body["preferred_locale"] = locale
        channel = arguments["channel"]
        enabled = bool(arguments.get("enabled", True))
        consent_field = _consent_field_for(channel)
        if consent_field:
            body[consent_field] = enabled
        address_field = _address_field_for(channel)
        if address_field:
            addr = arguments.get("channel_address")
            if addr is not None:
                body[address_field] = addr or None
        # Preferred channels list: when enabling, ensure the channel
        # appears in the list (idempotent). We use a server-side hint
        # field "append_channel" that the backend understands; if not,
        # fall back to a PATCH on the JSONB column.
        if enabled and channel not in ("email", "ics_attachment"):
            body["append_preferred_channel"] = channel
        result, _hdrs = await api_post_with_headers(
            f"/api/patients/{arguments['patient_id']}/contacts/{arguments['contact_id']}/configure-channel",
            json=body,
        )
        return json.dumps(result, indent=2, ensure_ascii=False)

    if name == "revoke_consent":
        body = {"channel": arguments["channel"]}
        result, _hdrs = await api_post_with_headers(
            f"/api/patients/{arguments['patient_id']}/contacts/{arguments['contact_id']}/revoke-consent",
            json=body,
        )
        return json.dumps(result, indent=2, ensure_ascii=False)

    if name == "send_test_notification":
        body = {
            "contact_id": arguments["contact_id"],
            "channel": arguments["channel"],
        }
        result, _hdrs = await api_post_with_headers(
            f"/api/patients/{arguments['patient_id']}/notifications/test",
            json=body,
        )
        return json.dumps(result, indent=2, ensure_ascii=False)

    if name == "start_telegram_link":
        result, _hdrs = await api_post_with_headers(
            f"/api/patients/{arguments['patient_id']}/contacts/{arguments['contact_id']}/telegram-link/start",
            json={},
        )
        return json.dumps(result, indent=2, ensure_ascii=False)

    if name == "check_telegram_link":
        result = await api_get(
            f"/api/patients/{arguments['patient_id']}/contacts/{arguments['contact_id']}/telegram-link/status",
        )
        return json.dumps(result, indent=2, ensure_ascii=False)

    if name == "unlink_telegram":
        result, _hdrs = await api_post_with_headers(
            f"/api/patients/{arguments['patient_id']}/contacts/{arguments['contact_id']}/telegram-link/unlink",
            json={},
        )
        return json.dumps(result, indent=2, ensure_ascii=False)

    raise ValueError(f"Unknown notifications tool: {name}")


__all__ = ["TOOLS", "handle"]
