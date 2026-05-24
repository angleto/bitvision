"""Discovery + Navigation + Ingestion + Synthesis tools — report contents.

The Expression layer is the heart of the data model. Tools here let
an agent:

* Discovery: list the narratives that talk about an event
  (``find_reports``).
* Navigation: read one narrative with all its workflow metadata
  (``get_report_content``).
* Ingestion: parse a source document into a new
  ``original`` / ``derived`` narrative (``extract_report_content``).
* Synthesis: draft a ``canonical_synthesis`` (the BitVision Referto)
  citing existing narratives (``create_canonical_referto``).
* Synthesis (cont.): cite a source artefact from any narrative
  (``cite_source``).

Sign / endorse / supersede ship in a follow-up: ``synthesis:sign``
is HUMAN-only at the backend, so the agent surface intentionally
stops at ``draft → final`` for synthesis content.
"""

from __future__ import annotations

import json
from typing import Any

from mcp.types import Tool

from bvmcp.tools.client import api_get, api_patch, api_post, api_post_with_headers

TOOLS = [
    # --- Discovery ----------------------------------------------------------
    Tool(
        name="find_reports",
        description=(
            "List the report_contents (narratives) attached to a clinical "
            "event. Each row carries ``authority`` (one of original / "
            "derived / canonical_synthesis), ``status`` (workflow state), "
            "the narrative_md body, and provenance (author_kind, "
            "model_id, agent_token_id). Use this to enumerate every "
            "interpretation of an event before deciding which to read "
            "in full or to consolidate into a canonical_synthesis."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "event_id": {
                    "type": "string",
                    "description": "UUID of the clinical event",
                },
            },
            "required": ["event_id"],
        },
    ),
    # --- Navigation --------------------------------------------------------
    Tool(
        name="get_report_content",
        description=(
            "Read one report_content (Expression) by id. Returns the "
            "full narrative, structured fields, workflow metadata "
            "(authority, status, supersede chain), authoring details "
            "(author_kind, model_id, parser_version, signing block "
            "when applicable), and the ETag for downstream If-Match "
            "writes."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "report_content_id": {
                    "type": "string",
                    "description": "UUID of the report_content",
                },
            },
            "required": ["report_content_id"],
        },
    ),
    # --- Ingestion ---------------------------------------------------------
    Tool(
        name="extract_report_content",
        description=(
            "Create a new ``original`` or ``derived`` report_content by "
            "extracting a narrative from a source document. The status "
            "starts at ``extracted_auto``; a human clinician can later "
            "endorse it via the ``endorse_report_content`` tool (when "
            "the agent has the ``reports:endorse`` scope) or via the "
            "UI. Use ``original`` when the source document is the "
            "primary copy from the issuing system; use ``derived`` "
            "when it is a downstream copy (scan of a fotocopia, OCR "
            "of a phone photo, etc.). The created content is "
            "automatically tagged with the calling agent's "
            "``agent_token_id`` for provenance."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "clinical_event_id": {
                    "type": "string",
                    "description": "UUID of the clinical event the narrative is about",
                },
                "authority": {
                    "type": "string",
                    "enum": ["original", "derived"],
                    "description": (
                        "Trust ladder. ``original`` = primary copy from the "
                        "issuing system; ``derived`` = downstream copy."
                    ),
                },
                "title": {"type": "string", "maxLength": 255},
                "narrative_md": {"type": "string"},
                "language": {"type": "string", "default": "it", "maxLength": 10},
                "structured_fields": {
                    "type": "object",
                    "description": (
                        "Free-form structured payload (e.g. parsed sections, "
                        "ICD codes, lab values). Stored as JSONB."
                    ),
                },
                "parser_version": {
                    "type": "string",
                    "maxLength": 64,
                    "description": (
                        "Identifier of the parser / model that produced "
                        "this extraction (e.g. ``ocr-tesseract-5.3.0``, "
                        "``llm-claude-opus-4.7-2026-04``)."
                    ),
                },
                "model_id": {"type": "string", "maxLength": 128},
                "provider": {"type": "string", "maxLength": 64},
            },
            "required": ["clinical_event_id", "authority"],
        },
    ),
    # --- Synthesis ---------------------------------------------------------
    Tool(
        name="create_canonical_referto",
        description=(
            "Draft a canonical_synthesis report_content — the BitVision "
            "Referto for a clinical event. Status starts at ``draft``; "
            "the agent can later transition it to ``final`` via the "
            "PATCH endpoint, but the ``signed`` transition is "
            "HUMAN-only and refused by the backend even if the agent "
            "has the ``synthesis:sign`` scope on paper. After drafting, "
            "use ``cite_source`` (zero or more times) to attach the "
            "original / derived contents that back the synthesis: "
            "the citations make the synthesis auditable rather than "
            "hallucinated prose."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "clinical_event_id": {
                    "type": "string",
                    "description": "UUID of the clinical event",
                },
                "title": {
                    "type": "string",
                    "maxLength": 255,
                    "description": ("Short title shown in the UI / cited in the patient timeline."),
                },
                "narrative_md": {"type": "string"},
                "findings_md": {"type": "string"},
                "recommendations_md": {"type": "string"},
                "language": {"type": "string", "default": "it", "maxLength": 10},
                "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                "structured_fields": {"type": "object"},
                "deidentified_input": {
                    "type": "boolean",
                    "description": (
                        "True iff the LLM was prompted with de-identified "
                        "input (the patient identity was masked before the "
                        "model saw it). Recorded in ``consent_snapshot`` "
                        "downstream."
                    ),
                },
                "model_id": {"type": "string", "maxLength": 128},
                "provider": {"type": "string", "maxLength": 64},
            },
            "required": ["clinical_event_id", "title"],
        },
    ),
    # --- Workflow transitions ------------------------------------------
    Tool(
        name="update_report_content",
        description=(
            "Edit a report_content in place — body fields and / or "
            "workflow status. Status transitions allowed via this "
            "endpoint follow the per-authority ladder enforced by the "
            "backend: a draft canonical_synthesis can move to "
            "``final``, an extracted_auto original / derived can move "
            "to ``endorsed``, etc. The ``signed`` transition is "
            "HUMAN-only and rejected here even with the right scope; "
            "use ``endorse_report_content`` for the lightweight "
            "endorsement workflow on original / derived. Terminal rows "
            "(``signed`` / ``stale`` / ``rejected``) refuse edits with "
            "409. Requires the row's current ETag as ``If-Match`` to "
            "block lost-update races."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "report_content_id": {"type": "string"},
                "etag": {
                    "type": "string",
                    "description": (
                        "ETag of the report_content; used as If-Match "
                        "to refuse the edit if the content changed "
                        "since the last read."
                    ),
                },
                "title": {"type": "string", "maxLength": 255},
                "narrative_md": {"type": "string"},
                "findings_md": {
                    "type": "string",
                    "description": (
                        "Only honoured for canonical_synthesis rows; ignored on original / derived."
                    ),
                },
                "recommendations_md": {
                    "type": "string",
                    "description": (
                        "Only honoured for canonical_synthesis rows; ignored on original / derived."
                    ),
                },
                "structured_fields": {"type": "object"},
                "status": {
                    "type": "string",
                    "description": (
                        "Optional workflow transition. Allowed targets "
                        "depend on authority + current status. "
                        "``signed`` is rejected: signing is human-only."
                    ),
                },
            },
            "required": ["report_content_id", "etag"],
        },
    ),
    Tool(
        name="endorse_report_content",
        description=(
            "Mark an extracted_auto report_content as endorsed (light "
            "workflow). Applies only to original / derived authorities; "
            "canonical_synthesis uses the heavy sign workflow. Endorse "
            "is the lightweight signal that a clinician has reviewed an "
            "extracted narrative and confirms it is a faithful rendering "
            "of the source — distinct from the medico-legal sign on a "
            "BitVision Referto. Sensitive: requires ``reports:endorse`` "
            "scope (granted only to assistants whose operator has "
            "vetted the agent's clinical accuracy)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "report_content_id": {"type": "string"},
                "etag": {
                    "type": "string",
                    "description": (
                        "ETag of the report_content; used as If-Match "
                        "to refuse the endorse if the content changed "
                        "since the last read."
                    ),
                },
            },
            "required": ["report_content_id", "etag"],
        },
    ),
    Tool(
        name="reject_report_content",
        description=(
            "Reject a canonical_synthesis (heavy workflow). Allowed "
            "from ``draft`` or ``final``; the row becomes terminal "
            "with ``rejected_reason`` populated. Use this when a "
            "synthesis is found to be incorrect, irrelevant, or "
            "superseded by external information that makes it not "
            "worth signing."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "report_content_id": {"type": "string"},
                "reason": {"type": "string", "minLength": 1, "maxLength": 2000},
                "etag": {"type": "string"},
            },
            "required": ["report_content_id", "reason", "etag"],
        },
    ),
    Tool(
        name="supersede_report_content",
        description=(
            "Replace an existing report_content with a new draft. "
            "The previous row transitions to ``status='stale'`` and "
            "gets ``superseded_by_id`` populated; the new row inherits "
            "the parent clinical_event, the authority, and the existing "
            "citations + content_document_links. Use case: a signed "
            "canonical_synthesis is replaced by a new draft after an "
            "addendum (signed contents are otherwise immutable); an "
            "extracted_auto is replaced by a fresh OCR with better "
            "quality. The new row's editable fields override the "
            "predecessor's content; omit a field to keep the previous "
            "value. The reason is recorded in the supersede provenance "
            "event."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "report_content_id": {"type": "string"},
                "reason": {"type": "string", "minLength": 1, "maxLength": 2000},
                "title": {"type": "string", "maxLength": 255},
                "narrative_md": {"type": "string"},
                "findings_md": {"type": "string"},
                "recommendations_md": {"type": "string"},
                "structured_fields": {"type": "object"},
                "etag": {"type": "string"},
            },
            "required": ["report_content_id", "reason", "etag"],
        },
    ),
    Tool(
        name="cite_source",
        description=(
            "Attach a citation from a report_content to a source artefact "
            "(another report_content, a document, an imaging study, a "
            "DICOM series, an annotation, a lab value). Cross-patient "
            "citations are refused by the backend. Use the fine-grained "
            "pointer columns when applicable: ``page`` + ``bbox`` for a "
            "specific paragraph in a PDF, ``slice_idx`` for a DICOM "
            "slice index, ``annotation_marker_idx`` for an SR-style "
            "sub-target."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "report_content_id": {
                    "type": "string",
                    "description": "UUID of the citing report_content",
                },
                "target_kind": {
                    "type": "string",
                    "enum": [
                        "clinical_event",
                        "imaging_study",
                        "series",
                        "report_content",
                        "document",
                        "marker",
                        "lab_value",
                    ],
                },
                "target_id": {
                    "type": "string",
                    "description": "UUID of the cited artefact",
                },
                "excerpt": {"type": "string"},
                "page": {"type": "integer", "minimum": 1},
                "bbox": {
                    "type": "object",
                    "description": (
                        "Bounding box on the page, e.g. "
                        "``{x: 100, y: 200, w: 300, h: 50, page: 3}``"
                    ),
                },
                "file_id": {
                    "type": "string",
                    "description": (
                        "UUID of the specific document_files row when the document is multi-file."
                    ),
                },
                "slice_idx": {"type": "integer", "minimum": 0},
                "annotation_marker_idx": {"type": "integer", "minimum": 0},
                "lab_value_id": {"type": "string"},
            },
            "required": ["report_content_id", "target_kind", "target_id"],
        },
    ),
]


