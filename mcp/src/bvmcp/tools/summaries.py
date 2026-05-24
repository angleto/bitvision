"""MCP polymorphic summarize tool.

Lets an LLM/agent request a natural-language summary of a series, study,
or patient fascicolo in a chosen language. The backend handles the
underlying generation (caching included); this tool is a thin wrapper
around ``POST /api/summaries/generate`` that returns the markdown plus a
short footer disclosing whether the summary was cached or freshly
generated and the token usage when available.
"""

from __future__ import annotations

import json
from typing import Literal

import httpx
from mcp.types import Tool
from pydantic import BaseModel, Field, ValidationError

from bvmcp.tools.client import api_post_raw

_TARGET_KINDS = ("series", "study", "patient")
_LANGS = ("it", "en")


class SummarizeInput(BaseModel):
    target_kind: Literal["series", "study", "patient"] = Field(
        ..., description="Kind of entity to summarize"
    )
    target_id: str = Field(..., description="UUID of the target entity")
    lang: Literal["it", "en"] = Field(default="it", description="Summary language (default: it)")
    force_refresh: bool = Field(
        default=False,
        description="If true, bypass the cache and regenerate the summary",
    )


TOOLS = [
    Tool(
        name="summarize",
        description=(
            "Generate a natural-language markdown summary for a series, "
            "study, or patient fascicolo in the chosen language (it/en). "
            "Summaries are cached server-side; set force_refresh=true to "
            "ignore the cache and regenerate. Returns markdown plus a "
            "footer indicating cached-vs-fresh and token usage."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "target_kind": {
                    "type": "string",
                    "enum": list(_TARGET_KINDS),
                    "description": "Kind of entity to summarize",
                },
                "target_id": {
                    "type": "string",
                    "description": "UUID of the target entity",
                },
                "lang": {
                    "type": "string",
                    "enum": list(_LANGS),
                    "default": "it",
                    "description": "Summary language (default: it)",
                },
                "force_refresh": {
                    "type": "boolean",
                    "default": False,
                    "description": ("If true, bypass the cache and regenerate the summary"),
                },
            },
            "required": ["target_kind", "target_id"],
        },
    ),
]


def _body_repr(resp: httpx.Response) -> str:
    try:
        return json.dumps(resp.json())
    except ValueError:
        return resp.text


def _format_token_usage(token_usage: object) -> str | None:
    """Render token usage as a short footer fragment, or None if not useful."""
    if not isinstance(token_usage, dict):
        return None
    # Accept common shapes: {input_tokens, output_tokens} or
    # {prompt_tokens, completion_tokens, total_tokens}.
    in_tok = token_usage.get("input_tokens") or token_usage.get("prompt_tokens")
    out_tok = token_usage.get("output_tokens") or token_usage.get("completion_tokens")
    total = token_usage.get("total_tokens")
    parts: list[str] = []
    if in_tok is not None:
        parts.append(f"in={in_tok}")
    if out_tok is not None:
        parts.append(f"out={out_tok}")
    if total is not None:
        parts.append(f"total={total}")
    if parts:
        return " ".join(parts)
    # Fall back to a compact JSON dump so the info is not lost on unknown shapes.
    try:
        return json.dumps(token_usage, separators=(",", ":"))
    except (TypeError, ValueError):
        return None


async def handle(name: str, arguments: dict) -> str:
    if name != "summarize":
        raise ValueError(f"unknown tool: {name}")

    try:
        payload_in = SummarizeInput(**arguments)
    except ValidationError as exc:
        return f"Validation error: {exc.errors()}"

    payload = {
        "target_kind": payload_in.target_kind,
        "target_id": payload_in.target_id,
        "lang": payload_in.lang,
        "force_refresh": payload_in.force_refresh,
    }

    try:
        resp = await api_post_raw("/api/summaries/generate", json=payload)
    except httpx.HTTPError as exc:
        return f"Error calling backend: {exc}"

    if resp.status_code >= 400:
        label = "Validation error from backend" if resp.status_code == 400 else "Backend error"
        return f"{label} ({resp.status_code}): {_body_repr(resp)}"

    try:
        data = resp.json()
    except ValueError:
        return f"Unexpected backend response (status {resp.status_code}): {resp.text}"

    summary_md = data.get("summary_md") or ""
    cached = bool(data.get("cached"))
    token_usage = data.get("token_usage")

    footer_bits: list[str] = [
        "_[cached summary]_" if cached else "_[freshly generated]_",
    ]
    token_fragment = _format_token_usage(token_usage)
    if token_fragment:
        footer_bits.append(f"_tokens: {token_fragment}_")

    footer = " · ".join(footer_bits)
    if not summary_md.strip():
        return f"(empty summary)\n\n{footer}"
    return f"{summary_md.rstrip()}\n\n---\n{footer}"
