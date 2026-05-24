// Client-side mirror of the patient_task FSM. The backend
// (services/patient_tasks_fsm.py) is the single source of truth; we
// mirror it here only to gate UI buttons (disable / hide transitions
// that the backend would reject with 422 invalid_transition anyway).
//
// Keep aligned manually with the backend FSM. If the two drift, the
// backend wins: the UI may show a button that 422s on click, never the
// reverse (a button that the FE thinks should be allowed but the
// backend rejects = an annoying but recoverable UX; a button the FE
// hides that the backend would have allowed = silent feature loss).

import type { TaskStatus, TaskTransitionVerb } from "@/lib/api_records";

const ALLOWED: Record<TaskStatus, ReadonlySet<TaskStatus>> = {
  pending: new Set<TaskStatus>(["in_progress", "snoozed", "done", "dropped"]),
  in_progress: new Set<TaskStatus>(["snoozed", "done", "dropped"]),
  snoozed: new Set<TaskStatus>(["pending", "in_progress"]),
  done: new Set<TaskStatus>(["pending"]),
  dropped: new Set<TaskStatus>(["pending"]),
};

/** Status values reachable from ``from_status`` in one transition. */
export function allowedNext(fromStatus: TaskStatus): ReadonlySet<TaskStatus> {
  return ALLOWED[fromStatus] ?? new Set<TaskStatus>();
}

/** ``true`` iff the FSM allows ``from_status → to_status``. */
export function isTransitionAllowed(fromStatus: TaskStatus, toStatus: TaskStatus): boolean {
  return allowedNext(fromStatus).has(toStatus);
}

/** Verb → target status mapping. Mirrors
 *  ``services/patient_tasks_fsm.ACTION_TO_TARGET_STATUS``. ``wake``
 *  defaults to ``pending``; the snooze/wake roundtrip optionally
 *  resumes ``in_progress`` via the ``resume_in_progress`` body flag,
 *  which is decided at action-time, not by this map. */
export const VERB_TARGET_STATUS: Record<TaskTransitionVerb, TaskStatus> = {
  start: "in_progress",
  snooze: "snoozed",
  wake: "pending",
  complete: "done",
  drop: "dropped",
  reopen: "pending",
};

/** Pre-conditions per verb: from which source status the verb is
 *  meaningful. Different verbs share the same target (``wake`` and
 *  ``reopen`` both produce ``pending``), so the verb-to-target map
 *  alone can't distinguish them — we also need the legal "from" set.
 *  Mirrors the backend transition sub-resources (each one's FSM
 *  guard rejects every non-listed source with 422 invalid_transition). */
const VERB_FROM_STATUS: Record<TaskTransitionVerb, ReadonlySet<TaskStatus>> = {
  start: new Set<TaskStatus>(["pending"]),
  snooze: new Set<TaskStatus>(["pending", "in_progress"]),
  wake: new Set<TaskStatus>(["snoozed"]),
  complete: new Set<TaskStatus>(["pending", "in_progress"]),
  drop: new Set<TaskStatus>(["pending", "in_progress"]),
  reopen: new Set<TaskStatus>(["done", "dropped"]),
};

/** Verbs that the FSM allows from the given current status. Used by
 *  TaskDrawer to render only the enabled transition buttons. A verb
 *  is offered when (a) its pre-condition set contains the current
 *  status AND (b) its target status is in ``allowedNext(...)`` — the
 *  pre-condition alone would suffice, but we double-check against
 *  ``allowedNext`` so a future drift in the target map fails closed. */
export function allowedVerbs(fromStatus: TaskStatus): TaskTransitionVerb[] {
  const reachable = allowedNext(fromStatus);
  const out: TaskTransitionVerb[] = [];
  for (const verb of ["start", "snooze", "wake", "complete", "drop", "reopen"] as const) {
    if (!VERB_FROM_STATUS[verb].has(fromStatus)) continue;
    if (!reachable.has(VERB_TARGET_STATUS[verb])) continue;
    out.push(verb);
  }
  return out;
}
