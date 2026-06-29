"""MCP tools — Fascicolo / study / folder export + tokenised download.

Closes the MCP-vs-GUI gap on the export surface: the backend has
shipped async-Job exports (``POST /api/patients/{id}/export`` and the
study / folder / bulk siblings) plus a single-use download-token
(``POST /api/auth/download-token`` → ``?dt=`` on the job's
``/result_download``) for a long time, but none of it was reachable
through MCP. An agent could enumerate a patient's studies but could
not hand the user a downloadable archive.

The flow this family exposes is exactly the one the user asked for —
"the MCP tool generates a one-time password to pass to curl":

1. ``export_fascicolo`` / ``export_study`` / ``export_folder`` /
   ``bulk_download`` → enqueue a Job, return its descriptor.
2. ``get_job`` → poll until ``status == "succeeded"``.
3. ``issue_download_token`` → mint a 5-minute single-use token bound
   to that job and return a ready ``curl`` command. The bytes stream
   curl → disk through the backend (storage-isolated: no bucket name,
   no presigned S3 URL ever leaves the platform), so a multi-GB DICOM
   archive never passes through the agent's context.

Layout: ``tree`` (the default here) mirrors the patient's curated
Folder tree with human-readable names — unzip and the archive is
navigable exactly like the in-app folder view. Pass ``layout="flat"``
for the legacy UUID-keyed layout. ``include`` defaults to the full
fascicolo *including DICOM pixel data* (``download:dicom`` grant
required server-side) so "download all the exams" works out of the
box; narrow it (e.g. ``"documents,reports"``) when the images are not
wanted.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
from mcp.types import Tool, ToolAnnotations

from bvmcp.tools.client import api_get, api_post_with_headers, format_http_error

# Public backend origin the curl command should target. The MCP
# server's own ``backend_base_url`` is the in-cluster address, which
# the user's laptop cannot reach; the download-token URL must point at
# the public Traefik vhost. Overridable per call via
# ``public_base_url`` for staging / preview hosts.
_DEFAULT_PUBLIC_BASE = "https://bitvision.xeno.garden"

# Full fascicolo including images. The backend's own default omits
# ``dicom``; we opt it in here because the canonical MCP use case is
# "give me everything to take to a second opinion".
_DEFAULT_INCLUDE = "studies,reports,documents,annotations,dicom"
_DEFAULT_LAYOUT = "tree"

_INCLUDE_DESC = (
    "Comma-joined sections to bundle: any of "
    "studies / reports / documents / annotations / dicom. Default "
    f"'{_DEFAULT_INCLUDE}' (the whole fascicolo, images included — "
    "needs the download:dicom grant). Drop 'dicom' to skip the "
    "multi-GB pixel data."
)
_LAYOUT_DESC = (
    "Archive layout. 'tree' (default) mirrors the patient's curated "
    "Folder tree with human-readable names (2024/<exam>/<study>/...); "
    "'flat' is the legacy UUID-keyed layout."
)


TOOLS: list[Tool] = [
    Tool(
        name="export_fascicolo",
        annotations=ToolAnnotations(
            title="Export a patient's full Health Record (job)",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
        description=(
            "Enqueue an async Job that bundles a patient's whole "
            "fascicolo (studies + DICOM + reports + documents + "
            "annotations) into a ZIP on object storage. Returns a Job "
            "descriptor — poll get_job(job_id) until status='succeeded', "
            "then issue_download_token(job_id) for a curl-able URL. The "
            "Job is deduped + cached for 48h, so re-calling with the "
            "same (patient, include, layout) returns the same artifact. "
            "Owner / admin only server-side; an agent token must hold "
            "the patient in its scope."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "patient_id": {"type": "string", "description": "UUID of the patient."},
                "include": {"type": "string", "description": _INCLUDE_DESC},
                "layout": {
                    "type": "string",
                    "enum": ["tree", "flat"],
                    "description": _LAYOUT_DESC,
                },
            },
            "required": ["patient_id"],
        },
    ),
    Tool(
        name="export_study",
        annotations=ToolAnnotations(
            title="Export a single study's DICOM (job)",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
        description=(
            "Enqueue a Job that ZIPs every DICOM instance of one study. "
            "Gated by the stricter download:dicom permission. Returns a "
            "Job descriptor — poll get_job then issue_download_token. "
            "Use layout='tree' to file the study under its curated "
            "Folder path with a human-readable name."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "study_id": {"type": "string", "description": "UUID of the study."},
                "layout": {
                    "type": "string",
                    "enum": ["tree", "flat"],
                    "description": _LAYOUT_DESC,
                },
            },
            "required": ["study_id"],
        },
    ),
    Tool(
        name="export_segmentation_dicom_seg",
        annotations=ToolAnnotations(
            title="Export a segmentation as DICOM SEG (job)",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
        description=(
            "Enqueue a Job that renders a stored segmentation mask as a "
            "conformant, geo-referenced DICOM SEG object (references the source "
            "series; openable in 3D Slicer / OHIF / MONAI — unlike the raw mask). "
            "Returns a Job descriptor — poll get_job, then issue_download_token to "
            "fetch the .dcm off-platform. Identify the mask by its series_id + "
            "label (see get_segmentations)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "series_id": {"type": "string", "description": "UUID of the series."},
                "label": {
                    "type": "string",
                    "description": "Segmentation label within the series (from get_segmentations).",
                },
            },
            "required": ["series_id", "label"],
        },
    ),
    Tool(
        name="export_folder",
        annotations=ToolAnnotations(
            title="Export the contents of a folder (job)",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
        description=(
            "Enqueue a Job that ZIPs only the items inside a folder "
            "(recursive over sub-folders). Optionally narrow the scope "
            "to a chosen subset via include_study_ids / "
            "include_document_ids (e.g. skip the multi-GB DICOM ISOs "
            "already on a physical disc). Returns a Job descriptor."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "folder_id": {"type": "string", "description": "UUID of the folder."},
                "include": {"type": "string", "description": _INCLUDE_DESC},
                "layout": {
                    "type": "string",
                    "enum": ["tree", "flat"],
                    "description": _LAYOUT_DESC,
                },
                "include_study_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Optional: restrict to these study UUIDs (must already "
                        "be in the folder). Omit to include every study."
                    ),
                },
                "include_document_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional: restrict to these document UUIDs.",
                },
            },
            "required": ["folder_id"],
        },
    ),
    Tool(
        name="bulk_download",
        annotations=ToolAnnotations(
            title="Export a heterogeneous selection (job)",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
        description=(
            "Enqueue a Job that ZIPs a caller-supplied list of studies "
            "and/or documents. All items must belong to ONE patient "
            "(the cross-patient invariant forbids mixing). Returns a "
            "Job descriptor."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "description": "Selection: each item is {id, kind} with kind in study|document.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "kind": {"type": "string", "enum": ["study", "document"]},
                        },
                        "required": ["id", "kind"],
                    },
                },
                "include": {"type": "string", "description": _INCLUDE_DESC},
                "layout": {
                    "type": "string",
                    "enum": ["tree", "flat"],
                    "description": _LAYOUT_DESC,
                },
            },
            "required": ["items"],
        },
    ),
    Tool(
        name="export_health_record_bundle",
        annotations=ToolAnnotations(
            title="Export the owner's portable PHR-Bundle (job)",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
        description=(
            "Enqueue an async Job that builds the token owner's PHR-Bundle "
            "— the portable, versioned open container (format "
            "'bitvision.phr-bundle', also the GDPR Art. 20 export) holding "
            "the FULL structured record the platform keeps about them: "
            "consents, owned studies, authored reports + markers, every "
            "managed patient and their documents, and the audit log. No "
            "DICOM pixels (use export_fascicolo / export_study for images). "
            "Account-wide and self-scoped: takes no target id, always "
            "exports the token's own owner. Returns a Job descriptor — poll "
            "get_job(job_id) until status='succeeded', then "
            "issue_download_token(job_id) for a curl-able URL. Needs the "
            "health_record:export grant."
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="get_job",
        annotations=ToolAnnotations(
            title="Poll an async Job",
            readOnlyHint=True,
            idempotentHint=True,
            openWorldHint=False,
        ),
        description=(
            "Read an async Job's status snapshot: status (queued / "
            "running / succeeded / failed / cancelled), progress_done / "
            "progress_total, stage, display_label, and "
            "result_download_url when finished. Use to poll any export "
            "(export_fascicolo / export_study / export_folder / "
            "bulk_download) or training-bundle job. Owner / admin only "
            "server-side (404 otherwise — never confirms a foreign job "
            "exists)."
        ),
        inputSchema={
            "type": "object",
            "properties": {"job_id": {"type": "string", "description": "UUID of the Job."}},
            "required": ["job_id"],
        },
    ),
    Tool(
        name="issue_download_token",
        annotations=ToolAnnotations(
            title="Mint a one-time download token (curl)",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=False,
        ),
        description=(
            "Mint a 5-minute, single-use, scope-bound download token "
            "for a succeeded job's result (resource_kind='job_result') "
            "and return a ready curl command. The token is the "
            "'one-time password': the download streams curl → disk "
            "through the backend with NO bucket name or presigned S3 "
            "URL exposed, so multi-GB archives never pass through the "
            "agent context. The token is consumed on first use (Redis "
            "GETDEL) — re-call to get a fresh one. Also supports "
            "resource_kind document / document_file for single-file "
            "downloads."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "job_id": {
                    "type": "string",
                    "description": (
                        "UUID of a succeeded export Job. Shorthand for "
                        "resource_kind='job_result', resource_id=<job_id>."
                    ),
                },
                "resource_kind": {
                    "type": "string",
                    "enum": ["job_result", "document", "document_file"],
                    "description": (
                        "Resource class to mint the token for. Defaults to "
                        "'job_result' when job_id is given."
                    ),
                },
                "resource_id": {
                    "type": "string",
                    "description": "UUID of the resource (alternative to job_id).",
                },
                "child_id": {
                    "type": "string",
                    "description": "Required only for resource_kind='document_file'.",
                },
                "public_base_url": {
                    "type": "string",
                    "description": (
                        f"Origin the curl command targets. Defaults to '{_DEFAULT_PUBLIC_BASE}'."
                    ),
                },
            },
        },
    ),
]


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


def _export_body(args: dict[str, Any]) -> dict[str, Any]:
    body: dict[str, Any] = {
        "include": args.get("include") or _DEFAULT_INCLUDE,
        "layout": args.get("layout") or _DEFAULT_LAYOUT,
    }
    return body


async def _enqueue(path: str, body: dict[str, Any]) -> str:
    try:
        payload, _headers = await api_post_with_headers(path, json=body)
    except httpx.HTTPStatusError as exc:
        return format_http_error(exc)
    return json.dumps(payload, indent=2, ensure_ascii=False)


async def _export_fascicolo(args: dict[str, Any]) -> str:
    return await _enqueue(f"/api/patients/{args['patient_id']}/export", _export_body(args))


async def _export_study(args: dict[str, Any]) -> str:
    body = {"layout": args.get("layout") or _DEFAULT_LAYOUT}
    return await _enqueue(f"/api/studies/{args['study_id']}/export", body)


async def _export_segmentation_dicom_seg(args: dict[str, Any]) -> str:
    return await _enqueue(
        f"/api/series/{args['series_id']}/segmentations/{args['label']}/dicom-seg/export", {}
    )


async def _export_folder(args: dict[str, Any]) -> str:
    body = _export_body(args)
    if args.get("include_study_ids") is not None:
        body["include_study_ids"] = args["include_study_ids"]
    if args.get("include_document_ids") is not None:
        body["include_document_ids"] = args["include_document_ids"]
    return await _enqueue(f"/api/folders/{args['folder_id']}/export", body)


async def _bulk_download(args: dict[str, Any]) -> str:
    body = _export_body(args)
    body["items"] = args.get("items") or []
    return await _enqueue("/api/bulk/download", body)


async def _export_health_record_bundle(_args: dict[str, Any]) -> str:
    # Account-wide, self-scoped: no body, no target id. The backend
    # resolves the subject from the token and dedups concurrent retries
    # to one in-flight job.
    return await _enqueue("/api/gdpr/export", {})


async def _get_job(args: dict[str, Any]) -> str:
    try:
        payload = await api_get(f"/api/jobs/{args['job_id']}")
    except httpx.HTTPStatusError as exc:
        return format_http_error(exc)
    return json.dumps(payload, indent=2, ensure_ascii=False)


async def _issue_download_token(args: dict[str, Any]) -> str:
    job_id = args.get("job_id")
    resource_kind = args.get("resource_kind")
    resource_id = args.get("resource_id")
    if job_id and not resource_id:
        resource_kind = resource_kind or "job_result"
        resource_id = job_id
    if not resource_kind or not resource_id:
        return json.dumps(
            {
                "error": "bad_request",
                "detail": "provide job_id, or both resource_kind and resource_id",
            },
            indent=2,
        )
    body: dict[str, Any] = {"resource_kind": resource_kind, "resource_id": resource_id}
    if args.get("child_id"):
        body["child_id"] = args["child_id"]
    try:
        payload, _headers = await api_post_with_headers("/api/auth/download-token", json=body)
    except httpx.HTTPStatusError as exc:
        return format_http_error(exc)

    token = payload.get("token") if isinstance(payload, dict) else None
    expires_in = payload.get("expires_in") if isinstance(payload, dict) else None
    base = (args.get("public_base_url") or _DEFAULT_PUBLIC_BASE).rstrip("/")
    if resource_kind == "job_result":
        path = f"/api/jobs/{resource_id}/result_download"
    elif resource_kind == "document_file" and args.get("child_id"):
        path = f"/api/documents/{resource_id}/files/{args['child_id']}/download"
    else:
        path = f"/api/documents/{resource_id}/download"
    url = f"{base}{path}?dt={token}"
    # ``-J -O`` makes curl honour the Content-Disposition filename the
    # backend sets (e.g. ``fascicolo-<name>-<uuid>.zip``) instead of the
    # query-string-laden URL tail.
    out = {
        "token": token,
        "expires_in_seconds": expires_in,
        "resource_kind": resource_kind,
        "resource_id": resource_id,
        "download_url": url,
        "curl": f'curl -L -J -O "{url}"',
        "note": (
            "Single-use, consumed on first GET (5-min TTL). The bytes "
            "stream through the backend — no bucket/presigned URL is "
            "exposed. Re-mint if it expires or is already consumed."
        ),
    }
    return json.dumps(out, indent=2, ensure_ascii=False)


_DISPATCH = {
    "export_fascicolo": _export_fascicolo,
    "export_study": _export_study,
    "export_segmentation_dicom_seg": _export_segmentation_dicom_seg,
    "export_folder": _export_folder,
    "bulk_download": _bulk_download,
    "export_health_record_bundle": _export_health_record_bundle,
    "get_job": _get_job,
    "issue_download_token": _issue_download_token,
}


async def handle(name: str, arguments: dict[str, Any]) -> str:
    fn = _DISPATCH.get(name)
    if fn is None:
        return f"Error: unknown tool '{name}' in exports module"
    return await fn(arguments or {})


__all__ = ["TOOLS", "handle"]
