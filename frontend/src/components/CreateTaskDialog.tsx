"use client";

// Form to create a new operational PatientTask. Posts to
// ``POST /api/patient-tasks`` with an Idempotency-Key. Mirrors the
// shape and UX of PlanEventDialog so the two creation flows feel
// uniform (modal, backdrop dismiss, Esc, busy state).
//
// Phase picker pulls the patient's care phases lazily on mount.
// Reminder offsets, labels and links are optional; everything else
// falls back to backend defaults.

import { useLocale, useTranslations } from "next-intl";
import { useEffect, useRef, useState } from "react";

import UrlListEditor, { type UrlItem } from "@/components/UrlListEditor";
import { ApiError } from "@/lib/api";
import {
  type CarePhase,
  type PatientTaskCreate,
  type TaskCategory,
  type TaskPriority,
  carePhasesApi,
  tasksApi,
} from "@/lib/api_records";

interface Props {
  patientId: string;
  onClose: () => void;
  onCreated: () => void;
}

const CATEGORIES: readonly TaskCategory[] = [
  "admin",
  "pharmacy",
  "appointment_prep",
  "transport",
  "communication",
  "personal",
  "other",
] as const;

const PRIORITIES: readonly TaskPriority[] = ["low", "normal", "high", "urgent"] as const;

const REMINDER_OFFSETS = [-15, -30, -120, -1440, -2880, -7200] as const;

