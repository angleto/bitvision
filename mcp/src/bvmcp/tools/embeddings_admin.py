"""MCP tools for the embeddings-admin maintenance surface (GUI parity).

Mirrors the ``/admin/embeddings`` dashboard: per-model coverage plus the
retry-failed / embed-missing enqueue actions, including the per-model
text-chunk re-embed whose routing lives on the ``embedding_models``
registry row. All five tools ride the sensitive ``admin:embeddings``
scope and the backend additionally requires the assistant's OWNER to be
a platform admin (``require_admin_or_scoped_agent``).

Storage isolation: the responses surfaced to the model are AGGREGATES
ONLY. The backend's series-coverage payload carries per-target failure
rows (``last_failures``) whose ``error_message`` can quote storage /
SQL internals and whose ``target_id`` enumerates cross-patient UUIDs;
both are stripped here — the admin GUI remains the place to drill into
individual failures.
"""

from __future__ import annotations

import json

from mcp.types import Tool

from bvmcp.tools.client import api_get, api_post_with_headers

TOOLS = [
    Tool(
        name="get_embedding_coverage",
        description=(
            "ADMIN: per-(model, target_kind) coverage of image/series embeddings "
            "(total, done, failed, pending, percentage). Aggregate counts only; "
            "per-failure detail lives in the admin GUI. Requires the admin:embeddings "
            "scope and an admin owner."
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="get_text_embedding_coverage",
        description=(
            "ADMIN: coverage of one text model's chunk embeddings over every "
            "text_chunks row, with a per-source-kind breakdown. 'model' defaults to "
            "the registry's active text default; pass a model name (e.g. bge-m3-v1) "
            "to inspect a specific store. Requires the admin:embeddings scope and an "
            "admin owner."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "model": {
                    "type": "string",
                    "description": "Registry text model name; omit for the active default",
                },
            },
        },
    ),
    Tool(
        name="retry_failed_embeddings",
        description=(
            "ADMIN: re-enqueue the embedding job for every target of a kind that has "
            "an error recorded but no embedding yet. Set dry_run=true to get the "
            "candidate count without enqueueing. Requires the admin:embeddings scope "
            "and an admin owner."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "model_id": {
                    "type": "string",
                    "description": "Embedding model id, e.g. biomedclip-v1",
                },
                "target_kind": {
                    "type": "string",
                    "enum": ["study", "series", "instance"],
                    "description": "Kind of target to retry",
                },
                "dry_run": {
                    "type": "boolean",
                    "description": "Preview the candidate count without enqueueing (default false)",
                },
            },
            "required": ["model_id", "target_kind"],
        },
    ),
    Tool(
        name="embed_missing_targets",
        description=(
            "ADMIN: enqueue the embedding job for every target of a kind that has no "
            "embedding for the model yet (including never-attempted ones — the usual "
            "step after adding a new model). Set dry_run=true to get the candidate "
            "count without enqueueing. Requires the admin:embeddings scope and an "
            "admin owner."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "model_id": {
                    "type": "string",
                    "description": "Embedding model id, e.g. biomedclip-v1",
                },
                "target_kind": {
                    "type": "string",
                    "enum": ["study", "series", "instance"],
                    "description": "Kind of target to embed",
                },
                "dry_run": {
                    "type": "boolean",
                    "description": "Preview the candidate count without enqueueing (default false)",
                },
            },
            "required": ["model_id", "target_kind"],
        },
    ),
    Tool(
        name="reembed_text_chunks",
        description=(
            "ADMIN: enqueue the per-model text-chunk (re)embedding jobs. Routing "
            "(arq task + pgvector store) comes from the model's embedding_models "
            "registry row. 'model' defaults to the active text default; "
            "only_missing=true (default) backfills chunks lacking a vector, "
            "only_missing=false re-embeds everything (the jobs are idempotent "
            "upserts). Set dry_run=true to get the candidate count without "
            "enqueueing. Requires the admin:embeddings scope and an admin owner."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "model": {
                    "type": "string",
                    "description": "Registry text model name; omit for the active default",
                },
                "only_missing": {
                    "type": "boolean",
                    "description": "Only chunks without a vector (default true)",
                },
                "dry_run": {
                    "type": "boolean",
                    "description": "Preview the candidate count without enqueueing (default false)",
                },
            },
        },
    ),
]


def _strip_failures(coverage: dict) -> dict:
    """Drop per-target failure rows from the series-coverage payload.

    ``last_failures`` carries raw worker ``error_message`` strings (which
    can quote storage/SQL internals) and cross-patient ``target_id``s;
    the agent only needs the aggregate counts.
    """
    return {
        "items": [
            {k: v for k, v in item.items() if k != "last_failures"}
            for item in coverage.get("items", [])
        ]
    }


async def handle(name: str, arguments: dict) -> str:
    if name == "get_embedding_coverage":
        result = await api_get("/api/embeddings/coverage")
        if isinstance(result, dict):
            result = _strip_failures(result)
        return json.dumps(result, indent=2)

    if name == "get_text_embedding_coverage":
        params = {}
        if arguments.get("model"):
            params["model"] = arguments["model"]
        result = await api_get("/api/embeddings/text-chunks/coverage", params=params or None)
        return json.dumps(result, indent=2)

    if name == "retry_failed_embeddings":
        body, _ = await api_post_with_headers(
            "/api/embeddings/retry-failed",
            params={
                "model_id": arguments["model_id"],
                "target_kind": arguments["target_kind"],
                "dry_run": bool(arguments.get("dry_run", False)),
            },
        )
        return json.dumps(body, indent=2)

    if name == "embed_missing_targets":
        body, _ = await api_post_with_headers(
            "/api/embeddings/embed-missing",
            params={
                "model_id": arguments["model_id"],
                "target_kind": arguments["target_kind"],
                "dry_run": bool(arguments.get("dry_run", False)),
            },
        )
        return json.dumps(body, indent=2)

    if name == "reembed_text_chunks":
        params: dict = {
            "only_missing": bool(arguments.get("only_missing", True)),
            "dry_run": bool(arguments.get("dry_run", False)),
        }
        if arguments.get("model"):
            params["model"] = arguments["model"]
        body, _ = await api_post_with_headers(
            "/api/embeddings/text-chunks/embed-missing",
            params=params,
        )
        return json.dumps(body, indent=2)

    raise ValueError(f"unknown tool: {name}")
