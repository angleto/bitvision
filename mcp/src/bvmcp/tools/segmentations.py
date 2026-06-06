"""MCP tools — segmentation writes (voxel-level mask production).

The viewer + worker tier already expose three segmentation write
surfaces:

* TotalSegmentator (multi-organ CT, async job, multi-label output).
* MedSAM-2 (click / bbox-prompted, single-slice, synchronous).
* Raw NIfTI / NRRD / DICOM SEG upload (manual or programmatic
  external producer).

Until now MCP was read-only on segmentations (``get_segmentations``).
That left the agent without a way to *produce* voxel-level masks even
though the backend was ready. This module fills the gap so an agent
can run the full discovery → segmentation → persistence loop in one
session (e.g. ``find_hot_spots`` → ``bbox.lesion`` marker →
``predict_segmentation_interactive`` with the bbox centroid →
persisted mask under a semantic label).

Provenance (P1, resolved): every mask write now also writes a
``Segmentation`` ORM row carrying producer (``manual`` / ``medsam`` /
``totalsegmentator``), patient_id, ``author_kind`` (human / agent /
system, so an AI mask is distinguishable), ``agent_token_id`` and the
``nonzero_voxels`` size metric. ``get_segmentations`` surfaces those
fields; the listing is ORM-authoritative and merges any legacy S3-only
blob as a fallback. An agent token cannot overwrite a human-authored
mask. DICOM SEG interop export remains a separate follow-up (blocked on
volume-build geometry preservation, not on this lineage work).
"""

from __future__ import annotations

import base64
import json
from typing import Any

import httpx
from mcp.types import Tool

from bvmcp.config import get_settings as _get_mcp_settings
from bvmcp.tools.client import (
    _augment_headers,
    api_delete,
    api_post_with_headers,
    format_http_error,
)

_SEG_HINT = (
    "Call help(topic='segmentations') for the full mask-production "
    "loop (auto vs interactive vs upload) and the per-tool expected "
    "shape."
)

