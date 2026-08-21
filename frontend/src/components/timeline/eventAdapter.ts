// Adapter: TimelineEvent → TimelineRailItem<TimelineEvent>.
//
// Lives outside of TimelineEventDot so the rail stays source-agnostic
// and the dot stays presentation-only. Sprint A3 will add a sibling
// ``taskAdapter.ts`` for the PatientTask source.

import type { TimelineEvent } from "@/lib/api_records";
import { authoritativeInstant, derivedEventDate } from "@/lib/event_dates";

import type { TimelineEventItem } from "./types";

/** Timestamp used for ordering within a day: the event's anchor, i.e. the
 * one the DB derives ``event_date`` from.
 *
 * This used to fall through to ``actual_start_at`` for any status outside
 * planned/confirmed, which put ``cancelled`` and ``rescheduled`` rows in the
 * wrong bucket: both belong to the PLANNED family per
 * ``fn_ce_derive_event_date`` (migration 0047), so a cancelled appointment
 * sorts by when it was booked for. ``authoritativeInstant`` is that rule.
 *
 * Returns null for a date-only row, which the swimlane reads as "no precise
 * time" and renders with reduced opacity. */
function pickSortKey(event: TimelineEvent): string | null {
  return authoritativeInstant(event);
}

/** Pick the date bucket: the anchor's day when there is an anchor, the
 * standalone ``event_date`` otherwise. Returns ``null`` only when the event
 * has no date at all (rare but possible for events backfilled without
 * one).
 *
 * The anchor's day is taken in the EVENT's timezone, via the same
 * ``derivedEventDate`` the dialogs preview and the DB trigger computes.
 * Slicing the first 10 characters of the ISO string instead reads the UTC
 * day, which is a different day for every row whose local time sits near
 * midnight: a 00:30 Europe/Rome appointment would be railed under the
 * previous date while the record itself says otherwise. */
function pickDateKey(event: TimelineEvent): string | null {
  const sort = pickSortKey(event);
  const derived = sort ? derivedEventDate(sort, event.timezone) : "";
  if (derived) return derived;
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
