"""MCP tool for the patient consent ledger.

Read-only mirror of ``GET /api/gdpr/consent-ledger``: the caller's
append-only grant/revoke history with point-in-time proof. Keeps the MCP
surface a superset of the GUI (the privacy page shows the same ledger).
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlencode

from mcp.types import Tool

from bvmcp.tools.client import api_get

TOOLS = [
    Tool(
        name="get_consent_ledger",
        description=(
            "Return the caller's append-only consent ledger: every grant and "
            "revoke event (most recent first) for account-level GDPR consents "
            "(terms, privacy, research use, AI model training, ...) and for "
            "per-study training opt-ins (tiers T3 / T4), plus the current state "
            "and the currently-active study consents. Derived from the "
            "authoritative consent records that gate data use, so it cannot "
            "drift from what actually governs processing. Pass 'as_of' "
            "(ISO-8601) for point-in-time proof of what was in effect at that "
            "instant (GDPR Art. 7(1)). Self-scoped: always the calling "
            "subject's own consents — no patient id, no PHI pixels."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "as_of": {
                    "type": "string",
                    "description": (
                        "Optional ISO-8601 timestamp. When given, the response "
                        "includes the consent state in effect at that instant."
                    ),
                },
            },
        },
    ),
]


async def handle(name: str, arguments: dict[str, Any]) -> str:
    if name != "get_consent_ledger":
        return f"Error: unknown tool '{name}' in consent module"
    args = arguments or {}
    path = "/api/gdpr/consent-ledger"
    as_of = args.get("as_of")
    if as_of:
        path = f"{path}?{urlencode({'as_of': str(as_of)})}"
    result = await api_get(path)
    return json.dumps(result, indent=2)


__all__ = ["TOOLS", "handle"]