TOOLS: list[Tool] = [
    Tool(
        name="auto_segment_series",
        description=(
            "Enqueue a TotalSegmentator job over a CT series. Produces "
            "one binary mask per ROI in ``roi_subset`` (defaults to a "
            "curated abdomen + thorax subset on the worker side); the "
            "result lands under the series' segmentations prefix with "
            "one label per ROI. Returns immediately with ``job_id`` + "
            "the requested ROI list — poll ``get_segmentations`` to "
            "see new labels appear. Use this for whole-volume "
            "anatomical priors before a lesion-level pass. "
            "Agent-authored runs are recorded in the audit trail."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "series_id": {"type": "string"},
                "roi_subset": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Optional whitelist of ROI names "
                        "(snake_case, TotalSegmentator vocabulary). "
                        "Omit for the default curated subset."
                    ),
                },
                "overwrite": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "When true, replace existing masks for the ROIs being recomputed."
                    ),
                },
                "fast": {
                    "type": "boolean",
                    "default": True,
                    "description": (
                        "Use the fast (3mm) TotalSegmentator preset. "
                        "Disable for a slower 1.5mm pass."
                    ),
                },
            },
            "required": ["series_id"],
        },
    ),
    Tool(
        name="predict_segmentation_interactive",
        description=(
            "Run MedSAM-2 on a single slice and return the resulting "
            "2D binary mask. Synchronous from the caller's perspective "
            "(~3-10 s CPU inference, capped at 60 s). ``points`` are "
            "in-slice pixel coordinates [(x, y), ...] referenced to "
            "the slice picked by ``axis`` + ``slice_idx``; ``labels`` "
            "are optional 0/1 markers per point (1 = include, "
            "0 = exclude). Set ``label`` to ALSO persist the slice "
            "mask into a full-volume binary under that semantic "
            "label, so subsequent calls can retrieve / display it. "
            "Typical agent loop: take a ``bbox.lesion`` marker, "
            "compute centroid + 4 corner points on the centroid "
            "slice, prompt MedSAM-2, persist with a descriptive "
            "label (e.g. ``lesion_3_iliac``). The response carries "
            "the base64 2D mask + (when persisted) ``persisted_label``."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "series_id": {"type": "string"},
                "axis": {
                    "type": "integer",
                    "enum": [0, 1, 2],
                    "description": (
                        "0 = sagittal (X-fixed), 1 = coronal (Y-fixed), 2 = axial (Z-fixed)."
                    ),
                },
                "slice_idx": {
                    "type": "integer",
                    "minimum": 0,
                    "description": (
                        "Voxel index along ``axis`` (the slice the points are referenced to)."
                    ),
                },
                "points": {
                    "type": "array",
                    "items": {
                        "type": "array",
                        "items": {"type": "number"},
                        "minItems": 2,
                        "maxItems": 2,
                    },
                    "minItems": 1,
                    "description": ("List of [(x, y), ...] in slice pixel coords."),
                },
                "labels": {
                    "type": "array",
                    "items": {"type": "integer", "enum": [0, 1]},
                    "description": (
                        "Optional 0/1 per point. 1 = include "
                        "(foreground), 0 = exclude (background). "
                        "Defaults to all-1."
                    ),
                },
                "label": {
                    "type": "string",
                    "pattern": r"^[a-zA-Z0-9._-]{1,64}$",
                    "description": (
                        "When set, persist the 2D mask embedded in a "
                        "full-volume binary under this label. Must "
                        "match [a-zA-Z0-9._-]{1,64}."
                    ),
                },
            },
            "required": ["series_id", "axis", "slice_idx", "points"],
        },
    ),
    Tool(
        name="delete_segmentation",
        description=(
            "Delete a persisted segmentation mask under the given "
            "``label`` for the series. Idempotent: a 204 comes back "
            "whether the label existed or not. Use this to clean up "
            "broken / overwritten masks from an iterative loop "
            "(typical pattern: predict with a tentative label, review "
            "in the viewer, delete + re-predict with adjusted prompt "
            "points). Pairs with ``upload_segmentation`` / "
            "``predict_segmentation_interactive`` for full CRUD."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "series_id": {"type": "string"},
                "label": {
                    "type": "string",
                    "pattern": r"^[a-zA-Z0-9._-]{1,64}$",
                    "description": "Semantic label to remove.",
                },
            },
            "required": ["series_id", "label"],
        },
    ),
    Tool(
        name="upload_segmentation",
        description=(
            "Upload a voxel-level mask (NIfTI .nii/.nii.gz or NRRD "
            ".nrrd) under a semantic ``label`` for the series. The "
            "backend binarises the input and resamples it to the "
            "source volume's dims; the result is stored as a raw "
            "uint8 buffer indexed by label. Use this for masks "
            "produced outside the platform (Slicer, ITK-SNAP, MONAI "
            "Label external, custom workers) or when an agent has "
            "computed a deterministic mask (e.g. SUV-threshold) it "
            "wants to persist. ``content_base64`` is the raw file "
            "bytes base64-encoded; max 200 MiB after decode."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "series_id": {"type": "string"},
                "label": {
                    "type": "string",
                    "pattern": r"^[a-zA-Z0-9._-]{1,64}$",
                    "description": "Semantic label, [a-zA-Z0-9._-]{1,64}.",
                },
                "filename": {
                    "type": "string",
                    "description": (
                        "Original filename with extension "
                        "(.nii, .nii.gz, .nrrd) so the importer "
                        "picks the right parser."
                    ),
                },
                "content_base64": {
                    "type": "string",
                    "description": (
                        "Mask file bytes, base64-encoded. The "
                        "backend rejects anything > 200 MiB after "
                        "decode."
                    ),
                },
            },
            "required": ["series_id", "label", "filename", "content_base64"],
        },
    ),
]


