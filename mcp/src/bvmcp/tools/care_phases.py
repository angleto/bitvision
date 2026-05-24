"""MCP tools — Care timeline & clinical phases.

CarePhase is the **temporal axis at the grouping level**: a semantic
wrapper over one or more ``ClinicalEvent`` rows (diagnosis workup,
surgery, follow-up, surveillance, ...). The atomic level
(``ClinicalEvent``) lives in ``clinical_events``; the organisational
axis (``Folder``) and the cross-cutting axis (``Tag``) are
orthogonal. Conceptual placement: see ``docs/data-model.md §0`` and
``docs/care-timeline-phases.md``.

The backend persists phases in the ``care_phase`` table; this module
exposes the REST surface in
``backend/src/bvphoenix/api/care_phases.py`` to LLM agents.

Cross-patient invariant
-----------------------
Every tool here takes ``patient_id`` as a required first input. Phase
ids and event ids that belong to a different patient surface as a 404
from the backend (composite FK + nested REST routes); we never silently
rewrite the response. There is no tool that takes two ``patient_id``s
and no tool that accepts a phase / event id without an enclosing
``patient_id``.

Storage isolation
-----------------
The backend never returns bucket names, S3 URLs or raw stack traces;
this layer is a pass-through, so the same guarantee holds at the MCP
boundary.

Scope mapping (declared in :mod:`bvmcp.scopes`):

* ``phases:read``    → ``get_care_timeline``, ``render_care_timeline_svg``,
  ``get_care_phase``, ``list_care_phase_material``,
  ``list_care_phase_revisions``
* ``phases:propose`` → ``propose_care_phases`` (LLM classifier; dry-run
  by default — does not mutate the persisted phase set)
* ``phases:write``   → ``apply_phase_proposal``, ``create_care_phase``,
  ``update_care_phase``, ``assign_event_to_phase``,
  ``unassign_event_from_phase``, ``reorder_care_phases``,
  ``restore_care_phase_revision``
"""

from __future__ import annotations

import json
from typing import Any

from mcp.types import TextContent, Tool

from bvmcp.tools.client import (
    api_delete,
    api_get,
    api_get_bytes,
    api_patch,
    api_post_with_headers,
    api_put,
)

_PHASE_KIND_ENUM = (
    "imaging",
    "surgery",
    "followup",
    "surveillance",
    "visit",
    "reassessment",
    "other",
)

_INCLUDE_ENUM = ("events", "material", "revisions")


