// TimelineRail / TimelineDaySwimlane — unit tests for the rail
// primitives (group by date, sort within day, date label formatter,
// event → rail-item adapter). These are pure functions, so no DOM
// environment is needed; the suite stays in the existing node-only
// vitest harness.
//
// The visual layout (horizontal swimlanes, mobile stacking, dot pill
// styling) is validated by hand in the browser; this suite asserts
// the contract on which the rendered output depends — same shape /
// same ordering / same locale formatting.

import { describe, expect, test } from "vitest";

import { formatDateLabel } from "@/components/timeline/TimelineDaySwimlane";
import { eventToRailItem, eventsToRailItems } from "@/components/timeline/eventAdapter";
import { UNDATED_BUCKET, groupByDate } from "@/components/timeline/groupByDate";
import type { TimelineEventItem, TimelineRailItem } from "@/components/timeline/types";
import type { TimelineEvent } from "@/lib/api_records";

// --------------------------------------------------------------------
// Helpers
// --------------------------------------------------------------------

function makeEvent(overrides: Partial<TimelineEvent> = {}): TimelineEvent {
  return {
    id: overrides.id ?? "ev-1",
    patient_id: "p-1",
    kind: "outpatient_visit",
    event_date: overrides.event_date ?? "2026-06-15",
    title: overrides.title ?? "Visita oncologica",
    body_part: null,
    code_loinc: null,
    code_snomed: null,
    narrative: null,
    phase_id: null,
    phase_assigned_by: null,
    phase_assignment_confidence: null,
    target: { url: "/events/ev-1", mcp_uri: "mcp://event/ev-1" } as TimelineEvent["target"],
    etag: "etag-1",
    event_status: overrides.event_status ?? "completed",
    planned_start_at: overrides.planned_start_at ?? null,
    actual_start_at: overrides.actual_start_at ?? null,
    timezone: null,
    ...overrides,
  };
}

function makeItem(
  id: string,
  dateKey: string | null,
  sortKey: string | null = null,
): TimelineRailItem<{ id: string }> {
  return {
    id,
    source: "event",
    dateKey,
    sortKey,
    color: "#000",
    payload: { id },
  };
}

// --------------------------------------------------------------------
// groupByDate
// --------------------------------------------------------------------

describe("groupByDate", () => {
  test("buckets items by dateKey, ordered descending by default", () => {
    const buckets = groupByDate([
      makeItem("a", "2026-06-15"),
      makeItem("b", "2026-06-16"),
      makeItem("c", "2026-06-15"),
    ]);
    expect(buckets.map((b) => b.dateKey)).toEqual(["2026-06-16", "2026-06-15"]);
    expect(buckets[1].items.map((i) => i.id)).toEqual(["a", "c"]);
  });

  test("respects ascending order when requested", () => {
    const buckets = groupByDate([makeItem("a", "2026-06-15"), makeItem("b", "2026-06-16")], "asc");
    expect(buckets.map((b) => b.dateKey)).toEqual(["2026-06-15", "2026-06-16"]);
  });

  test("intra-day sort is ASC by sortKey, nulls last regardless of outer dir", () => {
    const buckets = groupByDate(
      [
        makeItem("late", "2026-06-15", "2026-06-15T14:30:00Z"),
        makeItem("early", "2026-06-15", "2026-06-15T09:00:00Z"),
        makeItem("unknown", "2026-06-15", null),
      ],
      "desc",
    );
    expect(buckets).toHaveLength(1);
    expect(buckets[0].items.map((i) => i.id)).toEqual(["early", "late", "unknown"]);
  });

  test("undated items go to the trailing UNDATED_BUCKET", () => {
    const buckets = groupByDate([
      makeItem("dated", "2026-06-15"),
      makeItem("u1", null),
      makeItem("u2", null),
    ]);
    expect(buckets[buckets.length - 1].dateKey).toBe(UNDATED_BUCKET);
    expect(buckets[buckets.length - 1].undated).toBe(true);
    expect(buckets[buckets.length - 1].items.map((i) => i.id)).toEqual(["u1", "u2"]);
  });

  test("empty input returns empty bucket list", () => {
    expect(groupByDate([])).toEqual([]);
  });

  test("handles many items in the same bucket without losing any", () => {
    const items = Array.from({ length: 12 }, (_, i) =>
      makeItem(`x${i}`, "2026-06-15", `2026-06-15T${String(i).padStart(2, "0")}:00:00Z`),
    );
    const buckets = groupByDate(items);
    expect(buckets).toHaveLength(1);
    expect(buckets[0].items).toHaveLength(12);
    // Chronological order preserved across the wrap boundary.
    expect(buckets[0].items[0].id).toBe("x0");
    expect(buckets[0].items[11].id).toBe("x11");
  });
});

// --------------------------------------------------------------------
// formatDateLabel
// --------------------------------------------------------------------

