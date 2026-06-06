"""Sprint 5 MCP tools — imaging reads + annotation writes.

Reads:
* ``get_series_dicom_meta`` — allowlisted DICOM tags (ADR 0011).
* ``get_series_slice`` — proxy over the existing thumbnail endpoint
  (axial windowing + slice index). Coronal / sagittal MPR planes will
  land in a follow-up Sprint 5b once the SimpleITK pipeline is
  generalised over arbitrary normals.

Writes:
* ``write_annotation`` — POST a marker (the existing markers API
  surface).
* ``update_annotation`` / ``delete_annotation`` — PATCH / DELETE on
  the marker. Backend enforces the agent-cannot-touch-human rule
  (Sprint 5 ADR — see api/markers.py).
"""

from __future__ import annotations

import json
from typing import Any

import httpx
from mcp.types import TextContent, Tool, ToolAnnotations

from bvmcp.tools.client import (
    api_delete,
    api_get,
    api_get_bytes,
    api_patch,
    api_post_with_headers,
    format_http_error,
)

# Common disambiguation appended to every write-tool description.
#
# The MCP host (Claude.ai, Claude Desktop, ...) intercepts mutating tool
# calls and prompts the human user for explicit approval before the
# request ever reaches this server. When the user does not click
# "Approve" within the host's window, the host short-circuits the call
# and returns the literal string ``No approval received`` to the LLM.
#
# That string is generated CLIENT-SIDE: our server has not seen the
# request, and the failure has nothing to do with OAuth scopes, RBAC,
# patient-scope enforcement, or backend validation. Authentic
# scope/permission failures from this server come through as JSON
# payloads with ``error: "backend_error"`` and ``http_status: 401|403``
# (see ``format_http_error``). Tell the two apart by inspecting the
# response: structured JSON = server-side decision; bare "No approval
# received" = host approval flow.
#
# Recovery for "No approval received" is to ask the user to approve and
# retry the same call (passing ``idempotency_key`` if a write).
_APPROVAL_NOTE = (
    "APPROVAL FLOW: the MCP host prompts the human user for approval "
    "before this tool runs. If the call returns the literal string "
    '"No approval received" the user did not click Approve in time. '
    "That is NOT a scope or permission error; the request never "
    "reached the server. Authentic permission errors come back as "
    'JSON with ``error: "backend_error"`` and ``http_status`` 401/403. '
    "Recovery: ask the user to approve and retry (re-use "
    "``idempotency_key`` if you set one)."
)


