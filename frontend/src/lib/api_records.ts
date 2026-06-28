// Structured medical-record API client — clinical events, report
// contents, external identifiers, provenance, documents.
//
// Kept separate from ``api.ts`` (already 2.6 kloc) for navigation, not
// for versioning: ``api.ts`` covers auth and the cross-cutting helpers,
// this module covers the patient-record entities. The shared
// ``request<T>`` helper from ``./api`` is re-used; nothing in this file
// changes the auth / error-shape contract.

import { request } from "./api";

// ---------------------------------------------------------------------------
// ClinicalEvent
// ---------------------------------------------------------------------------

export type ClinicalEventKind =
  | "imaging_study"
  | "surgical_procedure"
  | "outpatient_visit"
  | "inpatient_admission"
  | "lab_batch"
  | "consultation_event"
  | "pathology_review"
  | "mdt_meeting"
  | "cardio_diagnostic"
  | "endoscopy"
  | "radiology_appointment"
  | "other";

export interface ClinicalEvent {
  id: string;
  patient_id: string;
  kind: ClinicalEventKind;
  event_date: string | null;
  title: string;
  body_part: string | null;
  code_loinc: string | null;
  code_snomed: string | null;
  narrative: string | null;
  imaging_study_id: string | null;
  etag: string;
  created_at: string;
  updated_at: string;
  // Calendar v1 fields (migration 0098). All optional in the wire
  // contract — old API responses pre-0098 still satisfy the type.
  event_status?: EventStatus;
  planned_start_at?: string | null;
  planned_end_at?: string | null;
  actual_start_at?: string | null;
  actual_end_at?: string | null;
  timezone?: string | null;
  location_struct?: {
    facility?: string;
    room?: string;
    city?: string;
    address?: string;
    phone?: string;
  } | null;
  recurrence_rule?: string | null;
  recurrence_exdates?: string[] | null;
  reminder_offsets_minutes?: number[] | null;
  parent_event_id?: string | null;
  status_changed_at?: string | null;
  status_changed_by_kind?: "human" | "agent" | "system" | null;
  status_change_reason?: string | null;
  // Calendar-app parity (migration 0101): conference URL + free-form
  // links. Binary attachments are now a dedicated sub-resource
  // (migration 0102) — see ``ClinicalEventAttachment`` below and
  // ``calendarApi.listAttachments`` / ``uploadAttachment``.
  meeting_url?: string | null;
  links?: { label?: string; url: string }[] | null;
}

export interface ClinicalEventAttachment {
  id: string;
  event_id: string;
  patient_id: string;
  filename: string;
  mime: string;
  size_bytes: number;
  uploaded_by_kind: "human" | "agent" | "system";
  created_at: string;
  /** Curated drive Document this raw upload is linked to (reconciled by
   *  content hash on upload, or promoted). Drives the "Open in Drive"
   *  affordance; null when the upload is not (yet) in the Drive. */
  document_id: string | null;
  /** True when ``document_id`` matched an already-curated document
   *  rather than a freshly ingested one. Absent on the list payload. */
  document_reconciled?: boolean | null;
}

/** A curated drive Document linked to a ClinicalEvent ("attach from
 *  Drive", or the curated face of a reconciled raw upload). */
export interface EventDocument {
  id: string; // link id
  event_id: string;
  patient_id: string;
  document_id: string;
  document_title: string;
  document_kind: string | null;
  document_date: string | null;
  /** Set when the link came from reconciling a raw event upload;
   *  null for a pure "attach from Drive" reference. */
  source_attachment_id: string | null;
  link_role: string;
  created_by_kind: string;
  created_at: string;
}

export interface ClinicalEventCreate {
  patient_id: string;
  kind: ClinicalEventKind;
  event_date?: string | null;
  title: string;
  body_part?: string | null;
  code_loinc?: string | null;
  code_snomed?: string | null;
  narrative?: string | null;
}

export interface ClinicalEventPatch {
  event_date?: string | null;
  title?: string;
  body_part?: string | null;
  code_loinc?: string | null;
  code_snomed?: string | null;
  narrative?: string | null;
}

