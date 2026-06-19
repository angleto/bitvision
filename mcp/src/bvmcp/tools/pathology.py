"""MCP pathology / WSI read tools.

Lets an MCP client list whole-slide images, read their metadata, and —
crucially for a vision-capable LLM — actually SEE the tissue: the
thumbnail, the macro overview, and arbitrary deep-zoom REGION crops at a
chosen pyramid level (the gigapixel-slide analogue of ``crop_series_roi``
for DICOM). All gated on ``pathology:read``; storage-isolated (bytes are
proxied through the backend, never an S3 URL).

Backend contract (see ``api/pathology.py``):
- ``GET /api/pathology-slides?patient_id=&public_only=`` — list
- ``GET /api/pathology-slides/{id}`` — metadata (base_width/height,
  mpp, magnification, dzi_ready, dzi_levels, slide_class, license)
- ``GET /api/pathology-slides/{id}/thumbnail`` | ``/macro`` — overview JPEGs
- ``GET /api/pathology-slides/{id}/region?x&y&w&h&level`` — stitched JPEG
"""

from __future__ import annotations

import base64
import json
from typing import Any

from mcp.types import ImageContent, TextContent, Tool

from bvmcp.tools.client import api_get, api_get_bytes

TOOLS = [
    Tool(
        name="list_pathology_slides",
        description=(
            "List pathology whole-slide images (vetrini). Pass ``patient_id`` "
            "to list a patient's slides (the clinical case), or leave it out "
            "with ``public_only=true`` for the OpenData public library. Each "
            "row carries stain, magnification, slide_class (wsi / gross / "
            "micrograph), dimensions, and ``dzi_ready`` (whether the deep-zoom "
            "pyramid is built and the slide is viewable / region-readable)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "patient_id": {
                    "type": "string",
                    "description": "UUID of the patient whose slides to list (optional).",
                },
                "public_only": {
                    "type": "boolean",
                    "default": False,
                    "description": "Only OpenData public slides. Forced true for anonymous callers.",
                },
                "limit": {"type": "integer", "default": 60, "minimum": 1, "maximum": 200},
                "offset": {"type": "integer", "default": 0, "minimum": 0},
            },
        },
    ),
    Tool(
        name="get_pathology_slide",
        description=(
            "Read one slide's metadata: base_width / base_height (the level-0 "
            "pixel dimensions you address with get_slide_region), mpp_x / mpp_y "
            "(micrometres per pixel — multiply pixel distances by this for "
            "physical size), magnification, dzi_levels / dzi_tile_size, "
            "slide_class, stain, and license / citation. Call this before "
            "get_slide_region to learn the slide's dimensions and tiling state."
        ),
        inputSchema={
            "type": "object",
            "properties": {"slide_id": {"type": "string", "description": "UUID of the slide"}},
            "required": ["slide_id"],
        },
    ),
    Tool(
        name="get_slide_thumbnail",
        description=(
            "Fetch the slide's low-resolution thumbnail JPEG (base64) so a "
            "vision-capable client can see the whole tissue at a glance. "
            "Storage-isolated proxy."
        ),
        inputSchema={
            "type": "object",
            "properties": {"slide_id": {"type": "string", "description": "UUID of the slide"}},
            "required": ["slide_id"],
        },
    ),
    Tool(
        name="get_slide_macro",
        description=(
            "Fetch the slide's MACRO overview JPEG (a 1x photo of the whole "
            "glass slide, label barcode cropped out), if the scanner embedded "
            "one. 404 when absent."
        ),
        inputSchema={
            "type": "object",
            "properties": {"slide_id": {"type": "string", "description": "UUID of the slide"}},
            "required": ["slide_id"],
        },
    ),
    Tool(
        name="get_slide_region",
        description=(
            "Crop a rectangular REGION of the slide at a chosen deep-zoom "
            "level and return it as a JPEG (base64) — the way to actually "
            "look at tissue detail at high magnification. ``level`` is a "
            "DeepZoom pyramid level (0 = tiny overview, dzi_levels-1 = full "
            "resolution); ``x`` / ``y`` / ``w`` / ``h`` are pixel coordinates "
            "IN THAT LEVEL's image. Call get_pathology_slide first for "
            "base_width / base_height and dzi_levels. Bounded to 16 MP per "
            "call (request a smaller region or a coarser level if rejected)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "slide_id": {"type": "string", "description": "UUID of the slide"},
                "x": {"type": "integer", "minimum": 0, "description": "Left pixel at ``level``"},
                "y": {"type": "integer", "minimum": 0, "description": "Top pixel at ``level``"},
                "w": {"type": "integer", "minimum": 1, "description": "Width in pixels"},
                "h": {"type": "integer", "minimum": 1, "description": "Height in pixels"},
                "level": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "DeepZoom level (0..dzi_levels-1; higher = more detail)",
                },
            },
            "required": ["slide_id", "x", "y", "w", "h", "level"],
        },
    ),
]


async def _image_result(path: str, params: dict | None, caption: str) -> list[Any]:
    content, content_type = await api_get_bytes(path, params=params)
    b64 = base64.b64encode(content).decode("ascii")
    mime = content_type.split(";")[0].strip() or "image/jpeg"
    return [
        ImageContent(type="image", data=b64, mimeType=mime),
        TextContent(type="text", text=caption),
    ]


async def handle(name: str, arguments: dict) -> list[TextContent | ImageContent] | str:
    if name == "list_pathology_slides":
        params: dict[str, Any] = {
            "public_only": bool(arguments.get("public_only", False)),
            "limit": int(arguments.get("limit", 60)),
            "offset": int(arguments.get("offset", 0)),
        }
        if arguments.get("patient_id"):
            params["patient_id"] = arguments["patient_id"]
        result = await api_get("/api/pathology-slides", params=params)
        return json.dumps(result, indent=2, ensure_ascii=False)

    if name == "get_pathology_slide":
        result = await api_get(f"/api/pathology-slides/{arguments['slide_id']}")
        return json.dumps(result, indent=2, ensure_ascii=False)

    if name == "get_slide_thumbnail":
        sid = arguments["slide_id"]
        return await _image_result(
            f"/api/pathology-slides/{sid}/thumbnail", None, f"Slide {sid} thumbnail"
        )

    if name == "get_slide_macro":
        sid = arguments["slide_id"]
        return await _image_result(
            f"/api/pathology-slides/{sid}/macro", None, f"Slide {sid} macro overview"
        )

    if name == "get_slide_region":
        sid = arguments["slide_id"]
        region = {
            "x": int(arguments["x"]),
            "y": int(arguments["y"]),
            "w": int(arguments["w"]),
            "h": int(arguments["h"]),
            "level": int(arguments["level"]),
        }
        caption = (
            f"Slide {sid} region x={region['x']} y={region['y']} "
            f"w={region['w']} h={region['h']} @level {region['level']}"
        )
        return await _image_result(f"/api/pathology-slides/{sid}/region", region, caption)

    raise ValueError(f"unknown tool: {name}")
