"""MCP tools for study/series/instance access."""

from __future__ import annotations

from mcp.types import Tool

from bvmcp.tools.client import api_get, api_post

TOOLS = [
    Tool(
        name="get_study",
        description=(
            "Get detailed information about a DICOM study including all its series. "
            "Returns study metadata (description, date, modalities, contribution tier) "
            "and a list of series with their modality, body part, and instance counts."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "study_id": {"type": "string", "description": "UUID of the study"},
            },
            "required": ["study_id"],
        },
    ),
    Tool(
        name="get_series",
        description=(
            "Get detailed information about a single DICOM series. "
            "Returns series metadata: modality, body part examined, description, "
            "instance count, and ingestion status."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "series_id": {"type": "string", "description": "UUID of the series"},
            },
            "required": ["series_id"],
        },
    ),
    Tool(
        name="describe_series",
        description=(
            "Generate an LLM-powered clinical description for a DICOM series. "
            "The description is saved as an annotation and used for search indexing. "
            "Optionally provide a hint from the user to guide the description."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "series_id": {"type": "string", "description": "UUID of the series"},
                "hint": {
                    "type": "string",
                    "description": "Optional hint to guide the description (e.g. 'focus on cardiac structures')",
                },
            },
            "required": ["series_id"],
        },
    ),
    Tool(
        name="embed_series",
        description=(
            "Enqueue a background job to generate a BiomedCLIP visual embedding "
            "for a DICOM series. Once complete, the series can be used in similarity "
            "search via the similar_to tool."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "series_id": {"type": "string", "description": "UUID of the series"},
            },
            "required": ["series_id"],
        },
    ),
]


async def handle(name: str, arguments: dict) -> str:
    import json

    if name == "get_study":
        result = await api_get(f"/api/studies/{arguments['study_id']}")
        return json.dumps(result, indent=2)

    if name == "get_series":
        result = await api_get(f"/api/series/{arguments['series_id']}")
        return json.dumps(result, indent=2)

    if name == "describe_series":
        result = await api_post(
            f"/api/series/{arguments['series_id']}/llm/describe",
            json={"hint": arguments.get("hint")},
        )
        return json.dumps(result, indent=2)

    if name == "embed_series":
        result = await api_post(f"/api/series/{arguments['series_id']}/embed")
        return json.dumps(result, indent=2)

    raise ValueError(f"unknown tool: {name}")
