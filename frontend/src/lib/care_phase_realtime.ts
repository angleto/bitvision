// Care-phase realtime subscription helper.
//
// Backend channel (per spec ``docs/care-timeline-phases.md`` §8.6):
//   ``patient:{patient_id}:phases``
//
// Push events arrive whenever an agent or peer mutates the care timeline
// (apply-proposal, assign/unassign, reorder, update, restore). The
// component re-fetches the timeline JSON on each notification — payload
// is intentionally a thin trigger, not a delta, to keep the wire shape
// trivial and to avoid divergent state between SSE and REST.
//
// IMPLEMENTATION STATUS
// ---------------------
// The backend SSE/WebSocket bus is not yet wired (tracked by
// roadmap §8.6 alongside the rest of the F-care-timeline epic). This
// module exposes the *final* surface so the components can be written
// against it now; today the subscriber stays idle and the unsubscribe
// is a no-op. When the backend lands one of the two transports below,
// implement the matching branch and remove the noop.
//
// Wire option A (SSE):
//   GET /api/patients/{id}/care-timeline/events  (text/event-stream)
//   event: phase.updated | phase.created | phase.deleted | event.assigned
//          | event.unassigned | proposal.ready | reorder
//   data:  { phase_id?, event_id?, ts }
//
// Wire option B (WebSocket via existing Channel infra):
//   wss://.../ws/patients/{id}/phases  (token in subprotocol or query)
//
// The component contract is a single ``onEvent`` callback; nothing in
// the UI cares about the transport choice.

export type CarePhaseRealtimeEventKind =
  | "phase.created"
  | "phase.updated"
  | "phase.deleted"
  | "event.assigned"
  | "event.unassigned"
  | "proposal.ready"
  | "reorder";

export interface CarePhaseRealtimeEvent {
  kind: CarePhaseRealtimeEventKind;
  phase_id?: string;
  event_id?: string;
  ts: string;
}

export type CarePhaseRealtimeHandler = (ev: CarePhaseRealtimeEvent) => void;
export type Unsubscribe = () => void;

/**
 * Subscribe to live care-phase mutations for a patient.
 *
 * Returns a stable unsubscribe function. Safe to call from a React
 * effect — the helper guarantees idempotent teardown even if the
 * underlying transport never connected.
 *
 * Until the backend bus is wired this function is a no-op subscriber
 * (it does NOT throw, does NOT poll). Call sites should still degrade
 * gracefully via manual refresh.
 */
export function subscribeCarePhases(
  _patientId: string,
  _onEvent: CarePhaseRealtimeHandler,
): Unsubscribe {
  // TODO(care-timeline §8.6): replace with EventSource("/api/patients/
  //  ${patientId}/care-timeline/events") once the backend lands. Honour
  //  the existing JWT pattern from api.ts (header relay) and
  //  exponential-backoff on reconnect.
  let active = true;
  return () => {
    active = false;
    void active; // satisfy biome no-unused
  };
}