TOOLS: list[Tool] = [
    # ------------------------------------------------------------------ #
    # READ
    # ------------------------------------------------------------------ #
    Tool(
        name="get_care_timeline",
        description=(
            "Return a patient's care timeline (clinical events grouped "
            "into semantic phases). ``format`` selects the rendering: "
            "``json`` returns the structured ``CareTimelineOut`` payload, "
            "``markdown`` collapses it to a phase-by-phase outline with "
            "``mcp://`` links to each event's natural target (study, "
            "report, document, consultation), ``svg`` returns the SVG "
            "rendered server-side (same byte-for-byte style as the "
            "reference timeline). ``lang`` controls localized strings "
            "(phase names, date format)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "patient_id": {
                    "type": "string",
                    "description": "UUID of the patient.",
                },
                "lang": {
                    "type": "string",
                    "enum": ["it", "en"],
                    "default": "it",
                },
                "format": {
                    "type": "string",
                    "enum": ["json", "markdown", "svg"],
                    "default": "json",
                },
            },
            "required": ["patient_id"],
        },
    ),
    Tool(
        name="render_care_timeline_svg",
        description=(
            "Render the care timeline as SVG. Convenience alias for "
            "``get_care_timeline(format='svg')`` with extra layout "
            "knobs (``theme`` ``light|dark``, ``width`` in pixels). "
            "The SVG body is returned as text; the agent can save it "
            "to a file or embed it inline."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "patient_id": {
                    "type": "string",
                    "description": "UUID of the patient.",
                },
                "lang": {
                    "type": "string",
                    "enum": ["it", "en"],
                    "default": "it",
                },
                "theme": {
                    "type": "string",
                    "enum": ["light", "dark"],
                    "default": "light",
                },
                "width": {
                    "type": "integer",
                    "minimum": 320,
                    "maximum": 4096,
                },
            },
            "required": ["patient_id"],
        },
    ),
    Tool(
        name="get_care_phase",
        description=(
            "Read one care phase by id, scoped to its owning patient. "
            "``include`` selects which optional sub-payloads to hydrate: "
            "``events`` (the assigned ClinicalEvent rows), ``material`` "
            "(grouped studies / documents / reports / consultations / "
            "annotations), ``revisions`` (audit history)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "patient_id": {
                    "type": "string",
                    "description": "UUID of the patient.",
                },
                "phase_id": {
                    "type": "string",
                    "description": "UUID of the care phase.",
                },
                "include": {
                    "type": "array",
                    "items": {"type": "string", "enum": list(_INCLUDE_ENUM)},
                    "description": ("Optional list of sub-payloads to hydrate."),
                },
            },
            "required": ["patient_id", "phase_id"],
        },
    ),
    Tool(
        name="list_care_phase_material",
        description=(
            "Return the material attached to a care phase, grouped by "
            "kind (studies, documents, reports, consultations, "
            "annotations). The phase must belong to ``patient_id``: "
            "the backend returns 404 for cross-patient lookups."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "patient_id": {"type": "string"},
                "phase_id": {"type": "string"},
            },
            "required": ["patient_id", "phase_id"],
        },
    ),
    Tool(
        name="list_care_phase_revisions",
        description=(
            "Return the revision history of a care phase (every "
            "create / update / assign / unassign / restore event). "
            "Each entry carries ``revision_no``, ``change_kind``, "
            "``author_kind`` and a JSON snapshot of the phase state, "
            "suitable for inspection or rollback via "
            "``restore_care_phase_revision``."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "patient_id": {"type": "string"},
                "phase_id": {"type": "string"},
            },
            "required": ["patient_id", "phase_id"],
        },
    ),
    # ------------------------------------------------------------------ #
    # WRITE — propose / apply (server-side LLM classifier)
    # ------------------------------------------------------------------ #
    # ``propose_care_phases`` and ``apply_phase_proposal`` are gated
    # by the ``llm_classifier`` feature flag (see
    # ``bvmcp/feature_flags.py``). When the backend has no Anthropic
    # API key configured, these tools are filtered out of
    # ``list_tools()`` dynamically so a BYO-mode agent does not see
    # them at all (it would 502 against the server). Kept in the
    # TOOLS list so the moment the operator provisions the key, a
    # MCP pod restart re-probes the backend and the tools light up.
    # ------------------------------------------------------------------ #
    Tool(
        name="propose_care_phases",
        description=(
            "Kick the LLM classifier to suggest a phase partition for "
            "the patient's clinical events. Returns a "
            "``care_phase_proposal`` row id plus the proposed phases / "
            "assignments. ``dry_run`` defaults to true: this tool only "
            "*proposes* the partition — actually persisting it requires "
            "a follow-up call to ``apply_phase_proposal`` (which carries "
            "the ``phases:write`` scope).\n"
            "\n"
            "Hidden from the toolkit when the backend has no LLM "
            "provider configured (BYO mode): in that case classify in "
            "your own LLM and use ``create_care_phase`` + "
            "``assign_event_to_phase`` directly."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "patient_id": {"type": "string"},
                "lang": {
                    "type": "string",
                    "enum": ["it", "en"],
                    "default": "it",
                },
                "dry_run": {"type": "boolean", "default": True},
            },
            "required": ["patient_id"],
        },
    ),
    Tool(
        name="apply_phase_proposal",
        description=(
            "Atomically persist a previously-returned proposal. The "
            "agent chooses which phases (``accept_phases``) and which "
            "event-to-phase assignments (``accept_assignments``) to "
            "accept — both lists carry the proposal-local ids. "
            "``idempotency_key`` is mandatory: re-running the same "
            "request with the same key replays the already-committed "
            "result instead of creating duplicates.\n"
            "\n"
            "Hidden from the toolkit when the backend has no LLM "
            "provider configured (BYO mode); without ``propose_care_"
            "phases`` there is no proposal id to apply."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "patient_id": {"type": "string"},
                "proposal_id": {"type": "string"},
                "accept_phases": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Slugs (or ids) of proposal-local phases to "
                        "persist. Empty array = persist nothing."
                    ),
                },
                "accept_assignments": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": ("Ids of proposal-local event assignments to persist."),
                },
                "idempotency_key": {
                    "type": "string",
                    "minLength": 1,
                    "description": (
                        "Client-generated key. Re-using the same key "
                        "returns the prior result without re-applying."
                    ),
                },
            },
            "required": [
                "patient_id",
                "proposal_id",
                "accept_phases",
                "accept_assignments",
                "idempotency_key",
            ],
        },
    ),
    Tool(
        name="create_care_phase",
        description=(
            "Create a *care phase* manually (no classifier). A care "
            "phase is a semantic grouping of clinical events on the "
            "timeline (e.g. 'Diagnosi', 'Chirurgia', 'Follow-up'). It "
            "is NOT a folder (Drive-style document container, see "
            "``create_folder``) and NOT an event (a single timeline "
            "item, see ``create_clinical_event``): a phase is the "
            "wrapper around one or more events. The phase is owned by "
            "``patient_id`` (the composite FK makes cross-patient "
            "assignment unrepresentable). ``slug`` must be unique "
            "within the patient's phase set; ``name_i18n`` MUST carry "
            "both ``it`` and ``en`` keys."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "patient_id": {"type": "string"},
                "slug": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 64,
                },
                "name_i18n": {
                    "type": "object",
                    "properties": {
                        "it": {"type": "string"},
                        "en": {"type": "string"},
                    },
                    "required": ["it", "en"],
                    "additionalProperties": {"type": "string"},
                },
                "kind": {
                    "type": "string",
                    "enum": list(_PHASE_KIND_ENUM),
                },
                "color_hex": {
                    "type": "string",
                    "pattern": "^#[0-9A-Fa-f]{6}$",
                },
                "ordinal": {"type": "integer", "minimum": 0},
                "narrative_md": {"type": "string"},
            },
            "required": ["patient_id", "slug", "name_i18n", "kind"],
        },
    ),
    Tool(
        name="update_care_phase",
        description=(
            "Patch a care phase. ``etag`` is sent as the ``If-Match`` "
            "header — a 412 from the backend means another writer "
            "(human or agent) committed in between; the caller should "
            "re-fetch the phase, merge, and retry. ``patch`` carries "
            "only the fields to change (``exclude_unset`` semantics)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "patient_id": {"type": "string"},
                "phase_id": {"type": "string"},
                "etag": {
                    "type": "string",
                    "description": (
                        "Strong ETag returned by the previous GET / "
                        "PATCH. Sent as the ``If-Match`` header."
                    ),
                },
                "patch": {
                    "type": "object",
                    "description": (
                        "Subset of ``CarePhaseUpdateIn`` fields. Allowed "
                        "keys: ``name``, ``name_i18n``, ``kind``, "
                        "``color_hex``, ``ordinal``, ``narrative_md``, "
                        "``start_date``, ``end_date``."
                    ),
                    "additionalProperties": True,
                },
            },
            "required": ["patient_id", "phase_id", "etag", "patch"],
        },
    ),
    Tool(
        name="assign_event_to_phase",
        description=(
            "Bind a clinical event to a care phase. The phase is "
            "addressed by ``phase_slug`` (human-friendly); this tool "
            "resolves the slug to the phase id by listing the patient's "
            "phases first, then issues the ``PUT "
            "/care-phases/{phase_id}/events/{event_id}``. "
            "``confidence`` (0..1) is optional and recorded as the "
            "agent's certainty when ``author_kind='agent'``."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "patient_id": {"type": "string"},
                "phase_slug": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 64,
                },
                "event_id": {"type": "string"},
                "confidence": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 1,
                },
            },
            "required": ["patient_id", "phase_slug", "event_id"],
        },
    ),
    Tool(
        name="unassign_event_from_phase",
        description=(
            "Detach an event from whatever phase currently owns it. "
            "The tool reads the event to find its ``phase_id`` and "
            "issues the ``DELETE "
            "/care-phases/{phase_id}/events/{event_id}``. The event "
            "itself is untouched; only the assignment row is removed "
            "(equivalent to ``phase_id := NULL``)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "patient_id": {"type": "string"},
                "event_id": {"type": "string"},
            },
            "required": ["patient_id", "event_id"],
        },
    ),
    Tool(
        name="reorder_care_phases",
        description=(
            "Re-order the patient's phases in one batch. ``ordinals`` "
            "is a list of ``{phase_id, ordinal}`` objects; the backend "
            "validates that every phase belongs to ``patient_id`` and "
            "applies the new ordinals atomically."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "patient_id": {"type": "string"},
                "ordinals": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "phase_id": {"type": "string"},
                            "ordinal": {"type": "integer", "minimum": 0},
                        },
                        "required": ["phase_id", "ordinal"],
                    },
                    "minItems": 1,
                },
            },
            "required": ["patient_id", "ordinals"],
        },
    ),
    Tool(
        name="list_care_phases",
        description=(
            "List the patient's care phases with counts (n_events, "
            "n_studies, n_documents, n_reports, n_consultations) but "
            "without the per-phase event detail. Cheaper than "
            "``get_care_timeline`` when the agent only needs the chip "
            "list (e.g. to populate a dropdown or to pick a phase to "
            "edit)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "patient_id": {"type": "string"},
            },
            "required": ["patient_id"],
        },
    ),
    Tool(
        name="get_care_timeline_health",
        description=(
            "Snapshot of the timeline's health for the patient: number "
            "of phases, number of events, percentage of events assigned "
            "to a phase, count of pending classifier proposals, "
            "timestamp of the last classifier run. Drives the salute "
            "panel in the GUI; use it to decide whether to invoke "
            "``propose_care_phases`` again."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "patient_id": {"type": "string"},
            },
            "required": ["patient_id"],
        },
    ),
    Tool(
        name="delete_care_phase",
        description=(
            "Hard-delete a care phase. The composite FK on "
            "``clinical_events.phase_id`` is ON DELETE SET NULL, so "
            "events previously assigned to this phase become orphans "
            "(``phase_id = NULL``) instead of being deleted themselves."
            " A revision row of kind ``delete`` is appended for audit."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "patient_id": {"type": "string"},
                "phase_id": {"type": "string"},
            },
            "required": ["patient_id", "phase_id"],
        },
    ),
    Tool(
        name="export_care_timeline_ics",
        description=(
            "Export the patient's care timeline as an iCalendar "
            "(RFC 5545) document. One VEVENT per clinical event, "
            "categorised by phase slug, with a deterministic UID so "
            "re-imports update existing entries instead of duplicating "
            "them. Returned as ``TextContent`` with ``_meta.mimeType = "
            "'text/calendar; charset=utf-8'``."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "patient_id": {"type": "string"},
                "lang": {
                    "type": "string",
                    "enum": ["it", "en"],
                    "default": "it",
                },
            },
            "required": ["patient_id"],
        },
    ),
    Tool(
        name="export_care_timeline_pdf",
        description=(
            "Export the patient's care timeline as a PDF document. "
            "Currently returns a structured 501 (not implemented yet) "
            "because PDF rendering depends on the ``weasyprint`` "
            "dependency which is not yet bundled. The tool is exposed "
            "so the GUI button and any agent that wants to drive the "
            "export have a stable name to call; once weasyprint lands "
            "the implementation flips without a tool-name change."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "patient_id": {"type": "string"},
                "lang": {
                    "type": "string",
                    "enum": ["it", "en"],
                    "default": "it",
                },
            },
            "required": ["patient_id"],
        },
    ),
    Tool(
        name="get_my_scopes",
        description=(
            "Return the OAuth scopes the calling token holds, plus the "
            "subject id of the actor and (when present) the agent token "
            "id. The GUI uses this to gate buttons (e.g. hide "
            "'Modifica fase' if the token lacks ``phases:write``); MCP "
            "agents use it to decide whether to attempt a write or to "
            "downgrade gracefully."
        ),
        inputSchema={
            "type": "object",
            "properties": {},
        },
    ),
    Tool(
        name="restore_care_phase_revision",
        description=(
            "Restore a prior revision of a care phase (rolls the "
            "phase state and its event assignments back to the "
            "snapshot stored in ``care_phase_revision``). A new "
            "revision row is appended (``change_kind='restore'``) so "
            "the audit trail stays append-only."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "patient_id": {"type": "string"},
                "phase_id": {"type": "string"},
                "revision_no": {"type": "integer", "minimum": 1},
            },
            "required": ["patient_id", "phase_id", "revision_no"],
        },
    ),
]


