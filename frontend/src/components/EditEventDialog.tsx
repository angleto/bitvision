"use client";

// Edit dialog for an existing ClinicalEvent. Wraps PATCH
// /api/clinical-events/{id} (If-Match required). Mutates metadata
// only: title, narrative (note), body_part, planned_start_at,
// planned_end_at, timezone, location_struct, reminder_offsets.
// event_status transitions live on the dedicated sub-resources
// (confirm/reschedule/...).

import { useTranslations } from "next-intl";
import { useRef, useState } from "react";

import AttachmentsManager from "@/components/AttachmentsManager";
import EvidenceEditor from "@/components/EvidenceEditor";
import UrlListEditor, { type UrlItem } from "@/components/UrlListEditor";
import { ApiError } from "@/lib/api";
import type { ClinicalEvent, ClinicalEventKind } from "@/lib/api_records";
import { calendarApi } from "@/lib/calendar_api";

// Same set the PlanEventDialog exposes plus ``imaging_study`` if the
// event already had that kind (read-only display: server rejects a
// non-imaging → imaging_study promotion to preserve the DICOM
// pipeline ownership invariant).
const PATCHABLE_KINDS: readonly ClinicalEventKind[] = [
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
];

interface Props {
  event: ClinicalEvent;
  onClose: () => void;
  onSaved: (updated: ClinicalEvent) => void;
}

export default function EditEventDialog({ event, onClose, onSaved }: Props) {
  const t = useTranslations("eventActions");
  const tKind = useTranslations("fascicolo.v3.events.kindSingular");
  const isFutureLike = event.event_status === "planned" || event.event_status === "confirmed";
  const isImagingStudy = event.kind === "imaging_study";
  const [kind, setKind] = useState<ClinicalEventKind>(event.kind);
  const [title, setTitle] = useState(event.title);
  const [note, setNote] = useState(event.narrative ?? "");
  const [bodyPart, setBodyPart] = useState(event.body_part ?? "");
  const [plannedStartAt, setPlannedStartAt] = useState(toLocalInputValue(event.planned_start_at));
  const [plannedEndAt, setPlannedEndAt] = useState(toLocalInputValue(event.planned_end_at));
  const [timezone, setTimezone] = useState(event.timezone ?? "");
  const [facility, setFacility] = useState(event.location_struct?.facility ?? "");
  const [room, setRoom] = useState(event.location_struct?.room ?? "");
  const [city, setCity] = useState(event.location_struct?.city ?? "");
  const initialReminders = new Set<number>(event.reminder_offsets_minutes ?? []);
  const [reminders, setReminders] = useState<Set<number>>(initialReminders);
  const [meetingUrl, setMeetingUrl] = useState(event.meeting_url ?? "");
  const [links, setLinks] = useState<UrlItem[]>(event.links ?? []);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // See PlanEventDialog: protect drag-out selections from closing
  // the dialog. ``mousedown`` on backdrop is required for the click
  // to count as a backdrop dismissal.
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
    setBusy(true);
    setError(null);
    try {
      const locationStruct =
        facility.trim() || room.trim() || city.trim()
          ? {
              facility: facility.trim() || undefined,
              room: room.trim() || undefined,
              city: city.trim() || undefined,
            }
          : null;
      const patch: Parameters<typeof calendarApi.patchEvent>[1] = {
        kind: kind !== event.kind ? kind : undefined,
        title: title.trim() !== event.title ? title.trim() : undefined,
        narrative: note.trim() !== (event.narrative ?? "") ? note.trim() || null : undefined,
        body_part:
          bodyPart.trim() !== (event.body_part ?? "") ? bodyPart.trim() || null : undefined,
        timezone: timezone.trim() !== (event.timezone ?? "") ? timezone.trim() || null : undefined,
        location_struct:
          JSON.stringify(locationStruct) !== JSON.stringify(event.location_struct ?? null)
            ? locationStruct
            : undefined,
        reminder_offsets_minutes:
          JSON.stringify(Array.from(reminders).sort((a, b) => a - b)) !==
          JSON.stringify(event.reminder_offsets_minutes ?? [])
            ? reminders.size > 0
              ? Array.from(reminders).sort((a, b) => a - b)
              : null
            : undefined,
        meeting_url:
          meetingUrl.trim() !== (event.meeting_url ?? "") ? meetingUrl.trim() || null : undefined,
        links:
          JSON.stringify(links.filter((l) => l.url.trim())) !== JSON.stringify(event.links ?? [])
            ? links.filter((l) => l.url.trim()).length > 0
              ? links.filter((l) => l.url.trim())
              : null
            : undefined,
      };
      if (isFutureLike) {
        const newStartIso = plannedStartAt ? new Date(plannedStartAt).toISOString() : null;
        const newEndIso = plannedEndAt ? new Date(plannedEndAt).toISOString() : null;
        if (newStartIso !== (event.planned_start_at ?? null)) patch.planned_start_at = newStartIso;
        if (newEndIso !== (event.planned_end_at ?? null)) patch.planned_end_at = newEndIso;
      }
      const updated = await calendarApi.patchEvent(event.id, patch, { etag: event.etag });
      onSaved(updated);
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
      aria-label={t("editTitle")}
      style={{
        position: "fixed",
        inset: 0,
        background: "rgba(0,0,0,0.4)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        zIndex: 1200,
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
        <h3 style={{ marginTop: 0 }}>{t("editTitle")}</h3>
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
              onChange={(e) => setKind(e.target.value as ClinicalEventKind)}
              style={inputStyle}
              disabled={isImagingStudy}
              title={isImagingStudy ? t("kindImagingLocked") : undefined}
            >
              {isImagingStudy && <option value="imaging_study">{tKind("imaging_study")}</option>}
              {PATCHABLE_KINDS.map((k) => (
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
            />
          </label>
          {isFutureLike && (
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
              <label style={field}>
                <span style={fieldLabel}>{t("plannedStart")}</span>
                <input
                  type="datetime-local"
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
          )}
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
          <label style={field}>
            <span style={fieldLabel}>{t("bodyPartLabel")}</span>
            <input
              type="text"
              value={bodyPart}
              onChange={(e) => setBodyPart(e.target.value)}
              maxLength={64}
              style={inputStyle}
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
          {isFutureLike && (
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
          )}
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
            <AttachmentsManager eventId={event.id} />
          </div>
          <div>
            <span style={fieldLabel}>{t("noteLabel")}</span>
            <EvidenceEditor value={note} onChange={setNote} embedded patientId={event.patient_id} />
          </div>
          {error && <p style={{ color: "var(--bv-danger, #c00)" }}>{error}</p>}
          <div style={{ display: "flex", justifyContent: "flex-end", gap: 8 }}>
            <button type="button" className="ghost" onClick={onClose} disabled={busy}>
              {t("cancelDialog")}
            </button>
            <button type="submit" disabled={busy}>
              {busy ? "…" : t("editSubmit")}
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

function toLocalInputValue(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const pad = (n: number): string => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
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
