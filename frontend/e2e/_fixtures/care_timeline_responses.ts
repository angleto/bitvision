// Mock API responses for the care-timeline E2E spec.
//
// Shape follows the wire types in ``src/lib/api_v3.ts`` (CarePhase,
// CareTimeline, TimelineEvent, TimelineHealth, ProposePhasesOut). The
// data mirrors the Patient X-golden case used by the unit
// fixtures in ``__tests__/_fixtures.ts`` so the timeline rendered by
// the spec stays visually equivalent.
//
// The responses are split by request stage:
//   - ``emptyTimeline`` / ``emptyHealth``: patient has events but zero
//     phases (initial render of step 2).
//   - ``proposalResponse``: payload returned by the propose endpoint
//     (step 3).
//   - ``populatedTimeline`` / ``populatedHealth``: timeline after the
//     proposal is applied (steps 4 onward).
//   - ``revisionsForFirstPhase``: revision history for the restore step.

export const E2E_PATIENT_ID = "00000000-cana-cana-cana-000000000001";

const PHASE_IDS = [
  "11111111-1111-1111-1111-111111111111",
  "22222222-2222-2222-2222-222222222222",
  "33333333-3333-3333-3333-333333333333",
  "44444444-4444-4444-4444-444444444444",
  "55555555-5555-5555-5555-555555555555",
  "66666666-6666-6666-6666-666666666666",
  "77777777-7777-7777-7777-777777777777",
] as const;

interface MockEvent {
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
  phase_assigned_by: string | null;
  phase_assignment_confidence: number | null;
  target: { kind: string; id: string; url: string; mcp_uri: string };
  etag: string;
}

function ev(i: number, date: string, title: string, phaseId: string | null): MockEvent {
  return {
    id: `00000000-0000-0000-0000-${String(1000 + i).padStart(12, "0")}`,
    patient_id: E2E_PATIENT_ID,
    kind: "imaging_study",
    event_date: date,
    title,
    body_part: null,
    code_loinc: null,
    code_snomed: null,
    narrative: null,
    phase_id: phaseId,
    phase_assigned_by: phaseId ? "agent" : null,
    phase_assignment_confidence: phaseId ? 0.95 : null,
    target: {
      kind: "imaging_study",
      id: `study-${i}`,
      url: `/studies/study-${i}`,
      mcp_uri: `mcp://study/study-${i}`,
    },
    etag: `etag-evt-${i}`,
  };
}

function phase(
  id: string,
  slug: string,
  name: string,
  kind: string,
  color: string,
  ordinal: number,
  events: MockEvent[],
) {
  return {
    id,
    patient_id: E2E_PATIENT_ID,
    slug,
    name,
    name_i18n: { it: name, en: name },
    kind,
    color_hex: color,
    start_date: events[0]?.event_date ?? null,
    end_date: events[events.length - 1]?.event_date ?? null,
    ordinal,
    narrative_md: null,
    author_kind: "agent",
    proposed_by_agent_id: null,
    confirmed_by_user_id: null,
    confirmed_at: null,
    etag: `etag-${slug}`,
    created_at: "2024-05-20T00:00:00Z",
    updated_at: "2024-05-20T00:00:00Z",
    counts: {
      n_events: events.length,
      n_studies: events.length,
      n_documents: 0,
      n_reports: 0,
      n_consultations: 0,
    },
    events,
  };
}

// ---- Pre-proposal (step 2): events but no phases ---------------------
const UNASSIGNED_EVENTS: MockEvent[] = [
  ev(1, "2024-05-20", "RM addome superiore", null),
  ev(2, "2024-07-29", "Fine procedura chirurgica", null),
  ev(3, "2024-08-06", "Relazione di dimissione", null),
  ev(4, "2024-08-13", "Biopsia post-operatoria", null),
  ev(5, "2024-09-16", "TC addome completo", null),
  ev(6, "2024-09-30", "RM addome + esame istologico", null),
  ev(7, "2024-10-25", "PET total body", null),
  ev(8, "2024-10-29", "Visita oncologica n.1", null),
];

export const emptyTimeline = {
  patient_id: E2E_PATIENT_ID,
  phases: [],
  unassigned_events: UNASSIGNED_EVENTS,
  generated_at: "2026-05-03T00:00:00Z",
  lang: "it",
};

export const emptyHealth = {
  patient_id: E2E_PATIENT_ID,
  n_phases: 0,
  n_events: UNASSIGNED_EVENTS.length,
  n_events_assigned: 0,
  pct_assigned: 0,
  pending_proposals: 0,
  last_classifier_run: null,
};

