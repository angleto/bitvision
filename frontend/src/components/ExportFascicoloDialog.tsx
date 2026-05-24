"use client";

// Modal that lets the user pick which sections of the fascicolo to
// include in a ZIP export (DESIGN.md §11.9 step 2).
//
// Asks the backend to enqueue a Job and polls for completion. Once
// the worker finishes, ``result_download_url`` is a freshly-signed
// S3 URL that we hand to an anchor element to trigger the browser
// download. The dialog stays open with a progress UI until the user
// dismisses it; cancelling closes the dialog without aborting the
// Job (cancel-during-run is a follow-up).
//
// DICOM is opt-in and triggers a warning because:
//   * it is the bulk of the data (study sizes commonly land in the
//     100s of MB to several GB);
//   * the backend rejects with HTTP 403 when the caller lacks the
//     ``download:dicom`` grant — we surface that error inline.

import { useTranslations } from "next-intl";
import { useEffect, useState } from "react";

import NativeDialog from "@/components/NativeDialog";
import {
  ApiError,
  type FolderExportItem,
  downloadJobResult,
  folderExportApi,
  patientsApi,
} from "@/lib/api";
import { jobsStorage } from "@/lib/jobs";
import { useJob } from "@/lib/useJob";

type ExportSection = "studies" | "reports" | "documents" | "annotations" | "dicom";

interface Props {
  patientId: string;
  open: boolean;
  onClose: () => void;
  /**
   * Whether the caller is known to have the ``download:dicom`` grant
   * for this patient. When ``false`` the DICOM checkbox is disabled
   * with an explanatory hint; when ``undefined`` the dialog leaves
   * the toggle enabled and lets the server reject with 403 if the
   * grant is missing (less ideal but safe).
   */
  canDownloadDicom?: boolean;
  /**
   * When set, the dialog enqueues a folder-scoped export instead of a
   * patient-wide one. The dialog otherwise keeps its UI shape — same
   * section picker, same polling loop, same browser download trigger.
   */
  folderId?: string | null;
}

const DEFAULT_SECTIONS: ExportSection[] = ["studies", "reports", "documents", "annotations"];

