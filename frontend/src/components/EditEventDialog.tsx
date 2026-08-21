"use client";

// Edit dialog for an existing ClinicalEvent. ONE dialog for every status,
// terminal ones included: the user thinks "fix this event", not "pick the
// sub-resource that owns this field".
//
// Two endpoints back it, because the server splits metadata from clinical
// time (migration 0047):
//   - POST /api/clinical-events/{id}/amend-time   → event_date /
//     planned_* / actual_* / timezone, audited, If-Match + Idempotency-Key
//   - PATCH /api/clinical-events/{id}             → everything else
// When both moved, the amendment goes FIRST and the PATCH reuses the etag
// the amendment returned; if the PATCH then fails we say so explicitly,
// because the date really was saved and a generic "error" would send the
// user back to redo a write that already landed.

import { useLocale, useTranslations } from "next-intl";
import { useId, useRef, useState } from "react";

import AttachmentsManager from "@/components/AttachmentsManager";
import EvidenceEditor from "@/components/EvidenceEditor";
import UrlListEditor, { type UrlItem } from "@/components/UrlListEditor";
import { ApiError } from "@/lib/api";
import type { ClinicalEvent, ClinicalEventKind } from "@/lib/api_records";
import { calendarApi, newIdempotencyKey } from "@/lib/calendar_api";
import {
  anchorFamily,
  authoritativeEnd,
  authoritativeInstant,
  derivedEventDate,
  formatInZone,
  fromLocalInputValue,
  isDateOnly,
  isValidTimezone,
  localInputZoneMismatch,
  nowLocalInputValue,
  reasonRequired,
  secondaryPlannedInstant,
  timezoneOptions,
  toLocalInputValue,
  todayLocalDate,
  validateAmend,
  whenCaptionKey,
} from "@/lib/event_dates";
import { buildAmendPayload, buildMetadataPatch, hasMetadataChanges } from "@/lib/event_edit";
import { describeEventError } from "@/lib/event_errors";

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
  /** Called with a fresh server copy while the dialog stays open: the
   *  amendment succeeded and the follow-up PATCH did not, or a 412 forced a
   *  re-read. Either way the parent's copy is stale, and the drawer behind
   *  the dialog must not keep showing a date the server no longer holds. */
  onPartialSave?: (updated: ClinicalEvent) => void;
}