TOOLS: list[Tool] = [
    Tool(
        name="get_series_dicom_meta",
        description=(
            "Return the allowlisted DICOM metadata for one instance of "
            "a series (Modality, BodyPart, geometry tags, kVp, TR/TE, …). "
            "PHI tags (PatientName, AccessionNumber, …) and private tags "
            "are filtered server-side. Pick ``instance_index`` to choose "
            "a slice; default 0 is the first."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "series_id": {"type": "string"},
                "instance_index": {
                    "type": "integer",
                    "default": 0,
                    "minimum": 0,
                },
                "version": {"type": "string", "default": "v1"},
            },
            "required": ["series_id"],
        },
    ),
    Tool(
        name="get_series_slice",
        description=(
            "Return one slice of the series as a JPEG (base64 inside the "
            "MCP response). ``index`` is 0-based; ``wc_delta`` and "
            "``ww_delta`` shift the DICOM default window. Coronal / "
            "sagittal planes are not yet implemented — Sprint 5b."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "series_id": {"type": "string"},
                "index": {"type": "integer", "minimum": 0},
                "wc_delta": {"type": "number", "default": 0},
                "ww_delta": {"type": "number", "default": 0},
                "max_side": {
                    "type": "integer",
                    "default": 512,
                    "minimum": 64,
                    "maximum": 2048,
                },
            },
            "required": ["series_id"],
        },
    ),
    Tool(
        name="write_annotation",
        annotations=ToolAnnotations(
            title="Write annotation (marker)",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
        description=(
            f"{_APPROVAL_NOTE}\n\n"
            "Create an annotation (marker) anchored to a study / series / "
            "instance. ``kind`` is a closed vocabulary, call "
            "``help(topic='annotation_kinds')`` for the full list and the "
            "JSON ``geometry`` shape each kind expects. Common kinds: "
            "``measurement.distance`` (2 points), ``measurement.bbox`` "
            "(2D box, 2 corners on one slice), ``bbox.lesion`` (3D box "
            "for a lesion, ``{min_ijk, max_ijk}``; round-trips "
            "``find_hot_spots`` output), ``fiducial`` (1 3D point), "
            "``reading-note`` (free-text bookmark, prose in ``body``). "
            "Use ``computed`` for derived metrics (e.g. SUV, volume_ml). "
            "Pass ``dry_run=true`` to validate the call (RBAC + kind + "
            "patient existence) without writing; the response is the "
            "would-be marker with ``id='dry-run'``. The backend records "
            "``author_kind=agent`` automatically when the caller is "
            "agent-authenticated. Unknown ``kind`` → HTTP 422 with the "
            "allowed list in ``ctx.allowed_kinds``. "
            ""
            "TARGET CONVENTION: a ``measurement.*`` you want the "
            "radiologist to see on the viewer canvas should use "
            "``target_kind='study'`` + ``target_id=<study_id>`` (the "
            "canvas draws all study-anchored measurements regardless of "
            "the open series). Series-anchored measurements still appear "
            "in the sidebar panel but only render on the canvas while "
            "viewing that specific series. ``bbox.lesion`` and "
            "``fiducial`` are naturally series-anchored (they reference "
            "a specific volume's voxel coordinates)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "patient_id": {"type": "string"},
                "target_kind": {
                    "type": "string",
                    "enum": ["study", "series", "instance"],
                },
                "target_id": {"type": "string"},
                "kind": {
                    "type": "string",
                    "enum": [
                        "measurement.distance",
                        "measurement.angle",
                        "measurement.area",
                        "measurement.ellipse",
                        "measurement.freehand",
                        "measurement.arrow",
                        "measurement.text",
                        "measurement.probe",
                        "measurement.bbox",
                        "measurement.sphere",
                        "bbox.lesion",
                        "bbox.exclusion",
                        "fiducial",
                        "reading-note",
                        "text-overlay",
                    ],
                    "description": (
                        "Marker kind. ``bbox.exclusion`` marks a region "
                        "ROI-stats / hot-spot search must ignore (same "
                        "geometry as bbox.lesion); ``measurement.sphere`` "
                        "is a CircleROI used for PERCIST SUVpeak. See "
                        "help(topic='annotation_kinds') for the per-kind "
                        "geometry shape."
                    ),
                },
                "geometry": {
                    "type": "object",
                    "description": (
                        "Kind-specific JSON. Measurement.*: "
                        "{axis, points: [[i,j,k], ...]}. bbox.lesion: "
                        "{min_ijk:[i,j,k], max_ijk:[i,j,k]}. "
                        "fiducial: {point:[i,j,k]}. "
                        "reading-note/text-overlay: {anchor:[i,j,k]}."
                    ),
                },
                "body": {
                    "type": "string",
                    "description": (
                        "Free-text label. Required for reading-note; optional everywhere else."
                    ),
                },
                "computed": {
                    "type": "object",
                    "description": (
                        "Derived metrics (e.g. {value, unit} for "
                        "measurements; {suv_max, volume_ml, ...} for "
                        "bbox.lesion). Server stores verbatim."
                    ),
                },
                "dry_run": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Validate everything and return the would-be marker without writing it."
                    ),
                },
                "idempotency_key": {"type": "string"},
            },
            "required": ["patient_id", "target_kind", "target_id", "kind"],
        },
    ),
    Tool(
        name="update_annotation",
        annotations=ToolAnnotations(
            title="Update annotation (marker)",
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=True,
            openWorldHint=False,
        ),
        description=(
            f"{_APPROVAL_NOTE}\n\n"
            "Mutate an existing annotation (marker). Every edit is tracked "
            "(revision history + audit) and attributed to the agent. The "
            "backend rejects the call with 403 when the agent tries to "
            "touch a marker authored by a human. Pass ``if_match`` (the "
            "``etag`` returned by get_annotations / a prior write) to make "
            "the edit safe against a concurrent change: a stale token "
            "→ HTTP 412 instead of silently clobbering. The response "
            "echoes the new ``etag`` to chain the next edit."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "marker_id": {"type": "string"},
                "geometry": {"type": "object"},
                "body": {"type": "string"},
                "computed": {"type": "object"},
                "if_match": {
                    "type": "string",
                    "description": (
                        "Optional optimistic-concurrency token (the "
                        "marker's current ``etag``). Recommended for "
                        "concurrent edits."
                    ),
                },
                "idempotency_key": {"type": "string"},
            },
            "required": ["marker_id"],
        },
    ),
    Tool(
        name="delete_annotation",
        annotations=ToolAnnotations(
            title="Delete annotation (marker)",
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=True,
            openWorldHint=False,
        ),
        description=(
            f"{_APPROVAL_NOTE}\n\n"
            "Soft-delete an annotation (marker). The removal is "
            "recoverable via ``restore_annotation`` and recorded in the "
            "revision history; it is not destroyed. 403 when an agent "
            "attempts to delete a human-authored marker. Pass ``if_match`` "
            "for safe concurrent deletion (stale token → 412)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "marker_id": {"type": "string"},
                "reason": {
                    "type": "string",
                    "description": "Optional free-text reason recorded on the tombstone.",
                },
                "if_match": {
                    "type": "string",
                    "description": "Optional optimistic-concurrency token (the marker's etag).",
                },
            },
            "required": ["marker_id"],
        },
    ),
    Tool(
        name="restore_annotation",
        annotations=ToolAnnotations(
            title="Restore annotation (marker)",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
        description=(
            f"{_APPROVAL_NOTE}\n\n"
            "Bring a soft-deleted annotation (marker) back to life. "
            "409 when the marker is not a tombstone. Use "
            "``get_annotations`` with ``include_deleted=true`` to find "
            "removed markers and their ids."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "marker_id": {"type": "string"},
            },
            "required": ["marker_id"],
        },
    ),
    Tool(
        name="get_annotation_revisions",
        annotations=ToolAnnotations(
            title="Get annotation revision history",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
        description=(
            "Read the full create / update / delete / restore history of "
            "an annotation (marker), most recent first. Each revision "
            "carries the acting ``author_kind`` (human / agent) and the "
            "marker snapshot at that point, so an agent edit is auditable "
            "and a prior value can be inspected."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "marker_id": {"type": "string"},
                "limit": {
                    "type": "integer",
                    "default": 200,
                    "minimum": 1,
                    "maximum": 1000,
                },
            },
            "required": ["marker_id"],
        },
    ),
    Tool(
        name="crop_series_roi",
        description=(
            "Crop a rectangular ROI on one slice of a series (Sprint 5b). "
            "``bbox`` is in JPEG-domain pixel coordinates "
            "(``[x0, y0, x1, y1]``); the response is a base64 JPEG. "
            "Window adjustments via ``wc_delta``/``ww_delta`` mirror "
            "the thumbnail endpoint."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "series_id": {"type": "string"},
                "instance_index": {"type": "integer", "default": 0, "minimum": 0},
                "bbox": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "minItems": 4,
                    "maxItems": 4,
                    "description": "[x0, y0, x1, y1]",
                },
                "wc_delta": {"type": "number", "default": 0},
                "ww_delta": {"type": "number", "default": 0},
                "max_side": {"type": "integer", "default": 512, "minimum": 64, "maximum": 2048},
            },
            "required": ["series_id", "bbox"],
        },
    ),
    Tool(
        name="measure_distance",
        description=(
            "Euclidean distance in mm between two pixel-space points "
            "``(i, j, k)`` over a series. Spacing comes from the "
            "allowlisted DICOM meta of the first instance "
            "(PixelSpacing + SliceThickness / SpacingBetweenSlices). "
            "422 ``measurement_unavailable`` when metadata is too "
            "sparse."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "series_id": {"type": "string"},
                "a": {
                    "type": "object",
                    "properties": {
                        "i": {"type": "number"},
                        "j": {"type": "number"},
                        "k": {"type": "number"},
                    },
                    "required": ["i", "j", "k"],
                },
                "b": {
                    "type": "object",
                    "properties": {
                        "i": {"type": "number"},
                        "j": {"type": "number"},
                        "k": {"type": "number"},
                    },
                    "required": ["i", "j", "k"],
                },
            },
            "required": ["series_id", "a", "b"],
        },
    ),
    Tool(
        name="measure_volume",
        description=(
            "Axis-aligned bounding-box volume in mm^3 (and ml) given "
            "two opposite corners ``(i, j, k)`` in pixel space. Same "
            "spacing source as ``measure_distance``."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "series_id": {"type": "string"},
                "p0": {
                    "type": "object",
                    "properties": {
                        "i": {"type": "number"},
                        "j": {"type": "number"},
                        "k": {"type": "number"},
                    },
                    "required": ["i", "j", "k"],
                },
                "p1": {
                    "type": "object",
                    "properties": {
                        "i": {"type": "number"},
                        "j": {"type": "number"},
                        "k": {"type": "number"},
                    },
                    "required": ["i", "j", "k"],
                },
            },
            "required": ["series_id", "p0", "p1"],
        },
    ),
    Tool(
        name="get_suv",
        description=(
            "Return SUV scaling factors for a PET series (Sprint 6). "
            "Response carries ``suv_factor_bw`` plus the LBM and BSA "
            "variants (Janmahasatian / James / Mosteller / DuBois) per "
            "EANM 2.0 / PERCIST 1.0. 422 ``suv_unavailable`` lists the "
            "missing DICOM tags when the metadata is incomplete."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "series_id": {"type": "string"},
            },
            "required": ["series_id"],
        },
    ),
    Tool(
        name="find_hot_spots",
        description=(
            "Find the top-N high-uptake connected regions in a "
            "series volume (PERCIST-style lesion finder). Default "
            "threshold is 50% of the volume max; ``threshold_mode="
            "'absolute_suv'`` accepts a literal SUV cut-off (requires "
            "a PET series). Returns each spot's centroid IJK, bbox "
            "IJK, voxel count, volume in mL, raw max/mean and "
            "SUVmax/mean/peak (1 cm³ kernel). 409 ``volume not yet "
            "packed`` when the cache is cold — open the viewer or "
            "GET /series/{id}/volume.raw first. "
            "Pass ``exclude_segmentation_labels`` (typical: "
            "['kidney_left','kidney_right','urinary_bladder']) to remove "
            "high-uptake organs from the search before connected-"
            "components, or ``exclude_marker_ids`` of "
            "``kind='bbox.exclusion'`` markers when no automatic "
            "segmentation mask is available."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "series_id": {"type": "string"},
                "threshold_mode": {
                    "type": "string",
                    "enum": ["percent_of_max", "absolute_suv"],
                    "default": "percent_of_max",
                },
                "threshold_value": {
                    "type": "number",
                    "exclusiveMinimum": 0,
                    "default": 0.5,
                },
                "min_volume_ml": {"type": "number", "minimum": 0, "default": 0.5},
                "top_n": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
                "suv_variant": {
                    "type": "string",
                    "enum": ["bw", "lbm-janma", "lbm-james", "bsa-mosteller", "bsa-dubois"],
                    "default": "bw",
                },
                "exclude_segmentation_labels": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 32,
                    "description": (
                        "TotalSegmentator labels to subtract before "
                        "connected-components. Missing labels are dropped "
                        "silently so a search can still run while a "
                        "segmentation job is in flight."
                    ),
                },
                "exclude_marker_ids": {
                    "type": "array",
                    "items": {"type": "string", "format": "uuid"},
                    "maxItems": 16,
                    "description": (
                        "Marker ids of kind='bbox.exclusion' whose ijk-"
                        "bbox is excluded. Fallback when no automatic "
                        "segmentation is available."
                    ),
                },
            },
            "required": ["series_id"],
        },
    ),
    Tool(
        name="compute_roi_stats",
        description=(
            "Compute deterministic ROI statistics on a series volume "
            "(mean / std / min / max / SUVpeak 1 cm³). Coordinates are "
            "in voxel index space ``[i, j, k]`` (same indexing the "
            "volume viewer uses). ``kind=rectangle|ellipse`` use the "
            "axis-aligned bbox ``min_ijk / max_ijk``; ``kind=sphere`` "
            "uses ``center_ijk + radius_mm`` to build a 3D spherical "
            "mask in physical space (the PERCIST 1.0 §4.3 liver-"
            "reference workflow: radius 15.0 mm = 3 cm diameter). "
            "When ``suv_variant`` is set on a PET series, the response "
            "also carries ``suv_mean / suv_sd / suv_max / suv_peak`` "
            "scaled with the corresponding factor. 409 ``volume not "
            "yet packed`` when the cache is cold — call "
            "``get_series_dicom_meta`` or open the viewer first to warm it."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "series_id": {"type": "string"},
                "min_ijk": {
                    "type": "array",
                    "items": {"type": "integer", "minimum": 0},
                    "minItems": 3,
                    "maxItems": 3,
                    "description": "[i_min, j_min, k_min], inclusive — required for rectangle/ellipse",
                },
                "max_ijk": {
                    "type": "array",
                    "items": {"type": "integer", "minimum": 0},
                    "minItems": 3,
                    "maxItems": 3,
                    "description": "[i_max, j_max, k_max], inclusive — required for rectangle/ellipse",
                },
                "center_ijk": {
                    "type": "array",
                    "items": {"type": "integer", "minimum": 0},
                    "minItems": 3,
                    "maxItems": 3,
                    "description": "Sphere center [i, j, k] — required when kind=sphere",
                },
                "radius_mm": {
                    "type": "number",
                    "exclusiveMinimum": 0,
                    "maximum": 200.0,
                    "description": "Sphere radius in mm — required when kind=sphere (15.0 for PERCIST liver reference)",
                },
                "kind": {
                    "type": "string",
                    "enum": ["rectangle", "ellipse", "sphere"],
                    "default": "rectangle",
                },
                "suv_variant": {
                    "type": "string",
                    "enum": ["bw", "lbm-janma", "lbm-james", "bsa-mosteller", "bsa-dubois"],
                    "description": "When set, scale mean/sd/max/peak by the matching SUV factor",
                },
                "exclude_segmentation_labels": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 32,
                    "description": (
                        "TotalSegmentator labels to subtract from the "
                        "ROI before stats. Typical PET use: "
                        "['kidney_left','kidney_right','urinary_bladder']."
                    ),
                },
                "exclude_marker_ids": {
                    "type": "array",
                    "items": {"type": "string", "format": "uuid"},
                    "maxItems": 16,
                    "description": (
                        "Marker ids of kind='bbox.exclusion' whose ijk-"
                        "bbox is subtracted from the ROI."
                    ),
                },
            },
            "required": ["series_id"],
        },
    ),
    Tool(
        name="get_segmentations",
        description=(
            "List segmentations registered against a series. Each row "
            "carries the ``label`` (e.g. ``kidney_left``, ``liver``, "
            "``urinary_bladder``), the raw ``size_bytes`` of the uint8 "
            "mask, and ``nonzero_voxels`` when available. Bytes are "
            "served (auth-checked) via ``GET /api/series/{id}/"
            "segmentations/{label}`` — no presigned URL, no bucket / "
            "key in the response (storage isolation)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "series_id": {"type": "string"},
            },
            "required": ["series_id"],
        },
    ),
    Tool(
        name="register_series",
        description=(
            "Enqueue a cross-modal registration job between two series "
            "(Sprint 6 P3). ``kind=rigid`` uses Mattes mutual "
            "information + regular-step gradient descent (the inter-"
            "modality classic). ``kind=demons`` is reserved but not "
            "yet implemented. The response carries the registration id; "
            "poll ``get_registration`` for status and the saved "
            "transform URL once the worker finishes."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "fixed_series_id": {"type": "string"},
                "moving_series_id": {"type": "string"},
                "kind": {
                    "type": "string",
                    "enum": ["rigid", "demons"],
                    "default": "rigid",
                },
            },
            "required": ["fixed_series_id", "moving_series_id"],
        },
    ),
    Tool(
        name="get_registration",
        description=(
            "Poll a registration. Returns status + result_meta + a "
            "presigned URL for the saved transform when the job has "
            "succeeded."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "registration_id": {"type": "string"},
                "ttl_seconds": {"type": "integer", "default": 300, "minimum": 60, "maximum": 900},
            },
            "required": ["registration_id"],
        },
    ),
]