export default function ExportFascicoloDialog({
  patientId,
  open,
  onClose,
  canDownloadDicom,
  folderId,
}: Props) {
  const t = useTranslations("export");
  const [sections, setSections] = useState<Set<ExportSection>>(() => new Set(DEFAULT_SECTIONS));
  const [enqueuing, setEnqueuing] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [jobId, setJobId] = useState<string | null>(null);
  const [downloaded, setDownloaded] = useState(false);

  // Per-item picker (folder mode only). When the user opens the
  // dialog we fetch the folder's enriched items and let them
  // deselect heavyweight files (multi-GB DICOM ISOs they may already
  // have on a physical disc). ``itemsLoaded`` is the gate so the
  // checkbox section only renders once we know what's in the folder.
  const [items, setItems] = useState<FolderExportItem[]>([]);
  const [itemsLoaded, setItemsLoaded] = useState(false);
  const [selectedItemIds, setSelectedItemIds] = useState<Set<string>>(() => new Set());

  const { job, error: pollError } = useJob(jobId);

  useEffect(() => {
    if (!open || !folderId) {
      setItems([]);
      setItemsLoaded(false);
      setSelectedItemIds(new Set());
      return;
    }
    let cancelled = false;
    void folderExportApi
      .items(folderId)
      .then((rows) => {
        if (cancelled) return;
        setItems(rows);
        // Default: every item selected (preserves the long-standing
        // "click ⬇ → get everything" behaviour). User opt-OUTs.
        setSelectedItemIds(new Set(rows.map((r) => `${r.resource_kind}:${r.resource_id}`)));
        setItemsLoaded(true);
      })
      .catch(() => {
        // Non-fatal: degrade to "no per-item picker", the dialog
        // still works and exports everything.
        if (!cancelled) {
          setItemsLoaded(true);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [open, folderId]);

  function toggleItem(key: string) {
    setSelectedItemIds((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }

  function toggleAllItems(select: boolean) {
    if (select) {
      setSelectedItemIds(new Set(items.map((r) => `${r.resource_kind}:${r.resource_id}`)));
    } else {
      setSelectedItemIds(new Set());
    }
  }

  // Once the Job succeeds, mint a signed download token and trigger
  // the browser's native streaming-to-disk via an anchor click. We
  // remember we already triggered the download so polling-driven
  // re-renders do not re-fire the request.
  //
  // The previous implementation used a bare ``<a href>`` against the
  // backend's ``/jobs/{id}/result_download`` route, which silently
  // 401'd: the browser does not send Authorization on top-level
  // navigation, and the route required Bearer auth.
  useEffect(() => {
    if (!job) return;
    if (downloaded) return;
    if (job.status !== "succeeded" || !job.result_download_url) return;
    let cancelled = false;
    void downloadJobResult(job.id).then(() => {
      if (!cancelled) setDownloaded(true);
    });
    return () => {
      cancelled = true;
    };
  }, [job, downloaded]);

  if (!open) return null;

  function toggle(section: ExportSection) {
    setSections((prev) => {
      const next = new Set(prev);
      if (next.has(section)) next.delete(section);
      else next.add(section);
      return next;
    });
  }

  async function enqueue() {
    if (sections.size === 0) return;
    setEnqueuing(true);
    setErr(null);
    setJobId(null);
    setDownloaded(false);
    try {
      let created: import("@/lib/jobs").JobOut;
      if (folderId) {
        // When the picker is loaded and the user deselected at
        // least one item, send the explicit subset. When ALL items
        // are still checked, omit the lists so the backend
        // continues exporting the full folder via its existing
        // default path.
        const allKeys = items.map((r) => `${r.resource_kind}:${r.resource_id}`);
        const everyoneChecked =
          itemsLoaded && allKeys.length > 0 && allKeys.every((k) => selectedItemIds.has(k));
        const selection = everyoneChecked
          ? undefined
          : {
              study_ids: items
                .filter(
                  (r) =>
                    r.resource_kind === "study" && selectedItemIds.has(`study:${r.resource_id}`),
                )
                .map((r) => r.resource_id),
              document_ids: items
                .filter(
                  (r) =>
                    r.resource_kind === "document" &&
                    selectedItemIds.has(`document:${r.resource_id}`),
                )
                .map((r) => r.resource_id),
            };
        created = await folderExportApi.request(
          folderId,
          Array.from(sections).join(","),
          selection,
        );
      } else {
        created = await patientsApi.requestFascicoloExport(patientId, Array.from(sections));
      }
      jobsStorage.add({ id: created.id, kind: created.kind });
      setJobId(created.id);
    } catch (e) {
      if (e instanceof ApiError) {
        if (e.status === 403) setErr(t("errDicomForbidden"));
        else if (e.status === 429) setErr(t("errCapExceeded"));
        else setErr(e.message);
      } else {
        setErr(t("errGeneric"));
      }
    } finally {
      setEnqueuing(false);
    }
  }

  function close() {
    onClose();
    // Reset local state so a subsequent open is fresh.
    setJobId(null);
    setDownloaded(false);
    setErr(null);
  }

  const status = job?.status ?? null;
  const stage = job?.stage ?? null;
  const progressTotal = job?.progress_total ?? null;
  const progressDone = job?.progress_done ?? 0;
  const isRunning = status === "queued" || status === "running";
  const succeeded = status === "succeeded";
  const failed = status === "failed";
  const cancelled = status === "cancelled";

  const polledError = pollError instanceof ApiError ? pollError.message : null;

  return (
    <NativeDialog open={open} onClose={close} ariaLabel={t("title")} className="bv-dialog">
      <div
        style={{
          background: "var(--bv-card-bg, #fff)",
          borderRadius: 8,
          padding: "1.25rem",
          maxWidth: 520,
          width: "calc(100% - 2rem)",
          maxHeight: "90vh",
          overflow: "auto",
          boxShadow: "0 10px 30px rgba(0,0,0,0.2)",
        }}
      >
        <h2 style={{ marginTop: 0 }}>{t("title")}</h2>
        <p className="meta">{t("subtitle")}</p>
        {err && <p className="error">{err}</p>}
        {polledError && !succeeded && <p className="error">{polledError}</p>}
        {jobId && status && (
          <div
            style={{
              border: "1px solid var(--bv-border, #ddd)",
              borderRadius: 4,
              padding: "8px 10px",
              fontSize: "0.85rem",
              marginBottom: "0.5rem",
              background: succeeded
                ? "var(--bv-success-soft, #ecfdf5)"
                : failed || cancelled
                  ? "var(--bv-error-soft, #fef2f2)"
                  : "var(--bv-info-soft, #eff6ff)",
            }}
          >
            <div>
              <strong>{t(`status.${status}`)}</strong>
              {stage && isRunning ? <> · {stage}</> : null}
            </div>
            {isRunning && progressTotal != null && progressTotal > 0 && (
              <div style={{ marginTop: 4 }}>
                <progress value={progressDone} max={progressTotal} />
                <span style={{ marginLeft: 8 }}>
                  {progressDone}/{progressTotal}
                </span>
              </div>
            )}
            {succeeded && (
              <div style={{ marginTop: 4 }}>
                {downloaded ? t("downloadStarted") : t("downloadPending")}
                {job?.result_download_url && (
                  <>
                    {" "}
                    <button
                      type="button"
                      className="ghost"
                      onClick={() => {
                        if (job?.id) void downloadJobResult(job.id);
                      }}
                      style={{
                        background: "none",
                        border: "none",
                        padding: 0,
                        color: "var(--bv-link, #1a73e8)",
                        textDecoration: "underline",
                        cursor: "pointer",
                        font: "inherit",
                      }}
                    >
                      {t("downloadAgain")}
                    </button>
                  </>
                )}
              </div>
            )}
            {failed && job?.error?.message && (
              <div style={{ marginTop: 4 }}>{job.error.message}</div>
            )}
          </div>
        )}
        {folderId && itemsLoaded && items.length > 0 && (
          <fieldset
            style={{
              border: "1px solid var(--bv-border, #ddd)",
              borderRadius: 6,
              padding: "0.5rem 0.75rem",
              margin: "0.5rem 0 0.25rem",
            }}
          >
            <legend style={{ fontSize: "0.85rem", padding: "0 0.4rem" }}>{t("itemsTitle")}</legend>
            <div
              style={{
                display: "flex",
                gap: "0.5rem",
                fontSize: "0.78rem",
                marginBottom: "0.4rem",
                opacity: 0.85,
              }}
            >
              <button
                type="button"
                className="ghost"
                onClick={() => toggleAllItems(true)}
                disabled={enqueuing || isRunning}
                style={{ padding: "0.15rem 0.5rem", fontSize: "0.78rem" }}
              >
                {t("itemsSelectAll")}
              </button>
              <button
                type="button"
                className="ghost"
                onClick={() => toggleAllItems(false)}
                disabled={enqueuing || isRunning}
                style={{ padding: "0.15rem 0.5rem", fontSize: "0.78rem" }}
              >
                {t("itemsSelectNone")}
              </button>
              <span style={{ marginLeft: "auto", opacity: 0.7 }}>
                {t("itemsCount", { n: selectedItemIds.size, total: items.length })}
              </span>
            </div>
            <div
              style={{
                maxHeight: 220,
                overflowY: "auto",
                display: "flex",
                flexDirection: "column",
                gap: "0.25rem",
              }}
            >
              {items.map((it) => {
                const key = `${it.resource_kind}:${it.resource_id}`;
                const checked = selectedItemIds.has(key);
                const sizeMb =
                  it.size_bytes && it.size_bytes > 0
                    ? it.size_bytes >= 1024 * 1024 * 1024
                      ? `${(it.size_bytes / 1024 / 1024 / 1024).toFixed(2)} GB`
                      : `${Math.round(it.size_bytes / 1024 / 1024)} MB`
                    : null;
                const meta: string[] = [];
                if (it.resource_kind === "study") {
                  if (it.modality) meta.push(it.modality);
                  if (it.study_date) meta.push(it.study_date);
                  if (it.file_count) meta.push(`${it.file_count} file`);
                } else {
                  if (it.document_type) meta.push(it.document_type);
                  if (it.document_date) meta.push(it.document_date);
                }
                if (sizeMb) meta.push(sizeMb);
                return (
                  <label
                    key={key}
                    style={{
                      display: "flex",
                      alignItems: "flex-start",
                      gap: "0.5rem",
                      cursor: "pointer",
                      fontSize: "0.85rem",
                      padding: "0.2rem 0.3rem",
                      borderRadius: 4,
                      background: checked ? "transparent" : "var(--bv-card-bg-soft, #f9fafb)",
                    }}
                  >
                    <input
                      type="checkbox"
                      checked={checked}
                      onChange={() => toggleItem(key)}
                      disabled={enqueuing || isRunning}
                      style={{ marginTop: 3 }}
                    />
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <div
                        style={{
                          overflow: "hidden",
                          textOverflow: "ellipsis",
                          whiteSpace: "nowrap",
                        }}
                      >
                        {it.name || `(${t(`itemKind.${it.resource_kind}`)})`}
                      </div>
                      {meta.length > 0 && (
                        <div style={{ fontSize: "0.75rem", opacity: 0.7 }}>{meta.join(" · ")}</div>
                      )}
                    </div>
                  </label>
                );
              })}
            </div>
            <p style={{ fontSize: "0.75rem", opacity: 0.7, margin: "0.4rem 0 0" }}>
              {t("itemsHint")}
            </p>
          </fieldset>
        )}
        <div
          style={{
            display: "flex",
            flexDirection: "column",
            gap: "0.4rem",
            marginTop: "0.5rem",
          }}
        >
          {(["studies", "reports", "documents", "annotations"] as const).map((s) => (
            <label
              key={s}
              style={{
                display: "flex",
                alignItems: "center",
                gap: "0.5rem",
                cursor: "pointer",
              }}
            >
              <input
                type="checkbox"
                checked={sections.has(s)}
                onChange={() => toggle(s)}
                disabled={enqueuing || isRunning}
              />
              <span>{t(`section.${s}`)}</span>
            </label>
          ))}
          <label
            style={{
              display: "flex",
              alignItems: "flex-start",
              gap: "0.5rem",
              cursor: canDownloadDicom === false ? "not-allowed" : "pointer",
              opacity: canDownloadDicom === false ? 0.6 : 1,
            }}
          >
            <input
              type="checkbox"
              checked={sections.has("dicom")}
              onChange={() => toggle("dicom")}
              disabled={enqueuing || isRunning || canDownloadDicom === false}
            />
            <div>
              <div>{t("section.dicom")}</div>
              <div className="meta" style={{ fontSize: "0.78rem" }}>
                {canDownloadDicom === false ? t("dicomForbiddenHint") : t("dicomLargeWarning")}
              </div>
            </div>
          </label>
        </div>
        <div
          style={{
            display: "flex",
            justifyContent: "flex-end",
            gap: "0.5rem",
            marginTop: "1rem",
          }}
        >
          <button type="button" className="ghost" onClick={close}>
            {succeeded || failed || cancelled ? t("close") : t("cancel")}
          </button>
          <button
            type="button"
            onClick={enqueue}
            disabled={
              enqueuing ||
              isRunning ||
              sections.size === 0 ||
              // Folder mode: refuse to enqueue an empty selection
              // (the backend would also 422, but failing fast at
              // the click site avoids the round-trip).
              !!(folderId && itemsLoaded && items.length > 0 && selectedItemIds.size === 0)
            }
          >
            {enqueuing ? t("enqueuing") : isRunning ? t("running") : t("export")}
          </button>
        </div>
      </div>
    </NativeDialog>
  );
}
