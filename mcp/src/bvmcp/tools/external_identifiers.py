"""Discovery + Ingestion tools — patient external identifiers.

Patient identity is UUID-only at the join layer; every
business identifier (CF, MRN, DICOM IssuerOfPatientID, lab id, ...)
lives as descriptive metadata in ``patients.external_identifiers``.
These two tools expose the identifier-as-metadata side to the agent:
adding an entry on a known patient (``link_external_identifier``),
and looking up the patients that carry a given (system, value)
across the visible patient set (``lookup_external_identifier``).

Both tools require sensitive scopes — see :mod:`bvmcp.scopes`.
"""

from __future__ import annotations

import json

from mcp.types import Tool

from bvmcp.tools.client import api_get, api_post

TOOLS = [
    Tool(
        name="link_external_identifier",
        description=(
            "Add a business identifier (codice fiscale, MRN, DICOM "
            "IssuerOfPatientID, lab id, registry id, ...) to a known "
            "patient. The entry is descriptive metadata: BitVision "
            "FK-joins on the UUID ``patient_id`` only, never on the "
            "external value. The operation is idempotent — calling "
            "twice with the same ``(system, value)`` upserts the entry. "
            "Returns the full ``external_identifiers`` array post-edit. "
            "Sensitive: requires ``patients:identify`` scope."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "patient_id": {"type": "string"},
                "system": {
                    "type": "string",
                    "maxLength": 255,
                    "description": (
                        "Identifier system URI. Examples: "
                        "``urn:oid:2.16.840.1.113883.2.9.4.3.2`` for the "
                        "Italian fiscal-code OID; ``https://aslto1.it/"
                        "patient-mrn`` for an MRN; "
                        "``DICOM:Issuer:<aetitle>`` for a DICOM PatientID."
                    ),
                },
                "value": {"type": "string", "maxLength": 255},
                "type": {
                    "type": "string",
                    "maxLength": 64,
                    "description": (
                        "Type code. Common values: ``fiscal-code``, "
                        "``MR`` (medical record), ``PI`` (passport-style)."
                    ),
                },
                "assigner": {
                    "type": "string",
                    "maxLength": 255,
                    "description": (
                        "Human-readable name of the issuer "
                        "(e.g. ``Agenzia delle Entrate``, "
                        "``ASL Torino 1``)."
                    ),
                },
            },
            "required": ["patient_id", "system", "value", "type"],
        },
    ),
    Tool(
        name="lookup_external_identifier",
        description=(
            "Find patients in the visible set that carry a given "
            "``(system, value)`` external identifier. The lookup is "
            "intentionally NON-deterministic: it returns 0, 1, or more "
            "candidates, never auto-binds. The caller (UI / agent) "
            "must confirm identity with the human before acting on the "
            "result. Restricted to patients the assistant can already "
            "see — this tool cannot enumerate identifiers across the "
            "global patient pool. Sensitive: requires ``lookup:external`` "
            "scope."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "system": {"type": "string", "maxLength": 255},
                "value": {"type": "string", "maxLength": 255},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
            },
            "required": ["system", "value"],
        },
    ),
]


async def handle(name: str, arguments: dict) -> str:
    if name == "link_external_identifier":
        body = {
            "system": arguments["system"],
            "value": arguments["value"],
            "type": arguments["type"],
        }
        if arguments.get("assigner"):
            body["assigner"] = arguments["assigner"]
        result = await api_post(
            f"/api/patients/{arguments['patient_id']}/external-identifiers",
            json=body,
        )
        return json.dumps(result, indent=2)

    if name == "lookup_external_identifier":
        result = await api_get(
            "/api/patients/lookup-external",
            params={
                "system": arguments["system"],
                "value": arguments["value"],
                "limit": arguments.get("limit", 10),
            },
        )
        return json.dumps(result, indent=2)

    raise ValueError(f"unknown tool: {name}")