# --------------------------------------------------------------------------- #
# Markdown rendering — server returns JSON, we collapse to a phase-by-phase
# outline with mcp:// links to the natural target of each event.
# --------------------------------------------------------------------------- #


_TARGET_KIND_TO_MCP = {
    "study": "study",
    "imaging_study": "study",
    "report": "report",
    "report_content": "report",
    "document": "document",
    "consultation": "consultation",
    "clinical_event": "event",
}


def _event_target_uri(event: dict[str, Any]) -> tuple[str, str]:
    """Return ``(label, mcp_uri)`` for a TimelineEvent.

    The backend's ``TimelineEventOut`` carries a resolved ``target``
    (discriminated union {kind, id, ...}). We pick the ``kind`` and
    construct an ``mcp://<kind>/<id>`` URI; falls back to the event
    itself when no target is resolved.
    """
    target = event.get("target") or {}
    kind = target.get("kind") or "clinical_event"
    tid = target.get("id") or event.get("id") or ""
    label = _TARGET_KIND_TO_MCP.get(kind, "event")
    return label, f"mcp://{label}/{tid}"


def _format_event_line(event: dict[str, Any]) -> str:
    date = (
        event.get("event_date")
        or event.get("date")
        or (event.get("target") or {}).get("date")
        or ""
    )
    title = (
        event.get("title")
        or (event.get("target") or {}).get("title")
        or event.get("kind")
        or "(untitled)"
    )
    label, uri = _event_target_uri(event)
    return f"- {date} — {title} ([{label}]({uri}))"


