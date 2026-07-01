"""MCP tools for the Finding entity — the structured diagnostic surface.

P3 of the annotation-layer overhaul (Flow note 76949928). Where
``write_annotation`` records a free-form geometric Marker, these tools let
an LLM record a *diagnosis* as a structured, coded, queryable ``Finding``
(``db/models/findings.py``): a controlled type / anatomy / morphology +
typed measurements, linked to its geometry, with full tracking
(create / edit / delete, provenance, If-Match, soft-delete + restore,
revision history). All wrap the backend ``/api/findings`` surface, which
enforces the conventions server-side.

Typical loop: ``get_finding_vocab`` (learn the slugs) → perceive
(``find_hot_spots`` / ``compute_roi_stats`` / ``get_series_slice``) →
``create_finding`` (assert the reperto) → ``update_finding`` /
``delete_finding`` (revise, always tracked) → ``search_findings`` (query
by type / size / morphology).
"""

from __future__ import annotations

import json
from typing import Any

import httpx
from mcp.types import Tool, ToolAnnotations

from bvmcp.tools.client import (
    api_delete,
    api_get,
    api_patch,
    api_post_with_headers,
    format_http_error,
)

_APPROVAL_NOTE = (
    "APPROVAL FLOW: the MCP host prompts the human for approval before "
    'this write runs. A bare "No approval received" is a host-side '
    "decline (the request never reached the server), NOT a scope error; "
    "authentic permission errors come back as JSON with "
    '``error: "backend_error"`` and ``http_status`` 401/403.'
)

_VOCAB_HINT = (
    "Call get_finding_vocab for the valid ``type`` / ``anatomy`` / "
    "``morphology`` slugs before creating; unknown slugs return HTTP 422."
)

# Measurement scalars a caller may set on a finding (kept in sync with the
# backend FindingCreateIn / FindingUpdateIn).
_MEASUREMENT_FIELDS = (
    "longest_diameter_mm",
    "short_axis_mm",
    "volume_ml",
    "suv_max",
    "suv_peak",
    "suv_mean",
    "hu_mean",
    "hu_std",
)

_MEASUREMENT_PROPS = {m: {"type": "number"} for m in _MEASUREMENT_FIELDS}


