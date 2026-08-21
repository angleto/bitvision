// Turning a clinical-event write rejection into a sentence the owner of the
// record can read.
//
// The backend answers these writes with an RFC 9457 problem body (see
// ``bvphoenix.middleware.problem_details``) whose members are merged at the
// TOP level, so a rejection looks like:
//
//   { "code": "anchor_not_clearable", "field": "planned_start_at",
//     "message": "planned_start_at defines this event's date and ...",
//     "type": ".../validation_failed", "title": "Validation failed",
//     "status": 422, "detail": "Validation failed", "instance": "/api/..." }
//
// Two consequences drive this module:
//
//   1. ``errorCode()`` in ``@/lib/api/core`` reads the ``type`` slug FIRST,
//      which for all of these is the generic ``validation_failed``. The
//      discriminating value is the ``code`` member, so we read that first
//      here rather than reuse it.
//   2. ``message`` is English server prose. It is a developer-grade fallback,
//      never the primary text: this platform's owner reads Italian.
//
// Rendering path: stable ``code`` → ``eventActions.serverError.<code>`` in
// both message catalogues → the server's ``message`` for anything the
// catalogue does not know yet → the transport-level description.

import { ApiError } from "@/lib/api/core";

/** Every stable ``code`` the clinical-event write paths emit, i.e. every one
 *  the message catalogues carry a localised sentence for. Kept as a literal
 *  tuple so a code added here without a translation is a lint-visible gap
 *  rather than an English sentence in the Italian UI. */
export const EVENT_ERROR_CODES = [
  // PATCH /clinical-events/{id}
  "use_amend_time",
  // POST /clinical-events/{id}/amend-time
  "nothing_to_amend",
  "wrong_anchor_for_status",
  "anchor_not_clearable",
  "event_date_is_derived",
  "end_before_start",
  "future_actual_time",
  "reason_required",
  "invalid_timezone",
  // POST /clinical-events
  "event_date_conflicts_with_anchor",
  // POST /clinical-events/{id}/{confirm,reschedule,complete,cancel,mark-missed}
  // ``assert_transition_allowed`` in services/clinical_events_fsm.py. Raised
  // as a bare HTTPException with a nested detail dict, which is why
  // ``eventErrorCode`` reads both shapes.
  "invalid_transition",
] as const;

export type EventErrorCode = (typeof EVENT_ERROR_CODES)[number];

function problemBody(e: unknown): Record<string, unknown> | null {
  if (!(e instanceof ApiError)) return null;
  const d = e.detail;
  return d && typeof d === "object" ? (d as Record<string, unknown>) : null;
}

/** The stable machine code, or null when this is not one of ours.
 *
 *  Accepts the nested ``detail.code`` shape too: a few endpoints raise the
 *  bare ``HTTPException(detail={"code": ...})`` without going through the
 *  problem-details helper, and the two shapes are indistinguishable to the
 *  caller. */
export function eventErrorCode(e: unknown): EventErrorCode | null {
  const body = problemBody(e);
  if (!body) return null;
  const nested = body.detail;
  const raw =
    typeof body.code === "string"
      ? body.code
      : nested &&
          typeof nested === "object" &&
          typeof (nested as { code?: unknown }).code === "string"
        ? (nested as { code: string }).code
        : null;
  return raw !== null && (EVENT_ERROR_CODES as readonly string[]).includes(raw)
    ? (raw as EventErrorCode)
    : null;
}

/** The server's own English sentence, when it carried one. Used only as the
 *  fallback for a code the catalogue does not know. */
export function serverErrorMessage(e: unknown): string | null {
  const body = problemBody(e);
  if (!body) return null;
  if (typeof body.message === "string" && body.message) return body.message;
  const nested = body.detail;
  if (nested && typeof nested === "object") {
    const m = (nested as { message?: unknown }).message;
    if (typeof m === "string" && m) return m;
  }
  // ``detail`` on a problem body is the long-form explanation; on a plain
  // FastAPI error it is the whole message.
  if (typeof nested === "string" && nested) return nested;
  return null;
}

/** ``useTranslations()`` narrowed to what this module needs. Declared
 *  structurally so the lib does not depend on next-intl's types. */
export interface Translate {
  (key: string): string;
  (key: string, values: Record<string, string | number>): string;
}

/**
 * The sentence to show the user for a failed clinical-event write.
 *
 * ``t`` must be bound to the ``eventActions`` namespace. Order:
 *   1. a known ``code``            → ``serverError.<code>``
 *   2. a precondition status       → ``serverError.etagMismatch`` / ``…Required``
 *   3. the server's own ``message``, prefixed with the status so a support
 *      request can quote something actionable
 *   4. the transport error text
 */
export function describeEventError(e: unknown, t: Translate): string {
  const code = eventErrorCode(e);
  if (code) return t(`serverError.${code}`);
  if (e instanceof ApiError) {
    if (e.status === 412) return t("serverError.etagMismatch");
    if (e.status === 428) return t("serverError.preconditionRequired");
    const message = serverErrorMessage(e);
    if (message) return t("serverError.withStatus", { status: e.status, message });
    return t("serverError.withStatus", { status: e.status, message: String(e.detail) });
  }
  if (e instanceof Error && e.message) return e.message;
  return t("serverError.unknown");
}