async def _get_series_dicom_meta(args: dict[str, Any]) -> str:
    series_id = args["series_id"]
    params: dict[str, Any] = {}
    if "instance_index" in args:
        params["instance_index"] = args["instance_index"]
    if "version" in args:
        params["version"] = args["version"]
    payload = await api_get(
        f"/api/series/{series_id}/dicom_meta",
        params=params or None,
    )
    return json.dumps(payload, indent=2, ensure_ascii=False)


async def _get_series_slice(args: dict[str, Any]) -> str:
    """Proxy over the existing /thumbnail endpoint.

    The MCP transport ships a base64-wrapped JPEG inside the JSON
    response so the agent can inspect / forward it without an extra
    binary fetch.
    """
    import base64

    series_id = args["series_id"]
    params: dict[str, Any] = {}
    if "index" in args:
        params["index"] = args["index"]
    if "wc_delta" in args:
        params["wc_delta"] = args["wc_delta"]
    if "ww_delta" in args:
        params["ww_delta"] = args["ww_delta"]
    if "max_side" in args:
        params["max_side"] = args["max_side"]
    data, ctype = await api_get_bytes(f"/api/series/{series_id}/thumbnail", params=params or None)
    return json.dumps(
        {
            "content_type": ctype,
            "base64": base64.b64encode(data).decode("ascii"),
            "size_bytes": len(data),
        }
    )


