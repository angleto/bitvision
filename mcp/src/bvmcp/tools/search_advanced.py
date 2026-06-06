"""MCP tools for advanced search: semantic (vector store) and hybrid (RRF 3-way).

- ``semantic_search`` wraps ``GET /api/search/semantic``: free-text query
  encoded via the selected embedding model (biomedclip / minilm) and
  matched against the vector store for the chosen target kind.
- ``search_hybrid`` wraps ``GET /api/search/hybrid``: fuses three signals
  (tag, text, image) using Reciprocal Rank Fusion, with caller-supplied
  weights. Results include a per-item signals breakdown.

HTTP errors are surfaced as readable TextContent (no raise), so the LLM
can recover gracefully if the backend endpoints are not yet deployed.
"""

from __future__ import annotations

import json

import httpx
from mcp.types import Tool

from bvmcp.tools.client import api_get

# NB: ``annotation`` is intentionally absent — the backend
# /search/semantic endpoint does not embed markers/findings (the
# structured query path is search_findings); advertising it here would
# only yield a 422. Findings join semantic search in a later phase.
_SEMANTIC_TARGETS = ("series", "report", "consultation", "document", "patient")
_SEMANTIC_MODELS = ("biomedclip", "minilm")


TOOLS = [
    Tool(
        name="semantic_search",
        description=(
            "Semantic search over the bitvision phoenix vector store. The query "
            "is encoded with the selected embedding model and matched against "
            "vectors for the chosen target kind (series, report, "
            "consultation, document, patient). Use 'biomedclip' for image-aware "
            "clinical content and 'minilm' for fast general-purpose text. "
            "Returns top-k matches with similarity scores and target metadata."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Free-text query to embed and search",
                },
                "target": {
                    "type": "string",
                    "enum": list(_SEMANTIC_TARGETS),
                    "description": "Target kind to search against (default 'series')",
                    "default": "series",
                },
                "model": {
                    "type": "string",
                    "enum": list(_SEMANTIC_MODELS),
                    "description": "Embedding model to use (default 'biomedclip')",
                    "default": "biomedclip",
                },
                "k": {
                    "type": "integer",
                    "description": "Number of matches to return (default 10)",
                    "default": 10,
                },
            },
            "required": ["query"],
        },
    ),
    Tool(
        name="search_hybrid",
        description=(
            "Hybrid search fusing three signals (tag, text, image) via "
            "Reciprocal Rank Fusion. Each item in the result includes a "
            "'signals' breakdown showing which signals contributed and their "
            "individual ranks. Weights are forwarded as-is to the backend in "
            "the form 'tag:<w>,text:<w>,image:<w>'; the default biases toward "
            "tag and image signals."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Free-text query",
                },
                "k": {
                    "type": "integer",
                    "description": "Number of fused results to return (default 20)",
                    "default": 20,
                },
                "weights": {
                    "type": "string",
                    "description": (
                        "Comma-separated signal weights (default 'tag:2,text:1,image:2')"
                    ),
                    "default": "tag:2,text:1,image:2",
                },
                "scope": {
                    "type": "string",
                    "enum": ["all", "public", "mine"],
                    "description": (
                        "Visibility scope. 'public' = OpenData library + studies marked "
                        "is_public. 'mine' = studies owned by the caller. Default 'all'."
                    ),
                },
            },
            "required": ["query"],
        },
    ),
]


async def _call(path: str, params: dict) -> str:
    """GET ``path`` and return JSON text, or a readable HTTP error message."""
    try:
        result = await api_get(path, params=params)
    except httpx.HTTPStatusError as exc:
        resp = exc.response
        try:
            body = json.dumps(resp.json())
        except ValueError:
            body = resp.text
        label = "Endpoint not found" if resp.status_code == 404 else "Backend error"
        return f"{label} ({resp.status_code}): {body}"
    except httpx.HTTPError as exc:
        return f"Error calling backend: {exc}"
    return json.dumps(result, indent=2)


async def handle(name: str, arguments: dict) -> str:
    query = arguments.get("query")
    if not query:
        return "Validation error: 'query' is required"

    if name == "semantic_search":
        return await _call(
            "/api/search/semantic",
            {
                "q": query,
                "target": arguments.get("target", "series"),
                "model": arguments.get("model", "biomedclip"),
                "k": arguments.get("k", 10),
            },
        )

    if name == "search_hybrid":
        params: dict = {
            "q": query,
            "k": arguments.get("k", 20),
            "weights": arguments.get("weights", "tag:2,text:1,image:2"),
        }
        if arguments.get("scope"):
            params["scope"] = arguments["scope"]
        return await _call("/api/search/hybrid", params)

    raise ValueError(f"unknown tool: {name}")