def _render_timeline_markdown(payload: dict[str, Any], lang: str) -> str:
    phases = payload.get("phases") or []
    events = payload.get("events") or []

    # Group events by phase_id (phases not in the phase list end up under
    # an "unassigned" bucket so the user sees them).
    by_phase: dict[str | None, list[dict[str, Any]]] = {}
    for ev in events:
        pid = ev.get("phase_id")
        by_phase.setdefault(pid, []).append(ev)

    lines: list[str] = []
    for phase in phases:
        name_i18n = phase.get("name_i18n") or {}
        name = name_i18n.get(lang) or phase.get("name") or phase.get("slug") or "(unnamed phase)"
        lines.append(f"## {name}")
        for ev in by_phase.get(phase.get("id"), []):
            lines.append(_format_event_line(ev))
        lines.append("")  # blank line between phases

    if None in by_phase:
        unassigned_label = "Non assegnati" if lang == "it" else "Unassigned"
        lines.append(f"## {unassigned_label}")
        for ev in by_phase[None]:
            lines.append(_format_event_line(ev))
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


# --------------------------------------------------------------------------- #
# Per-tool handlers
# --------------------------------------------------------------------------- #


def _timeline_path(patient_id: str) -> str:
    return f"/api/patients/{patient_id}/care-timeline"


