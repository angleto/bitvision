"""MCP tools for write operations on patient documents (Sprint 2).

Tools surfaced to LLM/agent clients:

* ``update_document`` — single PATCH with optional ETag + dry-run.
* ``bulk_update_documents`` — manifest of up to 100 metadata edits.
* ``link_document_to_study`` / ``unlink_document_from_study`` — manage
  the structured document ↔ study cross-reference (ADR 0004).

Every tool surfaces the backend ``ETag`` (when present) so the agent
can chain calls without an explicit re-read; the same ETag plugs into
``If-Match`` for the next mutation.
"""

from __future__ import annotations

import json
from typing import Any

from mcp.types import TextContent, Tool

from bvmcp.tools.client import (
    api_delete,
    api_patch,
    api_post_with_headers,
)

TOOLS: list[Tool] = [
    Tool(
        name="update_document",
        description=(
            "Update metadata of a single patient document (title, type, date, "
            "inline text). Supports optimistic concurrency via ``etag`` and "
            "preview via ``dry_run``. Files (multi-file gallery) are not "
            "edited here — re-upload the document to replace its files."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "patient_id": {"type": "string", "description": "Patient UUID"},
                "document_id": {"type": "string", "description": "Document UUID"},
                "title": {"type": "string"},
                "kind_id": {
                    "type": "string",
                    "description": (
                        "Catalog id from ``document_kinds`` "
                        "(``radiology_report``, ``surgical_report``, "
                        "``pathology_report``, ``discharge_summary``, "
                        "``specialist_visit_note``, ``cardio_report``, "
                        "``endoscopy_report``, ``lab_result``, …). The "
                        "canonical 3-axis FK on the documents table."
                    ),
                },
                "document_type": {
                    "type": "string",
                    "description": (
                        "Legacy single-axis alias of ``kind_id``. "
                        "Accepted for back-compat; prefer ``kind_id``. "
                        "When both are sent, ``kind_id`` wins."
                    ),
                },
                "document_date": {
                    "type": "string",
                    "description": "ISO date (YYYY-MM-DD) — clinically meaningful date",
                },
                "text": {
                    "type": ["string", "null"],
                    "description": "Inline text body. Null clears it.",
                },
                "etag": {
                    "type": "string",
                    "description": (
                        "Optional If-Match precondition. When supplied, the "
                        "server rejects with 412 if the document head changed "
                        "since the etag was issued."
                    ),
                },
                "idempotency_key": {
                    "type": "string",
                    "description": "Replay-safe key, valid for 24h.",
                },
                "dry_run": {
                    "type": "boolean",
                    "default": False,
                    "description": "Preview the diff without applying.",
                },
            },
            "required": ["patient_id", "document_id"],
        },
    ),
    Tool(
        name="bulk_update_documents",
        description=(
            "Apply metadata edits to up to 100 documents in one call. "
            "Each item carries the same shape as ``update_document``. Set "
            "``atomic=true`` for all-or-nothing semantics (rolls back on the "
            "first failure). Manifests larger than 50 items run async and "
            "return a job_id; poll ``GET /api/jobs/<id>`` for completion."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "patient_id": {"type": "string"},
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "document_id": {"type": "string"},
                            "title": {"type": "string"},
                            "kind_id": {
                                "type": "string",
                                "description": (
                                    "Catalog id from ``document_kinds``. "
                                    "Canonical 3-axis FK; preferred over "
                                    "the legacy ``document_type`` alias."
                                ),
                            },
                            "document_type": {
                                "type": "string",
                                "description": (
                                    "Legacy alias of ``kind_id``. Either "
                                    "field is accepted; ``kind_id`` wins "
                                    "on collision."
                                ),
                            },
                            "document_date": {"type": "string"},
                            "text": {"type": ["string", "null"]},
                            "etag": {"type": "string"},
                        },
                        "required": ["document_id"],
                    },
                },
                "atomic": {"type": "boolean", "default": False},
                "dry_run": {"type": "boolean", "default": False},
                "idempotency_key": {"type": "string"},
            },
            "required": ["patient_id", "items"],
        },
    ),
    Tool(
        name="link_document_to_study",
        description=(
            "Link a patient document to an imaging study. ``link_kind`` "
            "controls the role: ``primary_report`` (one per study, "
            "enforced by partial unique index), ``addendum``, "
            "``second_opinion``, or the citation roles "
            "``extracted_from``/``cites``/``mentions``. Cross-patient "
            "links are rejected (ADR 0004). Legacy ``report_of`` is "
            "auto-translated to ``primary_report`` for one release."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "patient_id": {"type": "string"},
                "document_id": {"type": "string"},
                "study_id": {"type": "string"},
                "link_kind": {
                    "type": "string",
                    "enum": [
                        "primary_report",
                        "addendum",
                        "second_opinion",
                        "extracted_from",
                        "cites",
                        "mentions",
                    ],
                    "default": "primary_report",
                },
                "note": {"type": "string"},
                "idempotency_key": {"type": "string"},
            },
            "required": ["patient_id", "document_id", "study_id"],
        },
    ),
    Tool(
        name="unlink_document_from_study",
        description="Remove a document ↔ study link of a specific kind.",
        inputSchema={
            "type": "object",
            "properties": {
                "patient_id": {"type": "string"},
                "document_id": {"type": "string"},
                "study_id": {"type": "string"},
                "link_kind": {
                    "type": "string",
                    "enum": [
                        "primary_report",
                        "addendum",
                        "second_opinion",
                        "extracted_from",
                        "cites",
                        "mentions",
                    ],
                    "default": "primary_report",
                },
            },
            "required": ["patient_id", "document_id", "study_id"],
        },
    ),
    Tool(
        name="delete_document",
        description=(
            "Soft-delete a patient document. The row stays in the DB as a "
            "tombstone with a 30-day retention window. Use ``restore_document`` "
            "before the purge worker hard-deletes it. ``hard=true`` is "
            "admin-only and skips the tombstone."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "patient_id": {"type": "string"},
                "document_id": {"type": "string"},
                "reason": {"type": "string", "maxLength": 255},
                "hard": {"type": "boolean", "default": False},
            },
            "required": ["patient_id", "document_id"],
        },
    ),
    Tool(
        name="restore_document",
        description=(
            "Undo a soft-delete. Document-ImagingStudy links survive the tombstone "
            "so no relink is needed; merged duplicates restored after a "
            "merge come back with files=[] (their files now belong to the "
            "primary, ADR 0017)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "patient_id": {"type": "string"},
                "document_id": {"type": "string"},
            },
            "required": ["patient_id", "document_id"],
        },
    ),
    Tool(
        name="merge_documents",
        description=(
            "Merge up to 20 duplicates into a primary document (Sprint 3, "
            "ADR 0017). File ownership transfers from each duplicate to the "
            "primary; duplicates are soft-deleted. Set "
            "``preserve_files_as_attachments=false`` to leave the duplicate "
            "files orphaned (they are reaped by the purge worker)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "patient_id": {"type": "string"},
                "primary_id": {"type": "string"},
                "duplicate_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 20,
                },
                "preserve_files_as_attachments": {
                    "type": "boolean",
                    "default": True,
                },
                "reason": {"type": "string", "maxLength": 255},
                "idempotency_key": {"type": "string"},
            },
            "required": ["patient_id", "primary_id", "duplicate_ids"],
        },
    ),
]