_ANNOTATION_KIND_HINT = (
    "Call help(topic='annotation_kinds') to see the valid marker kinds "
    "and the geometry shape each one expects."
)


async def _write_annotation(args: dict[str, Any]) -> str:
    patient_id = args["patient_id"]
    body = {
        "target_kind": args["target_kind"],
        "target_id": args["target_id"],
        "kind": args["kind"],
    }
    for k in ("geometry", "body", "computed"):
        if k in args:
            body[k] = args[k]
    params: dict[str, Any] = {}
    if args.get("dry_run"):
        params["dry_run"] = "true"
    try:
        payload, _headers = await api_post_with_headers(
            f"/api/patients/{patient_id}/markers",
            json=body,
            params=params or None,
            idempotency_key=args.get("idempotency_key"),
        )
    except httpx.HTTPStatusError as exc:
        return format_http_error(exc, hint=_ANNOTATION_KIND_HINT)
    return json.dumps(payload, indent=2, ensure_ascii=False)


async def _update_annotation(args: dict[str, Any]) -> str:
    marker_id = args["marker_id"]
    body: dict[str, Any] = {}
    for k in ("geometry", "body", "computed"):
        if k in args:
            body[k] = args[k]
    try:
        payload, _headers = await api_patch(
            f"/api/markers/{marker_id}",
            json=body,
            if_match=args.get("if_match"),
            idempotency_key=args.get("idempotency_key"),
        )
    except httpx.HTTPStatusError as exc:
        return format_http_error(exc, hint=_ANNOTATION_KIND_HINT)
    return json.dumps(payload, indent=2, ensure_ascii=False)


