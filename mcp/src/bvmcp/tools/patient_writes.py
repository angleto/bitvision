"""MCP write tools for patient anagrafica (demographics).

Companion to :mod:`bvmcp.tools.patients`, which is read-only. The
backend gates the ``PATCH /api/patients/{id}`` endpoint behind the
``patient:write`` scope (see ``backend/src/bvphoenix/api/ai_assistants.py``
scope catalog), so an assistant can only modify the patients it has
been explicitly granted access to *and* whose owner ticked the
``patient:write`` checkbox at token-mint time.

Mutating contract is the one shared with document / consultation
writes:

* ``etag`` — optional ``If-Match`` header. When present, must match the
  patient's main-branch commit hash; otherwise 412.
* ``idempotency_key`` — optional ``Idempotency-Key`` header. The
  backend caches the response for 24h; the same key + same body
  replays, the same key + different body returns 422.
* ``dry_run`` — optional query flag. The backend returns the diff
  without committing or auditing.

Patient registration (``POST /api/patients``) and deletion
(``DELETE /api/patients/{id}``) intentionally stay human-only and are
NOT exposed here: an agent token is bound to a fixed ``patient_ids``
allow-list, so it cannot meaningfully create a fascicolo it would not
even be able to read back, and erasing one is a destructive,
legally-significant action that belongs to the human owner.
"""

from __future__ import annotations

import json
from typing import Any

from mcp.types import TextContent, Tool

from bvmcp.tools.client import api_delete, api_get, api_patch, api_post_with_headers

_PATIENT_FIELDS: tuple[str, ...] = (
    "display_name",
    "external_id",
    "birth_date",
    "sex",
    "tax_id",
    "phone",
    "email",
    "address",
    "blood_type",
    "birth_place_city",
    "birth_place_province",
    "asl_code",
    "asl_name",
    "allergies",
    "notes",
    "contacts",
)

