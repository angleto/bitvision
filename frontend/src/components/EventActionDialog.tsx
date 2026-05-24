"use client";

// Generic modal that drives all five FSM transitions:
// confirm / reschedule / complete / cancel / markMissed.
// Each ``ActionKind`` value selects:
//   - which body fields to render
//   - which calendar_api method to call
//   - which i18n string for the button + title
// One component, no duplication.

import { useTranslations } from "next-intl";
import { useRef, useState } from "react";

import { ApiError } from "@/lib/api";
import type { ClinicalEvent } from "@/lib/api_records";
import { calendarApi, newIdempotencyKey } from "@/lib/calendar_api";

export type ActionKind = "confirm" | "reschedule" | "complete" | "cancel" | "markMissed";

interface Props {
  event: ClinicalEvent;
  action: ActionKind;
  onClose: () => void;
  onSuccess: (replacedEventId?: string | null) => void;
}

export default function EventActionDialog({ event, action, onClose, onSuccess }: Props) {
  const t = useTranslations("eventActions");
  const [reason, setReason] = useState("");
  const [newPlannedStartAt, setNewPlannedStartAt] = useState(
    toLocalInputValue(event.planned_start_at) || toLocalInputValue(new Date().toISOString()),
  );
  const [newPlannedEndAt, setNewPlannedEndAt] = useState("");
  const [actualStartAt, setActualStartAt] = useState(toLocalInputValue(new Date().toISOString()));
  const [narrative, setNarrative] = useState("");
  const [note, setNote] = useState("");
  const [confirmedAt, setConfirmedAt] = useState(toLocalInputValue(new Date().toISOString()));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Drag-out protection: mousedown must originate on backdrop for
  // the click to count as a dismissal.
  const mouseDownOnBackdrop = useRef(false);

  async function submit(): Promise<void> {
    if (busy) return;
    setBusy(true);
    setError(null);
    const idempotencyKey = newIdempotencyKey();
    try {
      if (action === "confirm") {
        await calendarApi.confirmEvent(event.id, {
          etag: event.etag,
          idempotencyKey,
          confirmedAt: confirmedAt ? new Date(confirmedAt).toISOString() : undefined,
        });
        onSuccess();
      } else if (action === "reschedule") {
        if (!reason.trim()) {
          setError(t("reasonRequired"));
          return;
        }
        const res = await calendarApi.rescheduleEvent(
          event.id,
          {
            new_planned_start_at: new Date(newPlannedStartAt).toISOString(),
            new_planned_end_at: newPlannedEndAt
              ? new Date(newPlannedEndAt).toISOString()
              : undefined,
            timezone: event.timezone ?? undefined,
            reason: reason.trim(),
          },
          { etag: event.etag, idempotencyKey },
        );
        onSuccess(res.replacedEventId);
      } else if (action === "complete") {
        await calendarApi.completeEvent(
          event.id,
          {
            actual_start_at: new Date(actualStartAt).toISOString(),
            narrative: narrative.trim() || undefined,
          },
          { etag: event.etag, idempotencyKey },
        );
        onSuccess();
      } else if (action === "cancel") {
        if (!reason.trim()) {
          setError(t("reasonRequired"));
          return;
        }
        await calendarApi.cancelEvent(
          event.id,
          { reason: reason.trim() },
          { etag: event.etag, idempotencyKey },
        );
        onSuccess();
      } else if (action === "markMissed") {
        await calendarApi.markMissed(
          event.id,
          { note: note.trim() || undefined },
          { etag: event.etag, idempotencyKey },
        );
        onSuccess();
      }
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
    // biome-ignore lint/a11y/useSemanticElements: native <dialog> doesn't support a centred modal with backdrop dismissal across browsers; we use role="dialog" + aria-modal.
    // biome-ignore lint/a11y/useKeyWithClickEvents: backdrop click is a nicety; Esc closes via the parent's keydown handler and the explicit cancel button is keyboard-focusable.
    <div
      role="dialog"
      aria-modal="true"
      aria-label={t(action)}
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
          width: "min(440px, 95%)",
          background: "var(--bv-card-bg, #fff)",
          borderRadius: 8,
          padding: "1rem",
        }}
      >
        <h3 style={{ marginTop: 0 }}>{t(`${action}Title`)}</h3>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            void submit();
          }}
          style={{ display: "flex", flexDirection: "column", gap: "0.6rem" }}
        >
          {action === "confirm" && (
            <Field label={t("confirmedAt")}>
              <input
                type="datetime-local"
                value={confirmedAt}
                onChange={(e) => setConfirmedAt(e.target.value)}
                style={inputStyle}
              />
            </Field>
          )}
          {action === "reschedule" && (
            <>
              <Field label={t("newStart")}>
                <input
                  type="datetime-local"
                  required
                  value={newPlannedStartAt}
                  onChange={(e) => setNewPlannedStartAt(e.target.value)}
                  style={inputStyle}
                />
              </Field>
              <Field label={t("newEndOptional")}>
                <input
                  type="datetime-local"
                  value={newPlannedEndAt}
                  onChange={(e) => setNewPlannedEndAt(e.target.value)}
                  style={inputStyle}
                />
              </Field>
              <Field label={t("reasonLabel")}>
                <input
                  type="text"
                  required
                  maxLength={255}
                  value={reason}
                  onChange={(e) => setReason(e.target.value)}
                  style={inputStyle}
                />
              </Field>
            </>
          )}
          {action === "complete" && (
            <>
              <Field label={t("actualStart")}>
                <input
                  type="datetime-local"
                  required
                  value={actualStartAt}
                  onChange={(e) => setActualStartAt(e.target.value)}
                  style={inputStyle}
                />
              </Field>
              <Field label={t("narrativeOptional")}>
                <textarea
                  rows={3}
                  value={narrative}
                  onChange={(e) => setNarrative(e.target.value)}
                  style={inputStyle}
                />
              </Field>
            </>
          )}
          {action === "cancel" && (
            <Field label={t("reasonLabel")}>
              <input
                type="text"
                required
                maxLength={255}
                value={reason}
                onChange={(e) => setReason(e.target.value)}
                style={inputStyle}
              />
            </Field>
          )}
          {action === "markMissed" && (
            <Field label={t("noteOptional")}>
              <input
                type="text"
                maxLength={255}
                value={note}
                onChange={(e) => setNote(e.target.value)}
                style={inputStyle}
              />
            </Field>
          )}
          {error && <p style={{ color: "var(--bv-danger, #c00)" }}>{error}</p>}
          <div style={{ display: "flex", justifyContent: "flex-end", gap: 8 }}>
            <button type="button" className="ghost" onClick={onClose} disabled={busy}>
              {t("cancelDialog")}
            </button>
            <button type="submit" disabled={busy}>
              {busy ? "…" : t(`${action}Submit`)}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

function Field({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    // biome-ignore lint/a11y/noLabelWithoutControl: ``children`` is the form control (input/textarea/select) injected by the parent; biome can't see through the slot pattern.
    <label style={{ display: "flex", flexDirection: "column", gap: 4 }}>
      <span style={{ fontSize: "0.75rem", color: "var(--bv-fg-soft)" }}>{label}</span>
      {children}
    </label>
  );
}

const inputStyle = {
  fontSize: "0.88rem",
  padding: "0.35rem 0.5rem",
  border: "1px solid var(--bv-card-border, #d0d5dd)",
  borderRadius: 6,
  background: "var(--bv-card-bg, transparent)",
  color: "var(--bv-fg)",
  width: "100%",
} as const;

function toLocalInputValue(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  // Build a yyyy-mm-ddThh:mm string in local time (the format
  // expected by <input type="datetime-local">).
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