async def handle(name: str, arguments: dict) -> str:
    if name == "find_reports":
        result = await api_get(f"/api/clinical-events/{arguments['event_id']}/report-contents")
        return json.dumps(result, indent=2)

    if name == "get_report_content":
        result = await api_get(f"/api/report-contents/{arguments['report_content_id']}")
        return json.dumps(result, indent=2)

    if name == "extract_report_content":
        body: dict[str, Any] = {
            "clinical_event_id": arguments["clinical_event_id"],
            "authority": arguments["authority"],
            "language": arguments.get("language", "it"),
        }
        for k in (
            "title",
            "narrative_md",
            "structured_fields",
            "parser_version",
            "model_id",
            "provider",
        ):
            v = arguments.get(k)
            if v is not None:
                body[k] = v
        result = await api_post("/api/report-contents", json=body)
        return json.dumps(result, indent=2)

    if name == "create_canonical_referto":
        body = {
            "clinical_event_id": arguments["clinical_event_id"],
            "authority": "canonical_synthesis",
            "title": arguments["title"],
            "language": arguments.get("language", "it"),
        }
        for k in (
            "narrative_md",
            "findings_md",
            "recommendations_md",
            "confidence",
            "structured_fields",
            "deidentified_input",
            "model_id",
            "provider",
        ):
            v = arguments.get(k)
            if v is not None:
                body[k] = v
        result = await api_post("/api/report-contents", json=body)
        return json.dumps(result, indent=2)

    if name == "update_report_content":
        rc_id = arguments["report_content_id"]
        body: dict[str, Any] = {}
        for k in (
            "title",
            "narrative_md",
            "findings_md",
            "recommendations_md",
            "structured_fields",
            "status",
        ):
            v = arguments.get(k)
            if v is not None:
                body[k] = v
        result, _ = await api_patch(
            f"/api/report-contents/{rc_id}",
            json=body,
            if_match=arguments["etag"],
        )
        return json.dumps(result, indent=2)

    if name == "endorse_report_content":
        rc_id = arguments["report_content_id"]
        result, _ = await api_post_with_headers(
            f"/api/report-contents/{rc_id}/endorse",
            json={},
            if_match=arguments["etag"],
        )
        return json.dumps(result, indent=2)

    if name == "reject_report_content":
        rc_id = arguments["report_content_id"]
        result, _ = await api_post_with_headers(
            f"/api/report-contents/{rc_id}/reject",
            json={"reason": arguments["reason"]},
            if_match=arguments["etag"],
        )
        return json.dumps(result, indent=2)

    if name == "supersede_report_content":
        rc_id = arguments["report_content_id"]
        body: dict[str, Any] = {"reason": arguments["reason"]}
        for k in (
            "title",
            "narrative_md",
            "findings_md",
            "recommendations_md",
            "structured_fields",
        ):
            v = arguments.get(k)
            if v is not None:
                body[k] = v
        result, _ = await api_post_with_headers(
            f"/api/report-contents/{rc_id}/supersede",
            json=body,
            if_match=arguments["etag"],
        )
        return json.dumps(result, indent=2)

    if name == "cite_source":
        rc_id = arguments["report_content_id"]
        body = {
            "target_kind": arguments["target_kind"],
            "target_id": arguments["target_id"],
        }
        for k in (
            "excerpt",
            "page",
            "bbox",
            "file_id",
            "slice_idx",
            "annotation_marker_idx",
            "lab_value_id",
        ):
            v = arguments.get(k)
            if v is not None:
                body[k] = v
        result = await api_post(f"/api/report-contents/{rc_id}/cite", json=body)
        return json.dumps(result, indent=2)

    raise ValueError(f"unknown tool: {name}")
