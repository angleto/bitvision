"""MCP server exposing bitvision phoenix resources to LLM/agent clients.

Run with: `uv run python -m bvmcp.server`

The server delegates all data access to the backend REST API — the MCP
process itself is stateless and holds a user-scoped token, so the
backend's authorization (RLS + grants) applies transparently to any
LLM/agent connecting over MCP.

Tools are organized into families:
- studies: get_study, get_series, describe_series, embed_series
- search: search_studies, similar_to
- search_advanced: semantic_search, search_hybrid
- tags: search_by_tags, list_tags (deterministic tag-based retrieval)
- annotations: get_annotations, list_reports
- patients: get_patient, get_fascicolo_index, get_patient_timeline, list_patient_documents
- images: get_series_thumbnail, get_study_thumbnails
- bundle: get_fascicolo_bundle (aggregates the full patient record)
- consultations: create_consultation (LLM writes structured consult back)
- summaries: summarize (polymorphic series/study/patient summary)
"""

import asyncio

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import ImageContent, TextContent, Tool

from bvmcp import __version__
from bvmcp._tool_registry import TOOL_MODULES
from bvmcp.config import get_settings
from bvmcp.feature_flags import get_feature_flags, is_tool_available
from bvmcp.tools.client import format_http_error

settings = get_settings()

server: Server = Server("bitvision-phoenix")

# Both transports (stdio here, HTTP in ``server_http.py``) read the
# canonical tool registry from ``bvmcp._tool_registry`` so they
# cannot drift. Pre-2026-05-03 each transport kept its own copy of
# the tuple and ``care_phases`` was registered on stdio but missing
# on HTTP — agents using the Claude.ai connector got "tool not
# found" on every phase call. The single registry makes that bug
# class structurally impossible.
_TOOL_MODULES = TOOL_MODULES

ALL_TOOLS: list[Tool] = [tool for mod in _TOOL_MODULES for tool in mod.TOOLS]

# name → coroutine handler. Handlers may return a string (wrapped into a
# single TextContent) or a pre-built list of TextContent / ImageContent
# for multi-block responses (bundle = markdown + JSON appendix; images =
# caption + base64 JPEG).
_HANDLERS: dict[str, object] = {}
for _mod in _TOOL_MODULES:
    for _tool in _mod.TOOLS:
        _HANDLERS[_tool.name] = _mod.handle


@server.list_tools()
async def list_tools() -> list[Tool]:
    flags = await get_feature_flags()
    return [t for t in ALL_TOOLS if is_tool_available(t.name, flags)]


@server.call_tool()
async def call_tool(name: str, arguments: dict | None) -> list[TextContent | ImageContent]:
    flags = await get_feature_flags()
    if not is_tool_available(name, flags):
        return [
            TextContent(
                type="text",
                text=(
                    f"Error: tool '{name}' is currently disabled — "
                    "the backend reports this feature is not configured. "
                    "BYO mode advice: classify in your own LLM and use "
                    "create_care_phase + assign_event_to_phase directly."
                ),
            )
        ]
    handler = _HANDLERS.get(name)
    if handler is None:
        return [TextContent(type="text", text=f"Error: unknown tool '{name}'")]
    try:
        result = await handler(name, arguments or {})  # type: ignore[operator]
    except httpx.HTTPStatusError as exc:
        # ``raise_for_status()`` otherwise collapses the backend's
        # structured detail (Pydantic loc/msg/type, RFC 7807 problem,
        # ``required_scope``) into the one-line repr of the exception.
        # Surfacing the body here means every write tool — present
        # and future — gives the agent enough information to self-
        # correct without per-tool try/except boilerplate.
        return [TextContent(type="text", text=format_http_error(exc))]
    except Exception as exc:
        return [TextContent(type="text", text=f"Error: {exc}")]
    if isinstance(result, str):
        return [TextContent(type="text", text=result)]
    return list(result)


async def main() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    print(f"bitvision phoenix MCP server {__version__}")
    asyncio.run(main())