def _strip_etag(headers: dict[str, str]) -> str | None:
    raw = headers.get("etag") or headers.get("ETag")
    if not raw:
        return None
    raw = raw.strip()
    if raw.startswith("W/"):
        raw = raw[2:].strip()
    if raw.startswith('"') and raw.endswith('"'):
        return raw[1:-1]
    return raw


async def _update_document(args: dict[str, Any]) -> str:
    patient_id = args["patient_id"]
    document_id = args["document_id"]
    body: dict[str, Any] = {}
    for k in ("title", "document_type", "kind_id", "document_date", "text"):
        if k in args:
            body[k] = args[k]
    params: dict[str, Any] = {}
    if args.get("dry_run"):
        params["dry_run"] = "true"
    payload, headers = await api_patch(
        f"/api/patients/{patient_id}/documents/{document_id}",
        json=body,
        params=params or None,
        if_match=args.get("etag"),
        idempotency_key=args.get("idempotency_key"),
    )
    out: dict[str, Any] = payload if isinstance(payload, dict) else {"data": payload}
    new_etag = _strip_etag(headers)
    if new_etag and "etag" not in out:
        out["etag"] = new_etag
    return json.dumps(out, indent=2, ensure_ascii=False)


async def _bulk_update_documents(args: dict[str, Any]) -> str:
    patient_id = args["patient_id"]
    body = {
        "items": args.get("items") or [],
        "atomic": bool(args.get("atomic", False)),
    }
    params: dict[str, Any] = {}
    if args.get("dry_run"):
        params["dry_run"] = "true"
    payload, headers = await api_post_with_headers(
        f"/api/patients/{patient_id}/documents/bulk_update",
        json=body,
        params=params or None,
        idempotency_key=args.get("idempotency_key"),
    )
    out: dict[str, Any] = payload if isinstance(payload, dict) else {"data": payload}
    new_etag = _strip_etag(headers)
    if new_etag and "head_etag" not in out:
        out["head_etag"] = new_etag
    job_id = headers.get("x-job-id") or headers.get("X-Job-Id")
    if job_id and not out.get("job_id"):
        out["job_id"] = job_id
    return json.dumps(out, indent=2, ensure_ascii=False)