def _phase_path(patient_id: str, phase_id: str) -> str:
    return f"/api/patients/{patient_id}/care-phases/{phase_id}"


async def _get_care_timeline(args: dict[str, Any]) -> str | list[TextContent]:
    patient_id = args["patient_id"]
    lang = args.get("lang", "it")
    fmt = args.get("format", "json")

    if fmt == "svg":
        body, _ctype = await api_get_bytes(
            _timeline_path(patient_id),
            params={"lang": lang, "format": "svg"},
        )
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            text = body.decode("utf-8", errors="replace")
        # TextContent has no native ``mimeType`` field, but the MCP type
        # is open (``additionalProperties: True``) — we tag the content
        # via ``_meta`` so a curious client can identify it as SVG.
        return [
            TextContent(
                type="text",
                text=text,
                _meta={"mimeType": "image/svg+xml"},
            )
        ]

    payload = await api_get(
        _timeline_path(patient_id),
        params={"lang": lang, "format": "json"},
    )
    if fmt == "markdown":
        if not isinstance(payload, dict):
            # The backend always returns an object for format=json; defend
            # against an unexpected list shape rather than crashing.
            return json.dumps(payload, indent=2, ensure_ascii=False)
        return _render_timeline_markdown(payload, lang)

    return json.dumps(payload, indent=2, ensure_ascii=False)


