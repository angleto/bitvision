"""Discovery + Navigation + Write tools — clinical events.

ClinicalEvent is the **temporal axis at the atomic level**: a single
event in the patient's timeline. Twelve kinds are defined: imaging
study, surgical procedure, outpatient visit, inpatient admission, lab
batch, consultation event, pathology review, MDT meeting, cardio
diagnostic, endoscopy, radiology appointment, other. Events are not
folders (organisational
axis, see ``folders``), not care phases (temporal axis at the
grouping level, see ``care_phases``), and not tags (cross-cutting
axis, see ``metadata_writes``). Each axis lives on its own table.

Canonical relation: ``CarePhase ⊃ ClinicalEvent``; ``Folder`` and
``Document`` are orthogonal to both; ``Tag`` labels imaging targets
and is orthogonal to all three. Conceptual placement: see
``docs/data-model.md §0``.

Tools delegate to the ``/api/clinical-events/...`` REST surface (see
``backend/src/bvphoenix/api/clinical_events.py``). Writes are scoped
to ``events:write`` and follow the project-wide write conventions:
``If-Match`` on update / delete, response carries the new ETag,
imaging events are owned by the ingestion pipeline (creation here is
refused for ``kind='imaging_study'``).

Clinical *time* is corrected in one place: ``amend_event_time``
(``POST .../amend-time``) is the only way to fix a recorded time
without moving ``event_status``, and the only way to re-date a row
that is already terminal. Recording a completion or a move still
writes timestamps as part of its own transition
(``complete_event`` takes ``actual_start_at`` / ``actual_end_at``,
``reschedule_event`` takes ``new_planned_start_at`` /
``new_planned_end_at``). ``update_clinical_event`` carries metadata
only, because ``event_date`` is derived by a DB trigger from the
status anchor: a PATCH writing it would be silently reverted on the
next trigger firing, so it is refused with 422 ``use_amend_time``
instead.
"""

from __future__ import annotations

import json
from typing import Any

from mcp.types import Tool

from bvmcp.tools.client import (
    api_delete,
    api_get,
    api_patch,
    api_post,
    api_post_with_headers,
)

