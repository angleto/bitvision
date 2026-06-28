"""MCP tools for search and similarity."""

from __future__ import annotations

from mcp.types import Tool

from bvmcp.tools.client import api_get

TOOLS = [
    Tool(
        name="search_studies",
        description=(
            "Search for DICOM studies by text query, modality, body part, date range, "
            "and tags. Returns paginated results with study metadata. "
            "The text query searches study and series descriptions using full-text search. "
            "Use this as the primary discovery tool for finding relevant imaging data."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "q": {
                    "type": "string",
                    "description": "Free-text search query (e.g. 'chest CT pneumonia')",
                },
                "modality": {
                    "type": "string",
                    "description": "Filter by DICOM modality code (CT, MR, CR, US, DX, etc.)",
                },
                "body_part": {
                    "type": "string",
                    "description": "Filter by body part examined (e.g. 'chest', 'head', 'abdomen')",
                },
                "date_from": {
                    "type": "string",
                    "description": "Start date filter (YYYY-MM-DD)",
                },
                "date_to": {
                    "type": "string",
                    "description": "End date filter (YYYY-MM-DD)",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results to return (1-200, default 20)",
                    "default": 20,
                },
                "offset": {
                    "type": "integer",
                    "description": "Pagination offset (default 0). Page with limit+offset.",
                    "default": 0,
                },
                "facets": {
                    "type": "boolean",
                    "description": (
                        "If true, also return per-field counts (modality / body_part / "
                        "year / top tags) over the filtered set, for exploratory drill-down."
                    ),
                    "default": False,
                },
                "scope": {
                    "type": "string",
                    "enum": ["all", "public", "mine"],
                    "description": (
                        "Visibility scope. 'public' = OpenData library + studies marked "
                        "is_public. 'mine' = studies owned by the caller. Default 'all' "
                        "= everything the caller can read."
                    ),
                },
                "tag": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Repeatable. Format 'namespace:value' (e.g. 'anatomy:lung'). "
                        "Studies must match every tag passed (AND semantics)."
                    ),
                },
            },
        },
    ),
    Tool(
        name="similar_to",
        description=(
            "Find visually similar studies using BiomedCLIP image embeddings. "
            "Provide a series_id or study_id as the query target. Returns studies "
            "ranked by visual similarity score (1.0 = identical, 0.0 = completely different). "
            "Requires that the target series has been embedded (use embed_series first)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "target_id": {
                    "type": "string",
                    "description": "UUID of a series or study to find similar cases for",
                },
                "k": {
                    "type": "integer",
                    "description": "Number of similar studies to return (1-100, default 10)",
                    "default": 10,
                },
                "modality": {
                    "type": "string",
                    "description": "Optionally filter results by modality (CT, MR, etc.)",
                },
                "scope": {
                    "type": "string",
                    "enum": ["all", "public", "mine", "shared"],
                    "description": (
                        "Visibility scope (UX narrowing only — never widens what you may "
                        "see). 'public' = OpenData + is_public; 'mine' = owned by you; "
                        "'shared' = shared with you via a grant; default 'all'."
                    ),
                },
                "diversify": {
                    "type": "boolean",
                    "description": (
                        "If true, MMR re-rank for visual diversity instead of returning "
                        "a cluster of near-identical studies."
                    ),
                    "default": False,
                },
            },
            "required": ["target_id"],
        },
    ),
]


async def handle(name: str, arguments: dict) -> str:
    import json

    if name == "search_studies":
        params: dict = {}
        if arguments.get("q"):
            params["q"] = arguments["q"]
        if arguments.get("modality"):
            params["modality"] = arguments["modality"]
        if arguments.get("body_part"):
            params["body_part"] = arguments["body_part"]
        if arguments.get("date_from"):
            params["date_from"] = arguments["date_from"]
        if arguments.get("date_to"):
            params["date_to"] = arguments["date_to"]
        if arguments.get("scope"):
            params["scope"] = arguments["scope"]
        if arguments.get("tag"):
            # httpx / urllib3 will repeat the key for list values, which
            # is exactly what FastAPI's Query(list[str]) expects.
            params["tag"] = arguments["tag"]
        params["limit"] = arguments.get("limit", 20)
        if arguments.get("offset"):
            params["offset"] = arguments["offset"]
        if arguments.get("facets"):
            params["facets"] = True
        result = await api_get("/api/search", params=params)
        return json.dumps(result, indent=2)

    if name == "similar_to":
        params = {"k": arguments.get("k", 10)}
        if arguments.get("modality"):
            params["modality"] = arguments["modality"]
        if arguments.get("scope"):
            params["scope"] = arguments["scope"]
        if arguments.get("diversify"):
            params["diversify"] = True
        result = await api_get(
            f"/api/similar-to/{arguments['target_id']}",
            params=params,
        )
        return json.dumps(result, indent=2)

    raise ValueError(f"unknown tool: {name}")