async def _auto_segment_series(args: dict[str, Any]) -> str:
    series_id = args["series_id"]
    body: dict[str, Any] = {
        "overwrite": bool(args.get("overwrite", False)),
        "fast": bool(args.get("fast", True)),
    }
    if args.get("roi_subset") is not None:
        body["roi_subset"] = list(args["roi_subset"])
    try:
        payload, _headers = await api_post_with_headers(
            f"/api/series/{series_id}/segmentations/auto",
            json=body,
        )
    except httpx.HTTPStatusError as exc:
        return format_http_error(exc, hint=_SEG_HINT)
    return json.dumps(payload, indent=2, ensure_ascii=False)


async def _predict_segmentation_interactive(args: dict[str, Any]) -> str:
    series_id = args["series_id"]
    body: dict[str, Any] = {
        "axis": int(args["axis"]),
        "slice_idx": int(args["slice_idx"]),
        "points": [[float(p[0]), float(p[1])] for p in args["points"]],
    }
    if args.get("labels") is not None:
        body["labels"] = [int(x) for x in args["labels"]]
    if args.get("label"):
        body["label"] = str(args["label"])
    try:
        payload, _headers = await api_post_with_headers(
            f"/api/series/{series_id}/segmentations/interactive/predict",
            json=body,
        )
    except httpx.HTTPStatusError as exc:
        return format_http_error(exc, hint=_SEG_HINT)
    # Trim the binary out of the conversation: the base64 mask can be
    # large. Echo size + shape so the agent can validate, and keep the
    # b64 payload available for the next tool call.
    if isinstance(payload, dict):
        mask_b64 = payload.get("mask_b64")
        if isinstance(mask_b64, str):
            payload = {
                **payload,
                "mask_b64_size": len(mask_b64),
            }
    return json.dumps(payload, indent=2, ensure_ascii=False)


async def _upload_segmentation(args: dict[str, Any]) -> str:
    series_id = args["series_id"]
    label = args["label"]
    filename = args["filename"]
    try:
        blob = base64.b64decode(args["content_base64"], validate=True)
    except (ValueError, TypeError) as exc:
        return json.dumps(
            {
                "error": "invalid_base64",
                "detail": str(exc),
                "hint": _SEG_HINT,
            }
        )
    if len(blob) > 200 * 1024 * 1024:
        return json.dumps(
            {
                "error": "payload_too_large",
                "size_bytes": len(blob),
                "limit_bytes": 200 * 1024 * 1024,
                "hint": (
                    "Re-encode the mask at a coarser dtype or split "
                    "into smaller labels before uploading."
                ),
            }
        )

    # The legacy upload endpoint is multipart-only; do the multipart
    # POST inline (mirrors the _crop_series_roi pattern). We strip the
    # JSON Accept header so httpx can pick the right boundary header.
    settings = _get_mcp_settings()
    url = f"{settings.backend_base_url}/api/series/{series_id}/segmentations"
    headers = _augment_headers()
    headers.pop("accept", None)
    files = {"file": (filename, blob, "application/octet-stream")}
    data = {"label": label}
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(url, headers=headers, files=files, data=data)
            resp.raise_for_status()
            payload = resp.json()
    except httpx.HTTPStatusError as exc:
        return format_http_error(exc, hint=_SEG_HINT)
    return json.dumps(payload, indent=2, ensure_ascii=False)


async def _delete_segmentation(args: dict[str, Any]) -> str:
    series_id = args["series_id"]
    label = args["label"]
    try:
        code = await api_delete(f"/api/series/{series_id}/segmentations/{label}")
    except httpx.HTTPStatusError as exc:
        return format_http_error(exc, hint=_SEG_HINT)
    return json.dumps(
        {
            "status": "deleted",
            "series_id": series_id,
            "label": label,
            "http_status": code,
        }
    )


_DISPATCH = {
    "auto_segment_series": _auto_segment_series,
    "predict_segmentation_interactive": _predict_segmentation_interactive,
    "upload_segmentation": _upload_segmentation,
    "delete_segmentation": _delete_segmentation,
}


async def handle(name: str, arguments: dict) -> str:
    fn = _DISPATCH.get(name)
    if fn is None:
        raise ValueError(f"unknown tool: {name}")
    return await fn(arguments or {})


__all__ = ["TOOLS", "handle"]
