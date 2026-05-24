"""Sprint 4 MCP tool: lab values time-series across a fascicolo."""

from __future__ import annotations

import json
from typing import Any

from mcp.types import TextContent, Tool

from bvmcp.tools.client import api_get

TOOLS: list[Tool] = [
    Tool(
        name="get_lab_timeseries",
        description=(
            "Aggregate lab values for a single analyte across every cached "
            "extractor output for the patient. The response carries the "
            "raw points sorted chronologically and a ``trend`` summary "
            "(direction up/down/stable, delta, %% delta) — direction is "
            "``unknown`` when fewer than 3 points are available."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "patient_id": {"type": "string"},
                "analyte": {
                    "type": "string",
                    "description": "Analyte substring (case-insensitive).",
                },
                "since": {
                    "type": "string",
                    "description": "ISO date — only include points on/after.",
                },
                "limit": {
                    "type": "integer",
                    "default": 100,
                    "minimum": 1,
                    "maximum": 500,
                },
            },
            "required": ["patient_id", "analyte"],
        },
    ),
]


async def _get_lab_timeseries(args: dict[str, Any]) -> str:
    patient_id = args["patient_id"]
    params: dict[str, Any] = {"analyte": args["analyte"]}
    if args.get("since"):
        params["since"] = args["since"]
    if args.get("limit"):
        params["limit"] = args["limit"]
    payload = await api_get(f"/api/patients/{patient_id}/labs", params=params)
    return json.dumps(payload, indent=2, ensure_ascii=False)


_DISPATCH = {
    "get_lab_timeseries": _get_lab_timeseries,
}


async def handle(name: str, arguments: dict[str, Any]) -> str | list[TextContent]:
    handler = _DISPATCH.get(name)
    if handler is None:
        return f"Error: unknown tool '{name}' in labs module"
    return await handler(arguments)
