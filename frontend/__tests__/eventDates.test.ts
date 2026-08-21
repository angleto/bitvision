// The date rules that the whole event UI reads from.
//
// These mirror ``backend/src/bvphoenix/api/clinical_events.py`` and the
// ``fn_ce_derive_event_date`` trigger (migration 0047). If one of these
// assertions changes, the DB trigger changed with it, or one of the two
// sides has drifted.

import { describe, expect, test } from "vitest";

import {
  WHEN_CAPTION,
  anchorFamily,
  authoritativeInstant,
  dateSource,
  derivedEventDate,
  formatInZone,
  isDateOnly,
  isValidTimezone,
  localInputZoneMismatch,
  reasonRequired,
  sameInstant,
  secondaryPlannedInstant,
  timezoneOptions,
  validateAmend,
  viewerTimezone,
  whenCaptionKey,
  whenSource,
} from "@/lib/event_dates";

// Local noon, deliberately: the date-only cases below assert that "today"
// reads as neither past nor future, and that only holds if ``now`` sits
// inside the RUNNER's local day. A fixed UTC instant would make the same
// assertion pass in Rome and fail in Auckland.
const NOW = new Date(2026, 7, 21, 12, 0, 0, 0).getTime();

describe("anchorFamily", () => {
  // All six statuses of the EventStatus union, explicitly, so a status
  // silently moving family shows up as a failed assertion.
  test.each([
    ["planned", "planned"],
    ["confirmed", "planned"],
    ["rescheduled", "planned"],
    ["cancelled", "planned"],
    ["completed", "actual"],
    ["missed", "actual"],
  ])("%s belongs to the %s family", (status, family) => {
    expect(anchorFamily(status)).toBe(family);
  });

  test("an absent status reads as the backend default (completed → actual)", () => {
    expect(anchorFamily(undefined)).toBe("actual");
    expect(anchorFamily(null)).toBe("actual");
  });

  test("cancelled and rescheduled are planned-family, not actual", () => {
    // The regression this file exists for: the timeline adapter used to
    // take actual_start_at for anything outside planned/confirmed.
    expect(anchorFamily("cancelled")).toBe("planned");
    expect(anchorFamily("rescheduled")).toBe("planned");
  });
});

describe("authoritativeInstant", () => {
  test("planned family reads planned_start_at even when actual_start_at is set", () => {
    expect(
      authoritativeInstant({
        event_status: "cancelled",
        planned_start_at: "2026-09-01T08:00:00Z",
        actual_start_at: "2026-08-01T08:00:00Z",
      }),
    ).toBe("2026-09-01T08:00:00Z");
  });

  test("actual family reads actual_start_at even when planned_start_at is set", () => {
    expect(
      authoritativeInstant({
        event_status: "completed",
        planned_start_at: "2026-09-01T08:00:00Z",
        actual_start_at: "2026-08-01T08:00:00Z",
      }),
    ).toBe("2026-08-01T08:00:00Z");
  });

  test("null when the family's anchor is missing", () => {
    expect(
      authoritativeInstant({ event_status: "missed", planned_start_at: "2026-09-01T08:00:00Z" }),
    ).toBeNull();
  });
});

describe("isDateOnly / dateSource", () => {
  test("a DICOM-style import with only event_date is date-only", () => {
    const ev = { event_status: "completed", event_date: "2019-04-03" };
    expect(isDateOnly(ev)).toBe(true);
    expect(dateSource(ev)).toBe("dateOnly");
  });

  test("a completed event with an actual anchor is not date-only", () => {
    const ev = { event_status: "completed", actual_start_at: "2026-08-01T08:00:00Z" };
    expect(isDateOnly(ev)).toBe(false);
    expect(dateSource(ev)).toBe("actual");
  });

  test("a planned event whose anchor is missing is date-only, not planned-sourced", () => {
    const ev = { event_status: "planned", event_date: "2026-09-01" };
    expect(isDateOnly(ev)).toBe(true);
    expect(dateSource(ev)).toBe("dateOnly");
  });

  test("a cancelled event keeps its planned anchor", () => {
    const ev = { event_status: "cancelled", planned_start_at: "2026-09-01T08:00:00Z" };
    expect(isDateOnly(ev)).toBe(false);
    expect(dateSource(ev)).toBe("planned");
  });
});