async def _delete_annotation(args: dict[str, Any]) -> str:
    marker_id = args["marker_id"]
    params: dict[str, Any] = {}
    if args.get("reason"):
        params["reason"] = args["reason"]
    try:
        code = await api_delete(
            f"/api/markers/{marker_id}",
            params=params or None,
            if_match=args.get("if_match"),
        )
    except httpx.HTTPStatusError as exc:
        return format_http_error(exc)
    return json.dumps({"status": "deleted", "recoverable": True, "http_status": code})


async def _restore_annotation(args: dict[str, Any]) -> str:
    marker_id = args["marker_id"]
    try:
        payload, _headers = await api_post_with_headers(
            f"/api/markers/{marker_id}/restore",
        )
    except httpx.HTTPStatusError as exc:
        return format_http_error(exc)
    return json.dumps(payload, indent=2, ensure_ascii=False)


async def _get_annotation_revisions(args: dict[str, Any]) -> str:
    marker_id = args["marker_id"]
    params: dict[str, Any] = {"limit": int(args.get("limit", 200))}
    try:
        payload = await api_get(f"/api/markers/{marker_id}/revisions", params=params)
    except httpx.HTTPStatusError as exc:
        return format_http_error(exc)
    return json.dumps(payload, indent=2, ensure_ascii=False)