TOOLS = [
    Tool(
        name="find_clinical_events",
        description=(
            "List a patient's clinical events (the v3 umbrella for "
            "every event in the timeline: imaging studies, surgical "
            "procedures, outpatient visits, admissions, lab batches, "
            "consultation events, pathology reviews, MDT meetings, "
            "cardio diagnostics, endoscopies, radiology appointments "
            "and 'other'). Returns "
            "events newest first by event_date. Filter by ``kind`` to "
            "scope to a single event class, and by ``statuses`` (multi) "
            "to slice the timeline on lifecycle: ``planned`` and "
            "``confirmed`` for future appointments, ``completed`` for "
            "events that already happened (the historical default), "
            "``cancelled`` / ``missed`` / ``rescheduled`` for the "
            "no-show and reschedule branches. Each row carries "
            "``event_status``, ``planned_start_at``, ``actual_start_at`` "
            "and ``timezone`` so the agent can render the lifecycle "
            "without an extra read. For ``kind='imaging_study'`` the "
            "row also surfaces ``imaging_study_id`` — pass that to "
            "``get_study`` for the DICOM-specific detail."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "patient_id": {
                    "type": "string",
                    "description": "UUID of the patient",
                },
                "kind": {
                    "type": "string",
                    "enum": [
                        "imaging_study",
                        "surgical_procedure",
                        "outpatient_visit",
                        "inpatient_admission",
                        "lab_batch",
                        "consultation_event",
                        "pathology_review",
                        "mdt_meeting",
                        "cardio_diagnostic",
                        "endoscopy",
                        "radiology_appointment",
                        "other",
                    ],
                    "description": ("Optional filter by event kind. Omit to see all kinds."),
                },
                "statuses": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": [
                            "planned",
                            "confirmed",
                            "completed",
                            "cancelled",
                            "missed",
                            "rescheduled",
                        ],
                    },
                    "description": (
                        "Optional filter by event_status (multi-select). "
                        "Use ``['planned','confirmed']`` for upcoming "
                        "events, ``['completed']`` for the historical "
                        "timeline, ``['cancelled','missed']`` for the "
                        "no-show branch. Omit to include all statuses."
                    ),
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 100},
                "offset": {"type": "integer", "minimum": 0, "default": 0},
            },
            "required": ["patient_id"],
        },
    ),
    Tool(
        name="get_event",
        description=(
            "Read one clinical event by id. Returns the umbrella metadata "
            "(kind, event_date, title, body_part, narrative, optional "
            "LOINC / SNOMED codes) plus, for ``kind == 'imaging_study'``, "
            "the id of the imaging_studies child row. For follow-up "
            "queries: use ``find_reports(event_id=...)`` to list the "
            "narratives written about this event, and ``get_study`` "
            "with the imaging_study_id for DICOM details."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "event_id": {"type": "string", "description": "UUID of the clinical event"},
            },
            "required": ["event_id"],
        },
    ),
    Tool(
        name="propose_event_link",
        description=(
            "Suggest a ranked list of ClinicalEvent candidates that an "
            "uploaded Document might belong to. Use when the agent has "
            "an unlinked document (a freshly OCR'd PDF, a scan from a "
            "DVD, etc.) and needs to attach it to the right event in "
            "the patient's timeline. The score is a hint, not a binding "
            "judgment — the agent still picks one and confirms via "
            "``confirm_event_link``. Cross-patient candidates are "
            "never returned: the lookup is bounded to the document's "
            "owning patient."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "document_id": {"type": "string", "description": "UUID of the document"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
            },
            "required": ["document_id"],
        },
    ),
    Tool(
        name="create_clinical_event",
        description=(
            "Create a non-imaging ClinicalEvent on a patient's timeline. "
            "Two scenarios:\n\n"
            "1. **Historical event** (default): set ``event_status='completed'`` "
            "(default) and supply ``actual_start_at`` for an event that already "
            "happened, or just ``event_date`` when only the day is known "
            "(a date-only row, re-datable later via ``amend_event_time``).\n\n"
            "2. **Planned appointment** (calendar): set "
            "``event_status='planned'`` and supply ``planned_start_at`` "
            "(ISO-8601 datetime). Optionally set ``timezone`` (IANA name "
            "like 'Europe/Rome'), ``location_struct``, ``planned_end_at``, "
            "and ``reminder_offsets_minutes``. The event appears on the "
            "calendar feed and the CareTimeline with a 'planned' badge "
            "and is movable via ``reschedule_event``, ``confirm_event``, "
            "``complete_event``, ``cancel_event``, ``mark_event_missed``.\n\n"
            "Imaging events (``kind='imaging_study'``) are owned by the "
            "DICOM ingestion pipeline and refused with 422 here. "
            "``idempotency_key`` is mandatory: same key + same body returns "
            "the previously-created event without duplicating."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "patient_id": {
                    "type": "string",
                    "description": "UUID of the patient owning the event.",
                },
                "kind": {
                    "type": "string",
                    "enum": [
                        "surgical_procedure",
                        "outpatient_visit",
                        "inpatient_admission",
                        "lab_batch",
                        "consultation_event",
                        "pathology_review",
                        "mdt_meeting",
                        "cardio_diagnostic",
                        "endoscopy",
                        "radiology_appointment",
                        "other",
                    ],
                    "description": (
                        "Event kind. ``imaging_study`` is intentionally "
                        "absent: those events are materialised by the "
                        "ingestion pipeline."
                    ),
                },
                "title": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 255,
                    "description": (
                        "Short human-readable label, shown on the chip "
                        "in the timeline (e.g. 'Visita oncologica', "
                        "'Ricovero Ospedale X', 'Intervento di "
                        "linfadenectomia')."
                    ),
                },
                "event_date": {
                    "type": "string",
                    "format": "date",
                    "description": (
                        "ISO-8601 date (YYYY-MM-DD). Optional. For "
                        "planned events leave this empty and use "
                        "``planned_start_at`` instead — the server "
                        "trigger derives ``event_date`` from the "
                        "timestamp + timezone. Sending it next to an "
                        "anchor that implies a different date is refused "
                        "with 422 ``event_date_conflicts_with_anchor``."
                    ),
                },
                "body_part": {
                    "type": "string",
                    "maxLength": 64,
                    "description": (
                        "Free-text body region (e.g. 'breast left'). "
                        "Optional; used for hints in classifiers."
                    ),
                },
                "code_loinc": {
                    "type": "string",
                    "maxLength": 32,
                    "description": "LOINC code if applicable.",
                },
                "code_snomed": {
                    "type": "string",
                    "maxLength": 32,
                    "description": "SNOMED code if applicable.",
                },
                "narrative": {
                    "type": "string",
                    "description": (
                        "Free-text narrative (e.g. operative note "
                        "summary, visit summary). Optional."
                    ),
                },
                "event_status": {
                    "type": "string",
                    "enum": [
                        "planned",
                        "confirmed",
                        "completed",
                        "cancelled",
                        "missed",
                    ],
                    "default": "completed",
                    "description": (
                        "Lifecycle stage at creation. ``planned`` for a "
                        "future appointment that still needs confirmation; "
                        "``confirmed`` if it's already locked in; "
                        "``completed`` for historical events that already "
                        "happened (default). Requires ``planned_start_at`` "
                        "for planned/confirmed."
                    ),
                },
                "planned_start_at": {
                    "type": "string",
                    "format": "date-time",
                    "description": (
                        "ISO-8601 datetime with offset (e.g. "
                        "'2026-05-25T10:00:00+02:00'). Required when "
                        "``event_status`` is planned or confirmed."
                    ),
                },
                "planned_end_at": {
                    "type": "string",
                    "format": "date-time",
                    "description": "Optional end timestamp for the planned slot.",
                },
                "actual_start_at": {
                    "type": "string",
                    "format": "date-time",
                    "description": (
                        "The realised start of an event that already "
                        "happened (visit at 14:30, surgery started "
                        "08:15). ``event_status='completed'`` plus "
                        "``actual_start_at`` is the one-call way to "
                        "record a past event. There is no time-of-day "
                        "fallback: supplying only ``event_date`` leaves "
                        "both anchors NULL, i.e. a date-only row, whose "
                        "date stays correctable later through "
                        "``amend_event_time``. An ``event_date`` that "
                        "disagrees with the anchor is refused with 422 "
                        "``event_date_conflicts_with_anchor``."
                    ),
                },
                "actual_end_at": {
                    "type": "string",
                    "format": "date-time",
                    "description": "Optional end timestamp for the actual event.",
                },
                "timezone": {
                    "type": "string",
                    "maxLength": 64,
                    "description": (
                        "IANA timezone (e.g. 'Europe/Rome'); anything "
                        "else is refused with 422 ``invalid_timezone``. "
                        "Used by the DB trigger to derive ``event_date`` "
                        "from timestamps. Defaults to 'UTC' if omitted."
                    ),
                },
                "location_struct": {
                    "type": "object",
                    "description": (
                        "Free-form location: ``{facility, room, city, "
                        "address, phone}``. All keys optional."
                    ),
                },
                "reminder_offsets_minutes": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": (
                        "Negative integers (minutes before the event) "
                        "for cross-channel reminders. Example: "
                        "[-1440, -120, -15] = 1 day, 2 hours, 15 minutes "
                        "before. Dispatching arrives in step 4."
                    ),
                },
                "meeting_url": {
                    "type": "string",
                    "maxLength": 512,
                    "description": (
                        "Click-to-join conference URL for telehealth "
                        "(Google Meet, Zoom, Jitsi). Surfaced as a "
                        "button on the event drawer."
                    ),
                },
                "links": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "label": {"type": "string"},
                            "url": {"type": "string"},
                        },
                        "required": ["url"],
                    },
                    "description": (
                        "Free-form references: booking portal URL, "
                        "structure website, referral letter online, "
                        "pre-results URL. Each entry: ``{label, url}``."
                    ),
                },
                "idempotency_key": {
                    "type": "string",
                    "minLength": 1,
                    "description": (
                        "Idempotency-Key header. Same key + same body "
                        "returns the previously-created event without "
                        "creating a duplicate row."
                    ),
                },
            },
            "required": ["patient_id", "kind", "title", "idempotency_key"],
        },
    ),
    Tool(
        name="update_clinical_event",
        description=(
            "Patch a ClinicalEvent's NON-temporal metadata. "
            "``patient_id`` is immutable (it defines the row's "
            "ownership). ``etag`` is sent as the ``If-Match`` header and "
            "MUST match the value returned by the previous read; a 412 "
            "means a concurrent writer committed in between, in which "
            "case the caller should re-read, merge, and retry. "
            "``patch`` carries only the fields to change.\n\n"
            "Temporal corrections do NOT belong here: ``event_date``, "
            "``planned_start_at``, ``planned_end_at``, "
            "``actual_start_at``, ``actual_end_at`` and ``timezone`` go "
            "through ``amend_event_time``, which validates the anchor "
            "family and records the correction in the audit chain. The "
            "``patch`` object below advertises the metadata fields only "
            "and is closed (``additionalProperties: false``), so those "
            "six have no slot here; sending one anyway is refused by "
            "the API with 422 ``{code: 'use_amend_time'}``."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "event_id": {"type": "string"},
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
                        "Subset of ``ClinicalEventUpdateIn`` fields. "
                        "Mirrors the backend's ``_UPDATABLE_FIELDS`` "
                        "exactly; temporal fields live on "
                        "``amend_event_time``."
                    ),
                    "properties": {
                        "kind": {
                            "type": "string",
                            "enum": [
                                "surgical_procedure",
                                "outpatient_visit",
                                "inpatient_admission",
                                "lab_batch",
                                "consultation_event",
                                "pathology_review",
                                "mdt_meeting",
                                "cardio_diagnostic",
                                "endoscopy",
                                "radiology_appointment",
                                "other",
                            ],
                            "description": (
                                "Reclassify the event. ``imaging_study`` "
                                "is absent on purpose: promoting a "
                                "non-imaging row into it is refused with "
                                "422, and a row that still has a live "
                                "imaging_studies projection cannot leave "
                                "that kind (409); delete the imaging "
                                "study through the DICOM path first."
                            ),
                        },
                        "title": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 255,
                        },
                        "body_part": {"type": "string", "maxLength": 64},
                        "code_loinc": {"type": "string", "maxLength": 32},
                        "code_snomed": {"type": "string", "maxLength": 32},
                        "narrative": {"type": "string"},
                        "location_struct": {"type": "object"},
                        "recurrence_rule": {"type": "string", "maxLength": 512},
                        "recurrence_exdates": {
                            "type": "array",
                            "items": {"type": "string", "format": "date"},
                        },
                        "reminder_offsets_minutes": {
                            "type": "array",
                            "items": {"type": "integer"},
                        },
                        "meeting_url": {"type": "string", "maxLength": 512},
                        "links": {"type": "array", "items": {"type": "object"}},
                    },
                    "additionalProperties": False,
                },
            },
            "required": ["event_id", "etag", "patch"],
        },
    ),
    Tool(
        name="delete_clinical_event",
        description=(
            "Delete a ClinicalEvent. Imaging events with a live "
            "imaging_studies row are refused with 409 (lifecycle owned "
            "by the DICOM deletion path; deleting the event would "
            "cascade-delete the imaging row). Orphan imaging events "
            "(``kind='imaging_study'`` with no surviving imaging row, "
            "typically left behind by a prior DICOM deletion) ARE "
            "deletable here. ``etag`` is sent as ``If-Match``."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "event_id": {"type": "string"},
                "etag": {
                    "type": "string",
                    "description": (
                        "Strong ETag from the most recent GET. Sent as "
                        "``If-Match`` to make the delete optimistic-"
                        "concurrency-safe."
                    ),
                },
            },
            "required": ["event_id", "etag"],
        },
    ),
    Tool(
        name="confirm_event_link",
        description=(
            "Bind a Document to a ClinicalEvent by extracting a "
            "ReportContent (Expression) for the event and creating a "
            "ContentDocumentLink with the chosen role. The agent runs "
            "this after picking a candidate from ``propose_event_link``. "
            "Two backend writes happen in sequence: "
            "``POST /api/report-contents`` (authority='derived', status="
            "'extracted_auto') followed by "
            "``POST /api/report-contents/{rc_id}/link-document`` "
            "(role='extracted_from'/'cites'/'mentions'). Both inherit "
            "the calling agent's token id for provenance."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "document_id": {"type": "string"},
                "event_id": {"type": "string"},
                "role": {
                    "type": "string",
                    "enum": ["extracted_from", "cites", "mentions"],
                    "default": "extracted_from",
                },
                "narrative_md": {
                    "type": "string",
                    "description": (
                        "Optional narrative the agent extracted from the "
                        "document. Empty string is allowed when the link "
                        "is metadata-only (no narrative extracted)."
                    ),
                },
                "title": {
                    "type": "string",
                    "maxLength": 255,
                    "description": "Title for the new ReportContent.",
                },
                "language": {"type": "string", "default": "it", "maxLength": 10},
                "parser_version": {"type": "string", "maxLength": 64},
                "model_id": {"type": "string", "maxLength": 128},
                "provider": {"type": "string", "maxLength": 64},
            },
            "required": ["document_id", "event_id"],
        },
    ),
    # ----- Lifecycle transition sub-resources -----------------------------
    # Each tool mirrors a POST /clinical-events/{id}/{verb} endpoint.
    # ``etag`` -> If-Match (412 on mismatch). ``idempotency_key`` ->
    # Idempotency-Key (replay returns the previous response). ``dry_run``
    # validates the FSM without persisting (handy when the agent needs
    # to preview a chain of operations before committing).
    Tool(
        name="confirm_event",
        description=(
            "Move a planned event to ``confirmed`` (the provider or "
            "patient has acknowledged the appointment). Allowed only "
            "when the current ``event_status`` is ``planned``. "
            "``etag`` must match the latest GET; on mismatch the call "
            "returns 412 and the agent should re-read + retry."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "event_id": {"type": "string"},
                "etag": {"type": "string"},
                "confirmed_at": {
                    "type": "string",
                    "format": "date-time",
                    "description": "Optional acknowledgement timestamp; defaults to server now.",
                },
                "idempotency_key": {"type": "string", "minLength": 1},
                "dry_run": {"type": "boolean", "default": False},
            },
            "required": ["event_id", "etag", "idempotency_key"],
        },
    ),
    Tool(
        name="reschedule_event",
        description=(
            "Move a planned/confirmed event to a new slot. The server "
            "creates a NEW event row with ``event_status='planned'`` "
            "pointing at the new timestamp, and flips the original row "
            "to ``rescheduled`` with ``parent_event_id`` linking them "
            "(same-patient enforced by composite DB FK). The response "
            "carries the NEW event id; the original id is in the "
            "``X-Replaced-Event-Id`` response header. ``reason`` is "
            "required so the audit chain is informative."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "event_id": {"type": "string"},
                "etag": {"type": "string"},
                "new_planned_start_at": {"type": "string", "format": "date-time"},
                "new_planned_end_at": {"type": "string", "format": "date-time"},
                "timezone": {"type": "string", "maxLength": 64},
                "reason": {"type": "string", "minLength": 1, "maxLength": 255},
                "idempotency_key": {"type": "string", "minLength": 1},
                "dry_run": {"type": "boolean", "default": False},
            },
            "required": [
                "event_id",
                "etag",
                "new_planned_start_at",
                "reason",
                "idempotency_key",
            ],
        },
    ),
    Tool(
        name="complete_event",
        description=(
            "Mark an event as ``completed`` and record the realised "
            "timestamp. Allowed from planned/confirmed/missed. Optional "
            "``narrative`` is cross-patient validated by the same "
            "mention-DSL guard used elsewhere."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "event_id": {"type": "string"},
                "etag": {"type": "string"},
                "actual_start_at": {"type": "string", "format": "date-time"},
                "actual_end_at": {"type": "string", "format": "date-time"},
                "narrative": {"type": "string"},
                "idempotency_key": {"type": "string", "minLength": 1},
                "dry_run": {"type": "boolean", "default": False},
            },
            "required": [
                "event_id",
                "etag",
                "actual_start_at",
                "idempotency_key",
            ],
        },
    ),
    Tool(
        name="cancel_event",
        description=(
            "Move a planned/confirmed event to ``cancelled``. Terminal "
            "state (no further transitions), though its recorded date "
            "can still be corrected with ``amend_event_time``, which is "
            "an amendment and not a transition. ``reason`` is mandatory."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "event_id": {"type": "string"},
                "etag": {"type": "string"},
                "reason": {"type": "string", "minLength": 1, "maxLength": 255},
                "idempotency_key": {"type": "string", "minLength": 1},
                "dry_run": {"type": "boolean", "default": False},
            },
            "required": ["event_id", "etag", "reason", "idempotency_key"],
        },
    ),
    Tool(
        name="mark_event_missed",
        description=(
            "Move a planned/confirmed event to ``missed`` (no-show). "
            "Not terminal: ``missed -> rescheduled`` and "
            "``missed -> completed`` (patient arrived late) are still "
            "allowed downstream."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "event_id": {"type": "string"},
                "etag": {"type": "string"},
                "note": {"type": "string", "maxLength": 255},
                "idempotency_key": {"type": "string", "minLength": 1},
                "dry_run": {"type": "boolean", "default": False},
            },
            "required": ["event_id", "etag", "idempotency_key"],
        },
    ),
    # ----- Record amendment (time correction, NOT a transition) -----------
    Tool(
        name="amend_event_time",
        description=(
            "Correct the recorded clinical date/time of an event WITHOUT "
            "moving its ``event_status``. This is the only way to "
            "correct a recorded time without a transition, and the only "
            "way to re-date a row that is already terminal "
            "(``completed``, ``cancelled``, ``rescheduled``): an FSM "
            "transition would be wrong there, because nothing about the "
            "event's state changed, only our record of when it "
            "happened. Recording a completion or a move is a different "
            "act and keeps its own tool: ``complete_event`` writes "
            "``actual_start_at`` / ``actual_end_at`` and "
            "``reschedule_event`` writes ``new_planned_start_at`` / "
            "``new_planned_end_at`` as part of the transition. "
            "``PATCH`` (see "
            "``update_clinical_event``) refuses temporal fields with 422 "
            "``{code: 'use_amend_time'}`` and points here.\n\n"
            "**Anchor family.** Send only the family matching the current "
            "status: ``planned_start_at`` / ``planned_end_at`` for "
            "``planned``, ``confirmed``, ``rescheduled``, ``cancelled``; "
            "``actual_start_at`` / ``actual_end_at`` for ``completed`` "
            "and ``missed``. Mixing families is refused with 422 "
            "``{code: 'wrong_anchor_for_status'}``.\n\n"
            "**Starts move, ends clear.** The START anchor of the "
            "family (``planned_start_at`` / ``actual_start_at``) can be "
            "moved to another instant but never removed: it is what the "
            "row's date is derived from, so nulling it is refused with "
            "422 ``{code: 'anchor_not_clearable'}`` whatever the status, "
            "with the refused field name in ``detail.field``. The same "
            "refusal covers ``event_date`` on a date-only row, which is "
            "the only date that row has. The END anchors do accept an "
            "explicit ``null``, which clears them, because 'we do not "
            "know when it finished' is a legitimate state.\n\n"
            "**event_date is derived.** ``event_date`` is writable ONLY "
            "on date-only rows whose family START anchor is NULL (DICOM "
            "``StudyDate`` imports, document backfills). When an anchor "
            "exists, or when the same call also sets one, the DB derives "
            "the date from it, so a direct write is refused with 422 "
            "``{code: 'event_date_is_derived'}`` instead of being "
            "silently reverted by the trigger: amend the anchor "
            "timestamp and let the date follow.\n\n"
            "**reason.** The rule keys on the row's STATUS, not on "
            "which field you send. It is mandatory for EVERY amendment "
            "of a row whose status is ``completed`` or ``missed`` (the "
            "actual family), a timezone-only correction included, and "
            "for any write of ``event_date``: both restate the record of "
            "something that already happened, so it must say why (422 "
            "``{code: 'reason_required'}`` otherwise). It is optional "
            "while the row is in the planned family (``planned``, "
            "``confirmed``, ``rescheduled``, ``cancelled``), where "
            "correcting a plan that has not happened yet is ordinary "
            "editing.\n\n"
            "Other 422 codes: ``nothing_to_amend`` (no temporal field "
            "sent), ``end_before_start``, ``future_actual_time`` (an "
            "event that already happened cannot be dated in the future), "
            "``invalid_timezone`` (not an IANA zone name). MOVING "
            "``planned_start_at`` on a planned / confirmed row "
            "re-materialises its reminder dispatches; a timezone-only "
            "change does not, because the instant itself is unchanged. Use "
            "``dry_run=true`` to preview the resulting row (including the "
            "re-derived ``event_date``) without persisting."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "event_id": {"type": "string"},
                "etag": {
                    "type": "string",
                    "description": (
                        "Strong ETag from the most recent GET. Sent as "
                        "``If-Match``; 412 on mismatch."
                    ),
                },
                "planned_start_at": {
                    "type": "string",
                    "format": "date-time",
                    "description": (
                        "New planned start. Planned family only "
                        "(planned / confirmed / rescheduled / cancelled). "
                        "Movable, never clearable: a ``null`` is refused "
                        "with 422 ``anchor_not_clearable``. A value "
                        "without an offset is read as UTC."
                    ),
                },
                "planned_end_at": {
                    "type": ["string", "null"],
                    "format": "date-time",
                    "description": "New planned end; explicit ``null`` clears it.",
                },
                "actual_start_at": {
                    "type": "string",
                    "format": "date-time",
                    "description": (
                        "New realised start. Actual family only "
                        "(completed / missed). Cannot be in the future, "
                        "and movable but never clearable: a ``null`` is "
                        "refused with 422 ``anchor_not_clearable``. "
                        "Requires ``reason``."
                    ),
                },
                "actual_end_at": {
                    "type": ["string", "null"],
                    "format": "date-time",
                    "description": (
                        "New realised end; explicit ``null`` clears it. "
                        "Requires ``reason``, like every amendment of a "
                        "completed / missed row."
                    ),
                },
                "event_date": {
                    "type": "string",
                    "format": "date",
                    "description": (
                        "ISO date. Accepted only on date-only rows whose "
                        "family start anchor is NULL; otherwise 422 "
                        "``event_date_is_derived``. Requires ``reason``."
                    ),
                },
                "timezone": {
                    "type": "string",
                    "maxLength": 64,
                    "description": (
                        "IANA name (e.g. 'Europe/Rome'); anything else "
                        "is 422 ``invalid_timezone``. The DB derives "
                        "``event_date`` from the anchor IN this zone, so "
                        "moving it can move the date by a day. On a "
                        "completed / missed row even a timezone-only "
                        "change needs ``reason``."
                    ),
                },
                "reason": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 255,
                    "description": (
                        "Why the recorded time was wrong. Mandatory for "
                        "ANY amendment of a ``completed`` / ``missed`` "
                        "row (a timezone-only one included) and for any "
                        "write of ``event_date``; optional while the row "
                        "is planned / confirmed / rescheduled / "
                        "cancelled. Stored on the ``amend_time`` "
                        "transition row."
                    ),
                },
                "idempotency_key": {"type": "string", "minLength": 1},
                "dry_run": {"type": "boolean", "default": False},
            },
            "required": ["event_id", "etag", "idempotency_key"],
        },
    ),
    # ----- Calendar discovery ---------------------------------------------
    Tool(
        name="find_upcoming_events",
        description=(
            "List upcoming events (status planned or confirmed) for a "
            "patient within the next ``within_days`` (default 30). "
            "Returns newest-first. Use when the agent needs to brief "
            "the user on what's coming up."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "patient_id": {"type": "string"},
                "within_days": {"type": "integer", "minimum": 1, "maximum": 365, "default": 30},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 100},
            },
            "required": ["patient_id"],
        },
    ),
    Tool(
        name="find_overdue_events",
        description=(
            "List events that are still in planned/confirmed state but "
            "whose ``planned_start_at`` is in the past — typically a "
            "no-show or a clerical 'forgot to mark complete'. Use to "
            "prompt the user for a decision."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "patient_id": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 100},
            },
            "required": ["patient_id"],
        },
    ),
    Tool(
        name="find_events_by_date_range",
        description=(
            "List events for a patient between ``from`` and ``to`` "
            "(inclusive DATE bounds), optionally filtered by status "
            "and/or kind. The canonical calendar-feed query."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "patient_id": {"type": "string"},
                "from": {"type": "string", "format": "date"},
                "to": {"type": "string", "format": "date"},
                "statuses": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": [
                            "planned",
                            "confirmed",
                            "completed",
                            "cancelled",
                            "missed",
                            "rescheduled",
                        ],
                    },
                },
                "kinds": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["patient_id"],
        },
    ),
    Tool(
        name="export_calendar_ics",
        description=(
            "Export the patient calendar as RFC 5545 iCalendar text. "
            "Status-aware: planned -> TENTATIVE, confirmed/completed -> "
            "CONFIRMED, cancelled/missed/rescheduled -> CANCELLED. UID "
            "is ``event-{event_id}@bitvision`` so re-imports update "
            "instead of duplicating. Optionally filter by date range "
            "and status set. NOTE: the feed is intentionally rendered "
            "WITHOUT VALARM blocks because calendar apps poll it on a "
            "schedule and would re-arm reminders on every refresh. For "
            "VALARM-bearing single-event invites, use ``export_event_ics``."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "patient_id": {"type": "string"},
                "from": {"type": "string", "format": "date"},
                "to": {"type": "string", "format": "date"},
                "statuses": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
            "required": ["patient_id"],
        },
    ),
    Tool(
        name="export_event_ics",
        description=(
            "Export a single clinical event as a standalone .ics file. "
            "VALARM blocks are emitted by default (``with_valarm=true``) "
            "from the event's ``reminder_offsets_minutes`` so a "
            "recipient who imports the file gets local notifications "
            "without any server-side push from BitVision. Capped at 5 "
            "VALARMs per event to bound the file size and avoid "
            "notification-spam scenarios. Use this for one-shot "
            "appointment invites; use ``export_calendar_ics`` for "
            "subscription feeds (no VALARMs there)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "event_id": {"type": "string"},
                "lang": {
                    "type": "string",
                    "enum": ["it", "en"],
                    "default": "it",
                    "description": (
                        "Locale of the DESCRIPTION text inside VALARM blocks. "
                        "The VEVENT body itself is language-neutral."
                    ),
                },
                "with_valarm": {
                    "type": "boolean",
                    "default": True,
                    "description": (
                        "When true (default), emit one VALARM block per offset "
                        "in ``reminder_offsets_minutes``. Set to false to ship "
                        "a calendar-app-friendly invite without local alarms."
                    ),
                },
            },
            "required": ["event_id"],
        },
    ),
]