async def _render_care_timeline_svg(args: dict[str, Any]) -> list[TextContent]:
    patient_id = args["patient_id"]
    params: dict[str, Any] = {
        "lang": args.get("lang", "it"),
        "format": "svg",
        "theme": args.get("theme", "light"),
    }
    if (width := args.get("width")) is not None:
        params["width"] = width
    body, _ctype = await api_get_bytes(_timeline_path(patient_id), params=params)
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        text = body.decode("utf-8", errors="replace")
    return [
        TextContent(
            type="text",
            text=text,
            _meta={"mimeType": "image/svg+xml"},
        )
    ]


async def _get_care_phase(args: dict[str, Any]) -> str:
    params: dict[str, Any] = {}
    include = args.get("include")
    if include:
        # FastAPI accepts repeated keys for list query params.
        params["include"] = list(include)
    payload = await api_get(
        _phase_path(args["patient_id"], args["phase_id"]),
        params=params or None,
    )
    return json.dumps(payload, indent=2, ensure_ascii=False)


async def _list_care_phase_material(args: dict[str, Any]) -> str:
    payload = await api_get(f"{_phase_path(args['patient_id'], args['phase_id'])}/material")
    return json.dumps(payload, indent=2, ensure_ascii=False)


async def _list_care_phase_revisions(args: dict[str, Any]) -> str:
    payload = await api_get(f"{_phase_path(args['patient_id'], args['phase_id'])}/revisions")
    return json.dumps(payload, indent=2, ensure_ascii=False)


async def _propose_care_phases(args: dict[str, Any]) -> str:
    patient_id = args["patient_id"]
    body = {
        "lang": args.get("lang", "it"),
        "dry_run": args.get("dry_run", True),
    }
    payload, _hdrs = await api_post_with_headers(
        f"/api/patients/{patient_id}/care-phases:propose",
        json=body,
    )
    return json.dumps(payload, indent=2, ensure_ascii=False)


async def _apply_phase_proposal(args: dict[str, Any]) -> str:
    patient_id = args["patient_id"]
    body = {
        "proposal_id": args["proposal_id"],
        "accept_phases": args["accept_phases"],
        "accept_assignments": args["accept_assignments"],
    }
    payload, _hdrs = await api_post_with_headers(
        f"/api/patients/{patient_id}/care-phases:apply-proposal",
        json=body,
        idempotency_key=args["idempotency_key"],
    )
    return json.dumps(payload, indent=2, ensure_ascii=False)


async def _create_care_phase(args: dict[str, Any]) -> str:
    patient_id = args["patient_id"]
    name_i18n = args["name_i18n"]
    body: dict[str, Any] = {
        "slug": args["slug"],
        "name_i18n": name_i18n,
        "kind": args["kind"],
        # Backend accepts ``name`` as optional (it derives from
        # ``name_i18n`` when omitted) but sending it explicitly keeps
        # the audit row deterministic and saves a server round of
        # locale-preference logic. Preference order matches the
        # service derivation: it → en → first available → slug.
        "name": (
            name_i18n.get("it")
            or name_i18n.get("en")
            or next(iter(name_i18n.values()), None)
            or args["slug"]
        ),
    }
    for k in ("color_hex", "ordinal", "narrative_md"):
        if k in args and args[k] is not None:
            body[k] = args[k]
    payload, _hdrs = await api_post_with_headers(
        f"/api/patients/{patient_id}/care-phases",
        json=body,
    )
    return json.dumps(payload, indent=2, ensure_ascii=False)


