"""Navigation tool — provenance lineage.

A single tool that exposes the append-only ``provenance_events``
history of any artefact. The agent uses this to answer "where did
this content come from / when was it last touched / which agent
endorsed it / why is this in the fascicolo".
"""

from __future__ import annotations

import json

from mcp.types import Tool

from bvmcp.tools.client import api_get

TOOLS = [
    Tool(
        name="get_provenance_chain",
        description=(
            "Return the append-only lineage history of a v3 artefact, "
            "newest event first. Each event records the ``activity`` "
            "(create / extract / endorse / sign / reject / supersede / "
            "merge / split / cite / link / unlink / redact / delete / "
            "restore / identify / update), the agent (``agent_kind`` "
            "= human / agent / system, plus ``agent_subject_id`` and "
            "``agent_token_id`` when applicable), and the source artefact "
            "the activity derived from (when applicable, e.g. "
            "supersede points back at the superseded entity). Use this "
            "to explain to a human reviewer how a particular content / "
            "document / synthesis arrived at its current state."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "target_kind": {
                    "type": "string",
                    "enum": [
                        "patient",
                        "clinical_event",
                        "imaging_study",
                        "series",
                        "report_content",
                        "document",
                        "document_file",
                        "marker",
                        "tag",
                        "external_identifier",
                        "content_document_link",
                        "report_content_citation",
                    ],
                },
                "target_id": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 100},
                "offset": {"type": "integer", "minimum": 0, "default": 0},
            },
            "required": ["target_kind", "target_id"],
        },
    ),
]


async def handle(name: str, arguments: dict) -> str:
    if name == "get_provenance_chain":
        result = await api_get(
            f"/api/provenance/{arguments['target_kind']}/{arguments['target_id']}",
            params={
                "limit": arguments.get("limit", 100),
                "offset": arguments.get("offset", 0),
            },
        )
        return json.dumps(result, indent=2)

    raise ValueError(f"unknown tool: {name}")