describe("formatDateLabel", () => {
  test("Italian format: weekday + day + short month + year", () => {
    expect(formatDateLabel("2026-06-15", "it")).toMatch(/^[a-z]{3} 15 giu 2026$/);
    expect(formatDateLabel("2024-12-25", "it")).toMatch(/^[a-z]{3} 25 dic 2024$/);
  });

  test("Italian weekday is one of the seven short tokens", () => {
    const label = formatDateLabel("2026-06-15", "it");
    const weekday = label.split(" ")[0];
    expect(["dom", "lun", "mar", "mer", "gio", "ven", "sab"]).toContain(weekday);
  });

  test("English locale uses Intl.DateTimeFormat", () => {
    const out = formatDateLabel("2026-06-15", "en");
    // Don't pin the exact string (locale data varies), but the year
    // must appear and the day number too.
    expect(out).toContain("2026");
    expect(out).toContain("15");
  });

  test("falls back to the raw key when the date is malformed", () => {
    expect(formatDateLabel("not-a-date", "it")).toBe("not-a-date");
  });
});

// --------------------------------------------------------------------
// eventToRailItem
// --------------------------------------------------------------------

describe("eventToRailItem", () => {
  test("planned event uses planned_start_at for both dateKey and sortKey", () => {
    const ev = makeEvent({
      id: "ev-planned",
      event_status: "planned",
      event_date: "2026-06-15",
      planned_start_at: "2026-06-15T09:30:00Z",
    });
    const item = eventToRailItem(ev, "#abc");
    expect(item.dateKey).toBe("2026-06-15");
    expect(item.sortKey).toBe("2026-06-15T09:30:00Z");
    expect(item.color).toBe("#abc");
    expect(item.source).toBe("event");
  });

  test("completed event with actual_start_at picks the actual timestamp", () => {
    const ev = makeEvent({
      id: "ev-completed",
      event_status: "completed",
      event_date: "2024-05-20",
      actual_start_at: "2024-05-20T14:30:00Z",
    });
    const item = eventToRailItem(ev, "#000");
    expect(item.dateKey).toBe("2024-05-20");
    expect(item.sortKey).toBe("2024-05-20T14:30:00Z");
  });

  test("DATE-only event falls back to event_date with null sortKey", () => {
    const ev = makeEvent({
      id: "ev-date-only",
      event_status: "completed",
      event_date: "2023-01-10",
      planned_start_at: null,
      actual_start_at: null,
    });
    const item = eventToRailItem(ev, "#000");
    expect(item.dateKey).toBe("2023-01-10");
    expect(item.sortKey).toBeNull();
  });

  test("undated event lands in the undated bucket (dateKey null)", () => {
    const ev = makeEvent({
      id: "ev-undated",
      event_status: "completed",
      event_date: null,
      planned_start_at: null,
      actual_start_at: null,
    });
    const item = eventToRailItem(ev, "#000");
    expect(item.dateKey).toBeNull();
    expect(item.sortKey).toBeNull();
  });

  test("eventsToRailItems maps the whole list preserving order", () => {
    const events = [
      makeEvent({ id: "a", event_date: "2026-06-15" }),
      makeEvent({ id: "b", event_date: "2026-06-16" }),
    ];
    const items = eventsToRailItems(events, "#fff");
    expect(items.map((i) => i.id)).toEqual(["a", "b"]);
    expect(items.every((i) => i.color === "#fff")).toBe(true);
  });
});

// --------------------------------------------------------------------
// Integration: rail item pipeline
// --------------------------------------------------------------------

describe("rail-item pipeline (event → rail-item → grouped buckets)", () => {
  test("multiple events on the same day collapse into a single swimlane", () => {
    const events = [
      makeEvent({
        id: "morning",
        event_status: "planned",
        event_date: "2026-06-15",
        planned_start_at: "2026-06-15T09:00:00Z",
      }),
      makeEvent({
        id: "afternoon",
        event_status: "planned",
        event_date: "2026-06-15",
        planned_start_at: "2026-06-15T14:30:00Z",
      }),
    ];
    const items = eventsToRailItems(events, "#000") as TimelineEventItem[];
    const buckets = groupByDate(items);
    expect(buckets).toHaveLength(1);
    expect(buckets[0].items.map((i) => i.id)).toEqual(["morning", "afternoon"]);
  });

  test("desc outer order keeps newer dates first while intra-day stays chronological", () => {
    const events = [
      makeEvent({
        id: "may-second",
        event_date: "2024-05-20",
        actual_start_at: "2024-05-20T14:00:00Z",
      }),
      makeEvent({
        id: "may-first",
        event_date: "2024-05-20",
        actual_start_at: "2024-05-20T09:00:00Z",
      }),
      makeEvent({
        id: "april",
        event_date: "2024-04-10",
        actual_start_at: "2024-04-10T11:00:00Z",
      }),
    ];
    const items = eventsToRailItems(events, "#000") as TimelineEventItem[];
    const buckets = groupByDate(items, "desc");
    expect(buckets.map((b) => b.dateKey)).toEqual(["2024-05-20", "2024-04-10"]);
    expect(buckets[0].items.map((i) => i.id)).toEqual(["may-first", "may-second"]);
  });
});