async def _update_care_phase(args: dict[str, Any]) -> str:
    patient_id = args["patient_id"]
    phase_id = args["phase_id"]
    payload, _hdrs = await api_patch(
        _phase_path(patient_id, phase_id),
        json=args["patch"],
        if_match=args["etag"],
    )
    return json.dumps(payload, indent=2, ensure_ascii=False)


async def _resolve_phase_id_by_slug(patient_id: str, slug: str) -> str:
    """Look up a phase id by slug, scoped to a patient.

    We list the patient's phases and pick the one matching ``slug``.
    Cross-patient is impossible: the LIST endpoint is nested under
    ``/api/patients/{patient_id}``. Raises ``ValueError`` if the slug
    is unknown so the caller surfaces a clean MCP error instead of an
    HTTP 404 from the next call.
    """
    phases = await api_get(f"/api/patients/{patient_id}/care-phases")
    if not isinstance(phases, list):  # defensive
        raise ValueError(f"unexpected /care-phases response shape for patient {patient_id}")
    for p in phases:
        if isinstance(p, dict) and p.get("slug") == slug:
            pid = p.get("id")
            if not pid:
                raise ValueError(f"phase {slug!r} has no id in patient {patient_id}")
            return pid
    raise ValueError(f"no care phase with slug {slug!r} for patient {patient_id}")


async def _assign_event_to_phase(args: dict[str, Any]) -> str:
    patient_id = args["patient_id"]
    slug = args["phase_slug"]
    event_id = args["event_id"]
    phase_id = await _resolve_phase_id_by_slug(patient_id, slug)
    body: dict[str, Any] = {}
    if (conf := args.get("confidence")) is not None:
        body["confidence"] = conf
    payload, _hdrs = await api_put(
        f"/api/patients/{patient_id}/care-phases/{phase_id}/events/{event_id}",
        json=body or None,
    )
    if not payload:
        payload = {"status": "assigned"}
    return json.dumps(payload, indent=2, ensure_ascii=False)


async def _unassign_event_from_phase(args: dict[str, Any]) -> str:
    patient_id = args["patient_id"]
    event_id = args["event_id"]
    # We need the phase_id to construct the DELETE path. The event
    # carries its current ``phase_id`` field; if it's NULL there is
    # nothing to detach.
    event = await api_get(f"/api/clinical-events/{event_id}")
    if not isinstance(event, dict):  # defensive
        raise ValueError(f"unexpected /clinical-events/{event_id} response shape")
    # Surface the cross-patient guarantee as a hard check: the event
    # must belong to ``patient_id``. The backend already rejects via
    # nested-route 404s; we also short-circuit here so the agent never
    # detaches an event from another patient by accident.
    ev_patient = event.get("patient_id")
    if ev_patient and ev_patient != patient_id:
        raise ValueError(f"event {event_id} does not belong to patient {patient_id}")
    phase_id = event.get("phase_id")
    if not phase_id:
        return json.dumps(
            {"status": "noop", "reason": "event has no phase assignment"},
            ensure_ascii=False,
        )
    code = await api_delete(f"/api/patients/{patient_id}/care-phases/{phase_id}/events/{event_id}")
    return json.dumps({"status": "unassigned", "http_status": code})


async def _reorder_care_phases(args: dict[str, Any]) -> str:
    patient_id = args["patient_id"]
    payload, _hdrs = await api_post_with_headers(
        f"/api/patients/{patient_id}/care-phases:reorder",
        json={"ordinals": args["ordinals"]},
    )
    return json.dumps(payload, indent=2, ensure_ascii=False)


async def _restore_care_phase_revision(args: dict[str, Any]) -> str:
    patient_id = args["patient_id"]
    phase_id = args["phase_id"]
    payload, _hdrs = await api_post_with_headers(
        f"{_phase_path(patient_id, phase_id)}/restore",
        json={"revision_no": args["revision_no"]},
    )
    return json.dumps(payload, indent=2, ensure_ascii=False)


