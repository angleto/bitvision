"""MCP tools — binary attachments on ClinicalEvent.

The backend exposes a full CRUD + promote surface on
``/api/clinical-events/{event_id}/attachments`` (see
``backend/src/bvphoenix/api/clinical_event_attachments.py``). MCP was
read-blind on this sub-resource until now: agents could create and
mutate events, but couldn't add the referral letter, the prior report,
or the lab printout that physically motivated the event.

Eight tools, mirroring the backend 1:1:

* ``upload_clinical_event_attachment`` — multipart upload, single file,
  25 MiB cap. Bytes go in via base64 to fit the JSON-only MCP
  transport. The backend auto-reconciles by content hash, so the
  response carries ``document_id`` when the bytes are already curated
  in the Drive.
* ``list_clinical_event_attachments`` — non-deleted only.
* ``download_clinical_event_attachment`` — base64 + size + mime so the
  agent can quote / OCR / hand off the binary; clinicians normally
  open the file from the viewer instead.
* ``delete_clinical_event_attachment`` — soft delete (the binary stays
  around for a future Restore endpoint; cleanup is a separate sweep).
* ``promote_clinical_event_attachment`` — reconcile-or-ingest a raw
  upload into the patient Documents drive; idempotent.

And, for pointing an event at an *already curated* drive document
without re-uploading bytes:

* ``link_event_document`` — link an existing Document to the event.
* ``list_event_documents`` — list the curated documents linked to the
  event.
* ``unlink_event_document`` — drop a curated-document link (soft; the
  document stays in the Drive).

Scopes follow the existing pattern: writes ride ``events:write`` (the
attachment is a sub-resource of the ClinicalEvent), reads ride
``events:read``. The backend enforces ``WRITE_REPORT`` /
``READ_METADATA`` on top, so an agent that holds the MCP scope but
lacks the platform permission still gets a 403 it can surface.

Provenance: every write call lands a row in ``provenance_activities``
(``attachment.upload`` / ``attachment.delete`` / ``attachment.promote``)
with ``author_kind=agent`` when the request carries an agent token —
no extra plumbing required from the MCP side.
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
    api_get,
    api_get_bytes,
    api_post_with_headers,
    format_http_error,
)

_HINT = (
    "Attachments are sub-resources of a ClinicalEvent and inherit "
    "its patient. Uploads are auto-reconciled by content hash: the "
    "response carries ``document_id`` when the bytes are already "
    "curated in the patient's Drive. To point an event at a document "
    "you already curated, prefer link_event_document over re-uploading."
)

# Mirror the backend cap (MAX_ATTACHMENT_BYTES) so the tool rejects
# oversized payloads client-side rather than wasting a round trip.
_MAX_BYTES = 25 * 1024 * 1024


TOOLS: list[Tool] = [
    Tool(
        name="upload_clinical_event_attachment",
        description=(
            "Attach a single binary file (referral letter, prior "
            "report PDF, lab printout, photo) to a ClinicalEvent. "
            "Bytes are base64-encoded in ``content_base64``; cap is "
            "25 MiB after decode. ``mime`` is recorded as-is. The "
            "backend stores the file in the raw bucket scoped to the "
            "event and creates an ``attachment.upload`` provenance "
            "row stamped ``author_kind=agent`` for agent callers. "
            "Use ``promote_clinical_event_attachment`` afterwards to "
            "materialise the attachment as a patient Document when "
            "it deserves to outlive the event lifecycle."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "event_id": {
                    "type": "string",
                    "description": "UUID of the ClinicalEvent.",
                },
                "filename": {
                    "type": "string",
                    "description": (
                        "Original filename, kept verbatim on the row "
                        "(the storage key uses a sanitised slug)."
                    ),
                },
                "content_base64": {
                    "type": "string",
                    "description": "File bytes, base64-encoded. ≤ 25 MiB after decode.",
                },
                "mime": {
                    "type": "string",
                    "description": (
                        "MIME type. Defaults to ``application/octet-stream`` when omitted."
                    ),
                },
            },
            "required": ["event_id", "filename", "content_base64"],
        },
    ),
    Tool(
        name="list_clinical_event_attachments",
        description=(
            "List active (non-deleted) attachments on a ClinicalEvent, "
            "newest first. Each row carries ``filename``, ``mime``, "
            "``size_bytes``, ``uploaded_by_kind`` "
            "(human / agent / system), and ``document_id`` (the curated "
            "drive Document this upload is linked to — non-null once the "
            "bytes are reconciled or promoted into the Drive). The "
            "binary is fetched via ``download_clinical_event_attachment``."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "event_id": {"type": "string"},
            },
            "required": ["event_id"],
        },
    ),
    Tool(
        name="download_clinical_event_attachment",
        description=(
            "Fetch the bytes of one attachment. Returns "
            "``content_base64``, ``size_bytes``, ``mime``, "
            "``filename``. The MCP transport carries the payload "
            "inline (base64), which is the same shape used by the "
            "imaging tools (``get_series_slice``, "
            "``predict_segmentation_interactive``). Use this to "
            "OCR / quote / hand off an attachment programmatically; "
            "clinicians normally read it from the viewer."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "event_id": {"type": "string"},
                "attachment_id": {"type": "string"},
            },
            "required": ["event_id", "attachment_id"],
        },
    ),
    Tool(
        name="delete_clinical_event_attachment",
        description=(
            "Soft-delete an attachment (``deleted_at`` set on the "
            "row, storage object stays around for a future Restore "
            "endpoint). 204 on success; 404 when the attachment is "
            "already deleted or never existed. The "
            "``attachment.delete`` provenance row records the agent "
            "as the author."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "event_id": {"type": "string"},
                "attachment_id": {"type": "string"},
            },
            "required": ["event_id", "attachment_id"],
        },
    ),
    Tool(
        name="promote_clinical_event_attachment",
        description=(
            "Promote a raw attachment into the patient Documents drive. "
            "Reconcile-or-ingest: if a byte-identical document already "
            "exists in the patient's Drive it is linked (no second "
            "copy); otherwise the bytes are ingested into a new "
            "Document via the canonical pipeline. Idempotent. The "
            "returned attachment carries ``document_id`` (the linked "
            "drive document) and ``document_reconciled`` (true when it "
            "matched an existing document rather than ingesting a new "
            "one)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "event_id": {"type": "string"},
                "attachment_id": {"type": "string"},
            },
            "required": ["event_id", "attachment_id"],
        },
    ),
    Tool(
        name="link_event_document",
        description=(
            "Point a ClinicalEvent at an already-curated patient "
            "Document ('attach from Drive') without re-uploading bytes "
            "— the right move when the referto is already in the Drive. "
            "Idempotent on (event, document). The document must belong "
            "to the event's patient and be live, else 404 "
            "(cross-patient links are unrepresentable). Returns the "
            "link row (id, document title/kind/date)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "event_id": {"type": "string"},
                "document_id": {
                    "type": "string",
                    "description": "UUID of the curated drive Document to link.",
                },
            },
            "required": ["event_id", "document_id"],
        },
    ),
    Tool(
        name="list_event_documents",
        description=(
            "List curated drive Documents linked to a ClinicalEvent "
            "(live links to live documents), newest first. Each row "
            "carries the link id, the document id / title / kind / "
            "date, ``link_role`` and ``source_attachment_id`` (set when "
            "the link came from reconciling a raw event upload)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "event_id": {"type": "string"},
            },
            "required": ["event_id"],
        },
    ),
    Tool(
        name="unlink_event_document",
        description=(
            "Remove a curated-document link from a ClinicalEvent (soft; "
            "the document stays in the Drive). Pass the ``link_id`` from "
            "list_event_documents. 204 on success, 404 when the link "
            "does not exist."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "event_id": {"type": "string"},
                "link_id": {"type": "string"},
            },
            "required": ["event_id", "link_id"],
        },
    ),
]


# ---------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------


async def _upload(args: dict[str, Any]) -> str:
    event_id = args["event_id"]
    filename = args["filename"]
    mime = args.get("mime") or "application/octet-stream"
    try:
        blob = base64.b64decode(args["content_base64"], validate=True)
    except (ValueError, TypeError) as exc:
        return json.dumps(
            {
                "error": "invalid_base64",
                "detail": str(exc),
                "hint": _HINT,
            }
        )
    if len(blob) > _MAX_BYTES:
        return json.dumps(
            {
                "error": "payload_too_large",
                "size_bytes": len(blob),
                "limit_bytes": _MAX_BYTES,
                "hint": (
                    "Split or compress the attachment before "
                    "uploading; the backend rejects anything > 25 MiB."
                ),
            }
        )

    # Multipart-only endpoint, so do the POST inline with httpx (same
    # pattern as ``upload_segmentation``). The JSON accept header is
    # dropped so httpx can pick the multipart boundary header itself.
    settings = _get_mcp_settings()
    url = f"{settings.backend_base_url}/api/clinical-events/{event_id}/attachments"
    headers = _augment_headers()
    headers.pop("accept", None)
    files = {"file": (filename, blob, mime)}
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(url, headers=headers, files=files)
            resp.raise_for_status()
            payload = resp.json()
    except httpx.HTTPStatusError as exc:
        return format_http_error(exc, hint=_HINT)
    return json.dumps(payload, indent=2, ensure_ascii=False)


async def _list(args: dict[str, Any]) -> str:
    event_id = args["event_id"]
    payload = await api_get(f"/api/clinical-events/{event_id}/attachments")
    return json.dumps(payload, indent=2, ensure_ascii=False)


async def _download(args: dict[str, Any]) -> str:
    event_id = args["event_id"]
    att_id = args["attachment_id"]
    data, content_type = await api_get_bytes(
        f"/api/clinical-events/{event_id}/attachments/{att_id}/download"
    )
    return json.dumps(
        {
            "event_id": event_id,
            "attachment_id": att_id,
            "mime": content_type,
            "size_bytes": len(data),
            "content_base64": base64.b64encode(data).decode("ascii"),
        }
    )


async def _delete(args: dict[str, Any]) -> str:
    event_id = args["event_id"]
    att_id = args["attachment_id"]
    code = await api_delete(f"/api/clinical-events/{event_id}/attachments/{att_id}")
    return json.dumps(
        {
            "status": "deleted",
            "event_id": event_id,
            "attachment_id": att_id,
            "http_status": code,
        }
    )


async def _promote(args: dict[str, Any]) -> str:
    event_id = args["event_id"]
    att_id = args["attachment_id"]
    payload, _headers = await api_post_with_headers(
        f"/api/clinical-events/{event_id}/attachments/{att_id}/promote-to-document",
    )
    return json.dumps(payload, indent=2, ensure_ascii=False)


async def _link_document(args: dict[str, Any]) -> str:
    event_id = args["event_id"]
    payload, _headers = await api_post_with_headers(
        f"/api/clinical-events/{event_id}/documents",
        json={"document_id": args["document_id"]},
    )
    return json.dumps(payload, indent=2, ensure_ascii=False)


async def _list_documents(args: dict[str, Any]) -> str:
    event_id = args["event_id"]
    payload = await api_get(f"/api/clinical-events/{event_id}/documents")
    return json.dumps(payload, indent=2, ensure_ascii=False)


async def _unlink_document(args: dict[str, Any]) -> str:
    event_id = args["event_id"]
    link_id = args["link_id"]
    code = await api_delete(f"/api/clinical-events/{event_id}/documents/{link_id}")
    return json.dumps(
        {
            "status": "unlinked",
            "event_id": event_id,
            "link_id": link_id,
            "http_status": code,
        }
    )


_DISPATCH = {
    "upload_clinical_event_attachment": _upload,
    "list_clinical_event_attachments": _list,
    "download_clinical_event_attachment": _download,
    "delete_clinical_event_attachment": _delete,
    "promote_clinical_event_attachment": _promote,
    "link_event_document": _link_document,
    "list_event_documents": _list_documents,
    "unlink_event_document": _unlink_document,
}


async def handle(name: str, arguments: dict) -> str:
    fn = _DISPATCH.get(name)
    if fn is None:
        raise ValueError(f"unknown tool: {name}")
    return await fn(arguments or {})


__all__ = ["TOOLS", "handle"]
