// The two bodies the edit dialog puts on the wire.
//
// The load-bearing assertion is negative: the PATCH body must NEVER carry a
// temporal key. Since migration 0047 the server answers 422
// {"code": "use_amend_time"} to event_date / planned_* / timezone on PATCH,
// so a temporal key leaking back into the patch is the exact shape of the
// "editing the date returns an error" bug this flow was built to kill.

import { describe, expect, test } from "vitest";

import type { ClinicalEvent } from "@/lib/api_records";
import { buildAmendPayload, buildMetadataPatch, hasMetadataChanges } from "@/lib/event_edit";

const TEMPORAL_KEYS = [
  "event_date",
  "planned_start_at",
  "planned_end_at",
  "actual_start_at",
  "actual_end_at",
  "timezone",
];

function event(over: Partial<ClinicalEvent> = {}): ClinicalEvent {
  return {
    id: "11111111-1111-1111-1111-111111111111",
    patient_id: "22222222-2222-2222-2222-222222222222",
    kind: "outpatient_visit",
    event_date: "2026-09-01",
    title: "Cardiology check-up",
    body_part: null,
    code_loinc: null,
    code_snomed: null,
    narrative: null,
    imaging_study_id: null,
    etag: "etag-1",
    created_at: "2026-08-01T10:00:00Z",
    updated_at: "2026-08-01T10:00:00Z",
    event_status: "planned",
    // The server writes an offset form; Date#toISOString writes ".000Z".
    planned_start_at: "2026-09-01T08:00:00+00:00",
    planned_end_at: null,
    actual_start_at: null,
    actual_end_at: null,
    timezone: "Europe/Rome",
    location_struct: null,
    reminder_offsets_minutes: null,
    meeting_url: null,
    links: null,
    ...over,
  };
}

function metaDraft(over: Record<string, unknown> = {}) {
  const ev = event();
  return {
    kind: ev.kind,
    title: ev.title,
    note: "",
    bodyPart: "",
    facility: "",
    room: "",
    city: "",
    reminders: [] as number[],
    meetingUrl: "",
    links: [] as { label?: string; url: string }[],
    ...over,
  };
}

function timeDraft(over: Partial<Record<string, string>> = {}) {
  return {
    start: "",
    end: "",
    eventDate: "",
    timezone: "Europe/Rome",
    reason: "",
    ...over,
  };
}

describe("buildMetadataPatch", () => {
  test("never emits a temporal key, whatever changed", () => {
    const patch = buildMetadataPatch(
      event(),
      metaDraft({
        title: "Cardiology follow-up",
        note: "bring prior ECG",
        bodyPart: "heart",
        facility: "Main Hospital",
        reminders: [-1440, -120],
        meetingUrl: "https://meet.example/abc",
        links: [{ label: "Booking", url: "https://cup.example" }],
      }),
    );
    for (const k of TEMPORAL_KEYS) {
      expect(Object.keys(patch)).not.toContain(k);
    }
  });

  test("an untouched form produces no changes at all", () => {
    const patch = buildMetadataPatch(event(), metaDraft());
    expect(hasMetadataChanges(patch)).toBe(false);
    // Every key present must be undefined, i.e. JSON.stringify drops them.
    expect(JSON.parse(JSON.stringify(patch))).toEqual({});
  });

  test("only the fields that moved are sent", () => {
    const patch = buildMetadataPatch(event(), metaDraft({ title: "New title" }));
    expect(JSON.parse(JSON.stringify(patch))).toEqual({ title: "New title" });
  });

  test("clearing an optional text field sends an explicit null, not an empty string", () => {
    const patch = buildMetadataPatch(event({ body_part: "heart" }), metaDraft({ bodyPart: "" }));
    expect(patch.body_part).toBeNull();
  });

  test("reminder order does not count as a change", () => {
    const patch = buildMetadataPatch(
      event({ reminder_offsets_minutes: [-1440, -120] }),
      metaDraft({ reminders: [-120, -1440] }),
    );
    expect(hasMetadataChanges(patch)).toBe(false);
  });
});

