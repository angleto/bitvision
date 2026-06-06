"""MCP tool — training cohort labels manifest (annotation overhaul P5).

Builds the de-identified, coded labels manifest for a training cohort
selected by a structured Finding query. The backend gates on training
consent + contribution tier + k-anonymity and re-keys to synthetic ids
(no patient / study / author identifier leaks). Admin / platform-owner
only server-side. The full image+mask byte bundle (a Job-backed ZIP) is a
separate follow-up; this tool returns the labels manifest the bundle is
built around.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
from mcp.types import Tool, ToolAnnotations

from bvmcp.tools.client import api_post_with_headers, format_http_error

TOOLS: list[Tool] = [
    Tool(
        name="export_training_manifest",
        annotations=ToolAnnotations(
            title="Export training labels manifest",
            readOnlyHint=True,
            idempotentHint=True,
            openWorldHint=False,
        ),
        description=(
            "Build the de-identified, coded labels manifest for a training "
            "cohort selected by a structured Finding query (type / anatomy / "
            "morphology / status + diameter / volume / SUV ranges). The "
            "backend gates on training consent + contribution tier (t3/t4) + "
            "k-anonymity and re-keys to synthetic ids — no patient / study / "
            "author identifier is returned. HTTP 422 names the (modality, "
            "body_part) bucket that fell below k_min. Admin / platform-owner "
            "only. (The image + mask byte bundle is a separate export.)"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "type": {"type": "string", "description": "finding type key"},
                "anatomy": {"type": "string", "description": "anatomy site key"},
                "laterality": {
                    "type": "string",
                    "enum": ["left", "right", "bilateral", "midline"],
                },
                "morphology": {"type": "array", "items": {"type": "string"}},
                "status": {
                    "type": "string",
                    "enum": ["candidate", "confirmed", "retracted"],
                },
                "min_diameter_mm": {"type": "number"},
                "max_diameter_mm": {"type": "number"},
                "min_volume_ml": {"type": "number"},
                "min_suv_max": {"type": "number"},
                "scope": {
                    "type": "string",
                    "enum": ["all", "mine", "public"],
                    "default": "all",
                },
                "k_min": {"type": "integer", "default": 5, "minimum": 1, "maximum": 1000},
            },
        },
    ),
]


async def _export_training_manifest(args: dict[str, Any]) -> str:
    body = {k: v for k, v in args.items() if v is not None}
    try:
        payload, _headers = await api_post_with_headers("/api/training-exports/manifest", json=body)
    except httpx.HTTPStatusError as exc:
        return format_http_error(exc)
    return json.dumps(payload, indent=2, ensure_ascii=False)


async def handle(name: str, arguments: dict[str, Any]) -> str:
    if name == "export_training_manifest":
        return await _export_training_manifest(arguments)
    return f"Error: unknown tool '{name}' in training module"
