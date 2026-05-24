"use client";

// "Operazioni in corso" header widget (DESIGN.md §11.8).
//
// Polls the active-jobs endpoint at a low frequency so the user can
// see what is currently running across the platform (Fascicolo
// export, GDPR export, future consumers). Clicking on a succeeded
// job triggers the browser download via the freshly-signed URL on
// the row; cancelling sets the row to ``cancelled`` immediately and
// the worker honours that at its next checkpoint.
//
// The panel is intentionally minimal: a button-with-count + a
// dropdown. A richer "operations" page can come later if the surface
// grows past a handful of concurrent jobs per user.

import { useTranslations } from "next-intl";
import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError, downloadJobResult } from "@/lib/api";
import { type JobOut, dismissedJobs, jobsApi, jobsStorage } from "@/lib/jobs";
import { useActiveJobs, useRecentJobs } from "@/lib/useJob";

export default function ActiveJobsPanel() {
  const t = useTranslations("activeJobs");
  const [open, setOpen] = useState(false);
  const [cancelBusy, setCancelBusy] = useState<string | null>(null);
  const [downloadBusy, setDownloadBusy] = useState<string | null>(null);
  const { jobs, refetch } = useActiveJobs();
  // Recent succeeded jobs that still have a downloadable result. Used
  // by users who closed the export dialog before completion: without
  // this surface the ZIP would be reachable only via a freshly-minted
  // /api/jobs/{id} fetch, and there's no UI hook to discover the id
  // after the dialog disappeared.
  const { jobs: recentJobs } = useRecentJobs({ intervalMs: 60_000, limit: 30 });
  // Per-device dismissed set so the user can clear rows they've
  // already consumed without waiting for the 48h TTL. Re-read on a
  // cross-tab event so dismissing on one tab updates the badge on
  // all the others.
  const [dismissedTick, setDismissedTick] = useState(0);
  useEffect(() => {
    function onChange() {
      setDismissedTick((v) => v + 1);
    }
    window.addEventListener("bv:dismissed-jobs-changed", onChange);
    return () => window.removeEventListener("bv:dismissed-jobs-changed", onChange);
  }, []);
  // Read into a memo-like local so children share the same Set.
  const dismissed = (() => {
    void dismissedTick; // re-evaluate on bump
    return dismissedJobs.list();
  })();
  const succeededDownloadable = recentJobs.filter(
    (j) => j.status === "succeeded" && j.result_download_url && !dismissed.has(j.id),
  );

  const dismissOne = useCallback((jobId: string) => {
    dismissedJobs.dismiss(jobId);
  }, []);
  const dismissAllRecent = useCallback(() => {
    dismissedJobs.dismissMany(succeededDownloadable.map((j) => j.id));
  }, [succeededDownloadable]);
  const popoverRef = useRef<HTMLDivElement | null>(null);
  const buttonRef = useRef<HTMLButtonElement | null>(null);

  // Click-outside dismissal. We listen on the document so any click
  // outside both the button and the popover closes the panel.
  useEffect(() => {
    if (!open) return;
    function onPointer(e: PointerEvent): void {
      const target = e.target as Node | null;
      if (!target) return;
      if (popoverRef.current?.contains(target)) return;
      if (buttonRef.current?.contains(target)) return;
      setOpen(false);
    }
    document.addEventListener("pointerdown", onPointer);
    return () => document.removeEventListener("pointerdown", onPointer);
  }, [open]);

  async function onCancel(job: JobOut): Promise<void> {
    setCancelBusy(job.id);
    try {
      await jobsApi.cancel(job.id);
      jobsStorage.remove(job.id);
      refetch();
    } catch {
      // Cancellation is best-effort; if it fails the row will resolve
      // through the worker's normal terminal path. No user-visible
      // error needed for this affordance.
    } finally {
      setCancelBusy(null);
    }
  }

  async function onDownloadRecent(job: JobOut): Promise<void> {
    setDownloadBusy(job.id);
    try {
      await downloadJobResult(job.id);
    } catch {
      // Failures (expired, cleaned up, server hiccup) leave the row
      // visible so the user can retry; we don't need a toast here.
    } finally {
      setDownloadBusy(null);
    }
  }

  // Hide the badge when there's nothing in either bucket. Users who
  // never enqueued a job shouldn't see an empty popover.
  if (jobs.length === 0 && succeededDownloadable.length === 0) return null;
  const activeCount = jobs.length;
  const readyCount = succeededDownloadable.length;
  const totalCount = activeCount + readyCount;

  return (
    <div style={{ position: "relative", display: "inline-block" }}>
      <button
        ref={buttonRef}
        type="button"
        onClick={() => setOpen((v) => !v)}
        title={t("toggleTitle")}
        style={{
          background: "transparent",
          border: "1px solid var(--border, #ccc)",
          borderRadius: 16,
          padding: "2px 10px",
          fontSize: "0.85rem",
          cursor: "pointer",
        }}
      >
        {t("badge", { active: activeCount, ready: readyCount, total: totalCount })}
      </button>
      {open && (
        <div
          ref={popoverRef}
          style={{
            position: "absolute",
            top: "calc(100% + 6px)",
            right: 0,
            minWidth: 320,
            maxWidth: 420,
            background: "var(--bv-card-bg, #fff)",
            color: "var(--bv-fg, inherit)",
            border: "1px solid var(--border, #ccc)",
            borderRadius: 6,
            boxShadow: "0 8px 24px rgba(0,0,0,0.12)",
            zIndex: 1100,
          }}
        >
          <div
            style={{
              padding: "0.5rem 0.75rem",
              borderBottom: "1px solid var(--border, #eee)",
              fontWeight: 600,
            }}
          >
            {t("title")}
          </div>
          <ul
            style={{
              listStyle: "none",
              margin: 0,
              padding: 0,
              maxHeight: 480,
              overflow: "auto",
            }}
          >
            {jobs.length === 0 && (
              <li
                style={{
                  padding: "0.5rem 0.75rem",
                  fontSize: "0.85em",
                  opacity: 0.7,
                }}
              >
                {t("emptyActive")}
              </li>
            )}
            {jobs.map((j) => (
              <ActiveJobRow
                key={j.id}
                job={j}
                onCancel={onCancel}
                cancelBusy={cancelBusy === j.id}
              />
            ))}
            {succeededDownloadable.length > 0 && (
              <li
                style={{
                  padding: "0.5rem 0.75rem",
                  borderTop: "1px solid var(--border, #eee)",
                  background: "var(--bv-bg-soft, #fafafa)",
                  fontWeight: 600,
                  fontSize: "0.85em",
                  display: "flex",
                  justifyContent: "space-between",
                  alignItems: "center",
                  gap: 8,
                }}
              >
                <span>{t("recentTitle")}</span>
                <button
                  type="button"
                  onClick={dismissAllRecent}
                  className="ghost"
                  title={t("dismissAllTitle")}
                  style={{ fontSize: "0.78em", padding: "0.15rem 0.5rem", fontWeight: 400 }}
                >
                  {t("dismissAll")}
                </button>
              </li>
            )}
            {succeededDownloadable.map((j) => (
              <RecentJobRow
                key={j.id}
                job={j}
                onDownload={onDownloadRecent}
                onDismiss={dismissOne}
                downloadBusy={downloadBusy === j.id}
              />
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

function ActiveJobRow({
  job,
  onCancel,
  cancelBusy,
}: {
  job: JobOut;
  onCancel: (j: JobOut) => Promise<void>;
  cancelBusy: boolean;
}) {
  const t = useTranslations("activeJobs");
  const enqueuedAt = new Date(job.created_at).toLocaleTimeString();
  // Progress is the loadbearing signal here: a clinician opening
  // /operazioni mid-export wants to see how far the job has gotten,
  // not just "running". When ``progress_total`` is set we render a
  // determinate bar with N/M + percent; otherwise we render an
  // indeterminate animated stripe so a queued job still looks
  // alive while the worker hasn't computed the total yet.
  const total = job.progress_total ?? 0;
  const done = job.progress_done ?? 0;
  const pct = total > 0 ? Math.min(100, Math.floor((done / total) * 100)) : null;
  const isRunning = job.status === "running";
  const isQueued = job.status === "queued";

  async function tryCancel(): Promise<void> {
    try {
      await onCancel(job);
    } catch (e) {
      // ApiError-only branch left explicit so a future toast hook
      // has a single point to wire into.
      if (e instanceof ApiError) {
        // swallowed: best-effort
      }
    }
  }

  return (
    <li
      style={{
        padding: "0.5rem 0.75rem",
        borderBottom: "1px solid var(--border, #f0f0f0)",
        display: "flex",
        flexDirection: "column",
        gap: 4,
      }}
    >
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          gap: 8,
        }}
      >
        <span style={{ fontWeight: 500 }}>
          {t.has(`kind.${job.kind}`) ? t(`kind.${job.kind}`) : job.kind}
        </span>
        <span style={{ fontSize: "0.8em", opacity: 0.7 }}>{enqueuedAt}</span>
      </div>
      {job.display_label && (
        <div
          style={{
            fontSize: "0.85em",
            opacity: 0.85,
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: "nowrap",
          }}
          title={job.display_label}
        >
          {job.display_label}
        </div>
      )}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          gap: 8,
          fontSize: "0.85em",
          opacity: 0.85,
        }}
      >
        <span>
          {t(`status.${job.status}`)}
          {job.stage ? ` · ${job.stage}` : ""}
        </span>
        {pct != null && total > 0 && (
          <span style={{ fontVariantNumeric: "tabular-nums", fontSize: "0.8em" }}>
            {done}/{total} · {pct}%
          </span>
        )}
      </div>
      {(isRunning || isQueued) && <ProgressBar pct={pct} indeterminate={pct == null || isQueued} />}
      <div style={{ display: "flex", justifyContent: "flex-end", gap: 8 }}>
        <button
          type="button"
          onClick={tryCancel}
          disabled={cancelBusy}
          className="ghost"
          style={{ fontSize: "0.85em" }}
        >
          {cancelBusy ? t("cancelling") : t("cancel")}
        </button>
      </div>
    </li>
  );
}

/**
 * Slim progress strip used by the active-jobs panel. ``pct`` drives a
 * determinate fill; ``indeterminate`` overrides with a CSS animated
 * stripe so queued jobs (no ``progress_total`` yet) still look alive.
 */
function ProgressBar({
  pct,
  indeterminate,
}: {
  pct: number | null;
  indeterminate: boolean;
}) {
  return (
    <div
      aria-hidden="true"
      style={{
        position: "relative",
        height: 4,
        borderRadius: 2,
        background: "var(--bv-card-border, #e5e7eb)",
        overflow: "hidden",
      }}
    >
      <div
        style={{
          position: "absolute",
          top: 0,
          bottom: 0,
          left: 0,
          width: indeterminate ? "30%" : `${pct ?? 0}%`,
          background: "var(--bv-accent, #2563eb)",
          transition: "width 0.4s ease",
          // Indeterminate uses a slow left-right slide so the strip
          // visibly moves even when ``pct`` is null. Falls back to
          // the static fill on browsers without keyframe support
          // (still visible: the 30% fill is parked at the start).
          animation: indeterminate ? "bvJobProgressSlide 1.6s ease-in-out infinite" : undefined,
        }}
      />
      <style>
        {`@keyframes bvJobProgressSlide {
          0%   { left: -30%; }
          50%  { left: 50%; }
          100% { left: 100%; }
        }`}
      </style>
    </div>
  );
}

/**
 * Format the time remaining until ``expires_at`` as a short Italian
 * relative-time string ("scade tra 23h", "scade tra 47 min", etc).
 * Past the deadline returns "scaduto"; the cleanup cron drops
 * expired rows so this branch is mostly defensive.
 */
function formatExpiresIn(expiresAtIso: string, t: ReturnType<typeof useTranslations>): string {
  const ms = new Date(expiresAtIso).getTime() - Date.now();
  if (Number.isNaN(ms) || ms <= 0) return t("expired");
  const minutes = Math.round(ms / 60_000);
  if (minutes < 60) return t("expiresInMinutes", { minutes });
  const hours = Math.round(minutes / 60);
  if (hours < 48) return t("expiresInHours", { hours });
  const days = Math.round(hours / 24);
  return t("expiresInDays", { days });
}

function RecentJobRow({
  job,
  onDownload,
  onDismiss,
  downloadBusy,
}: {
  job: JobOut;
  onDownload: (j: JobOut) => Promise<void>;
  onDismiss: (jobId: string) => void;
  downloadBusy: boolean;
}) {
  const t = useTranslations("activeJobs");
  const finishedAt = job.finished_at ? new Date(job.finished_at).toLocaleString() : "—";
  const expiresIn = formatExpiresIn(job.expires_at, t);
  // Surface the deadline in red when it's under 6h so the user
  // notices the artefact is about to be auto-deleted before they
  // click away.
  const ms = new Date(job.expires_at).getTime() - Date.now();
  const urgent = ms > 0 && ms < 6 * 60 * 60 * 1000;

  return (
    <li
      style={{
        padding: "0.5rem 0.75rem",
        borderBottom: "1px solid var(--border, #f0f0f0)",
        display: "flex",
        flexDirection: "column",
        gap: 4,
      }}
    >
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          gap: 8,
        }}
      >
        <span style={{ fontWeight: 500 }}>
          {t.has(`kind.${job.kind}`) ? t(`kind.${job.kind}`) : job.kind}
        </span>
        <span style={{ fontSize: "0.8em", opacity: 0.7 }}>{finishedAt}</span>
      </div>
      {job.display_label && (
        <div
          style={{
            fontSize: "0.85em",
            opacity: 0.85,
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: "nowrap",
          }}
          title={job.display_label}
        >
          {job.display_label}
        </div>
      )}
      <div style={{ fontSize: "0.8em", opacity: 0.85 }}>
        <span
          title={`${t("expiresAtLabel")}: ${new Date(job.expires_at).toLocaleString()}`}
          style={{ color: urgent ? "var(--bv-danger, #b42318)" : undefined }}
        >
          {expiresIn}
        </span>
      </div>
      <div
        style={{
          display: "flex",
          justifyContent: "flex-end",
          alignItems: "center",
          gap: 8,
        }}
      >
        <button
          type="button"
          onClick={() => onDismiss(job.id)}
          title={t("dismissOneTitle")}
          aria-label={t("dismissOne")}
          className="ghost"
          style={{
            fontSize: "0.85em",
            padding: "0.2rem 0.45rem",
            opacity: 0.6,
          }}
        >
          ×
        </button>
        <button
          type="button"
          onClick={() => {
            void onDownload(job);
          }}
          disabled={downloadBusy}
          // Primary affordance: this row exists *because* there is
          // something ready to save. A ghost button buried at the
          // bottom right was easy to miss; the user kept asking
          // "where do I actually click to download". Make it
          // unmistakable.
          style={{
            fontSize: "0.85em",
            padding: "0.3rem 0.75rem",
            background: "var(--bv-accent, #2563eb)",
            color: "#fff",
            border: "1px solid var(--bv-accent, #2563eb)",
            borderRadius: 4,
            cursor: downloadBusy ? "wait" : "pointer",
            fontWeight: 500,
          }}
        >
          ⬇ {downloadBusy ? t("downloading") : t("download")}
        </button>
      </div>
    </li>
  );
}