// ---- Propose response (step 3) ---------------------------------------
export const proposalResponse = {
  proposal_id: "proposal-aaaa-bbbb-cccc-000000000001",
  job_id: null,
  status: "ready",
  payload: {
    phases: [
      {
        slug: "imaging-pre-op",
        name: "Imaging pre-op",
        name_i18n: { it: "Imaging pre-op", en: "Pre-op imaging" },
        kind: "imaging",
        color_hex: "#185FA5",
        ordinal: 0,
        narrative_md: null,
      },
      {
        slug: "intervento-chirurgico",
        name: "Intervento chirurgico + degenza",
        name_i18n: { it: "Intervento chirurgico + degenza", en: "Surgery + stay" },
        kind: "surgery",
        color_hex: "#993C1D",
        ordinal: 1,
        narrative_md: null,
      },
      {
        slug: "follow-up-post-op",
        name: "Follow-up post-op + stadiazione",
        name_i18n: { it: "Follow-up post-op + stadiazione", en: "Post-op follow-up" },
        kind: "followup",
        color_hex: "#185FA5",
        ordinal: 2,
        narrative_md: null,
      },
    ],
    assignments: UNASSIGNED_EVENTS.map((event, idx) => ({
      event_id: event.id,
      phase_slug:
        idx < 1 ? "imaging-pre-op" : idx < 4 ? "intervento-chirurgico" : "follow-up-post-op",
      confidence: 0.95,
    })),
  },
  model_id: "stub-classifier-1",
  cached: false,
  created_at: "2026-05-03T00:00:00Z",
};

// ---- Post-apply (steps 4+): phases populated, events assigned --------
export const populatedTimeline = {
  patient_id: E2E_PATIENT_ID,
  phases: [
    phase(PHASE_IDS[0], "imaging-pre-op", "Imaging pre-op", "imaging", "#185FA5", 0, [
      ev(1, "2024-05-20", "RM addome superiore", PHASE_IDS[0]),
    ]),
    phase(
      PHASE_IDS[1],
      "intervento-chirurgico",
      "Intervento chirurgico + degenza",
      "surgery",
      "#993C1D",
      1,
      [
        ev(2, "2024-07-29", "Fine procedura chirurgica", PHASE_IDS[1]),
        ev(3, "2024-08-06", "Relazione di dimissione", PHASE_IDS[1]),
        ev(4, "2024-08-13", "Biopsia post-operatoria", PHASE_IDS[1]),
      ],
    ),
    phase(
      PHASE_IDS[2],
      "follow-up-post-op",
      "Follow-up post-op + stadiazione",
      "followup",
      "#185FA5",
      2,
      [
        ev(5, "2024-09-16", "TC addome completo", PHASE_IDS[2]),
        ev(6, "2024-09-30", "RM addome + esame istologico", PHASE_IDS[2]),
        ev(7, "2024-10-25", "PET total body", PHASE_IDS[2]),
        ev(8, "2024-10-29", "Visita oncologica n.1", PHASE_IDS[2]),
      ],
    ),
  ],
  unassigned_events: [],
  generated_at: "2026-05-03T00:01:00Z",
  lang: "it",
};

export const populatedHealth = {
  patient_id: E2E_PATIENT_ID,
  n_phases: 3,
  n_events: 8,
  n_events_assigned: 8,
  pct_assigned: 1,
  pending_proposals: 0,
  last_classifier_run: "2026-05-03T00:01:00Z",
};

// ---- Material for the phase detail page (step 6) ---------------------
export const materialForFirstPhase = {
  phase_id: PHASE_IDS[0],
  // Material items follow the MaterialItem shape declared in
  // src/lib/api_records.ts: { kind, id, title, secondary, event_id,
  // event_date, url, mcp_uri }. The previous fixture mistakenly used
  // a Study row shape (modality/description fields), which crashed
  // the MaterialList renderer on `appendCarePhaseBack(undefined, …)`
  // because `url` was missing.
  studies: [
    {
      kind: "study" as const,
      id: "study-1",
      title: "RM addome superiore",
      secondary: "MR",
      // Inlined event_id; referencing FIRST_EVENT_ID here would be a
      // forward reference (declared further down in this same module)
      // and trips the TDZ guard at module init.
      event_id: "evt-1",
      event_date: "2024-05-20",
      url: "/studies/study-1",
      mcp_uri: "mcp://study/study-1",
    },
  ],
  documents: [],
  reports: [],
  consultations: [],
  annotations: [],
};

export const detailFirstPhase = populatedTimeline.phases[0];

// ---- Revisions for the restore step (step 8) -------------------------
export const revisionsForFirstPhase = [
  {
    id: "rev-1",
    phase_id: PHASE_IDS[1],
    revision_no: 2,
    change_kind: "event.assigned",
    diff_summary: "Evento riassegnato a follow-up-post-op",
    author_kind: "human",
    author_id: null,
    created_at: "2026-05-03T00:02:00Z",
  },
  {
    id: "rev-0",
    phase_id: PHASE_IDS[1],
    revision_no: 1,
    change_kind: "phase.created",
    diff_summary: "Fase creata da proposta classifier",
    author_kind: "agent",
    author_id: null,
    created_at: "2026-05-03T00:01:00Z",
  },
];

// SVG byte body returned by the export endpoint (step 9).
export const svgExport =
  '<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg" width="100" height="100"><rect width="100" height="100" fill="#185FA5"/></svg>';

export const PHASE_ID_FIRST = PHASE_IDS[0];
export const PHASE_ID_SECOND = PHASE_IDS[1];
export const PHASE_ID_THIRD = PHASE_IDS[2];
export const FIRST_EVENT_ID = ev(1, "x", "x", null).id;
export const SECOND_EVENT_ID = ev(2, "x", "x", null).id;