export const clinicalEventsApi = {
  read: (id: string) => request<ClinicalEvent>(`/api/clinical-events/${id}`),
  listForPatient: (
    patientId: string,
    params: { kind?: ClinicalEventKind; limit?: number; offset?: number } = {},
  ) => {
    const qs = new URLSearchParams();
    if (params.kind) qs.set("kind", params.kind);
    if (params.limit !== undefined) qs.set("limit", String(params.limit));
    if (params.offset !== undefined) qs.set("offset", String(params.offset));
    const query = qs.toString();
    return request<ClinicalEvent[]>(
      `/api/patients/${patientId}/clinical-events${query ? `?${query}` : ""}`,
    );
  },
  create: (body: ClinicalEventCreate) =>
    request<ClinicalEvent>("/api/clinical-events", {
      method: "POST",
      json: body,
    }),
  /** Patch mutable metadata. Returns the fresh event with the new
   *  ETag in ``etag``; the caller should swap its local copy so the
   *  next mutation carries an If-Match that the server accepts. 412
   *  on stale ETag, 422 on cross-patient mention violations (detail
   *  shape: ``{code, violations}`` — see EvidenceLinkViolation). */
  update: (id: string, patch: ClinicalEventPatch, etag: string) =>
    request<ClinicalEvent>(`/api/clinical-events/${id}`, {
      method: "PATCH",
      json: patch,
      headers: { "If-Match": etag },
    }),
  /** Delete a non-imaging ClinicalEvent. Backend returns 409 for
   *  ``kind='imaging_study'`` (lifecycle owned by DICOM deletion path)
   *  and 412 if the supplied ETag is stale. */
  remove: (id: string, etag: string) =>
    request<void>(`/api/clinical-events/${id}`, {
      method: "DELETE",
      headers: { "If-Match": etag },
    }),
};

// ---------------------------------------------------------------------------
// ReportContent (Expression)
// ---------------------------------------------------------------------------

export type ReportContentAuthority = "original" | "derived" | "canonical_synthesis" | "stale";

export type ReportContentStatus =
  | "extracted_auto"
  | "endorsed"
  | "draft"
  | "final"
  | "signed"
  | "rejected"
  | "stale";

export interface LinkedDocumentRef {
  id: string;
  title: string | null;
  kind_id: string | null;
  document_date: string | null;
  role: string | null;
}

export interface ReportContent {
  id: string;
  clinical_event_id: string;
  authority: ReportContentAuthority;
  status: ReportContentStatus;
  language: string;
  title: string | null;
  narrative_md: string | null;
  structured_fields: Record<string, unknown>;
  findings_md: string | null;
  recommendations_md: string | null;
  confidence: number | null;
  deidentified_input: boolean | null;
  created_by_subject_id: string;
  author_kind: "human" | "agent";
  is_ai_generated: boolean;
  model_id: string | null;
  provider: string | null;
  agent_token_id: string | null;
  extracted_at: string | null;
  parser_version: string | null;
  endorsed_by_subject_id: string | null;
  endorsed_at: string | null;
  signed_by_subject_id: string | null;
  signed_at: string | null;
  rejected_reason: string | null;
  superseded_by_id: string | null;
  supersede_reason: string | null;
  etag: string;
  created_at: string;
  updated_at: string;
  /** Populated only by the ``/clinical-events/{id}/report-contents``
   *  list endpoint (batched join). The single-row read returns []. */
  linked_documents?: LinkedDocumentRef[];
}

export interface ReportContentCreate {
  clinical_event_id: string;
  authority: "original" | "derived" | "canonical_synthesis";
  title?: string;
  language?: string;
  narrative_md?: string;
  structured_fields?: Record<string, unknown>;
  findings_md?: string;
  recommendations_md?: string;
  confidence?: number;
  deidentified_input?: boolean;
  parser_version?: string;
  model_id?: string;
  provider?: string;
}