describe("buildAmendPayload", () => {
  test("an unchanged anchor produces no amendment", () => {
    // The bug: "+00:00" vs ".000Z" made the dialog re-send
    // planned_start_at on every single save.
    const ev = event();
    const draft = timeDraft({ start: localInput("2026-09-01T08:00:00Z") });
    expect(buildAmendPayload(ev, draft)).toBeNull();
  });

  test("a moved planned anchor sends planned_start_at and no reason", () => {
    const ev = event();
    const draft = timeDraft({ start: localInput("2026-09-02T08:00:00Z") });
    const payload = buildAmendPayload(ev, draft);
    expect(payload).not.toBeNull();
    expect(payload?.planned_start_at).toBe("2026-09-02T08:00:00.000Z");
    expect(payload?.actual_start_at).toBeUndefined();
    expect(payload?.event_date).toBeUndefined();
    expect(payload?.reason).toBeUndefined();
  });

  test("a completed row sends the ACTUAL family, never the planned one", () => {
    const ev = event({
      event_status: "completed",
      planned_start_at: "2026-09-01T08:00:00+00:00",
      actual_start_at: "2026-09-01T09:30:00+00:00",
    });
    const draft = timeDraft({
      start: localInput("2026-08-15T09:30:00Z"),
      reason: "the exam was done two weeks earlier",
    });
    const payload = buildAmendPayload(ev, draft);
    expect(payload?.actual_start_at).toBe("2026-08-15T09:30:00.000Z");
    expect(payload).not.toHaveProperty("planned_start_at");
    expect(payload?.reason).toBe("the exam was done two weeks earlier");
  });

  test("a cancelled row is planned-family, matching the DB trigger", () => {
    const ev = event({ event_status: "cancelled" });
    const draft = timeDraft({ start: localInput("2026-09-05T08:00:00Z") });
    expect(buildAmendPayload(ev, draft)).toHaveProperty("planned_start_at");
  });

  test("a date-only row sends event_date and nothing anchored", () => {
    const ev = event({
      event_status: "completed",
      planned_start_at: null,
      actual_start_at: null,
      event_date: "2019-04-03",
      timezone: null,
    });
    const payload = buildAmendPayload(ev, timeDraft({ eventDate: "2019-04-02", timezone: "" }));
    expect(payload).toEqual({ event_date: "2019-04-02" });
  });

  test("an emptied date box means 'leave it alone', not 'erase the date'", () => {
    const ev = event({
      event_status: "completed",
      planned_start_at: null,
      actual_start_at: null,
      event_date: "2019-04-03",
      timezone: null,
    });
    expect(buildAmendPayload(ev, timeDraft({ eventDate: "", timezone: "" }))).toBeNull();
  });

  test("a timezone change alone is a valid amendment", () => {
    const ev = event();
    const payload = buildAmendPayload(
      ev,
      timeDraft({ start: localInput("2026-09-01T08:00:00Z"), timezone: "Europe/Berlin" }),
    );
    expect(payload).toEqual({ timezone: "Europe/Berlin" });
  });

  test("an emptied START box never sends a null, whatever the status", () => {
    // The server refuses to clear a start anchor (422
    // ``anchor_not_clearable``, every status): it is the timestamp
    // ``event_date`` is derived from, so removing it would strand the row on
    // a date computed from something that no longer exists. An empty box is
    // therefore "left alone", exactly like the date-only branch.
    for (const status of ["planned", "confirmed", "rescheduled", "cancelled"]) {
      const ev = event({ event_status: status as ClinicalEvent["event_status"] });
      expect(buildAmendPayload(ev, timeDraft({ start: "" }))).toBeNull();
    }
    const done = event({
      event_status: "completed",
      actual_start_at: "2026-09-01T09:30:00+00:00",
    });
    expect(buildAmendPayload(done, timeDraft({ start: "" }))).toBeNull();
  });

  test("an emptied END box still sends an explicit null", () => {
    // Asymmetric on purpose: "we do not know when it finished" is a state
    // the server accepts, and there is no other way to express it.
    const ev = event({ planned_end_at: "2026-09-01T09:00:00+00:00" });
    const payload = buildAmendPayload(
      ev,
      timeDraft({ start: localInput("2026-09-01T08:00:00Z"), end: "" }),
    );
    expect(payload).toEqual({ planned_end_at: null });
  });
});

/** ISO instant → the local wall-clock string a ``datetime-local`` input
 *  holds, so these tests describe what the USER sees in the box rather
 *  than what the runner's timezone happens to be. */
function localInput(iso: string): string {
  const d = new Date(iso);
  const pad = (n: number): string => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}
