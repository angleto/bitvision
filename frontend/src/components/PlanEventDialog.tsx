"use client";

// Form to schedule a new planned event. Posts to
// POST /api/clinical-events with event_status='planned' +
// planned_start_at + timezone + location_struct + reminder offsets.
// Shows up via the "+ Pianifica visita" button on the calendar
// toolbar.

import { useTranslations } from "next-intl";
import { useRef, useState } from "react";

import AttachmentsManager from "@/components/AttachmentsManager";
import EvidenceEditor from "@/components/EvidenceEditor";
import UrlListEditor, { type UrlItem } from "@/components/UrlListEditor";
import { ApiError } from "@/lib/api";
import { calendarApi, newIdempotencyKey } from "@/lib/calendar_api";

interface Props {
  patientId: string;
  onClose: () => void;
  onCreated: () => void;
}

const KINDS = [
  "outpatient_visit",
  "radiology_appointment",
  "surgical_procedure",
  "inpatient_admission",
  "lab_batch",
  "consultation_event",
  "pathology_review",
  "mdt_meeting",
  "cardio_diagnostic",
  "endoscopy",
  "other",
] as const;

export default function PlanEventDialog({ patientId, onClose, onCreated }: Props) {
  const t = useTranslations("eventActions");
  const tKind = useTranslations("fascicolo.v3.events.kindSingular");
  const [kind, setKind] = useState<(typeof KINDS)[number]>("outpatient_visit");
  const [title, setTitle] = useState("");
  const [plannedStartAt, setPlannedStartAt] = useState(toLocalInputValue(defaultPlannedStart()));
  const [plannedEndAt, setPlannedEndAt] = useState("");
  const [timezone, setTimezone] = useState(detectTimezone());
  const [facility, setFacility] = useState("");
  const [room, setRoom] = useState("");
  const [city, setCity] = useState("");
  const [reminders, setReminders] = useState<Set<number>>(new Set([-1440, -120]));
  const [note, setNote] = useState("");
  const [meetingUrl, setMeetingUrl] = useState("");
  const [links, setLinks] = useState<UrlItem[]>([]);
  const [pendingFiles, setPendingFiles] = useState<File[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Track where mousedown originated so a text selection that ends
  // outside the dialog (drag-and-release on the backdrop) does not
  // dismiss the modal. We only close when BOTH mousedown and click
  // happened directly on the backdrop element.
  const mouseDownOnBackdrop = useRef(false);

  function toggleReminder(min: number): void {
    setReminders((prev) => {
      const next = new Set(prev);
      if (next.has(min)) next.delete(min);
      else next.add(min);
      return next;
    });
  }

  async function submit(): Promise<void> {
    if (busy) return;
    if (!title.trim()) {
      setError(t("titleRequired"));
      return;
    }
    if (!plannedStartAt) {
      setError(t("startRequired"));
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const locationStruct = [facility, room, city].some((x) => x.trim())
        ? {
            facility: facility.trim() || undefined,
            room: room.trim() || undefined,
            city: city.trim() || undefined,
          }
        : undefined;
      const cleanLinks = links.filter((l) => l.url.trim());
      const created = await calendarApi.createEvent({
        patient_id: patientId,
        kind,
        title: title.trim(),
        event_status: "planned",
        planned_start_at: new Date(plannedStartAt).toISOString(),
        planned_end_at: plannedEndAt ? new Date(plannedEndAt).toISOString() : undefined,
        timezone: timezone.trim() || undefined,
        location_struct: locationStruct,
        reminder_offsets_minutes:
          reminders.size > 0 ? Array.from(reminders).sort((a, b) => a - b) : undefined,
        narrative: note.trim() || undefined,
        meeting_url: meetingUrl.trim() || undefined,
        links: cleanLinks.length > 0 ? cleanLinks : undefined,
        idempotencyKey: newIdempotencyKey(),
      });
      // Upload the pending attachments now that the event exists.
      // Sequential to keep the error path simple — on first failure
      // we surface the message and let the user retry; the event is
      // already created so they don't lose the form.
      for (const f of pendingFiles) {
        await calendarApi.uploadAttachment(created.id, f);
      }
      onCreated();
    } catch (e) {
      setError(
        e instanceof ApiError
          ? `${e.status}: ${describeDetail(e.detail)}`
          : e instanceof Error
            ? e.message
            : "error",
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    // biome-ignore lint/a11y/useSemanticElements: native <dialog> is uneven across browsers for centred modals; role="dialog" + aria-modal is the documented pattern.
    // biome-ignore lint/a11y/useKeyWithClickEvents: backdrop dismissal is supplementary; Esc / Cancel button are keyboard-reachable.
    <div
      role="dialog"
      aria-modal="true"
      aria-label={t("plan")}
      style={{
        position: "fixed",
        inset: 0,
        background: "rgba(0,0,0,0.4)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        zIndex: 1100,
      }}
      onMouseDown={(e) => {
        mouseDownOnBackdrop.current = e.target === e.currentTarget;
      }}
      onClick={(e) => {
        const wasBackdropDown = mouseDownOnBackdrop.current;
        mouseDownOnBackdrop.current = false;
        if (wasBackdropDown && e.target === e.currentTarget) onClose();
      }}
    >
      <div
        style={{
          width: "min(520px, 95%)",
          background: "var(--bv-card-bg, #fff)",
          borderRadius: 8,
          padding: "1rem",
          maxHeight: "90vh",
          overflow: "auto",
        }}
      >
        <h3 style={{ marginTop: 0 }}>{t("planTitle")}</h3>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            void submit();
          }}
          style={{ display: "flex", flexDirection: "column", gap: "0.6rem" }}
        >
          <label style={field}>
            <span style={fieldLabel}>{t("kindLabel")}</span>
            <select
              value={kind}
              onChange={(e) => setKind(e.target.value as (typeof KINDS)[number])}
              style={inputStyle}
            >
              {KINDS.map((k) => (
                <option key={k} value={k}>
                  {tKind(k)}
                </option>
              ))}
            </select>
          </label>
          <label style={field}>
            <span style={fieldLabel}>{t("titleLabel")}</span>
            <input
              type="text"
              required
              maxLength={255}
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              style={inputStyle}
              placeholder={t("titlePlaceholder")}
            />
          </label>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
            <label style={field}>
              <span style={fieldLabel}>{t("plannedStart")}</span>
              <input
                type="datetime-local"
                required
                value={plannedStartAt}
                onChange={(e) => setPlannedStartAt(e.target.value)}
                style={inputStyle}
              />
            </label>
            <label style={field}>
              <span style={fieldLabel}>{t("plannedEndOptional")}</span>
              <input
                type="datetime-local"
                value={plannedEndAt}
                onChange={(e) => setPlannedEndAt(e.target.value)}
                style={inputStyle}
              />
            </label>
          </div>
          <label style={field}>
            <span style={fieldLabel}>{t("timezoneLabel")}</span>
            <input
              type="text"
              value={timezone}
              onChange={(e) => setTimezone(e.target.value)}
              style={inputStyle}
              placeholder="Europe/Rome"
              maxLength={64}
            />
          </label>
          <div
            style={{
              border: "1px dashed var(--bv-card-border, #e5e7eb)",
              borderRadius: 6,
              padding: "0.5rem",
            }}
          >
            <span style={fieldLabel}>{t("locationLabel")}</span>
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 6 }}>
              <input
                type="text"
                value={facility}
                onChange={(e) => setFacility(e.target.value)}
                placeholder={t("facilityPlaceholder")}
                style={inputStyle}
              />
              <input
                type="text"
                value={room}
                onChange={(e) => setRoom(e.target.value)}
                placeholder={t("roomPlaceholder")}
                style={inputStyle}
              />
              <input
                type="text"
                value={city}
                onChange={(e) => setCity(e.target.value)}
                placeholder={t("cityPlaceholder")}
                style={{ ...inputStyle, gridColumn: "1 / -1" }}
              />
            </div>
          </div>
          <div>
            <span style={fieldLabel}>{t("remindersLabel")}</span>
            <div style={{ display: "flex", flexWrap: "wrap", gap: 4, marginTop: 4 }}>
              {[-15, -30, -120, -1440, -2880, -7200].map((m) => (
                <button
                  key={m}
                  type="button"
                  className={reminders.has(m) ? undefined : "ghost"}
                  aria-pressed={reminders.has(m)}
                  onClick={() => toggleReminder(m)}
                  style={{ fontSize: "0.75rem", padding: "0.15rem 0.5rem", borderRadius: 999 }}
                >
                  {humanReminder(m)}
                </button>
              ))}
            </div>
          </div>
          <label style={field}>
            <span style={fieldLabel}>{t("meetingUrlLabel")}</span>
            <input
              type="url"
              value={meetingUrl}
              onChange={(e) => setMeetingUrl(e.target.value)}
              placeholder="https://meet.google.com/..."
              maxLength={512}
              style={inputStyle}
            />
          </label>
          <div>
            <span style={fieldLabel}>{t("linksLabel")}</span>
            <UrlListEditor
              items={links}
              onChange={setLinks}
              addLabel={t("addLink")}
              placeholderLabel={t("linkLabelPlaceholder")}
              placeholderUrl="https://..."
            />
          </div>
          <div>
            <span style={fieldLabel}>{t("attachmentsLabel")}</span>
            <AttachmentsManager
              eventId={null}
              pending={pendingFiles}
              onPendingChange={setPendingFiles}
            />
          </div>
          <div>
            <span style={fieldLabel}>{t("noteLabel")}</span>
            <EvidenceEditor value={note} onChange={setNote} embedded patientId={patientId} />
          </div>
          {error && <p style={{ color: "var(--bv-danger, #c00)" }}>{error}</p>}
          <div style={{ display: "flex", justifyContent: "flex-end", gap: 8 }}>
            <button type="button" className="ghost" onClick={onClose} disabled={busy}>
              {t("cancelDialog")}
            </button>
            <button type="submit" disabled={busy}>
              {busy ? "…" : t("planSubmit")}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

function humanReminder(min: number): string {
  const abs = Math.abs(min);
  if (abs >= 1440) return `${abs / 1440}d`;
  if (abs >= 60) return `${abs / 60}h`;
  return `${abs}m`;
}

function defaultPlannedStart(): string {
  const d = new Date();
  d.setMinutes(0, 0, 0);
  d.setHours(d.getHours() + 1);
  return d.toISOString();
}

function toLocalInputValue(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const pad = (n: number): string => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function detectTimezone(): string {
  if (typeof Intl !== "undefined") {
    try {
      return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
    } catch {
      return "UTC";
    }
  }
  return "UTC";
}

function describeDetail(detail: unknown): string {
  if (typeof detail === "string") return detail;
  if (detail && typeof detail === "object") {
    const d = detail as { detail?: { message?: string }; message?: string };
    if (d.detail?.message) return d.detail.message;
    if (d.message) return d.message;
    return JSON.stringify(detail);
  }
  return String(detail);
}

const field = { display: "flex", flexDirection: "column", gap: 4 } as const;
const fieldLabel = { fontSize: "0.75rem", color: "var(--bv-fg-soft)" } as const;
const inputStyle = {
  fontSize: "0.88rem",
  padding: "0.35rem 0.5rem",
  border: "1px solid var(--bv-card-border, #d0d5dd)",
  borderRadius: 6,
  background: "var(--bv-card-bg, transparent)",
  color: "var(--bv-fg)",
  width: "100%",
} as const;