export const reportContentsApi = {
  read: (id: string) => request<ReportContent>(`/api/report-contents/${id}`),
  listForEvent: (eventId: string) =>
    request<ReportContent[]>(`/api/clinical-events/${eventId}/report-contents`),
  create: (body: ReportContentCreate) =>
    request<ReportContent>("/api/report-contents", {
      method: "POST",
      json: body,
    }),
  endorse: (id: string, etag: string) =>
    request<ReportContent>(`/api/report-contents/${id}/endorse`, {
      method: "POST",
      json: {},
      headers: { "If-Match": etag },
    }),
  reject: (id: string, etag: string, reason: string) =>
    request<ReportContent>(`/api/report-contents/${id}/reject`, {
      method: "POST",
      json: { reason },
      headers: { "If-Match": etag },
    }),
  sign: (id: string, etag: string, confirmTitle: string) =>
    request<ReportContent>(`/api/report-contents/${id}/sign`, {
      method: "POST",
      json: { confirm_title: confirmTitle },
      headers: { "If-Match": etag },
    }),
  supersede: (
    id: string,
    etag: string,
    body: {
      reason: string;
      title?: string;
      narrative_md?: string;
      findings_md?: string;
      recommendations_md?: string;
      structured_fields?: Record<string, unknown>;
    },
  ) =>
    request<ReportContent>(`/api/report-contents/${id}/supersede`, {
      method: "POST",
      json: body,
      headers: { "If-Match": etag },
    }),
  /** Patch a non-terminal report_content. ``findings_md`` and
   *  ``recommendations_md`` are silently ignored on non-canonical
   *  rows (the backend stores them only for ``canonical_synthesis``);
   *  pass them anyway and the server will drop them. Returns the
   *  fresh row with the new ETag. 412 on stale etag, 422 with
   *  evidence-link violations on cross-patient mention, 409 on
   *  terminal status. */
  update: (
    id: string,
    etag: string,
    patch: {
      title?: string;
      narrative_md?: string;
      findings_md?: string;
      recommendations_md?: string;
      structured_fields?: Record<string, unknown>;
      status?: ReportContentStatus;
    },
  ) =>
    request<ReportContent>(`/api/report-contents/${id}`, {
      method: "PATCH",
      json: patch,
      headers: { "If-Match": etag },
    }),
};

// ---------------------------------------------------------------------------
// External identifiers
// ---------------------------------------------------------------------------

export interface ExternalIdentifier {
  system: string;
  value: string;
  type: string;
  assigner?: string | null;
}

export interface IdentifierLookupCandidate {
  patient_id: string;
  display_name: string;
  birth_date: string | null;
  sex: string | null;
}

export const externalIdentifiersApi = {
  list: (patientId: string) =>
    request<ExternalIdentifier[]>(`/api/patients/${patientId}/external-identifiers`),
  add: (patientId: string, body: ExternalIdentifier) =>
    request<ExternalIdentifier[]>(`/api/patients/${patientId}/external-identifiers`, {
      method: "POST",
      json: body,
    }),
  remove: (patientId: string, system: string, value: string) =>
    request<ExternalIdentifier[]>(
      `/api/patients/${patientId}/external-identifiers?system=${encodeURIComponent(
        system,
      )}&value=${encodeURIComponent(value)}`,
      { method: "DELETE" },
    ),
  lookup: (system: string, value: string, limit = 10) =>
    request<IdentifierLookupCandidate[]>(
      `/api/patients/lookup-external?system=${encodeURIComponent(
        system,
      )}&value=${encodeURIComponent(value)}&limit=${limit}`,
    ),
};

// ---------------------------------------------------------------------------
// Provenance
// ---------------------------------------------------------------------------

export type ProvenanceTargetKind =
  | "patient"
  | "clinical_event"
  | "imaging_study"
  | "series"
  | "report_content"
  | "document"
  | "document_file"
  | "marker"
  | "tag"
  | "external_identifier"
  | "content_document_link"
  | "report_content_citation";

export type ProvenanceActivity =
  | "create"
  | "classify"
  | "extract"
  | "endorse"
  | "sign"
  | "reject"
  | "supersede"
  | "merge"
  | "split"
  | "cite"
  | "link"
  | "unlink"
  | "redact"
  | "delete"
  | "restore"
  | "identify"
  | "update";

export interface ProvenanceEvent {
  id: string;
  recorded_at: string;
  target_kind: ProvenanceTargetKind;
  target_id: string;
  activity: ProvenanceActivity;
  agent_kind: "human" | "agent" | "system";
  agent_subject_id: string | null;
  agent_token_id: string | null;
  source_kind: string | null;
  source_id: string | null;
  diff: Record<string, unknown> | null;
  event_metadata: Record<string, unknown> | null;
  signature_hash: string | null;
}