async def handle(name: str, arguments: dict) -> str:
    if name == "find_clinical_events":
        params: dict[str, Any] = {}
        if arguments.get("kind"):
            params["kind"] = arguments["kind"]
        if arguments.get("statuses"):
            # httpx repeats the same key for list values, which FastAPI
            # picks up as ``list[str]`` on the Query side. No manual
            # ``,``-joining: that would land as a single string and
            # the server's whitelist would reject it.
            params["statuses"] = arguments["statuses"]
        params["limit"] = arguments.get("limit", 100)
        params["offset"] = arguments.get("offset", 0)
        result = await api_get(
            f"/api/patients/{arguments['patient_id']}/clinical-events",
            params=params,
        )
        return json.dumps(result, indent=2)

    if name == "get_event":
        result = await api_get(f"/api/clinical-events/{arguments['event_id']}")
        return json.dumps(result, indent=2)

    if name == "propose_event_link":
        result = await api_get(
            f"/api/documents/{arguments['document_id']}/propose-events",
            params={"limit": arguments.get("limit", 10)},
        )
        return json.dumps(result, indent=2)

    if name == "create_clinical_event":
        body: dict[str, Any] = {
            "patient_id": arguments["patient_id"],
            "kind": arguments["kind"],
            "title": arguments["title"],
        }
        for k in (
            "event_date",
            "body_part",
            "code_loinc",
            "code_snomed",
            "narrative",
            # planning + calendar fields (migration 0098)
            "event_status",
            "planned_start_at",
            "planned_end_at",
            "actual_start_at",
            "actual_end_at",
            "timezone",
            "location_struct",
            "reminder_offsets_minutes",
            # meet + links (migration 0101); binary attachments are a
            # dedicated sub-resource (clinical_event_attachments) and
            # do not flow through this tool.
            "meeting_url",
            "links",
        ):
            v = arguments.get(k)
            if v is not None:
                body[k] = v
        payload, _hdrs = await api_post_with_headers(
            "/api/clinical-events",
            json=body,
            idempotency_key=arguments["idempotency_key"],
        )
        return json.dumps(payload, indent=2, ensure_ascii=False)

    if name == "update_clinical_event":
        payload, hdrs = await api_patch(
            f"/api/clinical-events/{arguments['event_id']}",
            json=arguments["patch"],
            if_match=arguments["etag"],
        )
        # Surface the new ETag back to the agent so it can chain a
        # follow-up mutation without an intermediate GET.
        if isinstance(payload, dict):
            new_etag = hdrs.get("etag") or hdrs.get("ETag")
            if new_etag:
                payload = {**payload, "_etag_header": new_etag}
        return json.dumps(payload, indent=2, ensure_ascii=False)

    if name == "delete_clinical_event":
        code = await api_delete(
            f"/api/clinical-events/{arguments['event_id']}",
            if_match=arguments["etag"],
        )
        return json.dumps({"status": "deleted", "http_status": code})

    if name in ("confirm_event", "complete_event", "cancel_event", "mark_event_missed"):
        # All four share the shape: POST /clinical-events/{id}/<verb>
        # with If-Match + Idempotency-Key + optional ?dry_run=1.
        verb_path = {
            "confirm_event": "confirm",
            "complete_event": "complete",
            "cancel_event": "cancel",
            "mark_event_missed": "mark-missed",
        }[name]
        body: dict[str, Any] = {}
        for k in (
            "confirmed_at",
            "actual_start_at",
            "actual_end_at",
            "narrative",
            "reason",
            "note",
        ):
            v = arguments.get(k)
            if v is not None:
                body[k] = v
        params: dict[str, Any] = {}
        if arguments.get("dry_run"):
            params["dry_run"] = "true"
        payload, hdrs = await api_post_with_headers(
            f"/api/clinical-events/{arguments['event_id']}/{verb_path}",
            json=body,
            params=params or None,
            idempotency_key=arguments["idempotency_key"],
            if_match=arguments["etag"],
        )
        if isinstance(payload, dict):
            new_etag = hdrs.get("etag") or hdrs.get("ETag")
            if new_etag:
                payload = {**payload, "_etag_header": new_etag}
        return json.dumps(payload, indent=2, ensure_ascii=False)

    if name == "reschedule_event":
        body = {
            "new_planned_start_at": arguments["new_planned_start_at"],
            "reason": arguments["reason"],
        }
        for k in ("new_planned_end_at", "timezone"):
            v = arguments.get(k)
            if v is not None:
                body[k] = v
        reschedule_params: dict[str, Any] | None = (
            {"dry_run": "true"} if arguments.get("dry_run") else None
        )
        payload, hdrs = await api_post_with_headers(
            f"/api/clinical-events/{arguments['event_id']}/reschedule",
            json=body,
            params=reschedule_params,
            idempotency_key=arguments["idempotency_key"],
            if_match=arguments["etag"],
        )
        if isinstance(payload, dict):
            replaced_id = hdrs.get("x-replaced-event-id") or hdrs.get("X-Replaced-Event-Id")
            new_etag = hdrs.get("etag") or hdrs.get("ETag")
            extras = {}
            if replaced_id:
                extras["_replaced_event_id"] = replaced_id
            if new_etag:
                extras["_etag_header"] = new_etag
            if extras:
                payload = {**payload, **extras}
        return json.dumps(payload, indent=2, ensure_ascii=False)

    if name == "amend_event_time":
        # Same envelope as the transitions (If-Match + Idempotency-Key +
        # ?dry_run) but NOT an FSM verb: it corrects the recorded time
        # and leaves event_status alone.
        #
        # A START anchor can be MOVED but never CLEARED: it is what the
        # DB derives event_date from, so the backend answers 422
        # ``anchor_not_clearable``. The schema types both starts as plain
        # strings, so a null here is a caller bug: fail fast with the
        # same vocabulary instead of spending a round trip on it.
        for start_field in ("planned_start_at", "actual_start_at"):
            if start_field in arguments and arguments[start_field] is None:
                return json.dumps(
                    {
                        "error": "anchor_not_clearable",
                        "field": start_field,
                        "detail": (
                            f"{start_field} defines this event's date and cannot be "
                            "removed; send the corrected instant instead. Only "
                            "planned_end_at / actual_end_at accept an explicit null."
                        ),
                    },
                    indent=2,
                )

        # Presence, not truthiness, decides what goes on the wire for the
        # rest: the backend reads the body with ``exclude_unset=True``, so
        # an explicit ``null`` for planned_end_at / actual_end_at is the
        # documented way to CLEAR an end timestamp. Filtering on
        # ``is not None`` (as the transition tools do, where no field is
        # clearable) would make that unexpressible.
        amend_body: dict[str, Any] = {}
        for k in (
            "planned_start_at",
            "planned_end_at",
            "actual_start_at",
            "actual_end_at",
            "event_date",
            "timezone",
        ):
            if k in arguments:
                amend_body[k] = arguments[k]
        if arguments.get("reason") is not None:
            amend_body["reason"] = arguments["reason"]
        amend_params: dict[str, Any] | None = (
            {"dry_run": "true"} if arguments.get("dry_run") else None
        )
        payload, hdrs = await api_post_with_headers(
            f"/api/clinical-events/{arguments['event_id']}/amend-time",
            json=amend_body,
            params=amend_params,
            idempotency_key=arguments["idempotency_key"],
            if_match=arguments["etag"],
        )
        if isinstance(payload, dict):
            new_etag = hdrs.get("etag") or hdrs.get("ETag")
            if new_etag:
                payload = {**payload, "_etag_header": new_etag}
        return json.dumps(payload, indent=2, ensure_ascii=False)

    if name == "find_upcoming_events":
        from datetime import date, timedelta

        within = int(arguments.get("within_days", 30))
        params = {
            "from": date.today().isoformat(),
            "to": (date.today() + timedelta(days=within)).isoformat(),
            "statuses": ["planned", "confirmed"],
            "limit": arguments.get("limit", 100),
        }
        result = await api_get(
            f"/api/patients/{arguments['patient_id']}/clinical-events",
            params=params,
        )
        return json.dumps(result, indent=2)

    if name == "find_overdue_events":
        from datetime import date

        params = {
            "to": date.today().isoformat(),
            "statuses": ["planned", "confirmed"],
            "limit": arguments.get("limit", 100),
        }
        # Note: the list endpoint filters by event_date; for overdue
        # we want "anchor in the past", which is what event_date is
        # for planned/confirmed (derived from planned_start_at by the
        # trigger). Same semantic.
        result = await api_get(
            f"/api/patients/{arguments['patient_id']}/clinical-events",
            params=params,
        )
        return json.dumps(result, indent=2)

    if name == "find_events_by_date_range":
        params = {}
        for k in ("from", "to", "statuses", "kinds"):
            v = arguments.get(k)
            if v is not None:
                params[k] = v
        result = await api_get(
            f"/api/patients/{arguments['patient_id']}/calendar",
            params=params or None,
        )
        return json.dumps(result, indent=2)

    if name == "export_calendar_ics":
        from bvmcp.tools.client import api_get_bytes

        ics_params: dict[str, Any] = {"format": "ics"}
        for k in ("from", "to", "statuses"):
            v = arguments.get(k)
            if v is not None:
                ics_params[k] = v
        ics_bytes, _ctype = await api_get_bytes(
            f"/api/patients/{arguments['patient_id']}/calendar",
            params=ics_params,
        )
        # Return as plain text in the response (the agent can save it
        # to a file or pipe it to a calendar app).
        return ics_bytes.decode("utf-8", errors="replace")

    if name == "export_event_ics":
        from bvmcp.tools.client import api_get_bytes

        evt_params: dict[str, Any] = {
            "lang": arguments.get("lang", "it"),
            "with_valarm": "true" if arguments.get("with_valarm", True) else "false",
        }
        ics_bytes, _ctype = await api_get_bytes(
            f"/api/clinical-events/{arguments['event_id']}/calendar.ics",
            params=evt_params,
        )
        return ics_bytes.decode("utf-8", errors="replace")

    if name == "confirm_event_link":
        # Two backend writes: extract a derived ReportContent, then
        # link the document. The second call is gated on the first
        # succeeding; on partial failure the agent retries with the
        # returned report_content_id.
        document_id = arguments["document_id"]
        event_id = arguments["event_id"]
        body: dict[str, Any] = {
            "clinical_event_id": event_id,
            "authority": "derived",
            "language": arguments.get("language", "it"),
        }
        for k in ("title", "narrative_md", "parser_version", "model_id", "provider"):
            v = arguments.get(k)
            if v is not None:
                body[k] = v
        rc = await api_post("/api/report-contents", json=body)
        rc_id = rc["id"] if isinstance(rc, dict) else rc[0]["id"]
        link = await api_post(
            f"/api/report-contents/{rc_id}/link-document",
            json={
                "document_id": document_id,
                "role": arguments.get("role", "extracted_from"),
            },
        )
        return json.dumps(
            {"report_content": rc, "link": link},
            indent=2,
        )

    raise ValueError(f"unknown tool: {name}")