async def _crop_series_roi(args: dict[str, Any]) -> str:
    """POST /series/:sid/crop, base64-wrap the JPEG response."""
    import base64

    series_id = args["series_id"]
    body = {
        "instance_index": int(args.get("instance_index", 0)),
        "bbox": args["bbox"],
        "wc_delta": float(args.get("wc_delta", 0)),
        "ww_delta": float(args.get("ww_delta", 0)),
        "max_side": int(args.get("max_side", 512)),
    }
    # The crop endpoint returns image/jpeg directly — bypass the
    # JSON parser by using api_get_bytes-style access. We POST JSON
    # via httpx + decode the binary response inline.
    import httpx

    from bvmcp.config import get_settings as _get_mcp_settings
    from bvmcp.tools.client import _augment_headers as _aug

    settings = _get_mcp_settings()
    url = f"{settings.backend_base_url}/api/series/{series_id}/crop"
    headers = _aug()
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(url, headers=headers, json=body)
        resp.raise_for_status()
        data = resp.content
        rh = dict(resp.headers)
    return json.dumps(
        {
            "series_id": series_id,
            "content_type": rh.get("content-type") or "image/jpeg",
            "bbox": rh.get("x-bbox"),
            "image_size": rh.get("x-image-size"),
            "size_bytes": len(data),
            "base64": base64.b64encode(data).decode("ascii"),
        }
    )