describe("validateAmend", () => {
  test("nothing supplied is valid", () => {
    expect(validateAmend({ family: "actual", now: NOW })).toEqual({});
  });

  test("an actual time in the future is an error", () => {
    expect(validateAmend({ family: "actual", start: "2026-08-22T09:00:00Z", now: NOW })).toEqual({
      errorKey: "amendFutureOnPast",
    });
  });

  test("an actual time in the past is fine", () => {
    expect(validateAmend({ family: "actual", start: "2026-07-01T09:00:00Z", now: NOW })).toEqual(
      {},
    );
  });

  test("a planned time in the future is fine", () => {
    expect(validateAmend({ family: "planned", start: "2026-09-01T09:00:00Z", now: NOW })).toEqual(
      {},
    );
  });

  test("a planned time in the past only WARNS: backfilling a booking is legitimate", () => {
    expect(validateAmend({ family: "planned", start: "2026-07-01T09:00:00Z", now: NOW })).toEqual({
      warningKey: "amendPastOnPlanned",
    });
  });

  test("end before start is an error, and outranks the future check", () => {
    expect(
      validateAmend({
        family: "actual",
        start: "2026-08-22T09:00:00Z",
        end: "2026-08-22T08:00:00Z",
        now: NOW,
      }),
    ).toEqual({ errorKey: "amendEndBeforeStart" });
  });

  test("end equal to start is accepted", () => {
    expect(
      validateAmend({
        family: "actual",
        start: "2026-07-01T09:00:00Z",
        end: "2026-07-01T09:00:00Z",
        now: NOW,
      }),
    ).toEqual({});
  });

  test("an unparseable value is reported rather than silently ignored", () => {
    expect(validateAmend({ family: "actual", start: "not a date", now: NOW })).toEqual({
      errorKey: "amendInvalidDate",
    });
  });

  test("a date-only value spanning today is neither past nor future", () => {
    // A DATE has no instant: treating it as UTC midnight would make today
    // read as "in the past" for a planned row and shift the day westwards.
    const today = "2026-08-21";
    expect(validateAmend({ family: "actual", start: today, now: NOW })).toEqual({});
    expect(validateAmend({ family: "planned", start: today, now: NOW })).toEqual({});
  });

  test("a date-only value on a later day is still future for an actual row", () => {
    expect(validateAmend({ family: "actual", start: "2026-08-23", now: NOW })).toEqual({
      errorKey: "amendFutureOnPast",
    });
  });
});

describe("reasonRequired", () => {
  test("realised facts need a reason", () => {
    expect(reasonRequired("actual", false)).toBe(true);
    expect(reasonRequired("planned", true)).toBe(true);
  });

  test("moving a plan that has not happened yet does not", () => {
    expect(reasonRequired("planned", false)).toBe(false);
  });
});

describe("sameInstant", () => {
  test("the server's +00:00 and toISOString's .000Z are the same moment", () => {
    // The bug that made the edit dialog re-send planned_start_at on every
    // save: same instant, different text.
    expect(sameInstant("2026-08-21T10:00:00+00:00", "2026-08-21T10:00:00.000Z")).toBe(true);
  });

  test("two absent values are equal, one absent is not", () => {
    expect(sameInstant(null, "")).toBe(true);
    expect(sameInstant(undefined, null)).toBe(true);
    expect(sameInstant(null, "2026-08-21T10:00:00Z")).toBe(false);
  });

  test("different instants are different", () => {
    expect(sameInstant("2026-08-21T10:00:00Z", "2026-08-21T11:00:00Z")).toBe(false);
  });
});

describe("formatInZone", () => {
  test("renders the instant in the event's zone, not the viewer's", () => {
    const rome = formatInZone("2026-08-21T07:00:00Z", "en-GB", "Europe/Rome");
    const utc = formatInZone("2026-08-21T07:00:00Z", "en-GB", "UTC");
    expect(rome).toContain("09:00");
    expect(utc).toContain("07:00");
  });

  test("a DATE is never shifted by a timezone", () => {
    expect(formatInZone("2019-04-03", "en-GB", "America/Los_Angeles")).toContain("2019");
    expect(formatInZone("2019-04-03", "en-GB", "America/Los_Angeles")).toContain("3");
  });

  test("an unusable IANA name falls back instead of throwing", () => {
    expect(() => formatInZone("2026-08-21T07:00:00Z", "en-GB", "Nowhere/Fake")).not.toThrow();
    expect(formatInZone("2026-08-21T07:00:00Z", "en-GB", "Nowhere/Fake")).not.toBe("");
  });

  test("an absent value renders empty", () => {
    expect(formatInZone(null, "en-GB", "UTC")).toBe("");
  });
});