export const provenanceApi = {
  read: (
    targetKind: ProvenanceTargetKind,
    targetId: string,
    params: { limit?: number; offset?: number } = {},
  ) => {
    const qs = new URLSearchParams();
    if (params.limit !== undefined) qs.set("limit", String(params.limit));
    if (params.offset !== undefined) qs.set("offset", String(params.offset));
    const query = qs.toString();
    return request<ProvenanceEvent[]>(
      `/api/provenance/${targetKind}/${targetId}${query ? `?${query}` : ""}`,
    );
  },
};

// ---------------------------------------------------------------------------
// Documents v3 (merge / split / download)
// ---------------------------------------------------------------------------

export interface MergeAliasesResult {
  canonical_id: string;
  original_blob_hash: string;
  affected_ids: string[];
}

export const documentsApi = {
  merge: (body: {
    document_ids: string[];
    canonical_id?: string;
    reason?: string;
  }) =>
    request<MergeAliasesResult>("/api/documents/merge", {
      method: "POST",
      json: body,
    }),
  split: (documentId: string, reason?: string) =>
    request<{ document_id: string; original_blob_hash: string | null }>(
      `/api/documents/${documentId}/split`,
      { method: "POST", json: { reason } },
    ),
  /**
   * Returns the full backend URL the browser should hit to download the
   * blob inline. Storage isolation is enforced by the backend; the
   * client never sees the bucket / key. Caller is responsible for
   * attaching the JWT (typically by opening in a same-origin tab where
   * the cookie / Authorization header carries through).
   */
  downloadUrl: (documentId: string) => `/api/documents/${documentId}/download`,
};

// ---------------------------------------------------------------------------
// Care phases / Care timeline
// ---------------------------------------------------------------------------
//
// Wire types mirror ``backend/src/bvphoenix/api/_schemas_care_phase.py``.
// Keep this section *flat* and *typed*; do not add UI helpers here, those
// belong in the components that consume the data.

export type CarePhaseKind =
  | "imaging"
  | "surgery"
  | "followup"
  | "surveillance"
  | "visit"
  | "reassessment"
  | "other";

export type EventTargetKind = "imaging_study" | "report" | "document" | "consultation" | "event";

/**
 * Discriminated union — backend resolves the natural navigation target
 * for every timeline event (study / report / document / consultation /
 * fallback to clinical-event detail). The frontend reads ``url`` to
 * decide where a click on the dot should land; ``mcp_uri`` is reserved
 * for the MCP/A2A surface.
 */
export interface EventTargetBase {
  kind: EventTargetKind;
  id: string;
  url: string;
  mcp_uri: string;
}
export interface StudyTarget extends EventTargetBase {
  kind: "imaging_study";
}
export interface ReportTarget extends EventTargetBase {
  kind: "report";
}
export interface DocumentTarget extends EventTargetBase {
  kind: "document";
}
export interface ConsultationTarget extends EventTargetBase {
  kind: "consultation";
}
export interface GenericEventTarget extends EventTargetBase {
  kind: "event";
}
export type EventTarget =
  | StudyTarget
  | ReportTarget
  | DocumentTarget
  | ConsultationTarget
  | GenericEventTarget;

export interface CarePhaseCounts {
  n_events: number;
  n_studies: number;
  n_documents: number;
  n_reports: number;
  n_consultations: number;
}

export interface CarePhase {
  id: string;
  patient_id: string;
  slug: string;
  name: string;
  name_i18n: Record<string, string>;
  kind: CarePhaseKind | string;
  color_hex: string;
  start_date: string | null;
  end_date: string | null;
  ordinal: number;
  narrative_md: string | null;
  author_kind: "human" | "agent";
  proposed_by_agent_id: string | null;
  confirmed_by_user_id: string | null;
  confirmed_at: string | null;
  etag: string;
  created_at: string;
  updated_at: string;
  counts: CarePhaseCounts;
}

export type EventStatus =
  | "planned"
  | "confirmed"
  | "completed"
  | "cancelled"
  | "missed"
  | "rescheduled";