TOOLS: list[Tool] = [
    Tool(
        name="update_patient",
        description=(
            "Modify a patient's anagrafica (demographics). Editable fields: "
            "display_name, external_id, birth_date (YYYY-MM-DD), sex (M/F/O), "
            "tax_id (codice fiscale), phone, email, address, blood_type, "
            "allergies, notes, contacts. Only send the fields you want to "
            "change — omitted fields are left untouched. Pass ``dry_run=true`` "
            "to preview the diff without committing. Pass ``etag`` (from a "
            "prior get_patient response) to require optimistic-concurrency "
            "matching: a stale etag returns 412. Pass ``idempotency_key`` to "
            "make the call replay-safe for 24h. Scope: most fields require "
            "``patient:write`` (canonical: ``patients:write``). Identifier "
            "fields ``tax_id`` and ``external_id`` write into the cross-cutting "
            "external_identifiers store and additionally require the granular "
            "``patients:identify`` scope; without it the call returns 403 "
            "instead of silently dropping the field — use "
            "``link_external_identifier`` for finer-grained identifier "
            "management."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "patient_id": {
                    "type": "string",
                    "description": "UUID of the patient to update.",
                },
                "display_name": {"type": "string", "minLength": 1, "maxLength": 255},
                "external_id": {"type": ["string", "null"], "maxLength": 128},
                "birth_date": {
                    "type": ["string", "null"],
                    "description": "ISO 8601 date (YYYY-MM-DD) or null to clear.",
                },
                "sex": {
                    "type": ["string", "null"],
                    "maxLength": 1,
                    "description": "Single character: M, F, or O.",
                },
                "tax_id": {"type": ["string", "null"], "maxLength": 32},
                "phone": {"type": ["string", "null"], "maxLength": 32},
                "email": {"type": ["string", "null"], "maxLength": 255},
                "address": {"type": ["string", "null"]},
                "blood_type": {"type": ["string", "null"], "maxLength": 8},
                "birth_place_city": {
                    "type": ["string", "null"],
                    "maxLength": 128,
                    "description": (
                        "Comune of birth — free text. For Italian patients "
                        "the response includes a ``cf_warnings`` array if "
                        "this field disagrees with the Belfiore code "
                        "embedded in tax_id (the validator never "
                        "auto-corrects)."
                    ),
                },
                "birth_place_province": {
                    "type": ["string", "null"],
                    "maxLength": 8,
                    "description": (
                        "Province / state code (``MI``, ``RM``, "
                        "``AL``...). Free text; we don't enforce a "
                        "lookup so foreign codes are accepted."
                    ),
                },
                "asl_code": {
                    "type": ["string", "null"],
                    "maxLength": 16,
                    "description": (
                        "Italian regional health authority numeric code "
                        "(e.g. ``301`` for ASL Roma 1). Optional, leave "
                        "null for non-IT patients."
                    ),
                },
                "asl_name": {
                    "type": ["string", "null"],
                    "maxLength": 255,
                    "description": "Human-readable ASL label.",
                },
                "allergies": {"type": ["string", "null"]},
                "notes": {"type": ["string", "null"]},
                "contacts": {
                    "type": ["array", "null"],
                    "maxItems": 20,
                    "description": (
                        "Full replacement list of contacts. Each contact is an "
                        "object with optional id, name, relation, phone, email, "
                        "notes. Existing delegation pointers on contacts already "
                        "promoted to delegates are preserved when their id is "
                        "round-tripped."
                    ),
                    "items": {
                        "type": "object",
                        "additionalProperties": True,
                    },
                },
                "etag": {
                    "type": "string",
                    "description": (
                        "Optional If-Match: the etag returned by the last "
                        "get_patient / update_patient. 412 on mismatch."
                    ),
                },
                "idempotency_key": {
                    "type": "string",
                    "description": (
                        "Optional Idempotency-Key. Replays the same response "
                        "for 24h on retry; conflicts on body mismatch."
                    ),
                },
                "dry_run": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Preview the diff without committing. Audit and the "
                        "F12 versioning chain are not touched."
                    ),
                },
            },
            "required": ["patient_id"],
        },
    ),
    Tool(
        name="decode_codice_fiscale",
        description=(
            "Decode an Italian codice fiscale (tax id) and report any "
            "disagreement with stored demographic fields. Pure helper "
            "— does not touch the patient row, does not require any "
            "scope. Returns ``decoded`` (surname/first-name initials, "
            "birth_date, sex, Belfiore code) plus ``warnings`` listing "
            "every mismatch between the CF and the optional check "
            "fields the agent passes in. Useful before issuing an "
            "``update_patient`` to validate a CF the user just typed, "
            "or to derive a missing birth_date / sex / birth_place "
            "from the CF when the demographic form was incomplete."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "cf": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 32,
                    "description": "The codice fiscale to decode (16-char canonical form).",
                },
                "birth_date": {
                    "type": "string",
                    "description": "Optional ISO date to cross-check against the CF.",
                },
                "sex": {
                    "type": "string",
                    "maxLength": 1,
                    "description": "Optional ``M`` / ``F`` to cross-check.",
                },
                "birth_place_belfiore": {
                    "type": "string",
                    "maxLength": 4,
                    "description": (
                        "Optional 4-char Belfiore code to cross-check "
                        "(``H501`` = Roma, ``F205`` = Milano)."
                    ),
                },
            },
            "required": ["cf"],
        },
    ),
    Tool(
        name="add_patient_contact",
        description=(
            "Append a single contact to a patient's contacts list — "
            "ergonomic wrapper around ``update_patient`` so the agent "
            "doesn't have to read-modify-write the whole array. "
            "Each contact must have at least a ``name``; ``relation`` "
            "(e.g. spouse, daughter, GP), ``phone``, ``email``, "
            "``notes`` are optional. The backend assigns a stable "
            "``id`` to the new entry. Pass ``dry_run=true`` to "
            "preview, ``etag`` for optimistic concurrency, "
            "``idempotency_key`` for replay safety. Requires the "
            "``patient:write`` scope."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "patient_id": {"type": "string"},
                "name": {"type": "string", "minLength": 1, "maxLength": 255},
                "relation": {"type": "string", "maxLength": 64},
                "phone": {"type": "string", "maxLength": 64},
                "email": {"type": "string", "maxLength": 255},
                "notes": {"type": "string"},
                "etag": {"type": "string"},
                "idempotency_key": {"type": "string"},
                "dry_run": {"type": "boolean", "default": False},
            },
            "required": ["patient_id", "name"],
        },
    ),
    Tool(
        name="remove_patient_contact",
        description=(
            "Remove a single contact (identified by ``contact_id``) "
            "from a patient's contacts list. The contact id is "
            "returned by ``get_patient`` on each contact entry. "
            "Idempotent — removing an already-absent id is a no-op "
            "and returns the unchanged patient. Requires the "
            "``patient:write`` scope. NOTE: a contact that has been "
            "promoted to a delegate (carries a ``grant_id``) cannot "
            "be removed via this tool — the operator must revoke the "
            "delegation first via ``DELETE "
            "/api/patients/{id}/contacts/{cid}/delegate`` (no MCP "
            "tool wraps this on purpose: revoking access is human-"
            "only, like ``consultations:finalize``)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "patient_id": {"type": "string"},
                "contact_id": {"type": "string"},
                "etag": {"type": "string"},
                "idempotency_key": {"type": "string"},
                "dry_run": {"type": "boolean", "default": False},
            },
            "required": ["patient_id", "contact_id"],
        },
    ),
]


