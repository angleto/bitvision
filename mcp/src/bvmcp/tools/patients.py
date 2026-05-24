"""MCP tools for patient records and fascicolo."""

from __future__ import annotations

from mcp.types import Tool

from bvmcp.tools.client import api_get

TOOLS = [
    Tool(
        name="search_patients",
        description=(
            "List the patients the assistant token can see, optionally "
            "filtered by free-text query ``q`` (matches display_name, "
            "tax_id, external_id) and / or by ``tag`` "
            "(``namespace:value``). Returns a paginated array of "
            "patient summaries each carrying ``id`` (UUID), "
            "``display_name``, ``birth_date``, ``sex``, plus the "
            "lightweight demographic fields. Use this as the entry "
            "point when you only have a name and need the patient_id "
            "for the rest of the patient-scoped tools (get_patient, "
            "get_fascicolo_index, ...). The result respects the "
            "assistant's per-patient grant: tokens scoped to a fixed "
            "patient list will only see those patients; an unscoped "
            "human session sees everything they have visibility over."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "q": {
                    "type": "string",
                    "maxLength": 255,
                    "description": (
                        "Free-text search across display_name, "
                        "tax_id, external_id. Case-insensitive; "
                        "substring match."
                    ),
                },
                "tag": {
                    "type": "string",
                    "maxLength": 320,
                    "description": (
                        "Filter to patients owning at least one study "
                        "with this tag. Format ``namespace:value``."
                    ),
                },
                "scope": {
                    "type": "string",
                    "enum": ["personal", "mine", "shared", "public", "all"],
                    "default": "personal",
                    "description": (
                        "Slice of the visible set: ``personal`` "
                        "(default) = managed-by-me + shared-with-me; "
                        "``mine`` = managed-by-me only; ``shared`` = "
                        "via Grant; ``public`` = open-data datasets; "
                        "``all`` = everything visible."
                    ),
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
                "offset": {"type": "integer", "minimum": 0, "default": 0},
            },
        },
    ),
    Tool(
        name="get_patient",
        description=(
            "Get a patient's profile including demographics: name, birth date, sex, "
            "tax ID (codice fiscale), phone, email, blood type, allergies, and clinical notes."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "patient_id": {"type": "string", "description": "UUID of the patient"},
            },
            "required": ["patient_id"],
        },
    ),
    Tool(
        name="get_fascicolo_index",
        description=(
            "Get the structured index of a patient's fascicolo (radiology record). "
            "Returns per-section counts: diagnostic studies, reports, clinical documents, "
            "annotations, and personal notebook entries. Inspired by FSE 2.0 (Italian "
            "electronic health record standard). Use this to understand what data is "
            "available for a patient before diving into specific sections."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "patient_id": {"type": "string", "description": "UUID of the patient"},
            },
            "required": ["patient_id"],
        },
    ),
    Tool(
        name="get_patient_timeline",
        description=(
            "Get a chronological timeline of all events in a patient's radiology record. "
            "Includes studies, reports, annotations, and clinical documents, sorted by date. "
            "Optionally filter by section (studies, reports, documents, annotations)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "patient_id": {"type": "string", "description": "UUID of the patient"},
                "section": {
                    "type": "string",
                    "enum": ["studies", "reports", "documents", "annotations"],
                    "description": "Optionally show only one section",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max items to return (default 50)",
                    "default": 50,
                },
            },
            "required": ["patient_id"],
        },
    ),
    Tool(
        name="list_patient_documents",
        description=(
            "List clinical documents attached to a patient record. Documents include "
            "consents, discharge letters, prescriptions, referrals, lab results, "
            "ER reports, clinical notes, and personal notebook entries. "
            "Optionally filter by document type."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "patient_id": {"type": "string", "description": "UUID of the patient"},
                "type": {
                    "type": "string",
                    "enum": [
                        "consent",
                        "discharge_letter",
                        "prescription",
                        "referral",
                        "lab_result",
                        "er_report",
                        "clinical_note",
                        "personal_notebook",
                        "other",
                    ],
                    "description": "Filter by document type",
                },
            },
            "required": ["patient_id"],
        },
    ),
]


async def handle(name: str, arguments: dict) -> str:
    import json

    if name == "search_patients":
        params: dict[str, object] = {}
        for k in ("q", "tag", "scope"):
            v = arguments.get(k)
            if v:
                params[k] = v
        if arguments.get("limit"):
            params["limit"] = arguments["limit"]
        if arguments.get("offset") is not None:
            params["offset"] = arguments["offset"]
        result = await api_get("/api/patients", params=params or None)
        return json.dumps(result, indent=2)

    if name == "get_patient":
        result = await api_get(f"/api/patients/{arguments['patient_id']}")
        return json.dumps(result, indent=2)

    if name == "get_fascicolo_index":
        result = await api_get(f"/api/patients/{arguments['patient_id']}/index")
        return json.dumps(result, indent=2)

    if name == "get_patient_timeline":
        params = {}
        if arguments.get("section"):
            params["section"] = arguments["section"]
        params["limit"] = arguments.get("limit", 50)
        result = await api_get(
            f"/api/patients/{arguments['patient_id']}/timeline",
            params=params,
        )
        return json.dumps(result, indent=2)

    if name == "list_patient_documents":
        params = {}
        if arguments.get("type"):
            params["type"] = arguments["type"]
        result = await api_get(
            f"/api/patients/{arguments['patient_id']}/documents",
            params=params,
        )
        return json.dumps(result, indent=2)

    raise ValueError(f"unknown tool: {name}")