export interface TimelineEvent {
  id: string;
  patient_id: string;
  kind: string;
  event_date: string | null;
  title: string;
  body_part: string | null;
  code_loinc: string | null;
  code_snomed: string | null;
  narrative: string | null;
  phase_id: string | null;
  phase_assigned_by: "human" | "agent" | null;
  phase_assignment_confidence: number | null;
  target: EventTarget;
  etag: string;
  // Calendar / planning v1 (migration 0098). Defaults to ``completed``
  // server-side, so a JSON produced by a pre-0098 backend won't break
  // the FE — it just always reads as ``completed``.
  event_status: EventStatus;
  planned_start_at: string | null;
  actual_start_at: string | null;
  timezone: string | null;
}

export interface CarePhaseDetail extends CarePhase {
  events: TimelineEvent[];
}

export interface CareTimeline {
  patient_id: string;
  phases: CarePhaseDetail[];
  unassigned_events: TimelineEvent[];
  generated_at: string;
  lang: string;
}

export interface MaterialItem {
  kind: "study" | "document" | "report" | "consultation" | "annotation";
  id: string;
  title: string;
  secondary: string | null;
  event_id: string | null;
  event_date: string | null;
  url: string;
  mcp_uri: string;
}

export interface CarePhaseMaterial {
  phase_id: string;
  studies: MaterialItem[];
  documents: MaterialItem[];
  reports: MaterialItem[];
  consultations: MaterialItem[];
  annotations: MaterialItem[];
}

// ---- Calendar feed (migration 0098 + 0099, calendar UI) ----------------

export interface CalendarOccurrence {
  event_id: string;
  kind: string;
  title: string;
  event_status: EventStatus;
  // ISO 8601 with timezone offset (e.g. "2026-05-25T10:00:00+02:00") or null
  // when the event has no anchor timestamp (legacy DATE-only rows).
  occurrence_dt_start: string | null;
  occurrence_dt_end: string | null;
  timezone: string | null;
  location_struct: {
    facility?: string;
    room?: string;
    city?: string;
    address?: string;
    phone?: string;
  } | null;
  parent_event_id: string | null;
  etag: string;
}

export interface CalendarFeed {
  patient_id: string;
  range_from: string | null;
  range_to: string | null;
  timezone: string;
  occurrences: CalendarOccurrence[];
  counts: Record<EventStatus, number> | Record<string, number>;
  generated_at: string;
}

// (The full ClinicalEvent shape with calendar v1 fields is declared
// above, alongside the legacy fields, so there is a single source of
// truth for the wire contract.)

export interface CarePhaseRevision {
  id: string;
  phase_id: string;
  revision_no: number;
  snapshot: Record<string, unknown>;
  change_kind:
    | "create"
    | "update"
    | "assign"
    | "unassign"
    | "apply_proposal"
    | "restore"
    | "delete";
  author_kind: "human" | "agent";
  actor_id: string | null;
  diff_summary: string | null;
  created_at: string;
}

export interface TimelineHealth {
  patient_id: string;
  n_phases: number;
  n_events: number;
  n_events_assigned: number;
  pct_assigned: number;
  pending_proposals: number;
  last_classifier_run: string | null;
}

export interface CarePhaseCreateIn {
  slug: string;
  name: string;
  name_i18n?: Record<string, string>;
  kind: CarePhaseKind | string;
  color_hex?: string;
  start_date?: string | null;
  end_date?: string | null;
  ordinal?: number;
  narrative_md?: string | null;
}

export interface CarePhaseUpdateIn {
  name?: string;
  name_i18n?: Record<string, string>;
  kind?: CarePhaseKind | string;
  color_hex?: string;
  start_date?: string | null;
  end_date?: string | null;
  ordinal?: number;
  narrative_md?: string | null;
}

export interface AssignPhaseIn {
  confidence?: number;
}

export interface ReorderItem {
  phase_id: string;
  ordinal: number;
}
export interface ReorderIn {
  ordinals: ReorderItem[];
}

export interface ProposedPhase {
  slug: string;
  name: string;
  name_i18n: Record<string, string>;
  kind: string;
  color_hex: string | null;
  ordinal: number;
  narrative_md: string | null;
}
export interface ProposedAssignment {
  event_id: string;
  phase_slug: string;
  confidence: number;
}
export interface ProposalPayload {
  phases: ProposedPhase[];
  assignments: ProposedAssignment[];
}
export interface ProposePhasesOut {
  proposal_id: string;
  job_id: string | null;
  status: string;
  payload: ProposalPayload;
  model_id: string;
  cached: boolean;
  created_at: string;
}

