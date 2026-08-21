// Payload builders for the event edit flow.
//
// Kept out of ``EditEventDialog.tsx`` on purpose: these are the two bodies
// that hit the wire, and the split of "what goes in PATCH" vs "what goes in
// amend-time" is a contract with the backend, not a rendering detail. Pure
// functions here mean the split is unit-tested rather than eyeballed.
//
// The split itself is server-enforced since migration 0047: PATCH
// /clinical-events/{id} rejects event_date / planned_* / timezone with 422
// {"code": "use_amend_time"}, because a clinical date is an amendable fact
// that must carry an audit row, not a metadata field.

import type { ClinicalEvent, ClinicalEventKind } from "@/lib/api_records";
import type { AmendEventTimePayload, EventMetadataPatch } from "@/lib/calendar_api";
import { anchorFamily, fromLocalInputValue, isDateOnly, sameInstant } from "@/lib/event_dates";

/** The non-temporal half of the edit form. */
export interface EventMetadataDraft {
  kind: ClinicalEventKind;
  title: string;
  note: string;
  bodyPart: string;
  facility: string;
  room: string;
  city: string;
  reminders: number[];
  meetingUrl: string;
  links: { label?: string; url: string }[];
}

/** The temporal half, held exactly as the inputs hold it: ``start`` / ``end``
 *  are ``<input type="datetime-local">`` local wall-clock strings,
 *  ``eventDate`` is the ``YYYY-MM-DD`` of an ``<input type="date">``. */
export interface EventTimeDraft {
  start: string;
  end: string;
  eventDate: string;
  timezone: string;
  reason: string;
}

function cleanLinks(links: { label?: string; url: string }[]): { label?: string; url: string }[] {
  return links.filter((l) => l.url.trim());
}

/** Build the PATCH body: only fields that actually changed, and *never* a
 *  temporal one. The return type has no temporal key at all, so a future
 *  edit that reintroduces one fails to compile rather than 422 at runtime. */
export function buildMetadataPatch(
  event: ClinicalEvent,
  draft: EventMetadataDraft,
): EventMetadataPatch {
  const locationStruct =
    draft.facility.trim() || draft.room.trim() || draft.city.trim()
      ? {
          facility: draft.facility.trim() || undefined,
          room: draft.room.trim() || undefined,
          city: draft.city.trim() || undefined,
        }
      : null;
  const reminders = [...draft.reminders].sort((a, b) => a - b);
  const links = cleanLinks(draft.links);
  return {
    kind: draft.kind !== event.kind ? draft.kind : undefined,
    title: draft.title.trim() !== event.title ? draft.title.trim() : undefined,
    narrative:
      draft.note.trim() !== (event.narrative ?? "") ? draft.note.trim() || null : undefined,
    body_part:
      draft.bodyPart.trim() !== (event.body_part ?? "") ? draft.bodyPart.trim() || null : undefined,
    location_struct:
      JSON.stringify(locationStruct) !== JSON.stringify(event.location_struct ?? null)
        ? locationStruct
        : undefined,
    reminder_offsets_minutes:
      JSON.stringify(reminders) !== JSON.stringify(event.reminder_offsets_minutes ?? [])
        ? reminders.length > 0
          ? reminders
          : null
        : undefined,
    meeting_url:
      draft.meetingUrl.trim() !== (event.meeting_url ?? "")
        ? draft.meetingUrl.trim() || null
        : undefined,
    links:
      JSON.stringify(links) !== JSON.stringify(event.links ?? [])
        ? links.length > 0
          ? links
          : null
        : undefined,
  };
}

/** True when the PATCH body carries at least one real change. */
export function hasMetadataChanges(patch: EventMetadataPatch): boolean {
  return Object.values(patch).some((v) => v !== undefined);
}

/** Build the amend-time body, or null when no temporal field moved.
 *
 *  Only the anchor family matching the event's status is ever sent: the
 *  server rejects the other one with ``wrong_anchor_for_status``, and it is
 *  right to, since the DB derives ``event_date`` from that family alone.
 *  ``event_date`` is sent only for a date-only row, where the anchor is NULL
 *  and the date genuinely stands alone. */
export function buildAmendPayload(
  event: ClinicalEvent,
  draft: EventTimeDraft,
): AmendEventTimePayload | null {
  const payload: AmendEventTimePayload = {};
  if (isDateOnly(event)) {
    // An empty box is "left alone", not "clear the date": clearing would
    // leave the row with no date at all, and there is no UI intent for that.
    const next = draft.eventDate.trim();
    if (next && next !== (event.event_date ?? "")) payload.event_date = next;
  } else {
    const family = anchorFamily(event.event_status);
    const startKey = `${family}_start_at` as const;
    const endKey = `${family}_end_at` as const;
    const nextStart = fromLocalInputValue(draft.start);
    const nextEnd = fromLocalInputValue(draft.end);
    // An emptied START box is "left alone", exactly like the date-only
    // branch above, and for the same reason. The start anchor is what
    // ``event_date`` is derived from: clearing it would strand the row on a
    // date derived from a timestamp that no longer exists. The server says
    // so too (422 ``anchor_not_clearable``, whatever the status), so sending
    // an explicit null here could only ever produce an error.
    //
    // Instant comparison, not string comparison: the server's
    // "+00:00" and Date#toISOString's ".000Z" denote the same moment.
    if (nextStart !== null && !sameInstant(nextStart, event[startKey])) {
      payload[startKey] = nextStart;
    }
    // The END box is different: "we do not know when it finished" is a
    // legitimate state, and the server accepts a null there.
    if (!sameInstant(nextEnd, event[endKey])) payload[endKey] = nextEnd;
  }
  const tz = draft.timezone.trim();
  if (tz !== (event.timezone ?? "")) payload.timezone = tz || null;
  if (Object.keys(payload).length === 0) return null;
  const reason = draft.reason.trim();
  if (reason) payload.reason = reason;
  return payload;
}
