// The 422 bodies the clinical-event write paths emit, and the sentence the
// user ends up reading.
//
// The trap this file pins down: the backend's RFC 9457 handler merges
// ``HTTPException.detail`` at the TOP level and stamps a generic
// ``type: .../validation_failed``. ``errorCode()`` in @/lib/api/core reads
// ``type`` first, so on these bodies it answers "validation_failed" for every
// single one of them — useless for branching. The discriminator is ``code``.

import { describe, expect, test } from "vitest";

import { ApiError } from "@/lib/api/core";
import { describeEventError, eventErrorCode, serverErrorMessage } from "@/lib/event_errors";

/** A body shaped exactly as ``_build_body`` in
 *  bvphoenix/middleware/problem_details.py produces it. */
function problem(status: number, extra: Record<string, unknown>): ApiError {
  return new ApiError(status, {
    ...extra,
    type: "https://bitvision.example/errors/validation_failed",
    title: "Validation failed",
    status,
    detail: "Validation failed",
    instance: "/api/clinical-events/x/amend-time",
  });
}

/** ``useTranslations("eventActions")`` reduced to what the mapper calls:
 *  echoes the key so the assertions read as "which message was chosen". */
const t = ((key: string, values?: Record<string, string | number>): string =>
  values ? `${key}(${JSON.stringify(values)})` : key) as Parameters<typeof describeEventError>[1];

describe("eventErrorCode", () => {
  test("reads the code member, not the generic problem type", () => {
    const e = problem(422, { code: "anchor_not_clearable", field: "planned_start_at" });
    expect(eventErrorCode(e)).toBe("anchor_not_clearable");
  });

  test("every documented code is recognised", () => {
    for (const code of [
      "use_amend_time",
      "nothing_to_amend",
      "wrong_anchor_for_status",
      "anchor_not_clearable",
      "event_date_is_derived",
      "end_before_start",
      "future_actual_time",
      "reason_required",
      "invalid_timezone",
      "event_date_conflicts_with_anchor",
      "invalid_transition",
    ]) {
      expect(eventErrorCode(problem(422, { code }))).toBe(code);
    }
  });

  test("accepts the nested detail.code shape too", () => {
    expect(eventErrorCode(new ApiError(422, { detail: { code: "reason_required" } }))).toBe(
      "reason_required",
    );
  });

  test("an unknown code, a plain error and a non-ApiError all answer null", () => {
    expect(eventErrorCode(problem(422, { code: "something_new" }))).toBeNull();
    expect(eventErrorCode(new ApiError(500, "boom"))).toBeNull();
    expect(eventErrorCode(new Error("network"))).toBeNull();
  });
});

describe("serverErrorMessage", () => {
  test("prefers the message member over the canonical detail line", () => {
    const e = problem(422, { code: "end_before_start", message: "the end must follow the start" });
    expect(serverErrorMessage(e)).toBe("the end must follow the start");
  });

  test("falls back to a plain string detail", () => {
    expect(
      serverErrorMessage(new ApiError(422, { detail: "clinical_event_id not on this patient" })),
    ).toBe("clinical_event_id not on this patient");
  });
});

describe("describeEventError", () => {
  test("a known code is localised, and the server's English prose is dropped", () => {
    const e = problem(422, {
      code: "invalid_timezone",
      message: "timezone must be an IANA name, e.g. 'Europe/Rome'",
    });
    expect(describeEventError(e, t)).toBe("serverError.invalid_timezone");
  });

  test("412 and 428 get their own sentences instead of a raw status", () => {
    expect(describeEventError(new ApiError(412, { code: "etag_mismatch" }), t)).toBe(
      "serverError.etagMismatch",
    );
    expect(describeEventError(new ApiError(428, {}), t)).toBe("serverError.preconditionRequired");
  });

  test("an unknown structured error still quotes the status and the server text", () => {
    const e = problem(422, { code: "brand_new_rule", message: "not allowed" });
    expect(describeEventError(e, t)).toBe(
      'serverError.withStatus({"status":422,"message":"not allowed"})',
    );
  });

  test("the FSM rejection from a transition endpoint is localised too", () => {
    // ``assert_transition_allowed`` raises a bare HTTPException whose detail
    // is the dict, so this arrives nested. It is the code the five
    // transition verbs (confirm / reschedule / complete / cancel /
    // mark-missed) fail with, and the dialog driving them used to render the
    // English ``message`` verbatim.
    const e = new ApiError(422, {
      detail: {
        code: "invalid_transition",
        from: "completed",
        to: "confirmed",
        message: "event_status transition 'completed' -> 'confirmed' is not allowed",
      },
    });
    expect(eventErrorCode(e)).toBe("invalid_transition");
    expect(describeEventError(e, t)).toBe("serverError.invalid_transition");
  });

  test("a missing Idempotency-Key on a transition reads as a precondition, not a raw 428", () => {
    expect(describeEventError(new ApiError(428, "Idempotency-Key header required"), t)).toBe(
      "serverError.preconditionRequired",
    );
  });

  test("a transport failure surfaces its own message", () => {
    expect(describeEventError(new Error("Failed to fetch"), t)).toBe("Failed to fetch");
  });

  test("something with no message at all still produces a sentence", () => {
    expect(describeEventError({}, t)).toBe("serverError.unknown");
  });
});
