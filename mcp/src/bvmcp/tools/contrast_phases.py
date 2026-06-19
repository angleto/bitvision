"""MCP tools — multiphase contrast-CT acquisition phases.

* ``list_study_phases`` (imaging:read) — the study's series ordered by
  acquisition time, each with its classified contrast phase, confidence,
  source, and the packed volume's FrameOfReferenceUID (so the viewer can
  tell which phases share a coordinate frame and sync with no registration).
* ``detect_study_phases`` (imaging:compute) — run the classifier and
  persist ``phase_source='auto'`` labels. Idempotent; preserves human
  overrides unless ``force=true``.

These are the acquisition/contrast phase (non-contrast / arterial /
portal-venous / delayed / ...), NOT the care-timeline ``phases:*`` tools
(an unrelated concept). A low-confidence label carries
``needs_confirmation=true``: present it as a candidate for the human to
confirm, never as a settled fact.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
from mcp.types import TextContent, Tool, ToolAnnotations

from bvmcp.tools.client import (
    api_delete,
    api_get,
    api_patch,
    api_post_with_headers,
    format_http_error,
)

# Acquisition-phase vocabulary (mirrors backend db.models.dicom.ACQUISITION_PHASES;
# inlined because the MCP package cannot import the backend).
_ACQUISITION_PHASES = [
    "unenhanced",
    "arterial",
    "portal_venous",
    "delayed",
    "hepatobiliary",
    "corticomedullary",
    "nephrographic",
    "excretory",
    "dynamic",
    "other",
]

_APPROVAL_NOTE = (
    "APPROVAL FLOW: the MCP host prompts the human user for approval "
    "before this tool runs. If the call returns the literal string "
    '"No approval received" the user did not click Approve in time. '
    "That is NOT a scope or permission error; the request never reached "
    "the server. Authentic permission errors come back as JSON with "
    '``error: "backend_error"`` and ``http_status`` 401/403.'
)


TOOLS: list[Tool] = [
    Tool(
        name="list_study_phases",
        annotations=ToolAnnotations(
            title="List contrast/acquisition phases of a study",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
        description=(
            "Return the multiphase contrast-CT manifest for a study: its "
            "series ordered by acquisition time, each with the classified "
            "``acquisition_phase`` (unenhanced / arterial / portal_venous / "
            "delayed / hepatobiliary / corticomedullary / nephrographic / "
            "excretory / dynamic / other, or null when unclassified), "
            "``phase_confidence`` (0..1), ``phase_source`` (auto / human), "
            "``needs_confirmation`` (true = low-confidence candidate the "
            "human should confirm), ``acquisition_time_of_day``, the "
            "``contrast_bolus_agent``, ``frame_of_reference_uid`` (when "
            "the volume is packed), ``series_plane`` (axial / sagittal / "
            "coronal / oblique, from packed geometry), and "
            "``is_reviewable_phase`` (true = an axial CT phase volume to open "
            "as a pane; false = a localizer / capture / dose report / "
            "bolus-prep / MPR reformat to ignore). Read-only: reflects the "
            "persisted state; run ``detect_study_phases`` first if everything "
            "is unclassified."
        ),
        inputSchema={
            "type": "object",
            "properties": {"study_id": {"type": "string"}},
            "required": ["study_id"],
        },
    ),
    Tool(
        name="detect_study_phases",
        annotations=ToolAnnotations(
            title="Detect contrast/acquisition phases of a study",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
        description=(
            f"{_APPROVAL_NOTE}\n\n"
            "Run the contrast-phase classifier over the study's CT series "
            "and persist the auto labels (``phase_source='auto'``), then "
            "return the refreshed manifest (same shape as "
            "``list_study_phases``). Idempotent. By default a human-"
            "confirmed phase (``phase_source='human'``) is NEVER "
            "overwritten; pass ``force=true`` to re-classify and overwrite "
            "even those. The classifier is heuristic: trust high-confidence "
            "labels, treat ``needs_confirmation=true`` ones as candidates."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "study_id": {"type": "string"},
                "force": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Re-classify and OVERWRITE even human-confirmed "
                        "phases. Default false: human overrides are preserved."
                    ),
                },
            },
            "required": ["study_id"],
        },
    ),
    Tool(
        name="set_series_acquisition_phase",
        annotations=ToolAnnotations(
            title="Set/confirm a series' contrast phase (human override)",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
        description=(
            f"{_APPROVAL_NOTE}\n\n"
            "Human override of one series' contrast phase: the confirm/"
            "correct half of the candidate+confirm model. Pin a phase and "
            "it is stored as ``phase_source='human'`` so the auto "
            "classifier never overwrites it (``detect_study_phases`` skips "
            "human rows unless force=true). Pass ``acquisition_phase=null`` "
            "to clear the label and re-enable auto-classification. Use "
            "``dry_run=true`` to validate without writing. Requires study "
            "ownership."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "series_id": {"type": "string"},
                "acquisition_phase": {
                    "type": ["string", "null"],
                    "enum": [*_ACQUISITION_PHASES, None],
                    "description": ("The phase to pin, or null to clear and re-enable auto."),
                },
                "dry_run": {"type": "boolean", "default": False},
            },
            "required": ["series_id", "acquisition_phase"],
        },
    ),
    Tool(
        name="compute_phase_washout",
        annotations=ToolAnnotations(
            title="Compute cross-phase HU + wash-out (APW/RPW)",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
        description=(
            "Sample ONE patient-space (LPS) ROI in every classified CT phase "
            "of a study and compute the wash-out, REGION-AWARE. APW = "
            "100*(E-D)/(E-U), RPW = 100*(E-D)/E (E=enhanced/portal-venous, "
            "D=delayed, U=unenhanced). ``region`` scopes the interpretation: "
            "'adrenal' emits the adenoma threshold flags (apw_ge_60, "
            "rpw_ge_40, unenhanced_below_10hu — Korobkin 1998; Szolar 1998, "
            "NOT a diagnosis); 'liver' WITHHOLDS the adrenal indices/flags "
            "(liver wash-out is qualitative, relative to parenchyma per "
            "LI-RADS) and, given a parenchyma_center_lps + parenchyma_radius_mm "
            "reference sphere, returns the lesion-vs-parenchyma relative_curve "
            "(delta_hu<0 ⇒ lesion hypodense vs liver); omitted returns the raw "
            "indices without verdict flags. Lesion ROI in 'sphere' (center_lps "
            "+ radius_mm) or 'bbox' (min_lps + max_lps). Phases in a different "
            "frame of reference are reported under 'skipped'. Volumes must be "
            "packed (open the viewer or GET volume.raw first)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "study_id": {"type": "string"},
                "kind": {"type": "string", "enum": ["sphere", "bbox"], "default": "sphere"},
                "center_lps": {
                    "type": "array",
                    "items": {"type": "number"},
                    "minItems": 3,
                    "maxItems": 3,
                    "description": "[x, y, z] LPS centre — required for kind=sphere",
                },
                "radius_mm": {
                    "type": "number",
                    "exclusiveMinimum": 0,
                    "description": "Sphere radius in mm — required for kind=sphere",
                },
                "min_lps": {
                    "type": "array",
                    "items": {"type": "number"},
                    "minItems": 3,
                    "maxItems": 3,
                    "description": "[x, y, z] LPS — required for kind=bbox",
                },
                "max_lps": {
                    "type": "array",
                    "items": {"type": "number"},
                    "minItems": 3,
                    "maxItems": 3,
                    "description": "[x, y, z] LPS — required for kind=bbox",
                },
                "frame_of_reference_uid": {
                    "type": "string",
                    "description": "Frame the ROI is defined in (default: first packed phase).",
                },
                "region": {
                    "type": "string",
                    "enum": ["adrenal", "liver", "other"],
                    "description": (
                        "Anatomical region — scopes the interpretation. 'adrenal' emits the "
                        "APW/RPW adenoma flags; 'liver' withholds them (liver wash-out is "
                        "relative-to-parenchyma per LI-RADS, qualitative) and uses the "
                        "parenchyma ROI below; omitted/'other' returns raw indices, no flags."
                    ),
                },
                "parenchyma_center_lps": {
                    "type": "array",
                    "items": {"type": "number"},
                    "minItems": 3,
                    "maxItems": 3,
                    "description": (
                        "[x, y, z] LPS centre of the reference-parenchyma sphere (liver "
                        "workflow): sampled in every phase to report lesion-vs-parenchyma."
                    ),
                },
                "parenchyma_radius_mm": {
                    "type": "number",
                    "exclusiveMinimum": 0,
                    "description": "Radius of the reference-parenchyma sphere in mm.",
                },
            },
            "required": ["study_id"],
        },
    ),
    Tool(
        name="compute_washout_map",
        annotations=ToolAnnotations(
            title="Per-voxel wash-out / subtraction heat map",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
        description=(
            "Per-voxel wash-out / subtraction heat map over a lesion region, as "
            "a base64 PNG (GREEN = wash-out, RED = uptake; alpha by magnitude). "
            "metric='washout' = enhanced-delayed; 'subtraction' = "
            "enhanced-unenhanced. v1 needs the two phases on a COMMON voxel grid "
            "(same geometry) else 422. Returns metric, phase_a, phase_b, vabs (HU "
            "colour scale), width, height, png_base64."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "study_id": {"type": "string"},
                "center_lps": {
                    "type": "array",
                    "items": {"type": "number"},
                    "minItems": 3,
                    "maxItems": 3,
                    "description": "[x, y, z] LPS centre of the lesion region",
                },
                "radius_mm": {
                    "type": "number",
                    "exclusiveMinimum": 0,
                    "description": "in-plane half-extent of the cropped map, in mm",
                },
                "metric": {
                    "type": "string",
                    "enum": ["washout", "subtraction"],
                    "default": "washout",
                },
            },
            "required": ["study_id", "center_lps", "radius_mm"],
        },
    ),
    Tool(
        name="create_phase_enhancement_set",
        annotations=ToolAnnotations(
            title="Save a cross-phase wash-out measurement",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=False,
        ),
        description=(
            f"{_APPROVAL_NOTE}\n\n"
            "Persist a wash-out measurement as an auditable record: the ROI "
            "plus the per-phase HU ``samples`` (from compute_phase_washout) "
            "and a label. The server recomputes APW/RPW from the samples so "
            "the stored numbers are consistent. dry_run validates without "
            "writing. Requires study ownership; stamped with agent provenance."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "study_id": {"type": "string"},
                "roi_kind": {"type": "string", "enum": ["sphere", "bbox"]},
                "roi": {
                    "type": "object",
                    "description": "ROI coords in LPS (center_lps+radius_mm | min_lps+max_lps).",
                },
                "label": {"type": "string"},
                "samples": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "series_id": {"type": "string"},
                            "acquisition_phase": {"type": "string"},
                            "hu_mean": {"type": "number"},
                            "hu_std": {"type": "number"},
                            "voxel_count": {"type": "integer"},
                        },
                        "required": ["series_id", "acquisition_phase", "hu_mean"],
                    },
                },
                "region": {
                    "type": "string",
                    "enum": ["adrenal", "liver", "other"],
                    "description": (
                        "Scopes the recomputed indices/flags: 'adrenal' emits the adenoma "
                        "flags; 'liver' withholds the adrenal indices; omitted returns the raw "
                        "numbers without verdict flags. Mirrors compute_phase_washout."
                    ),
                },
                "dry_run": {"type": "boolean", "default": False},
            },
            "required": ["study_id", "roi_kind", "roi", "samples"],
        },
    ),
    Tool(
        name="list_phase_enhancement_sets",
        annotations=ToolAnnotations(
            title="List saved wash-out measurements of a study",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
        description=(
            "List the persisted wash-out measurements of a study (each with "
            "its ROI, per-phase samples, APW/RPW and provenance). Pass "
            "include_deleted=true to also list soft-deleted ones."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "study_id": {"type": "string"},
                "include_deleted": {"type": "boolean", "default": False},
            },
            "required": ["study_id"],
        },
    ),
    Tool(
        name="get_phase_enhancement_set",
        annotations=ToolAnnotations(
            title="Get a saved wash-out measurement",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
        description="Read one persisted wash-out measurement by id.",
        inputSchema={
            "type": "object",
            "properties": {"set_id": {"type": "string"}},
            "required": ["set_id"],
        },
    ),
    Tool(
        name="delete_phase_enhancement_set",
        annotations=ToolAnnotations(
            title="Delete a saved wash-out measurement",
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=True,
            openWorldHint=False,
        ),
        description=(
            f"{_APPROVAL_NOTE}\n\n"
            "Soft-delete a persisted wash-out measurement (recoverable via "
            "restore_phase_enhancement_set). An agent cannot delete a "
            "human-authored measurement. Requires study ownership."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "set_id": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["set_id"],
        },
    ),
    Tool(
        name="restore_phase_enhancement_set",
        annotations=ToolAnnotations(
            title="Restore a deleted wash-out measurement",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
        description=(
            f"{_APPROVAL_NOTE}\n\n"
            "Restore a soft-deleted wash-out measurement. Requires study "
            "ownership."
        ),
        inputSchema={
            "type": "object",
            "properties": {"set_id": {"type": "string"}},
            "required": ["set_id"],
        },
    ),
]


async def _list_study_phases(args: dict[str, Any]) -> str:
    study_id = args["study_id"]
    try:
        payload = await api_get(f"/api/studies/{study_id}/phases")
    except httpx.HTTPStatusError as exc:
        return format_http_error(exc)
    return json.dumps(payload, indent=2, ensure_ascii=False)


async def _detect_study_phases(args: dict[str, Any]) -> str:
    study_id = args["study_id"]
    params: dict[str, Any] = {}
    if args.get("force"):
        params["force"] = "true"
    try:
        payload, _headers = await api_post_with_headers(
            f"/api/studies/{study_id}/phases/detect",
            params=params or None,
        )
    except httpx.HTTPStatusError as exc:
        return format_http_error(exc)
    return json.dumps(payload, indent=2, ensure_ascii=False)


async def _set_series_acquisition_phase(args: dict[str, Any]) -> str:
    series_id = args["series_id"]
    body: dict[str, Any] = {"acquisition_phase": args.get("acquisition_phase")}
    if args.get("dry_run"):
        body["dry_run"] = True
    try:
        payload, _headers = await api_patch(
            f"/api/series/{series_id}/acquisition-phase",
            json=body,
        )
    except httpx.HTTPStatusError as exc:
        return format_http_error(exc)
    return json.dumps(payload, indent=2, ensure_ascii=False)


async def _compute_phase_washout(args: dict[str, Any]) -> str:
    study_id = args["study_id"]
    body: dict[str, Any] = {"kind": args.get("kind", "sphere")}
    for k in (
        "center_lps",
        "radius_mm",
        "min_lps",
        "max_lps",
        "frame_of_reference_uid",
        "region",
        "parenchyma_center_lps",
        "parenchyma_radius_mm",
    ):
        if args.get(k) is not None:
            body[k] = args[k]
    try:
        payload, _headers = await api_post_with_headers(
            f"/api/studies/{study_id}/phase-roi-stats",
            json=body,
        )
    except httpx.HTTPStatusError as exc:
        return format_http_error(exc)
    return json.dumps(payload, indent=2, ensure_ascii=False)


async def _compute_washout_map(args: dict[str, Any]) -> str:
    study_id = args["study_id"]
    body: dict[str, Any] = {
        "center_lps": args["center_lps"],
        "radius_mm": args["radius_mm"],
        "metric": args.get("metric", "washout"),
    }
    try:
        payload, _headers = await api_post_with_headers(
            f"/api/studies/{study_id}/washout-map",
            json=body,
        )
    except httpx.HTTPStatusError as exc:
        return format_http_error(exc)
    return json.dumps(payload, indent=2, ensure_ascii=False)


async def _create_phase_enhancement_set(args: dict[str, Any]) -> str:
    study_id = args["study_id"]
    body: dict[str, Any] = {
        "roi_kind": args["roi_kind"],
        "roi": args["roi"],
        "samples": args["samples"],
    }
    if args.get("label") is not None:
        body["label"] = args["label"]
    if args.get("region") is not None:
        body["region"] = args["region"]
    if args.get("dry_run"):
        body["dry_run"] = True
    try:
        payload, _headers = await api_post_with_headers(
            f"/api/studies/{study_id}/phase-enhancement-sets",
            json=body,
        )
    except httpx.HTTPStatusError as exc:
        return format_http_error(exc)
    return json.dumps(payload, indent=2, ensure_ascii=False)


async def _list_phase_enhancement_sets(args: dict[str, Any]) -> str:
    study_id = args["study_id"]
    params: dict[str, Any] = {}
    if args.get("include_deleted"):
        params["include_deleted"] = "true"
    try:
        payload = await api_get(
            f"/api/studies/{study_id}/phase-enhancement-sets", params=params or None
        )
    except httpx.HTTPStatusError as exc:
        return format_http_error(exc)
    return json.dumps(payload, indent=2, ensure_ascii=False)


async def _get_phase_enhancement_set(args: dict[str, Any]) -> str:
    try:
        payload = await api_get(f"/api/phase-enhancement-sets/{args['set_id']}")
    except httpx.HTTPStatusError as exc:
        return format_http_error(exc)
    return json.dumps(payload, indent=2, ensure_ascii=False)


async def _delete_phase_enhancement_set(args: dict[str, Any]) -> str:
    params: dict[str, Any] = {}
    if args.get("reason"):
        params["reason"] = args["reason"]
    try:
        code = await api_delete(
            f"/api/phase-enhancement-sets/{args['set_id']}", params=params or None
        )
    except httpx.HTTPStatusError as exc:
        return format_http_error(exc)
    return json.dumps({"status": "deleted", "recoverable": True, "http_status": code})


async def _restore_phase_enhancement_set(args: dict[str, Any]) -> str:
    try:
        payload, _headers = await api_post_with_headers(
            f"/api/phase-enhancement-sets/{args['set_id']}/restore"
        )
    except httpx.HTTPStatusError as exc:
        return format_http_error(exc)
    return json.dumps(payload, indent=2, ensure_ascii=False)


_DISPATCH = {
    "list_study_phases": _list_study_phases,
    "detect_study_phases": _detect_study_phases,
    "set_series_acquisition_phase": _set_series_acquisition_phase,
    "compute_phase_washout": _compute_phase_washout,
    "compute_washout_map": _compute_washout_map,
    "create_phase_enhancement_set": _create_phase_enhancement_set,
    "list_phase_enhancement_sets": _list_phase_enhancement_sets,
    "get_phase_enhancement_set": _get_phase_enhancement_set,
    "delete_phase_enhancement_set": _delete_phase_enhancement_set,
    "restore_phase_enhancement_set": _restore_phase_enhancement_set,
}


async def handle(name: str, arguments: dict[str, Any]) -> str | list[TextContent]:
    handler = _DISPATCH.get(name)
    if handler is None:
        return f"Error: unknown tool '{name}' in contrast_phases module"
    return await handler(arguments)
