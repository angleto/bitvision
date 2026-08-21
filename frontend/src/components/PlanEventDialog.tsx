"use client";

// Form to create a new event, in either of the two directions a care
// record actually needs:
//
//   "to be scheduled"  → event_status='planned' + planned_start_at
//                        + reminder offsets (POST /api/clinical-events)
//   "already happened" → event_status='completed' + actual_start_at,
//                        no planned_* and no reminders
//
// The second mode is what makes recording a PAST event possible at all:
// without it the only way in was to create the event now and let the
// insertion moment stand as the clinical date. The API has always
// supported it; the form did not offer it.
//
// Shows up via the "+ Pianifica visita" button on the calendar toolbar.

import { useLocale, useTranslations } from "next-intl";
import { useId, useRef, useState } from "react";

import AttachmentsManager from "@/components/AttachmentsManager";
import EvidenceEditor from "@/components/EvidenceEditor";
import UrlListEditor, { type UrlItem } from "@/components/UrlListEditor";
import { calendarApi, newIdempotencyKey } from "@/lib/calendar_api";
import {
  derivedEventDate,
  formatInZone,
  fromLocalInputValue,
  isValidTimezone,
  localInputZoneMismatch,
  nowLocalInputValue,
  timezoneOptions,
  toLocalInputValue,
  validateAmend,
  viewerTimezone,
} from "@/lib/event_dates";
import { describeEventError } from "@/lib/event_errors";

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

type PlanMode = "scheduled" | "happened";