async def _measure_distance(args: dict[str, Any]) -> str:
    series_id = args["series_id"]
    body = {"a": args["a"], "b": args["b"]}
    payload, _headers = await api_post_with_headers(
        f"/api/series/{series_id}/measure/distance", json=body
    )
    return json.dumps(payload, indent=2, ensure_ascii=False)


async def _measure_volume(args: dict[str, Any]) -> str:
    series_id = args["series_id"]
    body = {"p0": args["p0"], "p1": args["p1"]}
    payload, _headers = await api_post_with_headers(
        f"/api/series/{series_id}/measure/volume", json=body
    )
    return json.dumps(payload, indent=2, ensure_ascii=False)


async def _get_suv(args: dict[str, Any]) -> str:
    series_id = args["series_id"]
    payload = await api_get(f"/api/series/{series_id}/suv")
    return json.dumps(payload, indent=2, ensure_ascii=False)


async def _find_hot_spots(args: dict[str, Any]) -> str:
    series_id = args["series_id"]
    body: dict[str, Any] = {}
    for k in (
        "threshold_mode",
        "threshold_value",
        "min_volume_ml",
        "top_n",
        "suv_variant",
        "exclude_segmentation_labels",
        "exclude_marker_ids",
    ):
        if args.get(k) is not None:
            body[k] = args[k]
    payload, _headers = await api_post_with_headers(f"/api/series/{series_id}/hot-spots", json=body)
    return json.dumps(payload, indent=2, ensure_ascii=False)


