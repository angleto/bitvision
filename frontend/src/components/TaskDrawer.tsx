"use client";

// Side-panel drawer for one PatientTask. Mirrors EventDrawer's UX
// contract: right-edge slide-over with backdrop dismiss + Esc, full
// task detail + FSM transition buttons enabled per
// ``task_fsm.allowedVerbs(currentStatus)``.
//
// Transitions that need parameters (snooze → snooze_until, drop →
// reason) collect the input via a small inline form below the action
// row. Transitions without parameters (start, complete, reopen) fire
// immediately with a fresh Idempotency-Key.
//
// The drawer does NOT own the task list — it works on a single id and
// signals state changes via ``onChanged`` so the parent re-fetches
// (TaskTimeline / MergedTimelineView use this to refresh).

import { useTranslations } from "next-intl";
import { useEffect, useMemo, useRef, useState } from "react";

import { ApiError } from "@/lib/api";
import { type PatientTask, type TaskTransitionVerb, tasksApi } from "@/lib/api_records";
import { allowedVerbs } from "@/lib/task_fsm";

interface Props {
  taskId: string;
  isOwner: boolean;
  onClose: () => void;
  onChanged: () => void;
}

type PendingAction =
  | { verb: "snooze"; snooze_until: string; reason: string }
  | { verb: "drop"; reason: string }
  | { verb: "reopen"; reason: string }
  | null;