TOOLS: list[Tool] = [
    Tool(
        name="get_finding_vocab",
        annotations=ToolAnnotations(
            title="Get finding vocabulary",
            readOnlyHint=True,
            idempotentHint=True,
            openWorldHint=False,
        ),
        description=(
            "Return the controlled vocabularies a finding draws on: the "
            "valid finding ``type`` keys (with category), ``anatomy`` site "
            "keys (with laterality applicability), and ``morphology`` "
            "descriptor keys, each with its optional external code "
            "(RadLex / SNOMED / ICD-O). Read this first so create_finding / "
            "update_finding use valid slugs."
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="search_findings",
        annotations=ToolAnnotations(
            title="Search findings",
            readOnlyHint=True,
            idempotentHint=True,
            openWorldHint=False,
        ),
        description=(
            "Structured query over findings: filter by type / anatomy / "
            "laterality / morphology / status and by measurement ranges "
            "(diameter / volume / SUVmax). e.g. type='nodule', "
            "anatomy='lung_upper_lobe', morphology=['spiculated'], "
            'min_diameter_mm=10 answers "spiculated RUL nodule > 1 cm". '
            "Pass ``patient_id`` to scope to one patient; OMIT it for a "
            "corpus-wide search across every study you may read "
            "(visibility-scoped, never cross-patient). ``scope`` "
            "(all|mine|public) narrows the corpus search."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "patient_id": {
                    "type": "string",
                    "description": "Scope to one patient; omit for a corpus-wide search.",
                },
                "study_id": {"type": "string"},
                "type": {"type": "string", "description": "finding type key"},
                "anatomy": {"type": "string", "description": "anatomy site key"},
                "laterality": {
                    "type": "string",
                    "enum": ["left", "right", "bilateral", "midline"],
                },
                "morphology": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "require ALL of these morphology keys",
                },
                "status": {
                    "type": "string",
                    "enum": ["candidate", "confirmed", "retracted"],
                },
                "min_diameter_mm": {"type": "number"},
                "max_diameter_mm": {"type": "number"},
                "min_volume_ml": {"type": "number"},
                "min_suv_max": {"type": "number"},
                "scope": {
                    "type": "string",
                    "enum": ["all", "mine", "public"],
                    "default": "all",
                    "description": "Corpus search only (ignored when patient_id is set).",
                },
                "include_deleted": {"type": "boolean", "default": False},
                "limit": {"type": "integer", "default": 200, "minimum": 1, "maximum": 1000},
            },
        },
    ),
    Tool(
        name="find_similar_findings",
        annotations=ToolAnnotations(
            title="Find visually similar findings",
            readOnlyHint=True,
            idempotentHint=True,
            openWorldHint=False,
        ),
        description=(
            "Cohort-by-lesion discovery: given a finding, return other "
            "findings on studies whose imaging is visually similar (BiomedCLIP "
            "series embeddings), best match first — the core value of a "
            "biobank: 'show me comparable lesions across the corpus'. "
            "RETRIEVAL, NOT DIAGNOSIS: ranks by imaging appearance, makes no "
            "clinical claim. Visibility-scoped (never cross-patient). Each "
            "result carries a ``score`` (0..1 series similarity) and the "
            "``matched_series_id``. Pass ``same_type=true`` to restrict to the "
            "anchor's finding type, ``modality`` to filter, ``k`` to cap "
            "results. 422 when the finding has no series or its series is not "
            "yet indexed for visual search."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "finding_id": {"type": "string"},
                "k": {"type": "integer", "default": 10, "minimum": 1, "maximum": 100},
                "same_type": {
                    "type": "boolean",
                    "default": False,
                    "description": "Only findings of the same type as the anchor.",
                },
                "modality": {"type": "string"},
                "scope": {
                    "type": "string",
                    "enum": ["all", "public", "mine", "shared"],
                    "description": "Visibility narrowing (never widens the auth boundary).",
                },
            },
            "required": ["finding_id"],
        },
    ),
    Tool(
        name="get_finding",
        annotations=ToolAnnotations(
            title="Get finding",
            readOnlyHint=True,
            idempotentHint=True,
            openWorldHint=False,
        ),
        description="Read one finding by id, including its linked geometry. ETag in the response.",
        inputSchema={
            "type": "object",
            "properties": {"finding_id": {"type": "string"}},
            "required": ["finding_id"],
        },
    ),
    Tool(
        name="get_finding_revisions",
        annotations=ToolAnnotations(
            title="Get finding revision history",
            readOnlyHint=True,
            idempotentHint=True,
            openWorldHint=False,
        ),
        description=(
            "Full create / update / delete / restore history of a finding, "
            "most recent first, each with the acting ``author_kind`` and the "
            "snapshot at that revision."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "finding_id": {"type": "string"},
                "limit": {"type": "integer", "default": 200, "minimum": 1, "maximum": 1000},
            },
            "required": ["finding_id"],
        },
    ),
    Tool(
        name="create_finding",
        annotations=ToolAnnotations(
            title="Create finding",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
        description=(
            f"{_APPROVAL_NOTE}\n\n"
            "Record a structured diagnosis (finding) on a study: a coded "
            "``type`` (required) + optional ``anatomy`` + ``laterality`` + "
            "``morphology`` descriptors + typed measurements "
            "(longest_diameter_mm, volume_ml, suv_max, …) + optional "
            "``geometry_refs`` linking the underlying Marker(s) / "
            "Segmentation mask(s). The backend forces author_kind=agent and "
            "records a revision. Pass ``dry_run=true`` to validate without "
            f"writing. {_VOCAB_HINT}"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "patient_id": {"type": "string"},
                "study_id": {"type": "string"},
                "series_id": {"type": "string"},
                "frame_of_reference_uid": {"type": "string"},
                "type": {"type": "string", "description": "finding type key (required)"},
                "anatomy": {"type": "string", "description": "anatomy site key"},
                "laterality": {
                    "type": "string",
                    "enum": ["left", "right", "bilateral", "midline"],
                },
                "morphology": {"type": "array", "items": {"type": "string"}},
                **_MEASUREMENT_PROPS,
                "bbox_lps": {
                    "type": "object",
                    "description": "world-space bbox {min:[x,y,z], max:[x,y,z]}",
                },
                "status": {
                    "type": "string",
                    "enum": ["candidate", "confirmed", "retracted"],
                    "default": "candidate",
                },
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "description": {"type": "string"},
                "geometry_refs": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "marker_id": {"type": "string"},
                            "segmentation_id": {"type": "string"},
                            "role": {
                                "type": "string",
                                "enum": ["measurement", "bbox", "mask", "fiducial"],
                            },
                        },
                        "required": ["role"],
                    },
                },
                "dry_run": {"type": "boolean", "default": False},
                "idempotency_key": {"type": "string"},
            },
            "required": ["patient_id", "study_id", "type"],
        },
    ),
    Tool(
        name="update_finding",
        annotations=ToolAnnotations(
            title="Update finding",
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=True,
            openWorldHint=False,
        ),
        description=(
            f"{_APPROVAL_NOTE}\n\n"
            "Revise a finding (only the fields you pass change). Tracked + "
            "attributed to the agent; the backend rejects edits to a "
            "human-authored finding with 403. Pass ``if_match`` (the "
            "finding's ``etag``) for safe concurrent edits (stale → 412). "
            f"{_VOCAB_HINT}"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "finding_id": {"type": "string"},
                "type": {"type": "string"},
                "anatomy": {"type": "string"},
                "laterality": {
                    "type": "string",
                    "enum": ["left", "right", "bilateral", "midline"],
                },
                "morphology": {"type": "array", "items": {"type": "string"}},
                **_MEASUREMENT_PROPS,
                "bbox_lps": {"type": "object"},
                "status": {
                    "type": "string",
                    "enum": ["candidate", "confirmed", "retracted"],
                },
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "description": {"type": "string"},
                "if_match": {"type": "string"},
                "idempotency_key": {"type": "string"},
            },
            "required": ["finding_id"],
        },
    ),
    Tool(
        name="delete_finding",
        annotations=ToolAnnotations(
            title="Delete finding",
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=True,
            openWorldHint=False,
        ),
        description=(
            f"{_APPROVAL_NOTE}\n\n"
            "Soft-delete a finding (recoverable via restore_finding, "
            "recorded in the revision history). 403 when an agent attempts "
            "to delete a human-authored finding. Pass ``if_match`` for safe "
            "concurrent deletion."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "finding_id": {"type": "string"},
                "reason": {"type": "string"},
                "if_match": {"type": "string"},
            },
            "required": ["finding_id"],
        },
    ),
    Tool(
        name="restore_finding",
        annotations=ToolAnnotations(
            title="Restore finding",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
        description=(
            f"{_APPROVAL_NOTE}\n\n"
            "Bring a soft-deleted finding back to life. 409 when it is not a "
            "tombstone (use search_findings with include_deleted=true to find one)."
        ),
        inputSchema={
            "type": "object",
            "properties": {"finding_id": {"type": "string"}},
            "required": ["finding_id"],
        },
    ),
    Tool(
        name="add_finding_geometry",
        annotations=ToolAnnotations(
            title="Link geometry to finding",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=False,
        ),
        description=(
            f"{_APPROVAL_NOTE}\n\n"
            "Link an existing Marker or Segmentation mask to a finding as "
            "its geometry, with a ``role`` (measurement / bbox / mask / "
            "fiducial). The marker / segmentation must belong to the same "
            "patient."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "finding_id": {"type": "string"},
                "marker_id": {"type": "string"},
                "segmentation_id": {"type": "string"},
                "role": {
                    "type": "string",
                    "enum": ["measurement", "bbox", "mask", "fiducial"],
                },
            },
            "required": ["finding_id", "role"],
        },
    ),
    Tool(
        name="promote_finding_measurement",
        annotations=ToolAnnotations(
            title="Promote a live PET-VOI measurement onto a finding",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
        description=(
            f"{_APPROVAL_NOTE}\n\n"
            "Recompute a live measurement SERVER-SIDE and materialise it onto "
            "the finding's typed measurement columns, so the corpus is "
            "quantitatively queryable. The number is measured from the pixels, "
            "not taken from you. Sources:\n"
            "- ``voi_spherical`` (PET): ``center_mm`` + ``radius_mm`` → "
            "SUVmax/peak/mean + MTV (volume_ml).\n"
            "- ``voi_threshold`` (PET): ``seed_mm`` + ``threshold`` → same.\n"
            "- ``roi_stats``: ``roi`` (bbox/sphere in voxel indices) → "
            "hu_mean/hu_std (HU on CT), or the SUV columns when ``roi`` carries "
            "a PET ``suv_variant``.\n"
            "- ``measure_volume``: ``volume`` (two bbox corners in voxel "
            "indices) → longest_diameter_mm/short_axis_mm/volume_ml.\n"
            "VOI coords are mm (origin at voxel 0,0,0); roi/volume coords are "
            "voxel indices. The series must belong to the finding's study. The "
            "finding's ``status`` is left unchanged (a human confirms it). "
            "Optionally pass ``geometry_marker_id`` to link the placed marker "
            "as the measurement geometry. Idempotent on ``idempotency_key``; "
            "pass ``if_match`` (the finding's etag) for optimistic concurrency."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "finding_id": {"type": "string"},
                "series_id": {"type": "string"},
                "source": {
                    "type": "string",
                    "enum": ["voi_spherical", "voi_threshold", "roi_stats", "measure_volume"],
                },
                "center_mm": {
                    "type": "object",
                    "properties": {
                        "x": {"type": "number"},
                        "y": {"type": "number"},
                        "z": {"type": "number"},
                    },
                    "required": ["x", "y", "z"],
                    "description": "VOI sphere center (voi_spherical).",
                },
                "radius_mm": {
                    "type": "number",
                    "description": "VOI sphere radius (voi_spherical).",
                },
                "seed_mm": {
                    "type": "object",
                    "properties": {
                        "x": {"type": "number"},
                        "y": {"type": "number"},
                        "z": {"type": "number"},
                    },
                    "required": ["x", "y", "z"],
                    "description": "Seed point (voi_threshold).",
                },
                "threshold": {"type": "number", "description": "Cutoff (voi_threshold)."},
                "threshold_units": {"type": "string", "enum": ["SUV", "raw"]},
                "roi": {
                    "type": "object",
                    "description": (
                        "ROI geometry for source='roi_stats': "
                        "{kind:'rectangle'|'ellipse'|'sphere', min_ijk:[i,j,k], "
                        "max_ijk:[i,j,k]} or {kind:'sphere', center_ijk:[i,j,k], "
                        "radius_mm}. Optional suv_variant ('bw' etc.) on a PET series."
                    ),
                },
                "volume": {
                    "type": "object",
                    "description": (
                        "Bbox for source='measure_volume': "
                        "{p0:{i,j,k}, p1:{i,j,k}} (voxel indices)."
                    ),
                },
                "geometry_marker_id": {"type": "string"},
                "idempotency_key": {"type": "string"},
                "if_match": {"type": "string"},
            },
            "required": ["finding_id", "series_id", "source"],
        },
    ),
    Tool(
        name="create_findings_from_hot_spots",
        annotations=ToolAnnotations(
            title="Create candidate findings from PET/CT hot spots",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
        description=(
            f"{_APPROVAL_NOTE}\n\n"
            "Run the hot-spot lesion finder (``find_hot_spots``) on a series "
            "and create ONE ``candidate`` finding per detected spot — a "
            "creation flow, distinct from promote_finding_measurement (which "
            "writes onto one existing finding). Each finding gets the spot's "
            "volume_ml (+ SUVmax/peak/mean when the series is a dosed PET), "
            "author_kind=agent, and a bbox.lesion marker linked as its bbox "
            "geometry. Idempotent on the spot signature: re-running detection "
            "does not duplicate findings for spots already materialised. "
            "``type`` is the finding-type vocab key to assign. Findings stay "
            f"``candidate`` until a human confirms them. {_VOCAB_HINT}"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "series_id": {"type": "string"},
                "type": {"type": "string", "description": "finding type key (required)"},
                "hot_spots": {
                    "type": "object",
                    "description": (
                        "find_hot_spots params: {threshold_mode:'percent_of_max'|"
                        "'absolute_suv', threshold_value, top_n, min_volume_ml, "
                        "suv_variant?, slice_min?, slice_max?, "
                        "exclude_segmentation_labels?, exclude_marker_ids?}."
                    ),
                },
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "idempotency_key": {"type": "string"},
            },
            "required": ["series_id", "type", "hot_spots"],
        },
    ),
]


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def _get_finding_vocab(_args: dict[str, Any]) -> str:
    try:
        payload = await api_get("/api/findings/vocab")
    except httpx.HTTPStatusError as exc:
        return format_http_error(exc)
    return json.dumps(payload, indent=2, ensure_ascii=False)


