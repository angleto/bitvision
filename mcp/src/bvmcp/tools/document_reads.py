"""Sprint 3 read-side document tools (OCR text + binary download).

* ``get_document_text`` — returns the OCR text from cache; if missing
  and ``trigger=true``, runs the OCR pipeline inline.
* ``download_document_binary`` — fetches the original document file
  (PDF / image / scan) through the backend and hands it back as
  base64. Gated on the granular ``documents:download`` agent scope.

Storage isolation contract (memoria ``feedback_storage_isolation``):
the backend's storage layer (S3 bucket and DB) is never exposed across
the MCP boundary. ``download_document_binary`` does NOT return a
presigned URL — the bytes flow through the authenticated backend
streaming endpoint and are forwarded to the agent inline. A
backend-relative ``content_url`` is also returned so the agent can
hand a clickable link to the human user (the URL requires auth and
points at the backend, not at storage).

Per parallel-session note (memoria ``concurrent_work_sr_pr.md``), if
the document carries a DICOM Structured Report or Presentation State
the same tools work transparently — the SR/PR ingestion side stores
those payloads as ``patient_document_files`` rows once landed.
"""

from __future__ import annotations

import base64
import json
from typing import Any

import httpx
from mcp.types import TextContent, Tool

from bvmcp.tools.client import (
    api_get,
    api_get_bytes,
    api_post_with_headers,
)

# Cap inline base64 payloads so the agent's context doesn't get
# obliterated by a 50 MB scan. Above this, the tool returns metadata
# + a backend ``content_url`` and the agent must rely on
# ``get_document_text`` (or hand the URL to the human user).
_BINARY_INLINE_MAX_BYTES = 8 * 1024 * 1024


TOOLS: list[Tool] = [
    Tool(
        name="download_document_binary",
        description=(
            "Fetch the original binary attached to a Document "
            "(PDF, JPEG, PNG, scan, DICOM SR/PR, ...). The bytes are "
            "streamed through the backend and returned inline as "
            "base64-encoded ``content_b64`` together with "
            "``content_type``, ``size_bytes`` and a backend-relative "
            "``content_url`` the agent can hand to the human. Storage "
            "internals (bucket, key, signed URLs) never appear in the "
            "response — the agent only sees backend paths. Files larger "
            "than 8 MiB return metadata + ``content_url`` only "
            "(``content_b64`` will be null) so the assistant context "
            "is not blown up by a 50 MB scan; in that case prefer "
            "``get_document_text`` for textual reasoning. Requires the "
            "``documents:download`` scope on the assistant token — "
            "leave it OFF for assistants that only need OCR text."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "patient_id": {"type": "string"},
                "document_id": {"type": "string"},
                "file_id": {
                    "type": "string",
                    "description": (
                        "Optional file id when the document is "
                        "multi-file. When omitted the legacy single-"
                        "file slot or the first file by sequence is "
                        "returned."
                    ),
                },
            },
            "required": ["patient_id", "document_id"],
        },
    ),
    Tool(
        name="get_document_text",
        description=(
            "Return the OCR text of a document file. Cache hit by default; "
            "with ``trigger=true`` the API runs the pipeline (pdfminer "
            "text-layer + Tesseract italian fallback) and caches the result. "
            "Per-word bbox is returned via ``bbox_words`` for the rasterised "
            "engine — useful to render highlighters."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "patient_id": {"type": "string"},
                "document_id": {"type": "string"},
                "file_id": {"type": "string"},
                "engine": {
                    "type": "string",
                    "enum": ["pdfminer", "tesseract"],
                    "description": "Optional — restrict the cache lookup.",
                },
                "trigger": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Run OCR if no cached entry exists for this engine. "
                        "Always returns 200 with the freshly-extracted text."
                    ),
                },
                "force": {
                    "type": "boolean",
                    "default": False,
                    "description": "Re-run OCR even on cache hit.",
                },
                "language": {
                    "type": "string",
                    "description": (
                        "Tesseract language tag, used only when the PDF "
                        "text layer is empty and the rasterised pipeline "
                        'runs. Default behaviour (omit or pass "auto") '
                        "is multilingual: Tesseract loads ita+eng+deu+fra "
                        "and picks per region, so mixed-language clinical "
                        "scans work without you knowing the language. "
                        'Force a single tag ("ita", "eng", "deu", '
                        '"fra") when you have already classified the '
                        "document language: faster, slightly more accurate. "
                        'Custom "+"-joined subsets ("ita+eng") accepted. '
                        "Only honoured with trigger=true."
                    ),
                },
                "inline": {
                    "type": "boolean",
                    "default": True,
                    "description": (
                        "When false, the API enqueues an Arq job and "
                        "returns 202 with ``X-Job-Id`` for polling."
                    ),
                },
                "idempotency_key": {"type": "string"},
            },
            "required": ["patient_id", "document_id"],
        },
    ),
    Tool(
        name="get_document_references",
        description=(
            "Lista tutto ciò che fa riferimento a un documento: studi "
            "(con il loro ``link_kind`` ∈ "
            "``primary_report``/``addendum``/``second_opinion``/"
            "``extracted_from``/``cites``/``mentions``), report_contents "
            "(con ``role`` e ``clinical_event_id`` derivato), citazioni "
            "granulari (``ReportContentCitation``), e folder che lo "
            "contengono. È la vista 'Riferito da' della FE e l'input "
            "per il flusso che chiede all'utente di rimuovere reference "
            "attive prima di tombstonare un documento. Read-only; "
            "scope ``documents:read``. Cross-patient impossibile per "
            "costruzione (FK + path patient_id)."
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
        name="find_documents_by_content_hash",
        description=(
            "Trova i documenti vivi del paziente i cui byte (o "
            "l'artefatto originario) hanno un dato SHA-256. È la lookup "
            "di riconciliazione: usala per verificare se un file è già "
            "curato nel Drive prima di caricarlo su un evento (poi "
            "link_event_document invece di ricaricarlo). ``matched_on`` "
            "vale ``content`` o ``original``. Read-only; scope "
            "``documents:read``. Same-patient per costruzione."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "patient_id": {"type": "string"},
                "sha256": {
                    "type": "string",
                    "description": "Digest SHA-256, 64 char esadecimali minuscoli.",
                },
            },
            "required": ["patient_id", "sha256"],
        },
    ),
]