async def _update_patient(args: dict[str, Any]) -> str:
    patient_id = args["patient_id"]
    body: dict[str, Any] = {}
    for field in _PATIENT_FIELDS:
        if field in args:
            body[field] = args[field]
    params: dict[str, Any] = {}
    if args.get("dry_run"):
        params["dry_run"] = "true"
    payload, _headers = await api_patch(
        f"/api/patients/{patient_id}",
        json=body,
        params=params or None,
        if_match=args.get("etag"),
        idempotency_key=args.get("idempotency_key"),
    )
    return json.dumps(payload, indent=2, ensure_ascii=False)


async def _add_patient_contact(args: dict[str, Any]) -> str:
    """Hit the dedicated ``POST /api/patients/{id}/contacts`` endpoint.

    No more read-modify-write on a JSONB blob — the backend table is
    1:N (alembic 0071) and the endpoint INSERTs one row atomically,
    returning the server-assigned id. The agent's old ``name`` /
    ``relation`` argument names are mapped to the public schema
    (``label`` / ``relationship``) so the tool surface stays
    backward-compatible for existing assistant prompts.
    """
    patient_id = args["patient_id"]
    body: dict[str, Any] = {
        "label": args["name"],
    }
    if args.get("relation"):
        body["relationship"] = args["relation"]
    for k in ("email", "phone", "notes"):
        if args.get(k):
            body[k] = args[k]
    payload, _headers = await api_post_with_headers(
        f"/api/patients/{patient_id}/contacts",
        json=body,
        idempotency_key=args.get("idempotency_key"),
    )
    return json.dumps(payload, indent=2, ensure_ascii=False)


async def _remove_patient_contact(args: dict[str, Any]) -> str:
    """Hit the dedicated ``DELETE /api/patients/{id}/contacts/{cid}``
    endpoint. Refuses delegated contacts (HTTP 409) — the agent must
    revoke delegation first via the human-only delegation endpoint.
    """
    patient_id = args["patient_id"]
    contact_id = args["contact_id"]
    code = await api_delete(
        f"/api/patients/{patient_id}/contacts/{contact_id}",
        if_match=args.get("etag"),
    )
    return json.dumps({"status": "deleted", "http_status": code})


async def _decode_codice_fiscale(args: dict[str, Any]) -> str:
    params: dict[str, Any] = {"cf": args["cf"]}
    for k in ("birth_date", "sex", "birth_place_belfiore"):
        if args.get(k):
            params[k] = args[k]
    payload = await api_get("/api/patients/_decode_cf", params=params)
    return json.dumps(payload, indent=2, ensure_ascii=False)


_DISPATCH = {
    "update_patient": _update_patient,
    "decode_codice_fiscale": _decode_codice_fiscale,
    "add_patient_contact": _add_patient_contact,
    "remove_patient_contact": _remove_patient_contact,
}


async def handle(name: str, arguments: dict[str, Any]) -> str | list[TextContent]:
    handler = _DISPATCH.get(name)
    if handler is None:
        return f"Error: unknown tool '{name}' in patient_writes module"
    return await handler(arguments)