async def _search_findings(args: dict[str, Any]) -> str:
    params: dict[str, Any] = {}
    for k in (
        "type",
        "anatomy",
        "laterality",
        "status",
        "min_diameter_mm",
        "max_diameter_mm",
        "min_volume_ml",
        "min_suv_max",
        "limit",
    ):
        if args.get(k) is not None:
            params[k] = args[k]
    if args.get("morphology"):
        params["morphology"] = args["morphology"]
    if args.get("include_deleted"):
        params["include_deleted"] = "true"
    patient_id = args.get("patient_id")
    if patient_id:
        # Patient-scoped list (study_id filter only applies here).
        if args.get("study_id") is not None:
            params["study_id"] = args["study_id"]
        path = f"/api/patients/{patient_id}/findings"
    else:
        # Corpus-wide, visibility-scoped search.
        if args.get("scope"):
            params["scope"] = args["scope"]
        path = "/api/findings/search"
    try:
        payload = await api_get(path, params=params or None)
    except httpx.HTTPStatusError as exc:
        return format_http_error(exc)
    return json.dumps(payload, indent=2, ensure_ascii=False)


async def _get_finding(args: dict[str, Any]) -> str:
    try:
        payload = await api_get(f"/api/findings/{args['finding_id']}")
    except httpx.HTTPStatusError as exc:
        return format_http_error(exc)
    return json.dumps(payload, indent=2, ensure_ascii=False)