export interface ApplyProposalIn {
  proposal_id: string;
  accept_phases: string[];
  accept_assignments: string[];
}
export interface ApplyProposalOut {
  applied_phases: string[];
  applied_assignments: number;
  skipped_assignments: number;
}

export type CareTimelineFormat = "json" | "svg" | "markdown" | "pdf" | "ics";

/**
 * Care-phases REST client. Mirrors the FastAPI router under
 * ``/api/patients/{patientId}/care-phases``. Every method is
 * patient-scoped by construction; cross-patient calls are
 * unrepresentable here just like at the backend layer.
 */
export const carePhasesApi = {
  // ---- Reads ----
  timeline: (patientId: string, params: { lang?: string; format?: CareTimelineFormat } = {}) => {
    const qs = new URLSearchParams();
    if (params.lang) qs.set("lang", params.lang);
    if (params.format) qs.set("format", params.format);
    const query = qs.toString();
    return request<CareTimeline>(
      `/api/patients/${patientId}/care-timeline${query ? `?${query}` : ""}`,
    );
  },

  /** Backend URL for non-JSON formats (svg/pdf/ics). The browser hits it
   * directly; auth context still travels via Authorization header. */
  timelineUrl: (
    patientId: string,
    params: { lang?: string; format: Exclude<CareTimelineFormat, "json"> },
  ) => {
    const qs = new URLSearchParams();
    if (params.lang) qs.set("lang", params.lang);
    qs.set("format", params.format);
    return `/api/patients/${patientId}/care-timeline?${qs.toString()}`;
  },

  list: (patientId: string) => request<CarePhase[]>(`/api/patients/${patientId}/care-phases`),

  detail: (patientId: string, phaseId: string) =>
    request<CarePhaseDetail>(`/api/patients/${patientId}/care-phases/${phaseId}`),

  material: (patientId: string, phaseId: string) =>
    request<CarePhaseMaterial>(`/api/patients/${patientId}/care-phases/${phaseId}/material`),

  revisions: (patientId: string, phaseId: string) =>
    request<CarePhaseRevision[]>(`/api/patients/${patientId}/care-phases/${phaseId}/revisions`),

  health: (patientId: string) =>
    request<TimelineHealth>(`/api/patients/${patientId}/care-timeline/health`),

  // ---- Mutations ----
  create: (patientId: string, body: CarePhaseCreateIn) =>
    request<CarePhase>(`/api/patients/${patientId}/care-phases`, {
      method: "POST",
      json: body,
    }),

  /** ETag-conditional update; backend returns 412 on stale If-Match. */
  update: (patientId: string, phaseId: string, etag: string, body: CarePhaseUpdateIn) =>
    request<CarePhase>(`/api/patients/${patientId}/care-phases/${phaseId}`, {
      method: "PATCH",
      json: body,
      headers: { "If-Match": etag },
    }),

  remove: (patientId: string, phaseId: string) =>
    request<void>(`/api/patients/${patientId}/care-phases/${phaseId}`, {
      method: "DELETE",
    }),

  assignEvent: (patientId: string, phaseId: string, eventId: string, body: AssignPhaseIn = {}) =>
    request<TimelineEvent>(`/api/patients/${patientId}/care-phases/${phaseId}/events/${eventId}`, {
      method: "PUT",
      json: body,
    }),

  unassignEvent: (patientId: string, phaseId: string, eventId: string) =>
    request<void>(`/api/patients/${patientId}/care-phases/${phaseId}/events/${eventId}`, {
      method: "DELETE",
    }),

  /** Kicks the LLM classifier worker; returns an in-flight or cached
   * proposal. Long-op pattern: caller polls /api/jobs/{job_id} when
   * ``status === 'pending'``. */
  propose: (patientId: string, body: { lang?: string } = {}) =>
    request<ProposePhasesOut>(`/api/patients/${patientId}/care-phases:propose`, {
      method: "POST",
      json: body,
    }),

  /** Atomic apply. ``Idempotency-Key`` is mandatory on the backend;
   * caller is responsible for supplying a stable UUID per logical
   * apply intent. */
  applyProposal: (patientId: string, body: ApplyProposalIn, idempotencyKey: string) =>
    request<ApplyProposalOut>(`/api/patients/${patientId}/care-phases:apply-proposal`, {
      method: "POST",
      json: body,
      headers: { "Idempotency-Key": idempotencyKey },
    }),

  reorder: (patientId: string, body: ReorderIn) =>
    request<CarePhase[]>(`/api/patients/${patientId}/care-phases:reorder`, {
      method: "POST",
      json: body,
    }),

  restoreRevision: (patientId: string, phaseId: string, revisionNo: number) =>
    request<CarePhase>(`/api/patients/${patientId}/care-phases/${phaseId}/restore`, {
      method: "POST",
      json: { revision_no: revisionNo },
    }),
};