export default function EditEventDialog({ event, onClose, onSaved, onPartialSave }: Props) {
  const t = useTranslations("eventActions");
  const tKind = useTranslations("fascicolo.v3.events.kindSingular");
  const locale = useLocale();
  // The server's copy, refreshed after every successful write. Every diff
  // and every If-Match reads from here, so a retry after a partial failure
  // uses the fresh etag and no longer re-sends the part that already landed.
  const [live, setLive] = useState<ClinicalEvent>(event);
  const isFutureLike = live.event_status === "planned" || live.event_status === "confirmed";
  const isImagingStudy = live.kind === "imaging_study";
  const family = anchorFamily(live.event_status);
  const dateOnly = isDateOnly(live);
  const captionKey = whenCaptionKey(live);
  // The scheduled time a row still carries when its own anchor is missing
  // (a missed appointment), or when it happened at a different moment than
  // it was booked for (a completed one).
  const bookedInstant = secondaryPlannedInstant(live);

  const [kind, setKind] = useState<ClinicalEventKind>(event.kind);
  const [title, setTitle] = useState(event.title);
  const [note, setNote] = useState(event.narrative ?? "");
  const [bodyPart, setBodyPart] = useState(event.body_part ?? "");
  // One pair of date inputs, bound to whichever anchor family owns this
  // row's date. Showing "planned start" on a completed event (or the
  // reverse) would offer an edit the server rejects with
  // ``wrong_anchor_for_status``.
  const [anchorStart, setAnchorStart] = useState(toLocalInputValue(authoritativeInstant(event)));
  const [anchorEnd, setAnchorEnd] = useState(toLocalInputValue(authoritativeEnd(event)));
  const [eventDate, setEventDate] = useState(event.event_date ?? "");
  const [timezone, setTimezone] = useState(event.timezone ?? "");
  const [reason, setReason] = useState("");
  const [facility, setFacility] = useState(event.location_struct?.facility ?? "");
  const [room, setRoom] = useState(event.location_struct?.room ?? "");
  const [city, setCity] = useState(event.location_struct?.city ?? "");
  const [reminders, setReminders] = useState<Set<number>>(
    new Set<number>(event.reminder_offsets_minutes ?? []),
  );
  const [meetingUrl, setMeetingUrl] = useState(event.meeting_url ?? "");
  const [links, setLinks] = useState<UrlItem[]>(event.links ?? []);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Set when the server answered 412: somebody else wrote this row while the
  // form was open. The baseline is re-fetched so a second Save can succeed,
  // and the user is offered the server's values rather than left guessing.
  const [conflict, setConflict] = useState(false);
  // See PlanEventDialog: protect drag-out selections from closing
  // the dialog. ``mousedown`` on backdrop is required for the click
  // to count as a backdrop dismissal.
  const mouseDownOnBackdrop = useRef(false);
  // ONE key per logical save, not one per attempt. A key minted at submit
  // time made the server's replay guard useless: the classic failure it
  // exists for is "the write landed, the response did not", and a retry with
  // a fresh key would amend the row a second time. Regenerated only after a
  // success, i.e. when the next save is a genuinely new amendment.
  const idempotencyKey = useRef(newIdempotencyKey());

  const startId = useId();
  const endId = useId();
  const reasonId = useId();
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

  const timeDraft = {
    start: anchorStart,
    end: anchorEnd,
    eventDate,
    timezone,
    reason,
  };
  const amendPayload = buildAmendPayload(live, timeDraft);
  const needsReason = amendPayload !== null && reasonRequired(family, "event_date" in amendPayload);
  // Validated on every render so the past-plan warning shows while typing
  // rather than only after a rejected submit.
  const validation = validateAmend({
    family,
    start: dateOnly ? eventDate : anchorStart,
    end: dateOnly ? "" : anchorEnd,
  });
  const dateError = amendPayload !== null && validation.errorKey ? t(validation.errorKey) : null;
  const dateWarning =
    amendPayload !== null && validation.warningKey ? t(validation.warningKey) : null;
  // The zone is free-form on the wire, so validate it here: the server's
  // ZoneInfo lookup answers 422 invalid_timezone, and a round trip to learn
  // that "Europe/Roma" is not a zone is a round trip too many. An empty box
  // is legitimate: it clears the override and the server falls back to UTC.
  const tzTrimmed = timezone.trim();
  const tzError = tzTrimmed && !isValidTimezone(tzTrimmed) ? t("serverError.invalid_timezone") : "";
  // The date that will actually be RECORDED, derived here exactly as
  // fn_ce_derive_event_date derives it: anchor projected onto the calendar
  // of ``timezone``. Without this a zone edit silently moves the stored day
  // (23:30 Rome is the previous day in UTC) and nothing on screen says so.
  const previewDate = dateOnly
    ? eventDate
    : derivedEventDate(fromLocalInputValue(anchorStart), tzError ? live.timezone : tzTrimmed);
  const previewLabel = previewDate ? formatInZone(previewDate, locale) : "";
  // The anchor <input type="datetime-local"> is hard-wired to the DEVICE's
  // zone; the caption, the booked-time line and the preview above all render
  // in the EVENT's. When the two clocks differ the fieldset showed 08:00 in
  // the box and 09:00 two lines below with nothing saying why, so name both
  // zones. Naming, not converting: see localInputZoneMismatch for why a
  // conversion here is the one change that could store the wrong hour.
  // Judged at the instant being edited, so a summer appointment corrected in
  // winter is compared on its own DST state.
  const anchorInstantMs = Date.parse(fromLocalInputValue(anchorStart) ?? "");
  const eventZone = tzError ? (live.timezone ?? "") : tzTrimmed;
  const viewerZone = dateOnly
    ? null
    : localInputZoneMismatch(
        eventZone,
        Number.isNaN(anchorInstantMs) ? Date.now() : anchorInstantMs,
      );
  const startDescribedBy =
    [
      dateError ? errorId : dateWarning ? warningId : null,
      viewerZone ? zoneHintId : null,
      previewLabel ? previewId : null,
    ]
      .filter(Boolean)
      .join(" ") || undefined;

  function toggleReminder(min: number): void {
    setReminders((prev) => {
      const next = new Set(prev);
      if (next.has(min)) next.delete(min);
      else next.add(min);
      return next;
    });
  }

  // Localised, code-first. The server's English prose is the last resort,
  // not the default: the person who owns this record reads Italian.
  function describeError(e: unknown): string {
    return describeEventError(e, t);
  }

  /** Re-read the row and adopt it as the diff baseline (and, crucially, as
   *  the source of the If-Match etag). The user's typed values are kept: the
   *  next Save re-diffs them against what the server now holds. */
  async function refreshBaseline(): Promise<ClinicalEvent | null> {
    try {
      const fresh = await calendarApi.getEvent(live.id);
      setLive(fresh);
      onPartialSave?.(fresh);
      return fresh;
    } catch {
      return null;
    }
  }

  /** Discard the local edits and show what the server holds. Only offered
   *  after a 412, where "my values" and "their values" have actually diverged
   *  and the user has to be able to see theirs. Every field, not only the
   *  temporal ones: the button says "the current values" and must mean it. */
  function reseedFromServer(): void {
    setKind(live.kind);
    setTitle(live.title);
    setNote(live.narrative ?? "");
    setBodyPart(live.body_part ?? "");
    setAnchorStart(toLocalInputValue(authoritativeInstant(live)));
    setAnchorEnd(toLocalInputValue(authoritativeEnd(live)));
    setEventDate(live.event_date ?? "");
    setTimezone(live.timezone ?? "");
    setFacility(live.location_struct?.facility ?? "");
    setRoom(live.location_struct?.room ?? "");
    setCity(live.location_struct?.city ?? "");
    setReminders(new Set<number>(live.reminder_offsets_minutes ?? []));
    setMeetingUrl(live.meeting_url ?? "");
    setLinks(live.links ?? []);
    setConflict(false);
    setError(null);
  }

  async function submit(): Promise<void> {
    if (busy) return;
    if (!title.trim()) {
      setError(t("titleRequired"));
      return;
    }
    if (dateError) {
      setError(dateError);
      return;
    }
    if (tzError) {
      setError(tzError);
      return;
    }
    if (!dateOnly && !anchorStart) {
      // The start anchor is what event_date is derived from, for EVERY
      // status: emptying it would strand the row on a date computed from a
      // timestamp that no longer exists. The server refuses it too (422
      // anchor_not_clearable), and for planned/confirmed the DB CHECK
      // ck_clinical_events_time_required_by_status says the same.
      setError(t("startRequired"));
      return;
    }
    if (needsReason && !reason.trim()) {
      setError(t("reasonRequired"));
      return;
    }
    const patch = buildMetadataPatch(live, {
      kind,
      title,
      note,
      bodyPart,
      facility,
      room,
      city,
      reminders: Array.from(reminders),
      meetingUrl,
      links,
    });
    const wantsPatch = hasMetadataChanges(patch);
    if (!amendPayload && !wantsPatch) {
      // Nothing moved. Closing beats a no-op round trip that would bump
      // the etag and invalidate every other open view of this event.
      onClose();
      return;
    }
    setBusy(true);
    setError(null);
    setConflict(false);
    let current = live;
    if (amendPayload) {
      try {
        current = await calendarApi.amendEventTime(live.id, amendPayload, {
          etag: live.etag,
          idempotencyKey: idempotencyKey.current,
        });
        setLive(current);
        // The amendment landed: the next save is a NEW logical write and
        // must not replay this one.
        idempotencyKey.current = newIdempotencyKey();
      } catch (e) {
        setError(describeError(e));
        // 412 means the row moved under us. Re-base on the server's copy so
        // a second Save is possible at all, instead of leaving the user on a
        // stale etag that can only fail again.
        if (e instanceof ApiError && e.status === 412) {
          setConflict(true);
          await refreshBaseline();
        }
        setBusy(false);
        return;
      }
    }
    if (!wantsPatch) {
      setBusy(false);
      onSaved(current);
      return;
    }
    try {
      // The amendment bumped the etag; reusing the stale one would 412.
      current = await calendarApi.patchEvent(live.id, patch, { etag: current.etag });
      setBusy(false);
      onSaved(current);
    } catch (e) {
      setBusy(false);
      if (e instanceof ApiError && e.status === 412 && !amendPayload) {
        setError(describeError(e));
        setConflict(true);
        await refreshBaseline();
        return;
      }
      if (amendPayload) {
        // The date IS saved. Saying "error" here would read as "nothing
        // was written" and push the user into re-doing a landed write.
        setError(t("amendSavedPatchFailed", { error: describeError(e) }));
        onPartialSave?.(current);
      } else {
        setError(describeError(e));
      }
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
          color: "var(--bv-fg)",
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

          {/* Clinical time. One section, whatever the status: which fields
           * it shows is decided by the anchor family, not by the user. */}
          <fieldset style={timeBox}>
            <legend style={legendStyle}>{t("whenLabel")}</legend>
            {/* Same four-way caption the drawer uses: a row whose anchor is
             * missing is NOT a date-only import, and saying so would be
             * false for every missed appointment. */}
            <p style={captionStyle}>{t(captionKey)}</p>
            {/* ... and when there IS a booked time behind the missing
             * anchor, show it: it is the only time this row knows, and the
             * date being edited was derived from it. */}
            {captionKey === "dateSourceMissingAnchor" && bookedInstant && (
              <p style={captionStyle}>
                {t("originallyPlannedLabel")}: {formatInZone(bookedInstant, locale, live.timezone)}
                {/* Name the zone this clock is in, exactly as the drawer
                 * does: it is the one line here that shows an hour the
                 * reader's own device would render differently. */}
                {live.timezone ? ` · ${live.timezone}` : ""}
              </p>
            )}
            {dateOnly ? (
              <label style={field}>
                <span style={fieldLabel}>{t("eventDateLabel")}</span>
                <input
                  id={startId}
                  type="date"
                  required
                  value={eventDate}
                  max={family === "actual" ? todayLocalDate() : undefined}
                  onChange={(e) => setEventDate(e.target.value)}
                  aria-invalid={dateError ? true : undefined}
                  aria-describedby={startDescribedBy}
                  style={inputStyle}
                />
              </label>
            ) : (
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
                <label style={field}>
                  <span style={fieldLabel}>
                    {family === "planned" ? t("plannedStart") : t("actualStart")}
                  </span>
                  <input
                    id={startId}
                    type="datetime-local"
                    // Required for EVERY status: this is the timestamp
                    // event_date is derived from, and the server refuses to
                    // clear it (422 anchor_not_clearable).
                    required
                    value={anchorStart}
                    max={family === "actual" ? nowLocalInputValue() : undefined}
                    onChange={(e) => setAnchorStart(e.target.value)}
                    aria-invalid={dateError ? true : undefined}
                    aria-describedby={startDescribedBy}
                    style={inputStyle}
                  />
                </label>
                <label style={field}>
                  <span style={fieldLabel}>
                    {family === "planned" ? t("plannedEndOptional") : t("actualEndOptional")}
                  </span>
                  <input
                    id={endId}
                    type="datetime-local"
                    value={anchorEnd}
                    onChange={(e) => setAnchorEnd(e.target.value)}
                    aria-invalid={validation.errorKey === "amendEndBeforeStart" ? true : undefined}
                    aria-describedby={
                      validation.errorKey === "amendEndBeforeStart" ? errorId : undefined
                    }
                    style={inputStyle}
                  />
                </label>
              </div>
            )}
            {viewerZone && (
              <p id={zoneHintId} style={captionStyle}>
                {t("localTimeZoneHint", { viewer: viewerZone, event: eventZone })}
              </p>
            )}
            {family === "actual" && <p style={captionStyle}>{t("actualStartHint")}</p>}
            <label style={{ ...field, marginTop: 8 }}>
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
            {/* What will actually be stored in event_date. Live, so moving
             * the zone across midnight shows the day move instead of
             * changing the record behind the user's back. */}
            {previewLabel && (
              <p id={previewId} style={captionStyle}>
                {t("recordedDatePreview", { date: previewLabel })}
              </p>
            )}
            {needsReason && (
              <label style={{ ...field, marginTop: 8 }}>
                <span style={fieldLabel}>{t("amendReasonLabel")}</span>
                <input
                  id={reasonId}
                  type="text"
                  required
                  maxLength={255}
                  value={reason}
                  onChange={(e) => setReason(e.target.value)}
                  style={inputStyle}
                  placeholder={t("amendReasonPlaceholder")}
                />
                <span style={captionStyle}>{t("amendReasonHint")}</span>
              </label>
            )}
            {dateError && (
              <p id={errorId} role="alert" style={inlineError}>
                {dateError}
              </p>
            )}
            {!dateError && dateWarning && (
              <output id={warningId} style={inlineWarning}>
                {dateWarning}
              </output>
            )}
          </fieldset>

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
            <AttachmentsManager eventId={live.id} patientId={live.patient_id} />
          </div>
          <div>
            <span style={fieldLabel}>{t("noteLabel")}</span>
            <EvidenceEditor value={note} onChange={setNote} embedded patientId={live.patient_id} />
          </div>
          {error && (
            <p role="alert" style={inlineError}>
              {error}
            </p>
          )}
          {/* A 412 used to dead-end the dialog: the etag could never become
           * valid again without closing and re-opening. The baseline has
           * already been re-fetched, so Save works; this button is for the
           * user who would rather see the server's values than keep theirs. */}
          {conflict && (
            <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
              <span style={captionStyle}>{t("conflictReloadHint")}</span>
              <button type="button" className="ghost" onClick={reseedFromServer} disabled={busy}>
                {t("conflictReloadAction")}
              </button>
            </div>
          )}
          <div style={{ display: "flex", justifyContent: "flex-end", gap: 8 }}>
            <button type="button" className="ghost" onClick={onClose} disabled={busy}>
              {t("cancelDialog")}
            </button>
            <button type="submit" disabled={busy || dateError !== null || tzError !== ""}>
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
const timeBox = {
  border: "1px solid var(--bv-card-border, #e5e7eb)",
  borderRadius: 6,
  padding: "0.5rem 0.6rem 0.6rem",
  margin: 0,
  minWidth: 0,
} as const;
const legendStyle = {
  fontSize: "0.72rem",
  textTransform: "uppercase",
  letterSpacing: "0.04em",
  color: "var(--bv-fg-soft)",
  padding: "0 0.3rem",
} as const;
const captionStyle = {
  // Also used on a <span> inside a <label>, where inline layout would drop
  // the vertical margin.
  display: "block",
  fontSize: "0.72rem",
  color: "var(--bv-fg-soft)",
  margin: "0 0 0.4rem",
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
  margin: "0.4rem 0 0",
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
  margin: "0.4rem 0 0",
} as const;
