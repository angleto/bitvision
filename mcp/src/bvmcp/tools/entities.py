"""Sprint 4 MCP tool: extract / read clinical entities (ADR 0008).

Surfaced tool: ``extract_document_entities``.

Reads from the cache (``GET .../entities``); when ``trigger=true`` the
API runs the rule-based extractor inline (or async via worker for
long texts) and returns the new payload.
"""

from __future__ import annotations

import json
from typing import Any

from mcp.types import TextContent, Tool

from bvmcp.tools.client import api_get, api_post_with_headers

TOOLS: list[Tool] = [
    Tool(
        name="extract_document_entities",
        description=(
            "Return clinical entities extracted from a document's OCR text "
            "(rule-based v0, ADR 0008). With ``trigger=true`` the API runs "
            "the extractor and caches the result; without it the call only "
            "reads the cache (404 'entities_cache_miss' when empty)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "patient_id": {"type": "string"},
                "document_id": {"type": "string"},
                "extractor_version": {
                    "type": "string",
                    "description": "Optional cache pin; default = latest.",
                },
                "trigger": {
                    "type": "boolean",
                    "default": False,
                },
                "force": {
                    "type": "boolean",
                    "default": False,
                    "description": "Re-run the extractor even on cache hit.",
                },
                "inline": {
                    "type": "boolean",
                    "default": True,
                    "description": (
                        "When false the API enqueues an Arq job and returns 202 with X-Job-Id."
                    ),
                },
                "idempotency_key": {"type": "string"},
            },
            "required": ["patient_id", "document_id"],
        },
    ),
]


async def _extract_document_entities(args: dict[str, Any]) -> str:
    patient_id = args["patient_id"]
    document_id = args["document_id"]
    trigger = bool(args.get("trigger", False))
    if not trigger:
        params: dict[str, Any] = {}
        if args.get("extractor_version"):
            params["extractor_version"] = args["extractor_version"]
        try:
            payload = await api_get(
                f"/api/patients/{patient_id}/documents/{document_id}/entities",
                params=params or None,
            )
            return json.dumps(payload, indent=2, ensure_ascii=False)
        except Exception:
            return json.dumps(
                {
                    "status": "entities_cache_miss",
                    "hint": "set trigger=true to populate the cache",
                }
            )
    body = {
        "force": bool(args.get("force", False)),
        "inline": bool(args.get("inline", True)),
    }
    payload, headers = await api_post_with_headers(
        f"/api/patients/{patient_id}/documents/{document_id}/entities",
        json=body,
        idempotency_key=args.get("idempotency_key"),
    )
    out: dict[str, Any] = payload if isinstance(payload, dict) else {"data": payload}
    job_id = headers.get("x-job-id") or headers.get("X-Job-Id")
    if job_id and not out.get("job_id"):
        out["job_id"] = job_id
    return json.dumps(out, indent=2, ensure_ascii=False)


_DISPATCH = {
    "extract_document_entities": _extract_document_entities,
}


async def handle(name: str, arguments: dict[str, Any]) -> str | list[TextContent]:
    handler = _DISPATCH.get(name)
    if handler is None:
        return f"Error: unknown tool '{name}' in entities module"
    return await handler(arguments)