async def _list_care_phases(args: dict[str, Any]) -> str:
    patient_id = args["patient_id"]
    payload = await api_get(f"/api/patients/{patient_id}/care-phases")
    return json.dumps(payload, indent=2, ensure_ascii=False)


async def _get_care_timeline_health(args: dict[str, Any]) -> str:
    patient_id = args["patient_id"]
    payload = await api_get(f"/api/patients/{patient_id}/care-timeline/health")
    return json.dumps(payload, indent=2, ensure_ascii=False)


async def _delete_care_phase(args: dict[str, Any]) -> str:
    patient_id = args["patient_id"]
    phase_id = args["phase_id"]
    code = await api_delete(f"/api/patients/{patient_id}/care-phases/{phase_id}")
    return json.dumps({"status": "deleted", "http_status": code})


async def _export_care_timeline_ics(args: dict[str, Any]) -> list[TextContent]:
    patient_id = args["patient_id"]
    body, _ctype = await api_get_bytes(
        _timeline_path(patient_id),
        params={"lang": args.get("lang", "it"), "format": "ics"},
    )
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        text = body.decode("utf-8", errors="replace")
    return [
        TextContent(
            type="text",
            text=text,
            _meta={"mimeType": "text/calendar; charset=utf-8"},
        )
    ]


async def _export_care_timeline_pdf(args: dict[str, Any]) -> str:
    """Tool stub: backend currently returns 501.

    Surfaced as MCP tool now so the GUI button and any agent share the
    same name; once weasyprint is bundled the implementation flips to
    return ``TextContent(_meta.mimeType='application/pdf')`` without a
    rename.
    """
    patient_id = args["patient_id"]
    try:
        body, _ctype = await api_get_bytes(
            _timeline_path(patient_id),
            params={"lang": args.get("lang", "it"), "format": "pdf"},
        )
    except Exception as exc:
        return json.dumps(
            {
                "status": "not_implemented",
                "reason": str(exc),
                "hint": (
                    "PDF rendering requires the weasyprint dependency. "
                    "Tracked in docs/care-timeline-phases.md."
                ),
            },
            ensure_ascii=False,
        )
    # If backend ever returns 200, we still surface the bytes as base64
    # via TextContent. The _DISPATCH return type permits list[TextContent].
    import base64

    return json.dumps(
        {
            "status": "ok",
            "pdf_base64": base64.b64encode(body).decode("ascii"),
        },
        ensure_ascii=False,
    )


async def _get_my_scopes(_args: dict[str, Any]) -> str:
    payload = await api_get("/api/me/scopes")
    return json.dumps(payload, indent=2, ensure_ascii=False)


_DISPATCH = {
    "get_care_timeline": _get_care_timeline,
    "render_care_timeline_svg": _render_care_timeline_svg,
    "get_care_phase": _get_care_phase,
    "list_care_phase_material": _list_care_phase_material,
    "list_care_phase_revisions": _list_care_phase_revisions,
    "propose_care_phases": _propose_care_phases,
    "apply_phase_proposal": _apply_phase_proposal,
    "create_care_phase": _create_care_phase,
    "update_care_phase": _update_care_phase,
    "assign_event_to_phase": _assign_event_to_phase,
    "unassign_event_from_phase": _unassign_event_from_phase,
    "reorder_care_phases": _reorder_care_phases,
    "restore_care_phase_revision": _restore_care_phase_revision,
    # Gap-closing additions so the MCP layer is a strict superset of the GUI.
    "list_care_phases": _list_care_phases,
    "get_care_timeline_health": _get_care_timeline_health,
    "delete_care_phase": _delete_care_phase,
    "export_care_timeline_ics": _export_care_timeline_ics,
    "export_care_timeline_pdf": _export_care_timeline_pdf,
    "get_my_scopes": _get_my_scopes,
}


async def handle(name: str, arguments: dict[str, Any]) -> str | list[TextContent]:
    handler = _DISPATCH.get(name)
    if handler is None:
        return f"Error: unknown tool '{name}' in care_phases module"
    return await handler(arguments)
