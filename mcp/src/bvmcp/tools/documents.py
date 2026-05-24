"""Ingestion + Maintenance tools — documents.

Three tools that let an agent operate on the Manifestation layer:

* ``ingest_document`` — upload a single document (PDF, image, text)
  to a patient via a JSON POST with the binary inline (base64). The
  bulk async pipeline stays the canonical path for multi-file
  uploads; this is the agent-friendly one-shot variant.
* ``merge_aliases`` — declare that N documents are aliases of one
  canonical (they all share the canonical's ``original_blob_hash``).
  Use when the agent has detected, via similarity analysis, that
  multiple uploads carry the same originating artefact.
* ``split_alias`` — undo a previous merge for one document; reset
  its ``original_blob_hash`` to its own ``content_sha256``.
* ``download_source_document`` — proxy-stream the document blob
  through the backend (storage isolation; bucket / key never leak).
  Replaces the legacy ``download_document_binary`` tool, which is
  kept temporarily until the backend route alias falls.
"""

from __future__ import annotations

import json
from typing import Any

from mcp.types import Tool

from bvmcp.tools.client import api_get_bytes, api_post

TOOLS = [
    Tool(
        name="ingest_document",
        description=(
            "Upload one document into a patient's fascicolo. The "
            "binary is sent inline as base64; for free-text-only "
            "documents (a clinical note typed by the agent) omit "
            "``content_base64`` and supply ``text`` instead. The "
            "classifier hints (``kind_id`` / ``provenance_id`` / "
            "``authority_id``) default to safe catch-all values when "
            "omitted; the FK constraints reject unknown ids. Returns "
            "the new document_id plus the assigned 3-axis tags + "
            "content_sha256. The action is recorded in "
            "``provenance_events`` with the calling agent's token id. "
            "Sensitive: requires ``documents:ingest`` scope."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "patient_id": {"type": "string"},
                "filename": {"type": "string", "minLength": 1, "maxLength": 255},
                "content_base64": {
                    "type": "string",
                    "description": (
                        "Base64-encoded binary. Omit for inline-text-only "
                        "documents. Mutually optional with ``text`` (one "
                        "of the two must be present)."
                    ),
                },
                "text": {
                    "type": "string",
                    "description": "Inline text body when no binary is attached.",
                },
                "content_type": {"type": "string", "maxLength": 128},
                "kind_id": {
                    "type": "string",
                    "maxLength": 64,
                    "description": (
                        "One of the document_kinds catalog entries "
                        "(see /api/document-kinds). Default 'unclassified'."
                    ),
                },
                "provenance_id": {
                    "type": "string",
                    "maxLength": 64,
                    "description": (
                        "One of the document_provenances catalog entries. Default 'manual_entry'."
                    ),
                },
                "authority_id": {
                    "type": "string",
                    "enum": ["original", "derived", "canonical_synthesis", "stale"],
                    "default": "original",
                },
                "title": {"type": "string", "maxLength": 255},
                "document_date": {
                    "type": "string",
                    "description": "ISO YYYY-MM-DD date the document refers to (clinical date, not ingestion).",
                },
            },
            "required": ["patient_id", "filename"],
        },
    ),
    Tool(
        name="merge_aliases",
        description=(
            "Declare that N documents are copies of the same "
            "originating artefact. All N rows get the canonical's "
            "``content_sha256`` as their shared ``original_blob_hash``; "
            "the canonical row itself is unchanged. Cross-patient "
            "merges are refused. Idempotent: re-running with the same "
            "set is a no-op. The canonical defaults to the first id "
            "in ``document_ids`` when omitted; the canonical must "
            "have a non-null content_sha256 (inline-text-only "
            "documents cannot be the canonical). Sensitive: requires "
            "``documents:merge`` scope."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "document_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 2,
                    "maxItems": 50,
                },
                "canonical_id": {
                    "type": "string",
                    "description": (
                        "The document whose content_sha256 becomes the "
                        "shared original_blob_hash. Defaults to the first "
                        "id in document_ids."
                    ),
                },
                "reason": {"type": "string", "maxLength": 2000},
            },
            "required": ["document_ids"],
        },
    ),
    Tool(
        name="split_alias",
        description=(
            "Reverse a previous merge for one document: reset its "
            "``original_blob_hash`` to its own ``content_sha256``, "
            "removing it from the alias group. Other documents in the "
            "group are not affected. Use when a previously-merged "
            "document was mis-grouped and should be tracked as a "
            "distinct original."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "document_id": {"type": "string"},
                "reason": {"type": "string", "maxLength": 2000},
            },
            "required": ["document_id"],
        },
    ),
    Tool(
        name="download_source_document",
        description=(
            "Proxy-download a document's underlying blob through the "
            "backend. Storage isolation: the bucket name, S3 endpoint, "
            "and key are never exposed to the client (memoria "
            "feedback_storage_isolation). Useful for the radiologist "
            "workflow that wants the bit-perfect ISO of a DVD on a "
            "professional reading workstation, or for any agent that "
            "needs the original PDF for downstream processing. "
            "Sensitive: requires ``documents:download`` scope (PHI "
            "exfiltration risk)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "document_id": {"type": "string"},
            },
            "required": ["document_id"],
        },
    ),
]


async def handle(name: str, arguments: dict) -> str:
    if name == "ingest_document":
        body: dict[str, Any] = {
            "patient_id": arguments["patient_id"],
            "filename": arguments["filename"],
        }
        for k in (
            "content_base64",
            "text",
            "content_type",
            "kind_id",
            "provenance_id",
            "authority_id",
            "title",
            "document_date",
        ):
            v = arguments.get(k)
            if v is not None:
                body[k] = v
        result = await api_post("/api/documents/ingest", json=body)
        return json.dumps(result, indent=2)

    if name == "merge_aliases":
        body = {"document_ids": arguments["document_ids"]}
        if arguments.get("canonical_id"):
            body["canonical_id"] = arguments["canonical_id"]
        if arguments.get("reason"):
            body["reason"] = arguments["reason"]
        result = await api_post("/api/documents/merge", json=body)
        return json.dumps(result, indent=2)

    if name == "split_alias":
        body = {}
        if arguments.get("reason"):
            body["reason"] = arguments["reason"]
        result = await api_post(f"/api/documents/{arguments['document_id']}/split", json=body)
        return json.dumps(result, indent=2)

    if name == "download_source_document":
        # Returns the raw bytes; we surface the size + content-type
        # rather than dumping the binary into the conversation. The
        # agent that needs the actual bytes runs this through a
        # download-to-disk shim outside the MCP transport.
        try:
            data, content_type = await api_get_bytes(
                f"/api/documents/{arguments['document_id']}/download"
            )
        except Exception as exc:
            return json.dumps({"error": str(exc)}, indent=2)
        return json.dumps(
            {
                "document_id": arguments["document_id"],
                "content_type": content_type,
                "size_bytes": len(data),
                "note": (
                    "binary not returned through the MCP transport; "
                    "use the HTTP endpoint directly to stream the file."
                ),
            },
            indent=2,
        )

    raise ValueError(f"unknown tool: {name}")
