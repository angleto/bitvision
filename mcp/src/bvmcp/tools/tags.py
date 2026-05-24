"""MCP tools for deterministic tag-based search and tag discovery.

These tools give agents a low-cost, LLM-free retrieval channel:

- ``search_by_tags`` — filter studies by namespaced tags (e.g.
  ``anatomy:lung``, ``finding:nodule``). Supports ``all`` (AND, default)
  and ``any`` (OR) match semantics. Wraps the backend tag filter on
  ``/api/search`` (``?tag=...&tag=...``).
- ``list_tags`` — enumerate known tags (for autocomplete / discovery),
  optionally filtered by namespace or prefix. Wraps ``/api/tags``.

Both tools degrade gracefully when the backend endpoints are not yet
deployed — HTTP 404 is surfaced as a readable ``TextContent`` message so
the agent can adapt instead of crashing.
"""

from __future__ import annotations

import asyncio
import json

import httpx
from mcp.types import Tool

from bvmcp.tools.client import api_get

TOOLS = [
    Tool(
        name="search_by_tags",
        description=(
            "Deterministic study search by namespaced tags — e.g. "
            "['anatomy:lung', 'finding:nodule']. Use this as a low-cost "
            "retrieval channel whenever the agent can express the query as "
            "structured tags (no embeddings, no LLM rerank). "
            "match='all' (default) requires every tag to be present on the "
            "study; match='any' returns studies matching at least one tag."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "List of namespaced tags to filter by, in "
                        "'namespace:value' form (e.g. 'anatomy:lung', "
                        "'finding:nodule', 'modality:CT')."
                    ),
                    "minItems": 1,
                },
                "match": {
                    "type": "string",
                    "enum": ["all", "any"],
                    "description": (
                        "'all' requires every tag (AND, default); 'any' "
                        "requires at least one tag (OR)."
                    ),
                    "default": "all",
                },
                "k": {
                    "type": "integer",
                    "description": "Max studies to return (default 20).",
                    "default": 20,
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
            "required": ["tags"],
        },
    ),
    Tool(
        name="list_tags",
        description=(
            "Enumerate known tags for discovery / autocomplete. Returns a "
            "list of {namespace, value, count} entries. Optionally filter "
            "by namespace (e.g. 'anatomy') or by prefix ('q') to power "
            "typeahead. Pair this with search_by_tags to discover what "
            "tags are available before querying."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace": {
                    "type": "string",
                    "description": (
                        "Optional namespace filter (e.g. 'anatomy', 'finding', 'modality')."
                    ),
                },
                "q": {
                    "type": "string",
                    "description": "Optional prefix string for autocomplete on tag value.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max tags to return (default 50).",
                    "default": 50,
                },
            },
        },
    ),
]


def _format_http_error(exc: httpx.HTTPStatusError, hint: str = "") -> str:
    """Return a readable error string for a non-2xx backend response.

    S1 rewrite + S5 tags API may not be deployed yet — a 404 here is a
    likely signal that the feature has not landed. Surface that as text
    so the agent can fall back to another tool instead of bubbling up a
    raw exception.
    """
    status = exc.response.status_code
    if status == 404:
        msg = (
            f"Backend endpoint not found (404) at {exc.request.url.path}. "
            "The tag-based search/listing API may not be deployed yet."
        )
    else:
        msg = f"Backend error ({status}) at {exc.request.url.path}: {exc.response.text}"
    if hint:
        msg = f"{msg} — {hint}"
    return msg


async def _search_with_tags(tags: list[str], k: int, scope: str | None = None) -> list | dict:
    """Wrap ``GET /api/search?tag=...&tag=...&limit=k``.

    httpx serializes a list value by repeating the key, producing the
    ``tag=a&tag=b`` form expected by the backend. ``scope`` is forwarded
    untouched when present so the agent can restrict to OpenData / own
    studies, mirroring the /search UI segmented control.
    """
    params: dict = {"tag": tags, "limit": k}
    if scope:
        params["scope"] = scope
    return await api_get("/api/search", params=params)