export default function PlanEventDialog({ patientId, onClose, onCreated }: Props) {
  const t = useTranslations("eventActions");
  const tKind = useTranslations("fascicolo.v3.events.kindSingular");
  const locale = useLocale();
  const [mode, setMode] = useState<PlanMode>("scheduled");
  const [kind, setKind] = useState<(typeof KINDS)[number]>("outpatient_visit");
  const [title, setTitle] = useState("");
  const [anchorStart, setAnchorStart] = useState(toLocalInputValue(defaultPlannedStart()));
  const [anchorEnd, setAnchorEnd] = useState("");
  const [timezone, setTimezone] = useState(viewerTimezone());
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
  const startId = useId();
  const errorId = useId();
  const warningId = useId();
  const tzId = useId();
  // ``list`` resolves an id; React's useId values carry punctuation, so strip
  // it rather than rely on every engine's id parsing.
  const tzListId = `tzlist${useId().replace(/[^a-zA-Z0-9]/g, "")}`;
  const tzErrorId = useId();
  const previewId = useId();
  const zoneHintId = useId();
  // Read once per mount: the list is ~450 immutable strings.
  const zones = useRef(timezoneOptions()).current;
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

  // The anchor family follows the mode: a scheduled event is anchored on
  // planned_start_at, one that already happened on actual_start_at. Same
  // rule the server derives event_date from, so the form and the DB agree
  // before the request leaves the browser.
  const family = mode === "scheduled" ? ("planned" as const) : ("actual" as const);
  const validation = validateAmend({ family, start: anchorStart, end: anchorEnd });
  const startError = validation.errorKey ? t(validation.errorKey) : null;
  const startWarning = validation.warningKey ? t(validation.warningKey) : null;
  // Same client-side zone gate as the edit dialog: the server answers a bad
  // IANA name with 422 invalid_timezone, and this box is pre-filled from the
  // browser but freely editable.
  const tzTrimmed = timezone.trim();
  const tzError = tzTrimmed && !isValidTimezone(tzTrimmed) ? t("serverError.invalid_timezone") : "";
  // The date the row will actually carry: the anchor projected onto the
  // calendar of ``timezone``, exactly as fn_ce_derive_event_date does it.
  // Shown live so a zone edit across midnight is visible, not silent.
  const previewDate = derivedEventDate(
    fromLocalInputValue(anchorStart),
    tzError ? undefined : tzTrimmed,
  );
  const previewLabel = previewDate ? formatInZone(previewDate, locale) : "";
  // Same one-zone rule as the edit dialog: the datetime-local boxes speak
  // the DEVICE's zone, the recorded-date preview speaks the typed one. They
  // agree on open (the box is seeded from the browser) and can diverge the
  // moment the user types another zone, so name both rather than convert the
  // input's value. See localInputZoneMismatch.
  const anchorInstantMs = Date.parse(fromLocalInputValue(anchorStart) ?? "");
  const viewerZone = localInputZoneMismatch(
    tzError ? "" : tzTrimmed,
    Number.isNaN(anchorInstantMs) ? Date.now() : anchorInstantMs,
  );
  const startDescribedBy =
    [
      startError ? errorId : startWarning ? warningId : null,
      viewerZone ? zoneHintId : null,
      previewLabel ? previewId : null,
    ]
      .filter(Boolean)
      .join(" ") || undefined;

  async function submit(): Promise<void> {
    if (busy) return;
    if (!title.trim()) {
      setError(t("titleRequired"));
      return;
    }
    if (!anchorStart) {
      setError(t("startRequired"));
      return;
    }
    if (startError) {
      setError(startError);
      return;
    }
    if (tzError) {
      setError(tzError);
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
      const startIso = new Date(anchorStart).toISOString();
      const endIso = anchorEnd ? new Date(anchorEnd).toISOString() : undefined;
      // ``event_date`` is deliberately NOT sent: the server derives it from
      // the anchor and 422s (event_date_conflicts_with_anchor) if the two
      // disagree. Reminders only make sense ahead of a scheduled event.
      const anchor =
        mode === "scheduled"
          ? {
              event_status: "planned" as const,
              planned_start_at: startIso,
              planned_end_at: endIso,
              reminder_offsets_minutes:
                reminders.size > 0 ? Array.from(reminders).sort((a, b) => a - b) : undefined,
            }
          : {
              event_status: "completed" as const,
              actual_start_at: startIso,
              actual_end_at: endIso,
            };
      const created = await calendarApi.createEvent({
        patient_id: patientId,
        kind,
        title: title.trim(),
        ...anchor,
        timezone: timezone.trim() || undefined,
        location_struct: locationStruct,
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
      // Localised by stable code; the server's English prose is the last
      // resort, never the default.
      setError(describeEventError(e, t));
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
          color: "var(--bv-fg)",
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
          {/* Mode switch. Radios, not a select: two mutually exclusive
           * choices that change what the rest of the form means should be
           * visible at once, and each is reachable with one arrow key. */}
          <fieldset style={modeBox}>
            <legend style={legendStyle}>{t("planModeLabel")}</legend>
            <div style={{ display: "flex", flexWrap: "wrap", gap: "0.8rem" }}>
              {(["scheduled", "happened"] as const).map((m) => (
                <label key={m} style={modeOption}>
                  <input
                    type="radio"
                    name="plan-mode"
                    value={m}
                    checked={mode === m}
                    onChange={() => {
                      setMode(m);
                      // Re-seed the anchor so "already happened" does not open
                      // on a future default it would immediately reject.
                      setAnchorStart(
                        m === "happened"
                          ? nowLocalInputValue()
                          : toLocalInputValue(defaultPlannedStart()),
                      );
                      setAnchorEnd("");
                    }}
                  />
                  <span>{m === "scheduled" ? t("planModeScheduled") : t("planModeHappened")}</span>
                </label>
              ))}
            </div>
            <p style={captionStyle}>
              {mode === "scheduled" ? t("planModeScheduledHint") : t("planModeHappenedHint")}
            </p>
          </fieldset>
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
              <span style={fieldLabel}>
                {mode === "scheduled" ? t("plannedStart") : t("happenedStart")}
              </span>
              <input
                id={startId}
                type="datetime-local"
                required
                value={anchorStart}
                max={mode === "happened" ? nowLocalInputValue() : undefined}
                onChange={(e) => setAnchorStart(e.target.value)}
                aria-invalid={startError ? true : undefined}
                aria-describedby={startDescribedBy}
                style={inputStyle}
              />
            </label>
            <label style={field}>
              <span style={fieldLabel}>
                {mode === "scheduled" ? t("plannedEndOptional") : t("actualEndOptional")}
              </span>
              <input
                type="datetime-local"
                value={anchorEnd}
                max={mode === "happened" ? nowLocalInputValue() : undefined}
                onChange={(e) => setAnchorEnd(e.target.value)}
                aria-invalid={validation.errorKey === "amendEndBeforeStart" ? true : undefined}
                aria-describedby={
                  validation.errorKey === "amendEndBeforeStart" ? errorId : undefined
                }
                style={inputStyle}
              />
            </label>
          </div>
          {startError && (
            <p id={errorId} role="alert" style={inlineError}>
              {startError}
            </p>
          )}
          {!startError && startWarning && (
            <output id={warningId} style={inlineWarning}>
              {startWarning}
            </output>
          )}
          {viewerZone && (
            <p id={zoneHintId} style={captionStyle}>
              {t("localTimeZoneHint", { viewer: viewerZone, event: tzTrimmed })}
            </p>
          )}
          {mode === "happened" && <p style={captionStyle}>{t("actualStartHint")}</p>}
          {previewLabel && (
            <p id={previewId} style={captionStyle}>
              {t("recordedDatePreview", { date: previewLabel })}
            </p>
          )}
          <label style={field}>
            <span style={fieldLabel}>{t("timezoneLabel")}</span>
            <input
              id={tzId}
              type="text"
              value={timezone}
              onChange={(e) => setTimezone(e.target.value)}
              style={inputStyle}
              placeholder="Europe/Rome"
              maxLength={64}
              list={zones.length > 0 ? tzListId : undefined}
              aria-invalid={tzError ? true : undefined}
              aria-describedby={tzError ? tzErrorId : undefined}
            />
          </label>
          {/* A picker where the engine can supply one, free text where it
           * cannot (Safari < 15.4): either way isValidTimezone gates it. */}
          {zones.length > 0 && (
            <datalist id={tzListId}>
              {zones.map((z) => (
                <option key={z} value={z} />
              ))}
            </datalist>
          )}
          {tzError && (
            <p id={tzErrorId} role="alert" style={inlineError}>
              {tzError}
            </p>
          )}
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
          {/* A reminder for something that already happened is nonsense,
           * and the server drops the offsets for a completed row anyway. */}
          {mode === "scheduled" && (
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
          {error && (
            <p role="alert" style={inlineError}>
              {error}
            </p>
          )}
          <div style={{ display: "flex", justifyContent: "flex-end", gap: 8 }}>
            <button type="button" className="ghost" onClick={onClose} disabled={busy}>
              {t("cancelDialog")}
            </button>
            <button type="submit" disabled={busy || startError !== null || tzError !== ""}>
              {busy ? "…" : mode === "scheduled" ? t("planSubmit") : t("happenedSubmit")}
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
const modeBox = {
  border: "1px solid var(--bv-card-border, #e5e7eb)",
  borderRadius: 6,
  padding: "0.4rem 0.6rem 0.5rem",
  margin: "0 0 0.6rem",
  minWidth: 0,
} as const;
const modeOption = {
  display: "flex",
  alignItems: "center",
  gap: 6,
  fontSize: "0.85rem",
  color: "var(--bv-fg)",
} as const;
const legendStyle = {
  fontSize: "0.72rem",
  textTransform: "uppercase",
  letterSpacing: "0.04em",
  color: "var(--bv-fg-soft)",
  padding: "0 0.3rem",
} as const;
const captionStyle = {
  fontSize: "0.72rem",
  color: "var(--bv-fg-soft)",
  margin: "0.3rem 0 0",
} as const;
// Explicit fg + bg on both states: a bare colour on an inherited background
// is the project's recurring "unreadable in light mode" bug.
const inlineError = {
  color: "var(--bv-danger)",
  background: "var(--bv-danger-soft)",
  border: "1px solid var(--bv-danger)",
  borderRadius: 6,
  padding: "0.35rem 0.5rem",
  fontSize: "0.8rem",
  margin: 0,
} as const;
const inlineWarning = {
  // <output> is inline by default; the box needs to be a block.
  display: "block",
  color: "var(--bv-warning)",
  background: "var(--bv-warning-soft)",
  border: "1px solid var(--bv-warning)",
  borderRadius: 6,
  padding: "0.35rem 0.5rem",
  fontSize: "0.8rem",
  margin: 0,
} as const;