async def _download_document_binary(args: dict[str, Any]) -> str:
    patient_id = args["patient_id"]
    document_id = args["document_id"]
    file_id = args.get("file_id")

    # First hop: ``binary_url`` — gives us metadata (size, content_type,
    # backend-relative URL) without paying for the byte transfer if the
    # file is huge. The URL is a backend path, NOT a presigned storage
    # URL: it carries no signature, the auth is the same session token
    # used for this call.
    params: dict[str, Any] = {}
    if file_id:
        params["file_id"] = file_id
    try:
        meta = await api_get(
            f"/api/patients/{patient_id}/documents/{document_id}/binary_url",
            params=params or None,
        )
    except httpx.HTTPStatusError as exc:
        return json.dumps(
            {
                "error": "http_error",
                "status_code": exc.response.status_code,
                "detail": exc.response.text,
            }
        )

    if not isinstance(meta, dict):
        return json.dumps({"error": "unexpected_response", "payload": meta})

    size_bytes = meta.get("size_bytes")
    content_type = meta.get("content_type") or "application/octet-stream"
    content_url = meta.get("url")

    out: dict[str, Any] = {
        "document_id": meta.get("document_id"),
        "file_id": meta.get("file_id"),
        "content_type": content_type,
        "size_bytes": size_bytes,
        "content_url": content_url,
        "content_b64": None,
    }

    # Skip the inline byte fetch when the file is known to be too big:
    # bouncing 50 MB through the LLM context is wasteful and the agent
    # can either suggest the human follow ``content_url`` or fall back
    # to ``get_document_text`` for textual reasoning.
    if isinstance(size_bytes, int) and size_bytes > _BINARY_INLINE_MAX_BYTES:
        out["truncated"] = True
        out["truncation_reason"] = (
            f"file is {size_bytes} bytes; over the {_BINARY_INLINE_MAX_BYTES}-byte "
            "inline cap. Use content_url to download via the backend."
        )
        return json.dumps(out, indent=2, ensure_ascii=False)

    binary_path = f"/api/patients/{patient_id}/documents/{document_id}/binary"
    binary_params: dict[str, Any] = {}
    if file_id:
        binary_params["file_id"] = file_id
    try:
        body, ctype = await api_get_bytes(binary_path, params=binary_params or None)
    except httpx.HTTPStatusError as exc:
        out["error"] = "http_error"
        out["status_code"] = exc.response.status_code
        out["detail"] = exc.response.text
        return json.dumps(out, indent=2, ensure_ascii=False)

    if len(body) > _BINARY_INLINE_MAX_BYTES:
        # ``size_bytes`` was missing or stale; check after the fetch.
        out["truncated"] = True
        out["truncation_reason"] = (
            f"received {len(body)} bytes; over the {_BINARY_INLINE_MAX_BYTES}-byte "
            "inline cap. Use content_url to download via the backend."
        )
        return json.dumps(out, indent=2, ensure_ascii=False)

    out["content_type"] = ctype or content_type
    out["size_bytes"] = len(body)
    out["content_b64"] = base64.b64encode(body).decode("ascii")
    return json.dumps(out, indent=2, ensure_ascii=False)


