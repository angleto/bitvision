// Calendar feed + planning + transition API client.
// Wraps the /api/patients/{pid}/calendar feed and the 5 transition
// sub-resources (confirm / reschedule / complete / cancel / mark-missed)
// plus the historical/planned event creator (POST /api/clinical-events).

import { API_BASE_URL, ApiError, getStoredToken, request } from "@/lib/api";
import type {
  CalendarFeed,
  ClinicalEvent,
  ClinicalEventAttachment,
  EventStatus,
} from "@/lib/api_records";

export interface CalendarFeedFilters {
  from?: string; // YYYY-MM-DD inclusive
  to?: string; // YYYY-MM-DD inclusive
  statuses?: EventStatus[];
  kinds?: string[];
  tz?: string; // IANA, server side currently only echoes back
}

function buildQuery(filters: CalendarFeedFilters | undefined): string {
  if (!filters) return "";
  const params = new URLSearchParams();
  if (filters.from) params.set("from", filters.from);
  if (filters.to) params.set("to", filters.to);
  if (filters.tz) params.set("tz", filters.tz);
  for (const s of filters.statuses ?? []) params.append("statuses", s);
  for (const k of filters.kinds ?? []) params.append("kinds", k);
  const s = params.toString();
  return s ? `?${s}` : "";
}

export function calendarFeedUrl(
  patientId: string,
  filters?: CalendarFeedFilters,
  format: "json" | "ics" = "json",
): string {
  const params = new URLSearchParams();
  if (filters?.from) params.set("from", filters.from);
  if (filters?.to) params.set("to", filters.to);
  if (filters?.tz) params.set("tz", filters.tz);
  for (const s of filters?.statuses ?? []) params.append("statuses", s);
  for (const k of filters?.kinds ?? []) params.append("kinds", k);
  params.set("format", format);
  return `${API_BASE_URL}/api/patients/${patientId}/calendar?${params.toString()}`;
}