function generateIdemKey(): string {
  // Browser-side: prefer crypto.randomUUID when available; fall back
  // to a date+random suffix that's good enough for replay defence in
  // a single-user UI flow.
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return crypto.randomUUID();
  }
  return `idem-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
}

export default function TaskDrawer({ taskId, isOwner, onClose, onChanged }: Props) {
  const t = useTranslations("taskActions");
  const tStatus = useTranslations("taskStatus");
  const tCat = useTranslations("taskCategory");
  const tPri = useTranslations("taskPriority");
  const [task, setTask] = useState<PatientTask | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState<PendingAction>(null);
  const [busy, setBusy] = useState(false);
  const mouseDownOnBackdrop = useRef(false);

  useEffect(() => {
    let cancelled = false;
    tasksApi
      .read(taskId)
      .then((row) => {
        if (!cancelled) setTask(row);
      })
      .catch((e: unknown) => {
        if (!cancelled)
          setError(e instanceof ApiError ? `${e.status}: ${String(e.detail)}` : String(e));
      });
    return () => {
      cancelled = true;
    };
  }, [taskId]);

  useEffect(() => {
    function onKey(e: KeyboardEvent): void {
      if (e.key === "Escape") onClose();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const verbs = useMemo<TaskTransitionVerb[]>(
    () => (task ? allowedVerbs(task.status) : []),
    [task],
  );

  async function runTransition(
    verb: TaskTransitionVerb,
    body: Parameters<typeof tasksApi.transition>[2],
  ): Promise<void> {
    if (!task || busy) return;
    setBusy(true);
    setError(null);
    try {
      await tasksApi.transition(task.id, verb, body, task.etag, generateIdemKey());
      onChanged();
    } catch (e: unknown) {
      setError(e instanceof ApiError ? `${e.status}: ${String(e.detail)}` : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function handleVerbClick(verb: TaskTransitionVerb): Promise<void> {
    if (!task || busy) return;
    if (verb === "snooze") {
      setPending({ verb: "snooze", snooze_until: "", reason: "" });
      return;
    }
    if (verb === "drop") {
      setPending({ verb: "drop", reason: "" });
      return;
    }
    if (verb === "reopen") {
      setPending({ verb: "reopen", reason: "" });
      return;
    }
    if (verb === "start") {
      await runTransition("start", {} as never);
      return;
    }
    if (verb === "wake") {
      await runTransition("wake", { resume_in_progress: false });
      return;
    }
    if (verb === "complete") {
      await runTransition("complete", { completed_at: new Date().toISOString(), note: null });
      return;
    }
  }

  async function submitPendingAction(): Promise<void> {
    if (!pending || !task) return;
    if (pending.verb === "snooze") {
      if (!pending.snooze_until) {
        setError(t("snoozeRequireWhen"));
        return;
      }
      await runTransition("snooze", {
        snooze_until: pending.snooze_until,
        reason: pending.reason || null,
      });
      setPending(null);
      return;
    }
    if (pending.verb === "drop") {
      if (!pending.reason.trim()) {
        setError(t("dropRequireReason"));
        return;
      }
      await runTransition("drop", { reason: pending.reason });
      setPending(null);
      return;
    }
    if (pending.verb === "reopen") {
      await runTransition("reopen", { reason: pending.reason || null });
      setPending(null);
      return;
    }
  }

  async function handleSoftDelete(): Promise<void> {
    if (!task || busy) return;
    if (!confirm(t("confirmDelete"))) return;
    setBusy(true);
    setError(null);
    try {
      await tasksApi.remove(task.id, task.etag);
      onChanged();
    } catch (e: unknown) {
      setError(e instanceof ApiError ? `${e.status}: ${String(e.detail)}` : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    // biome-ignore lint/a11y/useSemanticElements: same modal pattern as EventDrawer — native <dialog> doesn't support right-edge slide-over with backdrop dismissal.
    // biome-ignore lint/a11y/useKeyWithClickEvents: Esc is wired via keydown; explicit ✕ button is the focusable close affordance.
    <div
      role="dialog"
      aria-modal="true"
      aria-label="Task detail"
      style={{
        position: "fixed",
        inset: 0,
        background: "rgba(0,0,0,0.35)",
        display: "flex",
        justifyContent: "flex-end",
        zIndex: 1000,
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
      <aside
        style={{
          width: "min(480px, 100%)",
          maxWidth: "100%",
          background: "var(--bv-card-bg, #fff)",
          borderLeft: "1px solid var(--bv-card-border, #d0d5dd)",
          padding: "1rem 1.2rem",
          overflowY: "auto",
        }}
      >
        <header
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            marginBottom: "0.6rem",
          }}
        >
          <h2 style={{ margin: 0, fontSize: "1rem" }}>{t("heading")}</h2>
          <button type="button" className="ghost" onClick={onClose} aria-label={t("close")}>
            ✕
          </button>
        </header>

        {error && (
          <p role="alert" style={{ color: "var(--bv-danger, #c00)", fontSize: "0.85rem" }}>
            {error}
          </p>
        )}

        {!task && !error && <p>{t("loading")}</p>}

        {task && (
          <>
            <h3 style={{ margin: "0.4rem 0 0.6rem", fontSize: "1rem" }}>{task.title}</h3>
            <dl
              style={{
                display: "grid",
                gridTemplateColumns: "auto 1fr",
                gap: "0.25rem 0.8rem",
                fontSize: "0.85rem",
                marginBottom: "0.85rem",
              }}
            >
              <dt style={dtStyle}>{t("fieldStatus")}</dt>
              <dd>{tStatus(task.status)}</dd>
              <dt style={dtStyle}>{t("fieldDueAt")}</dt>
              <dd>{task.due_at ? new Date(task.due_at).toLocaleString() : "—"}</dd>
              {task.snooze_until && (
                <>
                  <dt style={dtStyle}>{t("fieldSnoozeUntil")}</dt>
                  <dd>{new Date(task.snooze_until).toLocaleString()}</dd>
                </>
              )}
              {task.completed_at && (
                <>
                  <dt style={dtStyle}>{t("fieldCompletedAt")}</dt>
                  <dd>{new Date(task.completed_at).toLocaleString()}</dd>
                </>
              )}
              <dt style={dtStyle}>{t("fieldCategory")}</dt>
              <dd>{tCat(task.category)}</dd>
              <dt style={dtStyle}>{t("fieldPriority")}</dt>
              <dd>{tPri(task.priority)}</dd>
              <dt style={dtStyle}>{t("fieldAuthor")}</dt>
              <dd>
                {task.author_kind === "agent"
                  ? t("authorAgent")
                  : task.author_kind === "system"
                    ? t("authorSystem")
                    : t("authorHuman")}
              </dd>
              {task.description && (
                <>
                  <dt style={dtStyle}>{t("fieldDescription")}</dt>
                  <dd style={{ whiteSpace: "pre-wrap" }}>{task.description}</dd>
                </>
              )}
              {task.status_change_reason && (
                <>
                  <dt style={dtStyle}>{t("fieldLastReason")}</dt>
                  <dd>{task.status_change_reason}</dd>
                </>
              )}
            </dl>

            {isOwner && verbs.length > 0 && (
              <div
                style={{
                  display: "flex",
                  flexWrap: "wrap",
                  gap: "0.4rem",
                  marginBottom: "0.85rem",
                }}
              >
                {verbs.map((verb) => (
                  <button
                    key={verb}
                    type="button"
                    onClick={() => handleVerbClick(verb)}
                    disabled={busy}
                    aria-disabled={busy}
                  >
                    {t(`verb_${verb}`)}
                  </button>
                ))}
                <button
                  type="button"
                  className="ghost"
                  onClick={handleSoftDelete}
                  disabled={busy}
                  aria-disabled={busy}
                  title={t("softDeleteTitle")}
                  style={{ marginLeft: "auto" }}
                >
                  {t("softDelete")}
                </button>
              </div>
            )}

            {pending?.verb === "snooze" && (
              <PendingForm
                title={t("snoozeFormTitle")}
                onCancel={() => setPending(null)}
                onSubmit={submitPendingAction}
                busy={busy}
              >
                <label style={labelStyle}>
                  {t("snoozeWhenLabel")}
                  <input
                    type="datetime-local"
                    value={pending.snooze_until}
                    onChange={(e) => setPending({ ...pending, snooze_until: e.target.value })}
                    required
                    style={inputStyle}
                  />
                </label>
                <label style={labelStyle}>
                  {t("snoozeReasonLabel")}
                  <input
                    type="text"
                    value={pending.reason}
                    onChange={(e) => setPending({ ...pending, reason: e.target.value })}
                    style={inputStyle}
                  />
                </label>
              </PendingForm>
            )}

            {pending?.verb === "drop" && (
              <PendingForm
                title={t("dropFormTitle")}
                onCancel={() => setPending(null)}
                onSubmit={submitPendingAction}
                busy={busy}
              >
                <label style={labelStyle}>
                  {t("dropReasonLabel")}
                  <input
                    type="text"
                    value={pending.reason}
                    onChange={(e) => setPending({ ...pending, reason: e.target.value })}
                    required
                    style={inputStyle}
                  />
                </label>
              </PendingForm>
            )}

            {pending?.verb === "reopen" && (
              <PendingForm
                title={t("reopenFormTitle")}
                onCancel={() => setPending(null)}
                onSubmit={submitPendingAction}
                busy={busy}
              >
                <label style={labelStyle}>
                  {t("reopenReasonLabel")}
                  <input
                    type="text"
                    value={pending.reason}
                    onChange={(e) => setPending({ ...pending, reason: e.target.value })}
                    style={inputStyle}
                  />
                </label>
              </PendingForm>
            )}
          </>
        )}
      </aside>
    </div>
  );
}

function PendingForm({
  title,
  onCancel,
  onSubmit,
  busy,
  children,
}: {
  title: string;
  onCancel: () => void;
  onSubmit: () => void;
  busy: boolean;
  children: React.ReactNode;
}) {
  const t = useTranslations("taskActions");
  return (
    <section
      style={{
        border: "1px solid var(--bv-card-border, #d0d5dd)",
        borderRadius: 8,
        padding: "0.8rem 1rem",
        marginTop: "0.6rem",
      }}
    >
      <h4 style={{ margin: "0 0 0.6rem", fontSize: "0.9rem" }}>{title}</h4>
      <div style={{ display: "flex", flexDirection: "column", gap: "0.5rem" }}>{children}</div>
      <div style={{ display: "flex", gap: "0.4rem", marginTop: "0.8rem" }}>
        <button type="button" onClick={onSubmit} disabled={busy} aria-disabled={busy}>
          {t("submit")}
        </button>
        <button
          type="button"
          className="ghost"
          onClick={onCancel}
          disabled={busy}
          aria-disabled={busy}
        >
          {t("cancel")}
        </button>
      </div>
    </section>
  );
}

const dtStyle = {
  color: "var(--bv-fg-soft)",
  fontSize: "0.78rem",
} as const;

const labelStyle = {
  display: "flex",
  flexDirection: "column",
  gap: "0.2rem",
  fontSize: "0.82rem",
} as const;

const inputStyle = {
  fontSize: "0.85rem",
  padding: "0.3rem 0.4rem",
  border: "1px solid var(--bv-card-border, #d0d5dd)",
  borderRadius: 6,
  background: "var(--bv-card-bg, transparent)",
} as const;
