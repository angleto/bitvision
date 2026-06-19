"""MCP tools for annotations and reports.

After the marker-unification refactor (commit 96ea59d) the standalone
``Annotation`` table and its ``/api/annotations`` router were dropped.
"Annotation" is now an umbrella over two channels:

* ``Marker`` — geometric / measurement payloads anchored to study /
  series / instance. Lives at ``/api/patients/{pid}/markers``. See
  ``help(topic='annotation_kinds')`` for the closed vocabulary and the
  per-kind geometry shape (``measurement.*``, ``bbox.lesion``,
  ``fiducial``, ``reading-note``, ``text-overlay``).
* ``ClinicalNote`` — free-text prose, optional viewer anchor. Lives at
  ``/api/patients/{pid}/notes``.

This module exposes a unified read surface so callers don't have to know
which channel a given annotation lives in.
"""

from __future__ import annotations

import json
from typing import Any

from mcp.types import Tool

from bvmcp.tools.client import api_get

TOOLS = [
    Tool(
        name="get_annotations",
        description=(
            "Read annotations anchored to a DICOM target (study, series, or "
            "instance). Returns the unified view over markers (geometric / "
            "measurement payloads, e.g. measurement.distance, bbox.lesion) "
            "and, when ``include_notes=true``, free-text clinical notes. "
            "Each item carries ``author_kind`` ('human' / 'agent' / "
            "'system'), ``model_id`` / ``provider`` for AI-generated rows, "
            "and the channel-specific payload (``geometry`` / ``body`` / "
            "``computed`` for markers; ``text`` / ``anchor`` for notes). "
            "Filter by ``kind`` to scope to one marker family (e.g. "
            "``measurement.distance``, ``bbox.lesion``). See "
            "``help(topic='annotation_kinds')`` for the full vocabulary. "
            "``patient_id`` is required because the markers / notes API "
            "is patient-scoped by design (no cross-patient access)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "patient_id": {
                    "type": "string",
                    "description": "UUID of the patient that owns the target.",
                },
                "target_kind": {
                    "type": "string",
                    "enum": ["study", "series", "instance", "pathology_slide"],
                    "description": (
                        "Type of the target resource. ``pathology_slide`` "
                        "anchors to a whole-slide image (target_id = slide id) "
                        "for polygon / point annotations drawn in the WSI viewer."
                    ),
                },
                "target_id": {
                    "type": "string",
                    "description": "UUID of the target resource.",
                },
                "kind": {
                    "type": "string",
                    "description": (
                        "Optional marker kind filter (e.g. "
                        "``measurement.distance``, ``bbox.lesion``). See "
                        "``help(topic='annotation_kinds')`` for the full "
                        "list. Ignored for clinical notes."
                    ),
                },
                "author_kind": {
                    "type": "string",
                    "enum": ["human", "agent"],
                    "description": (
                        "Optionally filter by author. Applies to clinical "
                        "notes; markers are filtered client-side."
                    ),
                },
                "include_notes": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "When true, also include clinical notes attached to the same target."
                    ),
                },
                "include_deleted": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "When true, also include soft-deleted markers "
                        "(tombstones). Use to find a removed annotation's "
                        "id and ``restore_annotation`` it."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "default": 500,
                    "minimum": 1,
                    "maximum": 2000,
                },
            },
            "required": ["patient_id", "target_kind", "target_id"],
        },
    ),
    Tool(
        name="list_reports",
        description=(
            "List radiology reports for a study. Reports are versioned — "
            "multiple versions may exist for the same study. Each report has "
            "text content and optionally an attached file (PDF, DOCX)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "study_id": {
                    "type": "string",
                    "description": "UUID of the study",
                },
            },
            "required": ["study_id"],
        },
    ),
]


async def handle(name: str, arguments: dict) -> str:
    if name == "get_annotations":
        return await _get_annotations(arguments)

    if name == "list_reports":
        result = await api_get(f"/api/studies/{arguments['study_id']}/reports")
        return json.dumps(result, indent=2)

    raise ValueError(f"unknown tool: {name}")


async def _get_annotations(arguments: dict) -> str:
    patient_id = arguments["patient_id"]
    target_kind = arguments["target_kind"]
    target_id = arguments["target_id"]
    kind = arguments.get("kind")
    author_kind = arguments.get("author_kind")
    include_notes = bool(arguments.get("include_notes", False))
    include_deleted = bool(arguments.get("include_deleted", False))
    limit = int(arguments.get("limit", 500))

    marker_params: dict[str, Any] = {
        "target_kind": target_kind,
        "target_id": target_id,
        "limit": limit,
    }
    if kind:
        marker_params["kind"] = kind
    if include_deleted:
        marker_params["include_deleted"] = "true"
    markers = await api_get(f"/api/patients/{patient_id}/markers", params=marker_params)
    if author_kind and isinstance(markers, list):
        markers = [m for m in markers if m.get("author_kind") == author_kind]

    payload: dict[str, Any] = {
        "patient_id": patient_id,
        "target_kind": target_kind,
        "target_id": target_id,
        "markers": markers,
        "marker_count": len(markers) if isinstance(markers, list) else None,
    }

    if include_notes:
        note_params: dict[str, Any] = {
            "target_kind": target_kind,
            "target_id": target_id,
            "limit": min(limit, 500),
        }
        if author_kind:
            note_params["author_kind"] = author_kind
        notes = await api_get(f"/api/patients/{patient_id}/notes", params=note_params)
        payload["notes"] = notes
        payload["note_count"] = len(notes) if isinstance(notes, list) else None

    return json.dumps(payload, indent=2, ensure_ascii=False)