export const calendarApi = {
  async feed(patientId: string, filters?: CalendarFeedFilters): Promise<CalendarFeed> {
    return request<CalendarFeed>(`/api/patients/${patientId}/calendar${buildQuery(filters)}`);
  },

  // Patch a single event's mutable metadata. Status transitions live
  // on the dedicated sub-resources (confirmEvent, rescheduleEvent,
  // ...); PATCH only adjusts title / narrative / planned_* / location /
  // reminders / body_part / timezone. ``etag`` is sent as If-Match;
  // the response carries a fresh ETag.
  async patchEvent(
    eventId: string,
    patch: {
      kind?: string;
      title?: string;
      narrative?: string | null;
      body_part?: string | null;
      planned_start_at?: string | null;
      planned_end_at?: string | null;
      timezone?: string | null;
      location_struct?: Record<string, string | number | undefined> | null;
      reminder_offsets_minutes?: number[] | null;
      event_date?: string | null;
      meeting_url?: string | null;
      links?: { label?: string; url: string }[] | null;
      attachments?: { label?: string; url: string; mime?: string; size?: number }[] | null;
    },
    init: { etag: string },
  ): Promise<ClinicalEvent> {
    const token = getStoredToken();
    const resp = await fetch(`${API_BASE_URL}/api/clinical-events/${eventId}`, {
      credentials: "include",
      method: "PATCH",
      headers: {
        "content-type": "application/json",
        "if-match": init.etag,
        ...(token ? { authorization: `Bearer ${token}` } : {}),
      },
      body: JSON.stringify(patch),
      cache: "no-store",
    });
    if (!resp.ok) {
      let detail: unknown = await resp.text();
      try {
        detail = JSON.parse(detail as string);
      } catch {
        /* keep raw text */
      }
      throw new ApiError(resp.status, detail);
    }
    return (await resp.json()) as ClinicalEvent;
  },

  // Plan / create a new event. Supports both historical (status=completed)
  // and forward-looking (status=planned/confirmed) creation.
  async createEvent(payload: {
    patient_id: string;
    kind: string;
    title: string;
    event_status?: EventStatus;
    planned_start_at?: string;
    planned_end_at?: string;
    actual_start_at?: string;
    actual_end_at?: string;
    timezone?: string;
    location_struct?: Record<string, string | number | undefined>;
    reminder_offsets_minutes?: number[];
    body_part?: string;
    narrative?: string;
    event_date?: string;
    meeting_url?: string;
    links?: { label?: string; url: string }[];
    attachments?: { label?: string; url: string; mime?: string; size?: number }[];
    idempotencyKey: string;
  }): Promise<ClinicalEvent> {
    const { idempotencyKey, ...body } = payload;
    return request<ClinicalEvent>("/api/clinical-events", {
      method: "POST",
      headers: {
        "Idempotency-Key": idempotencyKey,
      },
      json: body,
    });
  },

  // Common request shape for the 5 FSM transitions.
  async _transition(
    eventId: string,
    verb: "confirm" | "reschedule" | "complete" | "cancel" | "mark-missed",
    body: Record<string, unknown>,
    init: { etag: string; idempotencyKey: string; dryRun?: boolean },
  ): Promise<{ event: ClinicalEvent; replacedEventId: string | null }> {
    const params = init.dryRun ? "?dry_run=true" : "";
    const path = `/api/clinical-events/${eventId}/${verb}${params}`;
    // We need to read the X-Replaced-Event-Id header for reschedule,
    // which means using fetch directly rather than ``request<T>``.
    const token = getStoredToken();
    const resp = await fetch(`${API_BASE_URL}${path}`, {
      credentials: "include",
      method: "POST",
      headers: {
        "content-type": "application/json",
        "if-match": init.etag,
        "idempotency-key": init.idempotencyKey,
        ...(token ? { authorization: `Bearer ${token}` } : {}),
      },
      body: JSON.stringify(body),
      cache: "no-store",
    });
    if (!resp.ok) {
      let detail: unknown = await resp.text();
      try {
        detail = JSON.parse(detail as string);
      } catch {
        /* keep raw text */
      }
      throw new ApiError(resp.status, detail);
    }
    const event = (await resp.json()) as ClinicalEvent;
    const replaced = resp.headers.get("x-replaced-event-id");
    return { event, replacedEventId: replaced };
  },

  async confirmEvent(
    eventId: string,
    init: { etag: string; idempotencyKey: string; confirmedAt?: string; dryRun?: boolean },
  ): Promise<ClinicalEvent> {
    const { event } = await this._transition(
      eventId,
      "confirm",
      init.confirmedAt ? { confirmed_at: init.confirmedAt } : {},
      init,
    );
    return event;
  },

  async rescheduleEvent(
    eventId: string,
    payload: {
      new_planned_start_at: string;
      new_planned_end_at?: string;
      timezone?: string;
      reason: string;
    },
    init: { etag: string; idempotencyKey: string; dryRun?: boolean },
  ): Promise<{ event: ClinicalEvent; replacedEventId: string | null }> {
    return this._transition(eventId, "reschedule", payload, init);
  },

  async completeEvent(
    eventId: string,
    payload: {
      actual_start_at: string;
      actual_end_at?: string;
      narrative?: string;
    },
    init: { etag: string; idempotencyKey: string; dryRun?: boolean },
  ): Promise<ClinicalEvent> {
    const { event } = await this._transition(eventId, "complete", payload, init);
    return event;
  },

  async cancelEvent(
    eventId: string,
    payload: { reason: string },
    init: { etag: string; idempotencyKey: string; dryRun?: boolean },
  ): Promise<ClinicalEvent> {
    const { event } = await this._transition(eventId, "cancel", payload, init);
    return event;
  },

  async markMissed(
    eventId: string,
    payload: { note?: string },
    init: { etag: string; idempotencyKey: string; dryRun?: boolean },
  ): Promise<ClinicalEvent> {
    const { event } = await this._transition(eventId, "mark-missed", payload, init);
    return event;
  },

  // ---- Binary attachments ---------------------------------------------

  async listAttachments(eventId: string): Promise<ClinicalEventAttachment[]> {
    return request<ClinicalEventAttachment[]>(`/api/clinical-events/${eventId}/attachments`);
  },

  async uploadAttachment(eventId: string, file: File): Promise<ClinicalEventAttachment> {
    const form = new FormData();
    form.append("file", file, file.name);
    const token = getStoredToken();
    const resp = await fetch(`${API_BASE_URL}/api/clinical-events/${eventId}/attachments`, {
      credentials: "include",
      method: "POST",
      headers: token ? { authorization: `Bearer ${token}` } : undefined,
      body: form,
      cache: "no-store",
    });
    if (!resp.ok) {
      let detail: unknown = await resp.text();
      try {
        detail = JSON.parse(detail as string);
      } catch {
        /* keep raw text */
      }
      throw new ApiError(resp.status, detail);
    }
    return (await resp.json()) as ClinicalEventAttachment;
  },

  async deleteAttachment(eventId: string, attId: string): Promise<void> {
    const token = getStoredToken();
    const resp = await fetch(
      `${API_BASE_URL}/api/clinical-events/${eventId}/attachments/${attId}`,
      {
        credentials: "include",
        method: "DELETE",
        headers: token ? { authorization: `Bearer ${token}` } : undefined,
        cache: "no-store",
      },
    );
    if (!resp.ok) {
      throw new ApiError(resp.status, await resp.text());
    }
  },

  async promoteAttachment(eventId: string, attId: string): Promise<ClinicalEventAttachment> {
    return request<ClinicalEventAttachment>(
      `/api/clinical-events/${eventId}/attachments/${attId}/promote-to-document`,
      { method: "POST" },
    );
  },

  attachmentDownloadUrl(eventId: string, attId: string): string {
    return `${API_BASE_URL}/api/clinical-events/${eventId}/attachments/${attId}/download`;
  },
};

// Convenience: generate a uuid v4 for Idempotency-Key headers.
// We avoid the ``crypto.randomUUID()`` API gate on older browsers
// (Safari <15.4) since the calendar UI lands on a fairly broad
// audience; this fallback uses ``crypto.getRandomValues`` which is
// universally available.
export function newIdempotencyKey(): string {
  const c = typeof crypto !== "undefined" ? crypto : undefined;
  if (c && typeof c.randomUUID === "function") {
    return c.randomUUID();
  }
  const buf = new Uint8Array(16);
  if (c && typeof c.getRandomValues === "function") c.getRandomValues(buf);
  else for (let i = 0; i < 16; i++) buf[i] = Math.floor(Math.random() * 256);
  buf[6] = (buf[6] & 0x0f) | 0x40;
  buf[8] = (buf[8] & 0x3f) | 0x80;
  const hex = Array.from(buf, (b) => b.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}
