// Shared test fixtures. The Patient X-timeline structure is
// the canonical golden case used by both backend (classifier 7/7
// golden test) and frontend tests; the slugs and ordering MUST stay
// in sync with ``backend/tests/fixtures/care_phases/canary_patient_expected.json``
// when the backend fixture lands.

import type { CarePhaseDetail, CareTimeline, TimelineEvent } from "@/lib/api_records";

const PATIENT_ID = "00000000-cana-cana-cana-000000000001";

function evt(i: number, date: string, title: string, phaseId: string | null): TimelineEvent {
  return {
    id: `00000000-0000-0000-0000-${String(1000 + i).padStart(12, "0")}`,
    patient_id: PATIENT_ID,
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
    event_status: "completed",
    planned_start_at: null,
    actual_start_at: null,
    timezone: null,
  };
}

function phase(
  id: string,
  slug: string,
  name: string,
  kind: string,
  color: string,
  ordinal: number,
  events: TimelineEvent[] = [],
): CarePhaseDetail {
  return {
    id,
    patient_id: PATIENT_ID,
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

export const PHASE_IDS = [
  "11111111-1111-1111-1111-111111111111",
  "22222222-2222-2222-2222-222222222222",
  "33333333-3333-3333-3333-333333333333",
  "44444444-4444-4444-4444-444444444444",
  "55555555-5555-5555-5555-555555555555",
  "66666666-6666-6666-6666-666666666666",
  "77777777-7777-7777-7777-777777777777",
] as const;

export function buildCanaryTimeline(): CareTimeline {
  const phases: CarePhaseDetail[] = [
    phase(PHASE_IDS[0], "imaging-pre-op", "Imaging pre-op", "imaging", "#185FA5", 0, [
      evt(1, "2024-05-20", "RM addome superiore", PHASE_IDS[0]),
    ]),
    phase(
      PHASE_IDS[1],
      "intervento-chirurgico",
      "Intervento chirurgico + degenza",
      "surgery",
      "#993C1D",
      1,
      [
        evt(2, "2024-07-29", "Fine procedura chirurgica", PHASE_IDS[1]),
        evt(3, "2024-08-06", "Relazione di dimissione", PHASE_IDS[1]),
        evt(4, "2024-08-13", "Biopsia post-operatoria", PHASE_IDS[1]),
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
        evt(5, "2024-09-16", "TC addome completo", PHASE_IDS[2]),
        evt(6, "2024-09-30", "RM addome + esame istologico", PHASE_IDS[2]),
        evt(7, "2024-10-25", "PET total body", PHASE_IDS[2]),
      ],
    ),
    phase(
      PHASE_IDS[3],
      "inizio-follow-up-oncologico",
      "Inizio follow-up oncologico",
      "followup",
      "#534AB7",
      3,
      [evt(8, "2024-10-29", "Visita oncologica n.1", PHASE_IDS[3])],
    ),
    phase(
      PHASE_IDS[4],
      "sorveglianza-periodica",
      "Sorveglianza periodica",
      "surveillance",
      "#185FA5",
      4,
      [evt(9, "2025-03-15", "TC torace-addome", PHASE_IDS[4])],
    ),
    phase(PHASE_IDS[5], "rivalutazione", "Rivalutazione recente", "reassessment", "#534AB7", 5, [
      evt(10, "2026-04-10", "Visita oncologica di controllo", PHASE_IDS[5]),
    ]),
    phase(PHASE_IDS[6], "altro", "Altro", "other", "#888780", 6, []),
  ];
  return {
    patient_id: PATIENT_ID,
    phases,
    unassigned_events: [evt(99, "2024-06-01", "Esame extra", null)],
    generated_at: "2026-05-03T00:00:00Z",
    lang: "it",
  };
}

export const CANARY_PATIENT_ID = PATIENT_ID;