function newIdempotencyKey(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return crypto.randomUUID();
  }
  return `idem-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
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

function humanReminder(min: number): string {
  const abs = Math.abs(min);
  if (abs >= 1440) return `${abs / 1440}d`;
  if (abs >= 60) return `${abs / 60}h`;
  return `${abs}m`;
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

function parseLabels(raw: string): string[] | null {
  const tokens = raw
    .split(",")
    .map((s) => s.trim())
    .filter((s) => s.length > 0);
  return tokens.length > 0 ? tokens : null;
}

export default function CreateTaskDialog({ patientId, onClose, onCreated }: Props) {
  const t = useTranslations("taskCreate");
  const tCat = useTranslations("taskCategory");
  const tPri = useTranslations("taskPriority");
  const locale = useLocale();

  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [category, setCategory] = useState<TaskCategory>("other");
  const [priority, setPriority] = useState<TaskPriority>("normal");
  const [dueAt, setDueAt] = useState("");
  const [timezone, setTimezone] = useState(detectTimezone());
  const [phaseId, setPhaseId] = useState<string>("");
  const [reminders, setReminders] = useState<Set<number>>(new Set());
  const [labelsRaw, setLabelsRaw] = useState("");
  const [links, setLinks] = useState<UrlItem[]>([]);

  const [phases, setPhases] = useState<CarePhase[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const titleInputRef = useRef<HTMLInputElement | null>(null);
  const mouseDownOnBackdrop = useRef(false);

  // Autofocus title on mount — the most common path is "type title, hit Enter".
  useEffect(() => {
    titleInputRef.current?.focus();
  }, []);

  // Esc closes — same UX as TaskDrawer.
  useEffect(() => {
    function onKey(e: KeyboardEvent): void {
      if (e.key === "Escape") onClose();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  // Lazy-load the patient's care phases so the user can attach the
  // task to one. Failure is non-fatal: the picker degrades to "no phase".
  useEffect(() => {
    let cancelled = false;
    carePhasesApi
      .list(patientId)
      .then((rows) => {
        if (!cancelled) setPhases(rows);
      })
      .catch(() => {
        if (!cancelled) setPhases([]);
      });
    return () => {
      cancelled = true;
    };
  }, [patientId]);

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
    const trimmedTitle = title.trim();
    if (!trimmedTitle) {
      setError(t("titleRequired"));
      titleInputRef.current?.focus();
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const cleanLinks = links
        .filter((l) => l.url.trim())
        .map((l) => ({ label: l.label, url: l.url }));
      const body: PatientTaskCreate = {
        patient_id: patientId,
        title: trimmedTitle,
        description: description.trim() || null,
        category,
        priority,
        due_at: dueAt ? new Date(dueAt).toISOString() : null,
        timezone: timezone.trim() || null,
        phase_id: phaseId || null,
        labels: parseLabels(labelsRaw),
        links: cleanLinks.length > 0 ? cleanLinks : null,
        reminder_offsets_minutes:
          reminders.size > 0 ? Array.from(reminders).sort((a, b) => a - b) : null,
      };
      await tasksApi.create(body, newIdempotencyKey());
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

  const phaseLabel = (p: CarePhase): string => p.name_i18n?.[locale] ?? p.name;

  return (
    // biome-ignore lint/a11y/useSemanticElements: same modal pattern as PlanEventDialog — native <dialog> is uneven across browsers.
    // biome-ignore lint/a11y/useKeyWithClickEvents: Esc + Cancel button are the keyboard-reachable dismissals.
    <div
      role="dialog"
      aria-modal="true"
      aria-label={t("dialogTitle")}
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
          color: "var(--bv-fg)",
        }}
      >
        <h3 style={{ marginTop: 0 }}>{t("dialogTitle")}</h3>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            void submit();
          }}
          style={{ display: "flex", flexDirection: "column", gap: "0.6rem" }}
        >
          <label style={field}>
            <span style={fieldLabel}>{t("titleLabel")}</span>
            <input
              ref={titleInputRef}
              type="text"
              required
              maxLength={255}
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              style={inputStyle}
              placeholder={t("titlePlaceholder")}
              aria-invalid={Boolean(error && !title.trim())}
            />
          </label>

          <label style={field}>
            <span style={fieldLabel}>{t("descriptionLabel")}</span>
            <textarea
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              style={{ ...inputStyle, minHeight: 60, fontFamily: "inherit" }}
              placeholder={t("descriptionPlaceholder")}
            />
          </label>

          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
            <label style={field}>
              <span style={fieldLabel}>{t("categoryLabel")}</span>
              <select
                value={category}
                onChange={(e) => setCategory(e.target.value as TaskCategory)}
                style={inputStyle}
              >
                {CATEGORIES.map((c) => (
                  <option key={c} value={c}>
                    {tCat(c)}
                  </option>
                ))}
              </select>
            </label>
            <label style={field}>
              <span style={fieldLabel}>{t("priorityLabel")}</span>
              <select
                value={priority}
                onChange={(e) => setPriority(e.target.value as TaskPriority)}
                style={inputStyle}
              >
                {PRIORITIES.map((p) => (
                  <option key={p} value={p}>
                    {tPri(p)}
                  </option>
                ))}
              </select>
            </label>
          </div>

          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
            <label style={field}>
              <span style={fieldLabel}>{t("dueAtLabel")}</span>
              <input
                type="datetime-local"
                value={dueAt}
                onChange={(e) => setDueAt(e.target.value)}
                style={inputStyle}
              />
            </label>
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
          </div>

          {phases && phases.length > 0 && (
            <label style={field}>
              <span style={fieldLabel}>{t("phaseLabel")}</span>
              <select
                value={phaseId}
                onChange={(e) => setPhaseId(e.target.value)}
                style={inputStyle}
              >
                <option value="">{t("phaseNone")}</option>
                {phases.map((p) => (
                  <option key={p.id} value={p.id}>
                    {phaseLabel(p)}
                  </option>
                ))}
              </select>
            </label>
          )}

          <div>
            <span style={fieldLabel}>{t("remindersLabel")}</span>
            <div style={{ display: "flex", flexWrap: "wrap", gap: 4, marginTop: 4 }}>
              {REMINDER_OFFSETS.map((m) => (
                <button
                  key={m}
                  type="button"
                  className={reminders.has(m) ? undefined : "ghost"}
                  aria-pressed={reminders.has(m)}
                  onClick={() => toggleReminder(m)}
                  disabled={!dueAt}
                  aria-disabled={!dueAt}
                  title={!dueAt ? t("remindersNeedDue") : undefined}
                  style={{ fontSize: "0.75rem", padding: "0.15rem 0.5rem", borderRadius: 999 }}
                >
                  {humanReminder(m)}
                </button>
              ))}
            </div>
            {!dueAt && (
              <p style={{ margin: "4px 0 0", fontSize: "0.72rem", color: "var(--bv-fg-soft)" }}>
                {t("remindersNeedDue")}
              </p>
            )}
          </div>

          <label style={field}>
            <span style={fieldLabel}>{t("labelsLabel")}</span>
            <input
              type="text"
              value={labelsRaw}
              onChange={(e) => setLabelsRaw(e.target.value)}
              style={inputStyle}
              placeholder={t("labelsPlaceholder")}
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

          {error && (
            <p role="alert" style={{ color: "var(--bv-danger, #c00)", margin: 0 }}>
              {error}
            </p>
          )}

          <div style={{ display: "flex", justifyContent: "flex-end", gap: 8 }}>
            <button type="button" className="ghost" onClick={onClose} disabled={busy}>
              {t("cancel")}
            </button>
            <button type="submit" disabled={busy} aria-disabled={busy}>
              {busy ? "…" : t("submit")}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
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