async def _find_similar_findings(args: dict[str, Any]) -> str:
    params: dict[str, Any] = {}
    for k in ("k", "modality", "scope"):
        if args.get(k) is not None:
            params[k] = args[k]
    if args.get("same_type"):
        params["same_type"] = "true"
    try:
        payload = await api_get(
            f"/api/findings/{args['finding_id']}/similar", params=params or None
        )
    except httpx.HTTPStatusError as exc:
        return format_http_error(exc)
    return json.dumps(payload, indent=2, ensure_ascii=False)


async def _get_finding_revisions(args: dict[str, Any]) -> str:
    params = {"limit": int(args.get("limit", 200))}
    try:
        payload = await api_get(f"/api/findings/{args['finding_id']}/revisions", params=params)
    except httpx.HTTPStatusError as exc:
        return format_http_error(exc)
    return json.dumps(payload, indent=2, ensure_ascii=False)


_CREATE_BODY_KEYS = (
    "study_id",
    "series_id",
    "frame_of_reference_uid",
    "type",
    "anatomy",
    "laterality",
    "morphology",
    *_MEASUREMENT_FIELDS,
    "bbox_lps",
    "status",
    "confidence",
    "description",
    "geometry_refs",
)


async def _create_finding(args: dict[str, Any]) -> str:
    patient_id = args["patient_id"]
    body = {k: args[k] for k in _CREATE_BODY_KEYS if k in args}
    params: dict[str, Any] = {}
    if args.get("dry_run"):
        params["dry_run"] = "true"
    try:
        payload, _headers = await api_post_with_headers(
            f"/api/patients/{patient_id}/findings",
            json=body,
            params=params or None,
            idempotency_key=args.get("idempotency_key"),
        )
    except httpx.HTTPStatusError as exc:
        return format_http_error(exc, hint=_VOCAB_HINT)
    return json.dumps(payload, indent=2, ensure_ascii=False)


