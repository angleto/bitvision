"use client";

// Per-card / per-detail control to enqueue + track + claim a single
// study's DICOM ZIP export.
//
// Why this exists. Study export is run by an arq worker and lives on
// S3 with a 48h TTL — it is the only path that survives client
// disconnect (memory ``feedback_long_ops_progress_recovery``,
// ``streaming_zip_pattern``). The two visible entry points (the
// study card in ContentPane, and the detail-page header button) both
// need the same state machine: idle → queued → running:NN%
// → succeeded → consumed (or failed → retry). Sharing the logic
// here keeps the two surfaces coherent and makes "I started an
// export, then closed my laptop" a non-event: ``useScopedJob``
// rebinds to the persisted job id from localStorage on remount.

import { useTranslations } from "next-intl";
import { useCallback, useState } from "react";

import { ApiError, studiesApi } from "@/lib/api";
import { downloadJobResult } from "@/lib/api";
import { type JobOut, jobsApi, jobsStorage } from "@/lib/jobs";
import { useScopedJob } from "@/lib/useJob";

const SCOPE_KIND = "study";
const JOB_KIND = "study_export";

interface BaseProps {
  studyId: string;
  /** Friendly study label used as a fallback ``download`` filename
   *  hint. The Content-Disposition emitted by the result endpoint
   *  is authoritative when present. */
  studyLabel?: string | null;
  /** ``"icon"`` renders a 22x22 affordance for the card grid /
   *  list-row toolbar; ``"button"`` renders a labelled inline-block
   *  control for the detail-page header. */
  variant?: "icon" | "button";
  /** Stop the click event from bubbling up to a parent that owns
   *  selection / navigation (study card grid). */
  stopPropagation?: boolean;
}

/**
 * Compute progress as a 0..100 integer; ``null`` when the worker
 * hasn't written a total yet (queued or first ticks of running).
 */
function progressPct(job: JobOut | null): number | null {
  if (!job) return null;
  if (!job.progress_total || job.progress_total <= 0) return null;
  const done = Math.max(0, Math.min(job.progress_done, job.progress_total));
  return Math.floor((done / job.progress_total) * 100);
}

