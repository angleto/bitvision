"""MCP image tools — return JPEG pixels (base64) so vision-capable LLM
clients can SEE DICOM slices, not just read metadata.

Two tools:

- ``get_series_thumbnail`` — one slice of one series. Middle slice by
  default; ``slice_index`` / ``wc`` / ``ww`` let the LLM pick + window.
- ``get_study_thumbnails`` — batch: one thumbnail per series in a study,
  capped by ``max_series``. The remainder is listed by id so the LLM
  can ask for specific ones via ``get_series_thumbnail``.

Backend contract: ``GET /api/series/{id}/thumbnail`` already renders
DICOM to JPEG. We just forward the call, base64-encode, and pair with
a ``TextContent`` caption.
"""

from __future__ import annotations

import asyncio
import base64
from typing import Any

from mcp.types import ImageContent, TextContent, Tool

from bvmcp.tools.client import api_get, api_get_bytes

TOOLS = [
    Tool(
        name="get_series_thumbnail",
        description=(
            "Fetch a single DICOM slice as a JPEG image (base64) so a vision-"
            "capable MCP client can actually see the pixels — not just metadata. "
            "By default returns the middle slice; pass ``slice_index`` to pick "
            "one. ``wc`` / ``ww`` nudge DICOM window center / width (offsets "
            "from the DICOM default, matching the clinical viewer)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "series_id": {"type": "string", "description": "UUID of the series"},
                "slice_index": {
                    "type": "integer",
                    "description": "0-based slice index (default: middle of the series)",
                },
                "wc": {
                    "type": "number",
                    "description": "Window center offset from DICOM default (default 0)",
                },
                "ww": {
                    "type": "number",
                    "description": "Window width offset from DICOM default (default 0)",
                },
                "max_side": {
                    "type": "integer",
                    "description": "Max long-side pixels for the returned JPEG (default 512)",
                    "default": 512,
                },
            },
            "required": ["series_id"],
        },
    ),
    Tool(
        name="get_study_thumbnails",
        description=(
            "Fetch one thumbnail (middle slice) per series of a DICOM study, "
            "returned as a batch of images the LLM can view in a single turn. "
            "Use this to get a quick visual overview of all series in a study. "
            "Series beyond ``max_series`` are listed by id but not rendered — "
            "call ``get_series_thumbnail`` individually for those."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "study_id": {"type": "string", "description": "UUID of the study"},
                "max_series": {
                    "type": "integer",
                    "description": "Max number of series to render as images (default 12)",
                    "default": 12,
                    "minimum": 1,
                    "maximum": 64,
                },
                "max_side": {
                    "type": "integer",
                    "description": "Max pixels on the longest side of each thumbnail (default 512)",
                    "default": 512,
                    "minimum": 64,
                    "maximum": 2048,
                },
            },
            "required": ["study_id"],
        },
    ),
]


async def _get_series_thumbnail(arguments: dict) -> list[TextContent | ImageContent]:
    series_id = arguments["series_id"]
    slice_index = arguments.get("slice_index")
    wc = float(arguments.get("wc") or 0)
    ww = float(arguments.get("ww") or 0)
    max_side = int(arguments.get("max_side") or 512)

    params: dict[str, Any] = {"wc_delta": wc, "ww_delta": ww, "max_side": max_side}
    if slice_index is not None:
        params["index"] = int(slice_index)

    content, content_type = await api_get_bytes(f"/api/series/{series_id}/thumbnail", params=params)
    b64 = base64.b64encode(content).decode("ascii")
    mime = content_type.split(";")[0].strip() or "image/jpeg"
    slice_desc = "middle" if slice_index is None else f"index {slice_index}"
    caption = f"Series {series_id}, slice {slice_desc}, W/L offsets {wc}/{ww}, max_side {max_side}"
    return [
        ImageContent(type="image", data=b64, mimeType=mime),
        TextContent(type="text", text=caption),
    ]


async def _fetch_one_thumbnail(series_id: str, max_side: int) -> tuple[str, bytes | None, str]:
    try:
        jpeg, mime = await api_get_bytes(
            f"/api/series/{series_id}/thumbnail",
            params={"max_side": max_side},
        )
        return series_id, jpeg, mime
    except Exception as exc:
        return series_id, None, f"error: {exc}"


async def _get_study_thumbnails(arguments: dict) -> list[TextContent | ImageContent]:
    study_id = arguments["study_id"]
    max_series = int(arguments.get("max_series", 12))
    max_side = int(arguments.get("max_side", 512))

    study: Any = await api_get(f"/api/studies/{study_id}")
    all_series: list[dict] = list(study.get("series") or [])
    shown = all_series[:max_series]
    omitted = all_series[max_series:]

    results = await asyncio.gather(*(_fetch_one_thumbnail(str(s["id"]), max_side) for s in shown))

    description = study.get("study_description") or "(no description)"
    summary_lines = [
        f"ImagingStudy {study_id} — {len(all_series)} series, showing {len(shown)} "
        f"thumbnails (middle slice of each).",
        f"Description: {description}",
    ]
    for i, s in enumerate(shown):
        summary_lines.append(
            f"  [{i + 1}] series={s['id']} "
            f"modality={s.get('modality') or '?'} "
            f"body={s.get('body_part_examined') or '-'} "
            f"{s.get('series_description') or ''}"
        )
    if omitted:
        summary_lines.append(f"{len(omitted)} series omitted — fetch with get_series_thumbnail:")
        for s in omitted:
            summary_lines.append(f"  - {s['id']} ({s.get('modality') or '?'})")

    content: list[TextContent | ImageContent] = [
        TextContent(type="text", text="\n".join(summary_lines))
    ]
    for series_id, jpeg, mime_or_err in results:
        if jpeg is None:
            content.append(TextContent(type="text", text=f"series {series_id}: {mime_or_err}"))
            continue
        content.append(
            ImageContent(
                type="image",
                data=base64.b64encode(jpeg).decode("ascii"),
                mimeType=mime_or_err or "image/jpeg",
            )
        )
    return content


async def handle(name: str, arguments: dict) -> list[TextContent | ImageContent]:
    if name == "get_series_thumbnail":
        return await _get_series_thumbnail(arguments)
    if name == "get_study_thumbnails":
        return await _get_study_thumbnails(arguments)
    raise ValueError(f"unknown tool: {name}")