_UPDATE_BODY_KEYS = (
    "type",
    "anatomy",
    "laterality",
    "morphology",
    *_MEASUREMENT_FIELDS,
    "bbox_lps",
    "status",
    "confidence",
    "description",
)


async def _update_finding(args: dict[str, Any]) -> str:
    finding_id = args["finding_id"]
    body = {k: args[k] for k in _UPDATE_BODY_KEYS if k in args}
    try:
        payload, _headers = await api_patch(
            f"/api/findings/{finding_id}",
            json=body,
            if_match=args.get("if_match"),
            idempotency_key=args.get("idempotency_key"),
        )
    except httpx.HTTPStatusError as exc:
        return format_http_error(exc, hint=_VOCAB_HINT)
    return json.dumps(payload, indent=2, ensure_ascii=False)


async def _delete_finding(args: dict[str, Any]) -> str:
    finding_id = args["finding_id"]
    params: dict[str, Any] = {}
    if args.get("reason"):
        params["reason"] = args["reason"]
    try:
        code = await api_delete(
            f"/api/findings/{finding_id}",
            params=params or None,
            if_match=args.get("if_match"),
        )
    except httpx.HTTPStatusError as exc:
        return format_http_error(exc)
    return json.dumps({"status": "deleted", "recoverable": True, "http_status": code})