async def _get_document_text(args: dict[str, Any]) -> str:
    patient_id = args["patient_id"]
    document_id = args["document_id"]
    file_id = args.get("file_id")
    engine = args.get("engine")
    trigger = bool(args.get("trigger", False))

    if not trigger:
        params: dict[str, Any] = {}
        if file_id:
            params["file_id"] = file_id
        if engine:
            params["engine"] = engine
        try:
            payload = await api_get(
                f"/api/patients/{patient_id}/documents/{document_id}/text",
                params=params or None,
            )
            return json.dumps(payload, indent=2, ensure_ascii=False)
        except httpx.HTTPStatusError as exc:
            # 404 with the ``ocr_cache_miss`` problem code is the only
            # failure that means "retry with trigger=true". Every other
            # status (auth, server error, OCR pipeline crash) must
            # surface verbatim so the caller does not loop on a real
            # outage thinking the cache is just cold.
            if exc.response.status_code == 404:
                detail: dict[str, Any] = {}
                try:
                    body = exc.response.json()
                    if isinstance(body, dict):
                        detail = body
                except ValueError:
                    pass
                if detail.get("code") == "ocr_cache_miss" or detail.get("type", "").endswith(
                    "ocr_cache_miss"
                ):
                    return json.dumps(
                        {
                            "status": "ocr_cache_miss",
                            "hint": "set trigger=true to populate the cache",
                        }
                    )
            return json.dumps(
                {
                    "error": "http_error",
                    "status_code": exc.response.status_code,
                    "detail": exc.response.text,
                }
            )

    body = {
        "file_id": file_id,
        "force": bool(args.get("force", False)),
        "inline": bool(args.get("inline", True)),
        "language": args.get("language"),
    }
    payload, headers = await api_post_with_headers(
        f"/api/patients/{patient_id}/documents/{document_id}/text",
        json={k: v for k, v in body.items() if v is not None},
        idempotency_key=args.get("idempotency_key"),
    )
    out: dict[str, Any] = payload if isinstance(payload, dict) else {"data": payload}
    job_id = headers.get("x-job-id") or headers.get("X-Job-Id")
    if job_id and not out.get("job_id"):
        out["job_id"] = job_id
    return json.dumps(out, indent=2, ensure_ascii=False)


async def _get_document_references(args: dict[str, Any]) -> str:
    patient_id = args["patient_id"]
    document_id = args["document_id"]
    payload = await api_get(f"/api/patients/{patient_id}/documents/{document_id}/references")
    return json.dumps(payload, indent=2, ensure_ascii=False)


async def _find_documents_by_content_hash(args: dict[str, Any]) -> str:
    patient_id = args["patient_id"]
    payload = await api_get(
        f"/api/patients/{patient_id}/documents-by-hash",
        params={"sha256": args["sha256"]},
    )
    return json.dumps(payload, indent=2, ensure_ascii=False)


_DISPATCH = {
    "download_document_binary": _download_document_binary,
    "get_document_text": _get_document_text,
    "get_document_references": _get_document_references,
    "find_documents_by_content_hash": _find_documents_by_content_hash,
}


async def handle(name: str, arguments: dict[str, Any]) -> str | list[TextContent]:
    handler = _DISPATCH.get(name)
    if handler is None:
        return f"Error: unknown tool '{name}' in document_reads module"
    return await handler(arguments)
