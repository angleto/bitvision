// Adapter: TimelineEvent → TimelineRailItem<TimelineEvent>.
//
// Lives outside of TimelineEventDot so the rail stays source-agnostic
// and the dot stays presentation-only. Sprint A3 will add a sibling
// ``taskAdapter.ts`` for the PatientTask source.

import type { TimelineEvent } from "@/lib/api_records";

import type { TimelineEventItem } from "./types";

/** Pick the best timestamp for ordering within a day. Priority:
 * planned_start_at (for future / confirmed events) > actual_start_at
 * (for completed events with a known clinical time) > null
 * (DATE-only event with no time component). */
function pickSortKey(event: TimelineEvent): string | null {
  const status = event.event_status ?? "completed";
  if ((status === "planned" || status === "confirmed") && event.planned_start_at) {
    return event.planned_start_at;
  }
  if (event.actual_start_at) return event.actual_start_at;
  // event_date is DATE-only; we keep null so the swimlane knows to
  // render this dot with reduced opacity (no precise time).
  return null;
}

/** Pick the date bucket. Prefer the planned timestamp when in the
 * future, the actual when completed, and the legacy event_date as
 * a fallback. Returns ``null`` only when the event has no date at
 * all (rare but possible for events backfilled without a date). */
function pickDateKey(event: TimelineEvent): string | null {
  const sort = pickSortKey(event);
  if (sort) return sort.slice(0, 10);
  if (event.event_date) return event.event_date.slice(0, 10);
  return null;
}

export function eventToRailItem(event: TimelineEvent, color: string): TimelineEventItem {
  return {
    id: event.id,
    source: "event",
    dateKey: pickDateKey(event),
    sortKey: pickSortKey(event),
    color,
    payload: event,
  };
}

export function eventsToRailItems(events: TimelineEvent[], color: string): TimelineEventItem[] {
  return events.map((ev) => eventToRailItem(ev, color));
}