async def _search_by_tags_any(tags: list[str], k: int, scope: str | None = None) -> dict:
    """match='any' implementation.

    First try the native ``match_any=true`` query (cheap, single call).
    If the backend rejects the param as unsupported, fall back to N
    parallel single-tag calls unioned client-side. The caption in the
    returned payload records which path was used. ``scope`` is
    forwarded to whichever path actually runs.
    """
    base_params: dict = {"tag": tags, "limit": k, "match_any": "true"}
    if scope:
        base_params["scope"] = scope
    try:
        native = await api_get("/api/search", params=base_params)
    except httpx.HTTPStatusError as exc:
        # 400/422 likely = unsupported param; 404 = endpoint missing (re-raise).
        if exc.response.status_code not in (400, 422):
            raise
        native = None

    if native is not None:
        return {
            "caption": (
                f"match=any via native match_any=true on /api/search (tags={tags}, limit={k})."
            ),
            "results": native,
        }

    pages = await asyncio.gather(
        *(_search_with_tags([tag], k, scope=scope) for tag in tags),
        return_exceptions=True,
    )

    # Re-raise a 404 so the caller surfaces "endpoint missing" instead of
    # returning an empty union that looks like a legit zero-hit result.
    for page in pages:
        if isinstance(page, httpx.HTTPStatusError) and page.response.status_code == 404:
            raise page

    seen: dict[str, dict] = {}
    order: list[str] = []
    for page in pages:
        if isinstance(page, BaseException):
            continue
        for item in _extract_items(page):
            sid = str(item.get("id") or item.get("study_id") or id(item))
            if sid in seen:
                continue
            seen[sid] = item
            order.append(sid)
            if len(order) >= k:
                break
        if len(order) >= k:
            break

    union = [seen[s] for s in order[:k]]
    return {
        "caption": (
            "match=any fallback: backend does not support match_any; "
            f"ran {len(tags)} parallel single-tag queries and unioned results "
            f"(tags={tags}, limit={k})."
        ),
        "results": union,
    }


def _extract_items(page: list | dict) -> list[dict]:
    """Best-effort extraction of the items list from a search response.

    The backend may return either a bare list or ``{items: [...], ...}``.
    """
    if isinstance(page, list):
        return page
    if isinstance(page, dict):
        for key in ("items", "results", "studies", "data"):
            val = page.get(key)
            if isinstance(val, list):
                return val
    return []


async def handle(name: str, arguments: dict) -> str:
    if name == "search_by_tags":
        tags = arguments.get("tags") or []
        if not tags or not isinstance(tags, list):
            return "Error: 'tags' must be a non-empty list of 'namespace:value' strings."
        match = arguments.get("match", "all")
        if match not in ("all", "any"):
            return "Error: 'match' must be 'all' or 'any'."
        k = arguments.get("k", 20)
        scope = arguments.get("scope")
        if scope and scope not in ("all", "public", "mine"):
            return "Error: 'scope' must be one of 'all', 'public', 'mine'."

        try:
            if match == "all":
                result = await _search_with_tags(tags, k, scope=scope)
                payload = {
                    "caption": (
                        f"match=all on /api/search (tags={tags}, limit={k}, "
                        f"scope={scope or 'all'}) — backend applies AND across tag filters."
                    ),
                    "results": result,
                }
            else:
                payload = await _search_by_tags_any(tags, k, scope=scope)
        except httpx.HTTPStatusError as exc:
            return _format_http_error(
                exc,
                hint="search_by_tags requires /api/search with tag filter (S1/S5).",
            )
        except httpx.HTTPError as exc:
            return f"Error calling backend: {exc}"

        return json.dumps(payload, indent=2)

    if name == "list_tags":
        params: dict = {}
        if arguments.get("namespace"):
            params["namespace"] = arguments["namespace"]
        if arguments.get("q"):
            params["q"] = arguments["q"]
        params["limit"] = arguments.get("limit", 50)

        try:
            result = await api_get("/api/tags", params=params)
        except httpx.HTTPStatusError as exc:
            return _format_http_error(
                exc,
                hint="list_tags requires /api/tags (S5 tags API).",
            )
        except httpx.HTTPError as exc:
            return f"Error calling backend: {exc}"

        return json.dumps(result, indent=2)

    raise ValueError(f"unknown tool: {name}")
