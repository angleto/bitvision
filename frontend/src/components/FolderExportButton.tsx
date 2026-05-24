"use client";

// Per-folder ⬇ icon: enqueue + track + claim a folder ZIP export.
//
// Mirrors StudyExportButton (same state machine, same JobsTray
// integration via useScopedJob), but the worker is the
// ``export_patient_zip`` task scoped to a folder via
// ``POST /api/folders/{id}/export`` — see backend
// ``api/patient_export.py::export_folder_async`` for the dedup key
// and the canonical_input shape.
//
// Why a separate component? StudyExportButton is hard-coded to the
// ``study_export`` job kind and the ``studiesApi.requestStudyExport``
// endpoint. The folder pipeline reuses ``fascicolo_export`` as its
// kind (folders are scoped patient exports), and the request shape
// differs (folder id, not study id). Sharing a single button across
// both surfaces would have meant a generic kind/endpoint prop with
// fewer guarantees and more conditionals — this twin keeps each path
// readable.

import { useTranslations } from "next-intl";
import { useCallback, useState } from "react";

import ExportFascicoloDialog from "@/components/ExportFascicoloDialog";
import { ApiError, downloadJobResult } from "@/lib/api";
import { type JobOut, jobsStorage } from "@/lib/jobs";
import { useScopedJob } from "@/lib/useJob";

const SCOPE_KIND = "folder";
const JOB_KIND = "fascicolo_export";

interface BaseProps {
  folderId: string;
  /** The patient that owns the folder. Required to mount
   *  ExportFascicoloDialog (it pulls patient-level fallback data). */
  patientId: string;
  /** Friendly folder name used as a fallback download filename. */
  folderLabel?: string | null;
  /** Stop propagation on click so the parent card doesn't navigate
   *  / select when the user just wanted to download. */
  stopPropagation?: boolean;
}

function progressPct(job: JobOut | null): number | null {
  if (!job) return null;
  if (!job.progress_total || job.progress_total <= 0) return null;
  const done = Math.max(0, Math.min(job.progress_done, job.progress_total));
  return Math.floor((done / job.progress_total) * 100);
}

export default function FolderExportButton({
  folderId,
  patientId,
  folderLabel,
  stopPropagation = true,
}: BaseProps) {
  const t = useTranslations("studyDetail");
  const tFasc = useTranslations("fascicolo.list");
  const { job, bind: _bind } = useScopedJob(SCOPE_KIND, folderId, JOB_KIND);
  // Picker dialog state. Click on idle ⬇ opens the dialog so the
  // user can deselect heavyweight items (DICOM ISOs etc) before
  // enqueueing. The dialog handles the enqueue itself; useScopedJob
  // here picks up the resulting Job and renders the queued/running/
  // ready/failed states on the icon — same UX as before, but with a
  // selection step in between.
  const [pickerOpen, setPickerOpen] = useState(false);
  const [downloading, setDownloading] = useState(false);
  const [localError, setLocalError] = useState<string | null>(null);
  const enqueueing = false;

  const expiresAtMs = job?.expires_at ? new Date(job.expires_at).getTime() : null;
  const isExpired =
    job?.status === "succeeded" &&
    (!job.result_download_url || (expiresAtMs !== null && expiresAtMs < Date.now()));
  const isActive = job !== null && (job.status === "queued" || job.status === "running");
  const isReady = job?.status === "succeeded" && !!job.result_download_url && !isExpired;
  const isFailed = job?.status === "failed" || job?.status === "cancelled";

  const errorCode = job?.error?.code;
  const errorMessageKey = errorCode ? `errorCodes.${errorCode}` : null;
  const errorText = isFailed
    ? errorMessageKey && t.has(errorMessageKey)
      ? t(errorMessageKey)
      : job?.error?.message || t("exportPrepFailed")
    : null;

  const handleDownload = useCallback(async () => {
    if (!job) return;
    setDownloading(true);
    try {
      const safe = (folderLabel || folderId).replace(/[^A-Za-z0-9._-]+/g, "_");
      await downloadJobResult(job.id, `folder-${safe}-${folderId.slice(0, 8)}.zip`);
      jobsStorage.remove(job.id);
      _bind(null);
    } catch (e) {
      setLocalError(e instanceof ApiError ? e.message : "download failed");
    } finally {
      setDownloading(false);
    }
  }, [job, folderId, folderLabel, _bind]);

  const onIconClick = useCallback(
    (e: React.MouseEvent) => {
      if (stopPropagation) e.stopPropagation();
      if (isReady) {
        void handleDownload();
      } else if (isExpired || isFailed) {
        // Re-open picker so user can re-pick before retrying. The
        // previous (now-cleared) Job stays in the audit; useScopedJob
        // will rebind to whatever the dialog enqueues next.
        if (job) jobsStorage.remove(job.id);
        _bind(null);
        setPickerOpen(true);
      } else if (!isActive) {
        setPickerOpen(true);
      }
    },
    [stopPropagation, isReady, isExpired, isFailed, isActive, handleDownload, job, _bind],
  );

  const pct = progressPct(job);
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
              : tFasc("downloadFolder");

  const glyph = isActive ? "…" : isFailed || isExpired ? "⚠" : "⬇";
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
      <ExportFascicoloDialog
        open={pickerOpen}
        onClose={() => setPickerOpen(false)}
        patientId={patientId}
        folderId={folderId}
      />
    </span>
  );
}