export default function StudyExportButton({
  studyId,
  studyLabel,
  variant = "icon",
  stopPropagation = true,
}: BaseProps) {
  const t = useTranslations("studyDetail");
  const tList = useTranslations("fascicolo.list");
  const { job, bind } = useScopedJob(SCOPE_KIND, studyId, JOB_KIND);
  const [enqueueing, setEnqueueing] = useState(false);
  const [downloading, setDownloading] = useState(false);
  const [localError, setLocalError] = useState<string | null>(null);

  // ``isExpired``: the row is succeeded but the artifact is gone — the
  // 48h cleanup cron dropped the S3 object, the row is no longer
  // downloadable but lingers until ``expires_at``. Detect either by
  // ``result_download_url`` going null or by clock comparison so we
  // surface a "scaduto, rilancia" affordance instead of a cryptic 404
  // when the user clicks ⬇.
  const expiresAtMs = job?.expires_at ? new Date(job.expires_at).getTime() : null;
  const isExpired =
    job?.status === "succeeded" &&
    (!job.result_download_url || (expiresAtMs !== null && expiresAtMs < Date.now()));
  const isActive = job !== null && (job.status === "queued" || job.status === "running");
  const isReady = job?.status === "succeeded" && !!job.result_download_url && !isExpired;
  const isFailed = job?.status === "failed" || job?.status === "cancelled";

  // Map the worker's structured error code to a localised message so
  // a user sees "Permesso revocato" instead of "permission_denied".
  // Falls back to the raw message string when the code is unknown so
  // we never lose information from a worker we haven't taught here.
  const errorCode = job?.error?.code;
  const errorMessageKey = errorCode ? `errorCodes.${errorCode}` : null;
  const errorText = isFailed
    ? errorMessageKey && t.has(errorMessageKey)
      ? t(errorMessageKey)
      : job?.error?.message || t("exportFailed")
    : null;

  const handleEnqueue = useCallback(async () => {
    setLocalError(null);
    setEnqueueing(true);
    try {
      const enqueued = await studiesApi.requestStudyExport(studyId);
      jobsStorage.add({
        id: enqueued.id,
        kind: JOB_KIND,
        scope_kind: SCOPE_KIND,
        scope_id: studyId,
      });
      bind(enqueued.id);
    } catch (e) {
      setLocalError(e instanceof ApiError ? e.message : "request failed");
    } finally {
      setEnqueueing(false);
    }
  }, [studyId, bind]);

  const handleDownload = useCallback(async () => {
    if (!job) return;
    setDownloading(true);
    try {
      const safe = (studyLabel || studyId).replace(/[^A-Za-z0-9._-]+/g, "_");
      await downloadJobResult(job.id, `study-${safe}-${studyId.slice(0, 8)}.zip`);
      // Once the user has the bytes, drop the localStorage entry so
      // the next render shows a fresh "request export" affordance.
      jobsStorage.remove(job.id);
      bind(null);
    } catch (e) {
      setLocalError(e instanceof ApiError ? e.message : "download failed");
    } finally {
      setDownloading(false);
    }
  }, [job, studyId, studyLabel, bind]);

  const handleCancel = useCallback(async () => {
    if (!job) return;
    try {
      await jobsApi.cancel(job.id);
    } catch {
      // Best-effort: the row may already be terminal.
    }
    jobsStorage.remove(job.id);
    bind(null);
  }, [job, bind]);

  const handleRetry = useCallback(async () => {
    // Drop the failed entry first so a stale cached row doesn't
    // shadow the new enqueue.
    if (job) jobsStorage.remove(job.id);
    bind(null);
    await handleEnqueue();
  }, [job, bind, handleEnqueue]);

  const onIconClick = useCallback(
    (e: React.MouseEvent) => {
      if (stopPropagation) e.stopPropagation();
      if (isReady) {
        void handleDownload();
      } else if (isExpired || isFailed) {
        // Both expired and failed states route to retry — the
        // affordance is identical to the user ("rilancia") even
        // though the underlying reason differs.
        void handleRetry();
      } else if (!isActive && !enqueueing) {
        void handleEnqueue();
      }
    },
    [
      stopPropagation,
      isReady,
      isExpired,
      isFailed,
      isActive,
      enqueueing,
      handleDownload,
      handleRetry,
      handleEnqueue,
    ],
  );

  const pct = progressPct(job);
  // Title attribute = the verbose, doctor-friendly explanation the
  // user reads on hover. We deliberately keep it longer than the
  // button label so the radiologist understands "the archive is
  // being prepared and the download will start by itself when it's
  // ready" — not just "Queued" with no further context. The label
  // (button face) stays short.
  const isQueued = job?.status === "queued" || enqueueing;
  const isRunning = job?.status === "running";
  const titleText = isReady
    ? t("exportReadyTitle")
    : isExpired
      ? `${t("exportExpired")} — ${t("exportExpiredHint")}`
      : isRunning && pct != null
        ? t("exportRunningTitle", {
            done: job?.progress_done ?? 0,
            total: job?.progress_total ?? 0,
            pct,
          })
        : isRunning
          ? t("exportPreparingTitle")
          : isQueued
            ? t("exportQueuedTitle")
            : isFailed
              ? errorText || t("exportPrepFailed")
              : tList("download");

  if (variant === "icon") {
    // Compact icon for card / row toolbars. Single button switches
    // glyph by state so the card layout stays stable: ⬇ for idle &
    // ready (different titles), spinner for active, ⚠ for failed.
    const glyph = isActive ? "…" : isFailed || isExpired ? "⚠" : isReady ? "⬇" : "⬇";
    const busy = enqueueing || downloading;
    return (
      <span style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
        <button
          type="button"
          onClick={onIconClick}
          disabled={busy}
          title={titleText}
          aria-label={titleText}
          style={{
            width: 22,
            height: 22,
            padding: 0,
            borderRadius: 6,
            border: "1px solid var(--bv-card-border, #e5e7eb)",
            background: isReady
              ? "var(--bv-success-soft, #dcfce7)"
              : isFailed || isExpired
                ? "var(--bv-danger-soft, #fee2e2)"
                : "var(--bv-card-bg, #fff)",
            color: "var(--bv-fg, #0f172a)",
            fontSize: "0.78rem",
            lineHeight: 1,
            cursor: busy ? "wait" : "pointer",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            opacity: busy ? 0.55 : 1,
          }}
        >
          {busy ? "…" : glyph}
        </button>
        {isActive && pct != null && (
          // Tiny inline progress strip beside the icon when there's
          // room (grid card foot has ~28px height, this fits). Total
          // count is in the title attribute already.
          <span
            aria-hidden="true"
            style={{
              display: "inline-block",
              width: 24,
              height: 4,
              borderRadius: 2,
              background: "var(--bv-card-border, #e5e7eb)",
              position: "relative",
              overflow: "hidden",
            }}
          >
            <span
              style={{
                position: "absolute",
                inset: 0,
                width: `${pct}%`,
                background: "var(--bv-accent, #2563eb)",
              }}
            />
          </span>
        )}
      </span>
    );
  }

  // ``button`` variant: detail-page header / dialog. Shows the full
  // state in text + an explicit Cancel while running and Retry on
  // failure.
  // Button label = short ("Preparazione…", "In coda…", "Salva
  // archivio") — the title carries the full explanation. The
  // running-without-progress case used to fall back to "In coda…"
  // which is wrong: the worker IS running, it just hasn't reported
  // progress_total yet. We now show "Preparazione…" instead so the
  // user gets an honest read of the state.
  const labelText = isReady
    ? t("exportReady")
    : isExpired
      ? t("exportExpired")
      : enqueueing
        ? t("exportEnqueueing")
        : job?.status === "queued"
          ? t("exportQueued")
          : job?.status === "running"
            ? pct != null
              ? `${t("exportRunning", {
                  done: job.progress_done,
                  total: job.progress_total ?? 0,
                })} (${pct}%)`
              : t("exportPreparing")
            : isFailed
              ? t("exportFailedRetry")
              : t("downloadDicomZip");
  const primaryAction = isReady
    ? handleDownload
    : isExpired || isFailed
      ? handleRetry
      : isActive
        ? () => {}
        : handleEnqueue;
  const primaryDisabled = enqueueing || downloading || (isActive && !isReady);

  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: "0.5rem" }}>
      <button
        type="button"
        className="ghost"
        onClick={(e) => {
          if (stopPropagation) e.stopPropagation();
          void primaryAction();
        }}
        disabled={primaryDisabled}
        title={titleText}
      >
        {labelText}
      </button>
      {isActive && (
        <button
          type="button"
          className="ghost"
          onClick={(e) => {
            if (stopPropagation) e.stopPropagation();
            void handleCancel();
          }}
          style={{ fontSize: "0.78rem" }}
        >
          {t("exportCancel")}
        </button>
      )}
      {(isFailed || isExpired || localError) && (
        <span className="error" style={{ fontSize: "0.78rem" }}>
          {localError || (isExpired ? t("exportExpiredHint") : errorText) || t("exportFailed")}
        </span>
      )}
    </span>
  );
}