async def _link_document_to_study(args: dict[str, Any]) -> str:
    patient_id = args["patient_id"]
    document_id = args["document_id"]
    body = {
        "study_id": args["study_id"],
        "link_kind": args.get("link_kind", "primary_report"),
    }
    if args.get("note"):
        body["note"] = args["note"]
    payload, _headers = await api_post_with_headers(
        f"/api/patients/{patient_id}/documents/{document_id}/links",
        json=body,
        idempotency_key=args.get("idempotency_key"),
    )
    return json.dumps(payload, indent=2, ensure_ascii=False)


async def _unlink_document_from_study(args: dict[str, Any]) -> str:
    patient_id = args["patient_id"]
    document_id = args["document_id"]
    study_id = args["study_id"]
    link_kind = args.get("link_kind", "primary_report")
    status_code = await api_delete(
        f"/api/patients/{patient_id}/documents/{document_id}/links/{study_id}/{link_kind}"
    )
    return json.dumps({"status": "deleted", "http_status": status_code})


async def _delete_document(args: dict[str, Any]) -> str:
    patient_id = args["patient_id"]
    document_id = args["document_id"]
    params: list[str] = []
    if args.get("reason"):
        from urllib.parse import quote

        params.append(f"reason={quote(str(args['reason']))}")
    if args.get("hard"):
        params.append("hard=true")
    qs = ("?" + "&".join(params)) if params else ""
    code = await api_delete(f"/api/patients/{patient_id}/documents/{document_id}{qs}")
    return json.dumps({"status": "deleted", "http_status": code})


async def _restore_document(args: dict[str, Any]) -> str:
    patient_id = args["patient_id"]
    document_id = args["document_id"]
    payload, _headers = await api_post_with_headers(
        f"/api/patients/{patient_id}/documents/{document_id}/restore",
    )
    return json.dumps(payload, indent=2, ensure_ascii=False)


async def _merge_documents(args: dict[str, Any]) -> str:
    patient_id = args["patient_id"]
    primary_id = args["primary_id"]
    body = {
        "duplicate_ids": args["duplicate_ids"],
        "preserve_files_as_attachments": bool(args.get("preserve_files_as_attachments", True)),
    }
    if args.get("reason"):
        body["reason"] = args["reason"]
    payload, headers = await api_post_with_headers(
        f"/api/patients/{patient_id}/documents/{primary_id}/merge",
        json=body,
        idempotency_key=args.get("idempotency_key"),
    )
    out: dict[str, Any] = payload if isinstance(payload, dict) else {"data": payload}
    new_etag = _strip_etag(headers)
    if new_etag and "etag" not in out:
        out["etag"] = new_etag
    return json.dumps(out, indent=2, ensure_ascii=False)


_DISPATCH = {
    "update_document": _update_document,
    "bulk_update_documents": _bulk_update_documents,
    "link_document_to_study": _link_document_to_study,
    "unlink_document_from_study": _unlink_document_from_study,
    "delete_document": _delete_document,
    "restore_document": _restore_document,
    "merge_documents": _merge_documents,
}


async def handle(name: str, arguments: dict[str, Any]) -> str | list[TextContent]:
    handler = _DISPATCH.get(name)
    if handler is None:
        return f"Error: unknown tool '{name}' in document_writes module"
    return await handler(arguments)