async def _compute_roi_stats(args: dict[str, Any]) -> str:
    series_id = args["series_id"]
    kind = args.get("kind", "rectangle")
    body: dict[str, Any] = {"kind": kind}
    if kind == "sphere":
        if "center_ijk" not in args or "radius_mm" not in args:
            return (
                "Error: kind=sphere requires center_ijk (list of 3 ints) "
                "and radius_mm (positive number)"
            )
        body["center_ijk"] = args["center_ijk"]
        body["radius_mm"] = args["radius_mm"]
    else:
        if "min_ijk" not in args or "max_ijk" not in args:
            return f"Error: kind={kind} requires min_ijk and max_ijk (both lists of 3 ints)"
        body["min_ijk"] = args["min_ijk"]
        body["max_ijk"] = args["max_ijk"]
    if args.get("suv_variant"):
        body["suv_variant"] = args["suv_variant"]
    if args.get("exclude_segmentation_labels"):
        body["exclude_segmentation_labels"] = args["exclude_segmentation_labels"]
    if args.get("exclude_marker_ids"):
        body["exclude_marker_ids"] = args["exclude_marker_ids"]
    payload, _headers = await api_post_with_headers(f"/api/series/{series_id}/roi-stats", json=body)
    return json.dumps(payload, indent=2, ensure_ascii=False)


async def _get_segmentations(args: dict[str, Any]) -> str:
    series_id = args["series_id"]
    payload = await api_get(f"/api/series/{series_id}/segmentations")
    return json.dumps(payload, indent=2, ensure_ascii=False)


async def _register_series(args: dict[str, Any]) -> str:
    body = {
        "fixed_series_id": args["fixed_series_id"],
        "moving_series_id": args["moving_series_id"],
        "kind": args.get("kind", "rigid"),
    }
    payload, _headers = await api_post_with_headers("/api/registrations", json=body)
    return json.dumps(payload, indent=2, ensure_ascii=False)


async def _get_registration(args: dict[str, Any]) -> str:
    registration_id = args["registration_id"]
    params: dict[str, Any] = {}
    if args.get("ttl_seconds"):
        params["ttl_seconds"] = args["ttl_seconds"]
    payload = await api_get(f"/api/registrations/{registration_id}", params=params or None)
    return json.dumps(payload, indent=2, ensure_ascii=False)


_DISPATCH = {
    "get_series_dicom_meta": _get_series_dicom_meta,
    "get_series_slice": _get_series_slice,
    "write_annotation": _write_annotation,
    "update_annotation": _update_annotation,
    "delete_annotation": _delete_annotation,
    "restore_annotation": _restore_annotation,
    "get_annotation_revisions": _get_annotation_revisions,
    "crop_series_roi": _crop_series_roi,
    "measure_distance": _measure_distance,
    "measure_volume": _measure_volume,
    "get_suv": _get_suv,
    "compute_roi_stats": _compute_roi_stats,
    "find_hot_spots": _find_hot_spots,
    "get_segmentations": _get_segmentations,
    "register_series": _register_series,
    "get_registration": _get_registration,
}


async def handle(name: str, arguments: dict[str, Any]) -> str | list[TextContent]:
    handler = _DISPATCH.get(name)
    if handler is None:
        return f"Error: unknown tool '{name}' in imaging module"
    return await handler(arguments)
