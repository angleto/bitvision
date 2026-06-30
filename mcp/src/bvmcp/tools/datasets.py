"""MCP tools for the public dataset catalog (GUI parity).

Mirrors the public ``/catalog/*`` surface: browse the OpenData commons by
collection, read one collection's aggregate counts + provenance, and pull
its citation in any supported format. These are read-only and expose only
the same aggregate, attribution-only data the public landing page shows —
no per-patient or per-study PHI, no S3 keys.

The catalog is public-by-design, so these tools let an agent answer
"what public datasets are available and how do I cite them?" without a
GUI, keeping the MCP a superset of the browse surface.
"""

from __future__ import annotations

import json

from mcp.types import Tool

from bvmcp.tools.client import api_get, api_get_bytes

TOOLS = [
    Tool(
        name="list_public_datasets",
        description=(
            "List the public OpenData collections (TCIA/IDC-style datasets) with "
            "aggregate counts (subjects/studies/series/images), modalities, "
            "license, and whether commercial reuse is allowed. No auth needed; "
            "returns only aggregate, citable metadata."
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="get_public_dataset",
        description=(
            "Read one public dataset collection by slug: full counts, modalities, "
            "body parts, license, the stable PID + landing URL, the upstream "
            "citation, a DataCite-4 metadata block, and a few sample studies."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "slug": {
                    "type": "string",
                    "description": "Collection slug, e.g. 'tcia-qin-breast' (from list_public_datasets).",
                }
            },
            "required": ["slug"],
        },
    ),
    Tool(
        name="get_dataset_citation",
        description=(
            "Get a public dataset's citation in the requested format: 'text' "
            "(plain), 'bibtex', 'ris' (reference managers), or 'datacite' "
            "(DataCite-4 metadata JSON). Credits the upstream source and the "
            "bitvision OpenData redistribution."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "slug": {"type": "string", "description": "Collection slug."},
                "format": {
                    "type": "string",
                    "enum": ["text", "bibtex", "ris", "datacite"],
                    "description": "Citation format (default 'text').",
                },
            },
            "required": ["slug"],
        },
    ),
]


async def handle(name: str, arguments: dict) -> str:
    if name == "list_public_datasets":
        return json.dumps(await api_get("/catalog/collections"), indent=2)

    if name == "get_public_dataset":
        return json.dumps(await api_get(f"/catalog/collections/{arguments['slug']}"), indent=2)

    if name == "get_dataset_citation":
        fmt = arguments.get("format", "text")
        path = f"/catalog/collections/{arguments['slug']}/citation"
        if fmt == "datacite":
            return json.dumps(await api_get(path, params={"format": fmt}), indent=2)
        data, _ = await api_get_bytes(path, params={"format": fmt})
        return data.decode("utf-8")

    raise ValueError(f"unknown tool: {name}")


__all__ = ["TOOLS", "handle"]