describe("whenSource / secondaryPlannedInstant", () => {
  // The regression these two exist for: ``mark-missed`` never writes
  // ``actual_start_at``, so a missed appointment is actual-family with a
  // NULL anchor. The drawer captioned it "date only, no time recorded" and
  // dropped the booked time entirely — factually wrong, the appointment did
  // have a time and the row still carries it.
  const missed = {
    event_status: "missed",
    planned_start_at: "2026-09-01T08:00:00Z",
    actual_start_at: null,
    event_date: "2026-09-01",
  };

  test("a missed appointment is a missing anchor, not a date-only import", () => {
    expect(isDateOnly(missed)).toBe(true);
    expect(dateSource(missed)).toBe("dateOnly");
    expect(whenSource(missed)).toBe("missingAnchor");
  });

  test("a genuine date-only import stays date-only", () => {
    const dicom = { event_status: "completed", event_date: "2019-04-03" };
    expect(whenSource(dicom)).toBe("dateOnly");
    expect(secondaryPlannedInstant(dicom)).toBeNull();
  });

  test("the booked time survives on a missed row", () => {
    expect(secondaryPlannedInstant(missed)).toBe("2026-09-01T08:00:00Z");
  });

  test("a completed row shows what it was booked for alongside what happened", () => {
    const done = {
      event_status: "completed",
      planned_start_at: "2026-09-01T08:00:00Z",
      actual_start_at: "2026-09-03T10:15:00Z",
    };
    expect(whenSource(done)).toBe("actual");
    expect(secondaryPlannedInstant(done)).toBe("2026-09-01T08:00:00Z");
  });

  test("a planned row does not repeat its own anchor as a secondary time", () => {
    const planned = { event_status: "planned", planned_start_at: "2026-09-01T08:00:00Z" };
    expect(whenSource(planned)).toBe("planned");
    expect(secondaryPlannedInstant(planned)).toBeNull();
  });

  test("the same instant written two ways is not a second time", () => {
    const done = {
      event_status: "completed",
      planned_start_at: "2026-09-01T08:00:00+00:00",
      actual_start_at: "2026-09-01T08:00:00.000Z",
    };
    expect(secondaryPlannedInstant(done)).toBeNull();
  });
});

describe("isValidTimezone", () => {
  test("accepts IANA names the engine can resolve", () => {
    expect(isValidTimezone("Europe/Rome")).toBe(true);
    expect(isValidTimezone("UTC")).toBe(true);
    expect(isValidTimezone("America/Argentina/Buenos_Aires")).toBe(true);
  });

  test("rejects a plausible-looking name that does not exist", () => {
    // The typo the server answers with 422 invalid_timezone.
    expect(isValidTimezone("Europe/Roma")).toBe(false);
    expect(isValidTimezone("Nowhere/Fake")).toBe(false);
  });

  test("rejects a bare UTC offset, which ZoneInfo would refuse server-side", () => {
    expect(isValidTimezone("+01:00")).toBe(false);
  });

  test("rejects an empty value: callers decide whether unset is allowed", () => {
    expect(isValidTimezone("")).toBe(false);
    expect(isValidTimezone(null)).toBe(false);
    expect(isValidTimezone(undefined)).toBe(false);
  });
});

describe("timezoneOptions", () => {
  test("either lists real zones or degrades to an empty list", () => {
    const zones = timezoneOptions();
    expect(Array.isArray(zones)).toBe(true);
    if (zones.length > 0) expect(zones).toContain("Europe/Rome");
  });
});

describe("derivedEventDate", () => {
  // Mirrors fn_ce_derive_event_date: (anchor AT TIME ZONE tz)::date.
  test("projects the instant onto the event's own calendar", () => {
    expect(derivedEventDate("2026-09-01T22:30:00Z", "Europe/Rome")).toBe("2026-09-02");
    expect(derivedEventDate("2026-09-01T22:30:00Z", "UTC")).toBe("2026-09-01");
  });

  test("an absent zone means UTC, exactly as the trigger's COALESCE does", () => {
    expect(derivedEventDate("2026-09-01T22:30:00Z", null)).toBe("2026-09-01");
    expect(derivedEventDate("2026-09-01T22:30:00Z", "")).toBe("2026-09-01");
  });

  test("a zone west of Greenwich can move the day backwards", () => {
    expect(derivedEventDate("2026-09-02T03:00:00Z", "America/Los_Angeles")).toBe("2026-09-01");
  });

  test("an unusable zone falls back to UTC instead of throwing", () => {
    expect(derivedEventDate("2026-09-01T22:30:00Z", "Nowhere/Fake")).toBe("2026-09-01");
  });

  test("a DATE-only value is already the answer", () => {
    expect(derivedEventDate("2019-04-03", "Europe/Rome")).toBe("2019-04-03");
  });

  test("empty and unparseable inputs render nothing", () => {
    expect(derivedEventDate("", "UTC")).toBe("");
    expect(derivedEventDate(null, "UTC")).toBe("");
    expect(derivedEventDate("not a date", "UTC")).toBe("");
  });
});