// ---------------------------------------------------------------------------
// PatientTask (operational checklist, v3.4)
// ---------------------------------------------------------------------------
//
// Operational to-dos attached to a fascicolo: "book the TAC", "ask the
// GP for the impegnativa", "buy the medication". Distinct from
// ClinicalEvent: not part of the medical record, not exported to
// FSE/HL7, separate FSM (pending → in_progress → done | dropped |
// snoozed). Backend surface: ``api/patient_tasks.py``.

export type TaskStatus = "pending" | "in_progress" | "snoozed" | "done" | "dropped";

export type TaskPriority = "low" | "normal" | "high" | "urgent";

export type TaskCategory =
  | "admin"
  | "pharmacy"
  | "appointment_prep"
  | "transport"
  | "communication"
  | "personal"
  | "other";

export type TaskTransitionVerb = "start" | "snooze" | "wake" | "complete" | "drop" | "reopen";

export interface PatientTask {
  id: string;
  patient_id: string;
  title: string;
  description: string | null;
  category: TaskCategory;
  priority: TaskPriority;
  status: TaskStatus;
  due_at: string | null;
  snooze_until: string | null;
  completed_at: string | null;
  timezone: string | null;
  phase_id: string | null;
  phase_assigned_by: "human" | "agent" | "system" | null;
  phase_assigned_at: string | null;
  recurrence_rule: string | null;
  parent_task_id: string | null;
  assigned_to_contact_id: string | null;
  related_event_id: string | null;
  related_document_id: string | null;
  labels: string[] | null;
  links: { label?: string; url: string }[] | null;
  reminder_offsets_minutes: number[] | null;
  etag: string;
  author_kind: "human" | "agent" | "system";
  status_changed_at: string | null;
  status_changed_by_kind: "human" | "agent" | "system" | null;
  status_change_reason: string | null;
  deleted_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface PatientTaskCreate {
  patient_id: string;
  title: string;
  description?: string | null;
  category?: TaskCategory;
  priority?: TaskPriority;
  due_at?: string | null;
  timezone?: string | null;
  phase_id?: string | null;
  recurrence_rule?: string | null;
  assigned_to_contact_id?: string | null;
  related_event_id?: string | null;
  related_document_id?: string | null;
  labels?: string[] | null;
  links?: { label?: string; url: string }[] | null;
  reminder_offsets_minutes?: number[] | null;
}

export interface PatientTaskPatch {
  title?: string;
  description?: string | null;
  category?: TaskCategory;
  priority?: TaskPriority;
  due_at?: string | null;
  timezone?: string | null;
  phase_id?: string | null;
  recurrence_rule?: string | null;
  assigned_to_contact_id?: string | null;
  related_event_id?: string | null;
  related_document_id?: string | null;
  labels?: string[] | null;
  links?: { label?: string; url: string }[] | null;
  reminder_offsets_minutes?: number[] | null;
}

export interface PatientTaskListParams {
  statuses?: TaskStatus[];
  category?: TaskCategory;
  priority?: TaskPriority;
  due_from?: string;
  due_to?: string;
  include_deleted?: boolean;
  limit?: number;
  offset?: number;
}

/** Transition bodies. Each verb takes a small shape mapping to the
 *  matching backend ``POST /api/patient-tasks/{id}/{verb}`` endpoint. */
export interface SnoozeBody {
  snooze_until: string;
  reason?: string | null;
}
export interface WakeBody {
  resume_in_progress?: boolean;
}
export interface CompleteTaskBody {
  completed_at?: string | null;
  note?: string | null;
}
export interface DropBody {
  reason: string;
}
export interface ReopenBody {
  reason?: string | null;
}

export const tasksApi = {
  /** List operational tasks on a patient's checklist. Hidden by
   *  default: ``status='deleted'`` rows (set ``include_deleted=true``
   *  to surface tombstones). */
  list: (patientId: string, params: PatientTaskListParams = {}) => {
    const qs = new URLSearchParams();
    if (params.statuses) for (const s of params.statuses) qs.append("statuses", s);
    if (params.category) qs.set("category", params.category);
    if (params.priority) qs.set("priority", params.priority);
    if (params.due_from) qs.set("due_from", params.due_from);
    if (params.due_to) qs.set("due_to", params.due_to);
    if (params.include_deleted !== undefined)
      qs.set("include_deleted", params.include_deleted ? "true" : "false");
    if (params.limit !== undefined) qs.set("limit", String(params.limit));
    if (params.offset !== undefined) qs.set("offset", String(params.offset));
    const query = qs.toString();
    return request<PatientTask[]>(`/api/patients/${patientId}/tasks${query ? `?${query}` : ""}`);
  },

  read: (taskId: string, params: { include_deleted?: boolean } = {}) => {
    const qs = new URLSearchParams();
    if (params.include_deleted !== undefined)
      qs.set("include_deleted", params.include_deleted ? "true" : "false");
    const query = qs.toString();
    return request<PatientTask>(`/api/patient-tasks/${taskId}${query ? `?${query}` : ""}`);
  },

  /** Create a task. ``Idempotency-Key`` is mandatory backend-side. */
  create: (body: PatientTaskCreate, idempotencyKey: string) =>
    request<PatientTask>("/api/patient-tasks", {
      method: "POST",
      json: body,
      headers: { "Idempotency-Key": idempotencyKey },
    }),

  /** Patch mutable metadata. ``etag`` → ``If-Match``. */
  update: (taskId: string, patch: PatientTaskPatch, etag: string) =>
    request<PatientTask>(`/api/patient-tasks/${taskId}`, {
      method: "PATCH",
      json: patch,
      headers: { "If-Match": etag },
    }),

  /** Soft-delete (tombstone). Idempotent: deleting an already-deleted
   *  task is a 204 no-op. */
  remove: (taskId: string, etag: string) =>
    request<void>(`/api/patient-tasks/${taskId}`, {
      method: "DELETE",
      headers: { "If-Match": etag },
    }),

  /** Clear the tombstone. Use the etag from a GET with
   *  ``include_deleted=true``. */
  restore: (taskId: string, etag: string) =>
    request<PatientTask>(`/api/patient-tasks/${taskId}/restore`, {
      method: "POST",
      json: {},
      headers: { "If-Match": etag },
    }),

  /** FSM transition wrapper. The verb maps 1:1 to the backend
   *  ``POST /api/patient-tasks/{id}/{verb}`` endpoint. Each call
   *  requires the current ETag + a fresh Idempotency-Key. */
  transition: <V extends TaskTransitionVerb>(
    taskId: string,
    verb: V,
    body: TransitionBody<V>,
    etag: string,
    idempotencyKey: string,
    opts: { dry_run?: boolean } = {},
  ) => {
    const qs = opts.dry_run ? "?dry_run=true" : "";
    return request<PatientTask>(`/api/patient-tasks/${taskId}/${verb}${qs}`, {
      method: "POST",
      json: body ?? {},
      headers: {
        "If-Match": etag,
        "Idempotency-Key": idempotencyKey,
      },
    });
  },
};

/** Resolve the request body shape for a given transition verb. Keeps
 *  ``tasksApi.transition`` callers type-safe: ``tasksApi.transition(id,
 *  "snooze", { snooze_until: "..." }, ...)`` is the only legal shape. */
export type TransitionBody<V extends TaskTransitionVerb> = V extends "start"
  ? Record<string, never>
  : V extends "snooze"
    ? SnoozeBody
    : V extends "wake"
      ? WakeBody
      : V extends "complete"
        ? CompleteTaskBody
        : V extends "drop"
          ? DropBody
          : V extends "reopen"
            ? ReopenBody
            : never;
