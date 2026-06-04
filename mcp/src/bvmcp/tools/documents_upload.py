"""MCP tools for the resumable upload session API (DESIGN.md §11.6).

GUI-superset parity: every UploadSession action the web uploader performs is
expressible here, so an agent can ingest a document end-to-end without the GUI
(create -> chunk -> commit), resume (get), or clean up (abort). Pass-through to
the same ``/api/upload/sessions`` routes; the agent never sees an S3 key
(storage isolation) — only the session id + per-file offsets.

Practical note: the chunk tool ships raw bytes base64-encoded, which inflates
the payload ~33% and means one call per ``chunk_size`` (8 MiB). That is fine
for small clinical documents; multi-GB imaging belongs in the web uploader /
``bvphoenix-import`` CLI, which stream the bytes directly.
"""

from __future__ import annotations

import base64
import json

from mcp.types import Tool

from bvmcp.tools.client import api_delete, api_get, api_patch_bytes, api_post

TOOLS = [
    Tool(
        name="create_upload_session",
        description=(
            "Open a resumable upload session for one or more files BEFORE sending any bytes. "
            "Returns the session id, the server-authoritative chunk_size (bytes; every chunk "
            "except a file's last must be exactly this), and per-file received_offset (0 on a "
            "fresh session). Then call upload_session_chunk per file from offset 0, and "
            "commit_upload_session when every file's received_offset == its declared size."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "files": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "properties": {
                            "filename": {"type": "string"},
                            "relative_path": {"type": "string"},
                            "size": {"type": "integer", "minimum": 0},
                            "sha256": {"type": "string"},
                        },
                        "required": ["filename", "relative_path", "size"],
                    },
                },
                "patient_id": {"type": "string", "description": "Target patient UUID."},
                "folder_id": {"type": "string", "description": "Target folder UUID (optional)."},
                "tier": {"type": "string", "enum": ["t1", "t2", "t3", "t4"]},
                "keep_iso_archive": {"type": "boolean"},
                "wrap_iso_in_folder": {"type": "boolean"},
                "extract_iso_contents": {"type": "boolean"},
            },
            "required": ["files"],
        },
    ),
    Tool(
        name="upload_session_chunk",
        description=(
            "Append one chunk to a file in an upload session. ``offset`` is the byte position "
            "this chunk starts at (the server's current received_offset for the file). "
            "``chunk_base64`` is the raw bytes base64-encoded; decoded length must equal "
            "chunk_size for every chunk except the file's last. Idempotent: re-sending an "
            "already-acked offset is a no-op. On a gap the server returns 409 with the expected "
            "offset — re-sync to it. Returns the new received_offset + status."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "file_index": {"type": "integer", "minimum": 0},
                "offset": {"type": "integer", "minimum": 0},
                "chunk_base64": {"type": "string"},
            },
            "required": ["session_id", "file_index", "offset", "chunk_base64"],
        },
    ),
    Tool(
        name="get_upload_session",
        description=(
            "Read an upload session: status + per-file received_offset (use this to resume — "
            "continue each file from its received_offset) and the linked job_id once committed."
        ),
        inputSchema={
            "type": "object",
            "properties": {"session_id": {"type": "string"}},
            "required": ["session_id"],
        },
    ),
    Tool(
        name="commit_upload_session",
        description=(
            "Finalize a fully-staged upload session: hands the staged files to the ingest "
            "worker and returns a JobOut (poll its job id for ingest progress). Idempotent: "
            "re-committing returns the same job."
        ),
        inputSchema={
            "type": "object",
            "properties": {"session_id": {"type": "string"}},
            "required": ["session_id"],
        },
    ),
    Tool(
        name="abort_upload_session",
        description=(
            "Abort an in-flight upload session: releases its S3 multipart uploads and deletes "
            "any staged bytes. Use to discard an interrupted upload you won't resume."
        ),
        inputSchema={
            "type": "object",
            "properties": {"session_id": {"type": "string"}},
            "required": ["session_id"],
        },
    ),
]


async def handle(name: str, arguments: dict) -> str:
    if name == "create_upload_session":
        result = await api_post(
            "/api/upload/sessions",
            json={
                "files": arguments["files"],
                "tier": arguments.get("tier", "t1"),
                "patient_id": arguments.get("patient_id"),
                "folder_id": arguments.get("folder_id"),
                "keep_iso_archive": arguments.get("keep_iso_archive", True),
                "wrap_iso_in_folder": arguments.get("wrap_iso_in_folder", True),
                "extract_iso_contents": arguments.get("extract_iso_contents", True),
            },
        )
        return json.dumps(result, indent=2)

    if name == "upload_session_chunk":
        body = base64.b64decode(arguments["chunk_base64"])
        result = await api_patch_bytes(
            f"/api/upload/sessions/{arguments['session_id']}/files/{arguments['file_index']}",
            body,
            upload_offset=int(arguments["offset"]),
        )
        return json.dumps(result, indent=2)

    if name == "get_upload_session":
        result = await api_get(f"/api/upload/sessions/{arguments['session_id']}")
        return json.dumps(result, indent=2)

    if name == "commit_upload_session":
        result = await api_post(f"/api/upload/sessions/{arguments['session_id']}/commit")
        return json.dumps(result, indent=2)

    if name == "abort_upload_session":
        await api_delete(f"/api/upload/sessions/{arguments['session_id']}")
        return json.dumps({"status": "aborted", "session_id": arguments["session_id"]}, indent=2)

    raise ValueError(f"unknown tool: {name}")