describe("whenCaptionKey", () => {
  // The caption table used to be copied into EditEventDialog and
  // EventDrawer, one click apart: the same row could be captioned "no time
  // recorded" in one and show the booked time in the other. One table now,
  // and these four cases are what both surfaces read.
  test.each([
    [{ event_status: "planned", planned_start_at: "2026-09-01T08:00:00Z" }, "dateSourcePlanned"],
    [{ event_status: "completed", actual_start_at: "2026-09-01T08:00:00Z" }, "dateSourceActual"],
    [
      {
        event_status: "missed",
        planned_start_at: "2026-09-01T08:00:00Z",
        event_date: "2026-09-01",
      },
      "dateSourceMissingAnchor",
    ],
    [{ event_status: "completed", event_date: "2019-04-03" }, "dateSourceDateOnly"],
  ])("%o is captioned %s", (event, key) => {
    expect(whenCaptionKey(event)).toBe(key);
    // ... and it is exactly whenSource's answer, put through one table.
    expect(WHEN_CAPTION[whenSource(event)]).toBe(key);
  });
});

describe("viewerTimezone", () => {
  test("answers a zone this engine can actually resolve", () => {
    const tz = viewerTimezone();
    expect(typeof tz).toBe("string");
    expect(isValidTimezone(tz)).toBe(true);
  });
});

describe("localInputZoneMismatch", () => {
  // ``<input type="datetime-local">`` has no timeZone option: it always
  // shows the DEVICE's wall clock. Everything else in the same fieldset
  // renders in the event's zone. This is what tells the form to say so.
  const JULY = Date.parse("2026-07-15T10:00:00Z");
  const JANUARY = Date.parse("2026-01-15T10:00:00Z");

  test("names the viewer's zone when the two clocks differ", () => {
    // The reported case: London editing a 09:00 Europe/Rome appointment.
    expect(localInputZoneMismatch("Europe/Rome", JULY, "Europe/London")).toBe("Europe/London");
  });

  test("stays quiet when the viewer already sits in the event's zone", () => {
    expect(localInputZoneMismatch("Europe/Rome", JULY, "Europe/Rome")).toBeNull();
  });

  test("compares offsets, not names: same clock, different name, no hint", () => {
    expect(localInputZoneMismatch("Europe/Vatican", JULY, "Europe/Rome")).toBeNull();
  });

  test("judged at the instant being edited, so DST is not guessed", () => {
    // Phoenix never moves; Denver does. Same offset in January, one hour
    // apart in July, and the hint has to follow the instant, not the clock
    // on the day the correction is typed.
    expect(localInputZoneMismatch("America/Denver", JANUARY, "America/Phoenix")).toBeNull();
    expect(localInputZoneMismatch("America/Denver", JULY, "America/Phoenix")).toBe(
      "America/Phoenix",
    );
  });

  test("a row with no usable zone renders in the viewer's zone anyway: no hint", () => {
    // formatInZone falls back to the viewer's zone for these, so there is
    // nothing on screen to disagree with the input box.
    expect(localInputZoneMismatch("", JULY, "Europe/London")).toBeNull();
    expect(localInputZoneMismatch(null, JULY, "Europe/London")).toBeNull();
    expect(localInputZoneMismatch(undefined, JULY, "Europe/London")).toBeNull();
    expect(localInputZoneMismatch("Europe/Roma", JULY, "Europe/London")).toBeNull();
  });

  test("an unusable viewer zone degrades to silence, never to a wrong claim", () => {
    expect(localInputZoneMismatch("Europe/Rome", JULY, "Nowhere/Fake")).toBeNull();
  });

  test("half-hour and three-quarter-hour zones are compared to the minute", () => {
    expect(localInputZoneMismatch("Asia/Kolkata", JULY, "Asia/Kathmandu")).toBe("Asia/Kathmandu");
    expect(localInputZoneMismatch("Asia/Kolkata", JULY, "Asia/Colombo")).toBeNull();
  });
});
