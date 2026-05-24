"""Q&A MCP tools — orchestrator + sub-document chunk search.

Two tools:

* ``ask_about_patient`` — high-level, calls the server-side
  orchestrator (``POST /api/patients/{id}/ask``). Spends platform-paid
  LLM tokens; gated by the dedicated ``qna:ask`` scope so the
  operator must opt in explicitly when granting an assistant.
* ``search_text_chunks`` — low-level RAG primitive
  (``GET /api/patients/{id}/search/chunks``). Same retrieval the
  orchestrator uses internally, exposed as a standalone tool so a
  BYO agent can run its own loop without spending Q&A tokens.

Both forward the agent's bearer to the backend; the backend enforces
patient-scope on the agent token (cross-patient calls are rejected
with 404 just like the human user path), so the MCP layer is a thin
relay.

The high-level tool returns the orchestrator's structured payload
(answer markdown + citations + tool plan + usage); the chunk search
returns a hits array. No S3 keys, no bucket names.
"""

from __future__ import annotations

import json
from typing import Any

from mcp.types import Tool

from bvmcp.tools.client import api_get, api_post

TOOLS = [
    Tool(
        name="ask_about_patient",
        description=(
            "Run the BitVision Q&A orchestrator for one patient. The "
            "server-side agent loop executes a curated tool catalog "
            "(find_clinical_events, search_text_chunks, get_event, "
            "get_document_text, list_recent_documents) and returns a "
            "natural-language answer with inline citations. The tier "
            "(free / standard / premium), provider, and model are "
            "resolved server-side from the user's settings; this tool "
            "does not select the model. Free tier returns deterministic "
            "retrieval results without LLM synthesis. Cross-patient "
            "calls are rejected with 404."
        ),
        inputSchema={
            "type": "object",
            "required": ["patient_id", "query"],
            "properties": {
                "patient_id": {
                    "type": "string",
                    "format": "uuid",
                    "description": "UUID of the patient. Must be in the agent token's scope.",
                },
                "query": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 2000,
                    "description": "Natural-language question.",
                },
                "lang": {
                    "type": "string",
                    "enum": ["it", "en"],
                    "default": "it",
                },
            },
        },
    ),
    Tool(
        name="search_text_chunks",
        description=(
            "Hybrid (vector + Italian FTS) sub-document search across "
            "the patient's documents, clinical notes, AI summaries, "
            "and structured report contents. Use to gather relevant "
            "passages before drafting an answer or to feed a BYO "
            "agent loop. Filter by source kind, author kind "
            "(human / agent / system / unknown), authority "
            "(original / derived / canonical_synthesis / stale), "
            "document kind, date bounds, or a single source row."
        ),
        inputSchema={
            "type": "object",
            "required": ["patient_id", "query"],
            "properties": {
                "patient_id": {"type": "string", "format": "uuid"},
                "query": {"type": "string", "minLength": 1, "maxLength": 512},
                "k": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 20,
                    "default": 8,
                },
                "source_kind": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": [
                            "document",
                            "clinical_note",
                            "summary",
                            "report_content",
                        ],
                    },
                },
                "author_kind": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": ["human", "agent", "system", "unknown"],
                    },
                },
                "exclude_ai": {"type": "boolean", "default": False},
                "authority_id": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "List of allowed authority ids (e.g. ['original'] for originals only)."
                    ),
                },
                "document_kind_id": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "List of allowed document kinds. Only meaningful "
                        "for source_kind='document'."
                    ),
                },
                "since": {
                    "type": "string",
                    "format": "date",
                    "description": "ISO date YYYY-MM-DD inclusive lower bound.",
                },
                "until": {
                    "type": "string",
                    "format": "date",
                },
                "source_id": {
                    "type": "string",
                    "format": "uuid",
                    "description": "Restrict to a single source row.",
                },
            },
        },
    ),
]


async def handle(name: str, arguments: dict) -> str:
    args = arguments or {}
    if name == "ask_about_patient":
        return await _ask_about_patient(args)
    if name == "search_text_chunks":
        return await _search_text_chunks(args)
    return json.dumps({"error": f"unknown qna tool: {name}"})


async def _ask_about_patient(args: dict[str, Any]) -> str:
    patient_id = args.get("patient_id")
    query = args.get("query")
    if not patient_id or not query:
        return json.dumps({"error": "patient_id and query are required"})

    try:
        body = await api_post(
            f"/patients/{patient_id}/ask",
            json={
                "query": query,
                "lang": args.get("lang", "it"),
            },
        )
    except Exception as exc:
        return json.dumps({"error": str(exc)})
    return json.dumps(body, default=str)


async def _search_text_chunks(args: dict[str, Any]) -> str:
    patient_id = args.get("patient_id")
    query = args.get("query")
    if not patient_id or not query:
        return json.dumps({"error": "patient_id and query are required"})

    params: dict[str, Any] = {"q": query, "k": args.get("k", 8)}
    for key in (
        "source_kind",
        "author_kind",
        "authority_id",
        "document_kind_id",
    ):
        if args.get(key):
            params[key] = args[key]
    if args.get("exclude_ai") is not None:
        params["exclude_ai"] = bool(args["exclude_ai"])
    for key in ("since", "until", "source_id"):
        if args.get(key):
            params[key] = args[key]

    try:
        body = await api_get(
            f"/patients/{patient_id}/search/chunks",
            params=params,
        )
    except Exception as exc:
        return json.dumps({"error": str(exc)})
    return json.dumps(body, default=str)


__all__ = ["TOOLS", "handle"]