async def _restore_finding(args: dict[str, Any]) -> str:
    try:
        payload, _headers = await api_post_with_headers(
            f"/api/findings/{args['finding_id']}/restore"
        )
    except httpx.HTTPStatusError as exc:
        return format_http_error(exc)
    return json.dumps(payload, indent=2, ensure_ascii=False)


async def _add_finding_geometry(args: dict[str, Any]) -> str:
    finding_id = args["finding_id"]
    body: dict[str, Any] = {"role": args["role"]}
    for k in ("marker_id", "segmentation_id"):
        if args.get(k):
            body[k] = args[k]
    try:
        payload, _headers = await api_post_with_headers(
            f"/api/findings/{finding_id}/geometry", json=body
        )
    except httpx.HTTPStatusError as exc:
        return format_http_error(exc)
    return json.dumps(payload, indent=2, ensure_ascii=False)


async def _promote_finding_measurement(args: dict[str, Any]) -> str:
    finding_id = args["finding_id"]
    body: dict[str, Any] = {"series_id": args["series_id"], "source": args["source"]}
    for k in (
        "center_mm",
        "radius_mm",
        "seed_mm",
        "threshold",
        "threshold_units",
        "roi",
        "volume",
        "geometry_marker_id",
    ):
        if args.get(k) is not None:
            body[k] = args[k]
    try:
        payload, _headers = await api_post_with_headers(
            f"/api/findings/{finding_id}/promote-measurement",
            json=body,
            idempotency_key=args.get("idempotency_key"),
            if_match=args.get("if_match"),
        )
    except httpx.HTTPStatusError as exc:
        return format_http_error(exc)
    return json.dumps(payload, indent=2, ensure_ascii=False)


async def _create_findings_from_hot_spots(args: dict[str, Any]) -> str:
    series_id = args["series_id"]
    body: dict[str, Any] = {"type": args["type"], "hot_spots": args["hot_spots"]}
    if args.get("confidence") is not None:
        body["confidence"] = args["confidence"]
    try:
        payload, _headers = await api_post_with_headers(
            f"/api/series/{series_id}/findings-from-hot-spots",
            json=body,
            idempotency_key=args.get("idempotency_key"),
        )
    except httpx.HTTPStatusError as exc:
        return format_http_error(exc, hint=_VOCAB_HINT)
    return json.dumps(payload, indent=2, ensure_ascii=False)


_DISPATCH = {
    "get_finding_vocab": _get_finding_vocab,
    "search_findings": _search_findings,
    "find_similar_findings": _find_similar_findings,
    "get_finding": _get_finding,
    "get_finding_revisions": _get_finding_revisions,
    "create_finding": _create_finding,
    "update_finding": _update_finding,
    "delete_finding": _delete_finding,
    "restore_finding": _restore_finding,
    "add_finding_geometry": _add_finding_geometry,
    "promote_finding_measurement": _promote_finding_measurement,
    "create_findings_from_hot_spots": _create_findings_from_hot_spots,
}


async def handle(name: str, arguments: dict[str, Any]) -> str:
    handler = _DISPATCH.get(name)
    if handler is None:
        return f"Error: unknown tool '{name}' in findings module"
    return await handler(arguments)
