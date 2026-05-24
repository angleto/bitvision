"use client";

// Inline variant of the fascicolo uploader. Unlike the full-screen
// /upload page (UniversalUploader / DicomUploader), this mounts as a
// modal overlay on top of the Drive-like fascicolo layout so a user
// dropping files onto a folder stays in context — same patient, same
// folder, same visual frame.
//
// Routing is fully delegated to the backend /api/upload/bulk endpoint
// (U2): the UI hands over files + patient_id + target_folder_id and
// just renders whatever summary comes back. DICOM becomes Studies,
// PDFs/office docs become Documents, everything else falls through to
// the generic bucket — the client does not try to classify anything.

import { useTranslations } from "next-intl";
import { useCallback, useEffect, useRef, useState } from "react";

import NativeDialog from "@/components/NativeDialog";
import { API_BASE_URL, ApiError, getStoredToken } from "@/lib/api";

export interface BulkUploadRouted {
  filename: string;
  kind: "study" | "document" | "report" | "annotation" | "other";
  id?: string | null;
  note?: string | null;
}

export interface BulkUploadSummary {
  patient_id: string;
  target_folder_id: string | null;
  routed: BulkUploadRouted[];
  studies_created: string[];
  documents_created: string[];
  instances_created: number;
  instances_existing: number;
  errors: { filename: string; message: string }[];
}

type Phase = "preview" | "uploading" | "done" | "error";

interface Props {
  patientId: string;
  targetFolderId: string | null;
  /** Files the user dropped/picked. When non-empty the overlay is shown. */
  initialFiles?: File[];
  /** Called after a successful upload with the backend summary. */
  onComplete?: (summary: BulkUploadSummary) => void;
  /** Called when the user dismisses the overlay (cancel / esc / done). */
  onClose?: () => void;
}

export default function InlineFascicoloUploader({
  patientId,
  targetFolderId,
  initialFiles = [],
  onComplete,
  onClose,
}: Props) {
  const tIu = useTranslations("inlineUploader");
  const [files, setFiles] = useState<File[]>(initialFiles);
  const [phase, setPhase] = useState<Phase>("preview");
  const [progress, setProgress] = useState(0);
  const [summary, setSummary] = useState<BulkUploadSummary | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const xhrRef = useRef<XMLHttpRequest | null>(null);

  const removeFile = (idx: number) => {
    if (phase !== "preview") return;
    setFiles((cur) => cur.filter((_, i) => i !== idx));
  };

  // Esc / backdrop / explicit close — all guarded against firing while
  // the XHR is in flight, so this is a pure "tell the parent to unmount
  // me" signal. Aborting is wired separately to the "Interrompi" button.
  const dismiss = useCallback(() => {
    onClose?.();
  }, [onClose]);

  useEffect(() => {
    if (phase === "uploading") return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") dismiss();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [dismiss, phase]);

  const startUpload = async () => {
    if (files.length === 0) return;
    setPhase("uploading");
    setErr(null);
    setProgress(0);
    setSummary(null);

    const form = new FormData();
    for (const f of files) form.append("files", f, f.name);
    form.append("patient_id", patientId);
    if (targetFolderId) form.append("target_folder_id", targetFolderId);

    const token = getStoredToken();
    try {
      const resp = await new Promise<BulkUploadSummary>((resolve, reject) => {
        const xhr = new XMLHttpRequest();
        xhrRef.current = xhr;
        xhr.open("POST", `${API_BASE_URL}/api/upload/bulk`);
        if (token) xhr.setRequestHeader("authorization", `Bearer ${token}`);
        xhr.upload.onprogress = (ev) => {
          if (ev.lengthComputable) {
            setProgress(Math.round((ev.loaded / ev.total) * 100));
          }
        };
        xhr.onload = () => {
          if (xhr.status >= 200 && xhr.status < 300) {
            try {
              resolve(JSON.parse(xhr.responseText) as BulkUploadSummary);
            } catch (e) {
              reject(new ApiError(xhr.status, `invalid JSON response: ${e}`));
            }
          } else {
            let detail: unknown = xhr.responseText;
            try {
              detail = JSON.parse(xhr.responseText);
            } catch {
              /* keep text */
            }
            reject(new ApiError(xhr.status, detail));
          }
        };
        xhr.onerror = () => reject(new ApiError(0, "network error"));
        xhr.onabort = () => reject(new ApiError(0, "upload cancelled"));
        xhr.send(form);
      });
      setSummary(resp);
      setPhase("done");
      onComplete?.(resp);
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "upload failed");
      setPhase("error");
    } finally {
      xhrRef.current = null;
    }
  };

  const totalBytes = files.reduce((acc, f) => acc + f.size, 0);

  return (
    // Backdrop click closes — but only when not actively uploading
    // (an in-flight upload would be aborted otherwise). NativeDialog
    // forwards backdrop clicks to onClose; we gate the close at
    // call-time.
    <NativeDialog
      open
      onClose={() => {
        if (phase !== "uploading") dismiss();
      }}
      ariaLabel={tIu("uploadAria")}
      className="bv-dialog"
    >
      <div
        className="card"
        style={{
          width: "min(560px, 100%)",
          maxHeight: "min(80vh, 720px)",
          display: "flex",
          flexDirection: "column",
          gap: "0.75rem",
          // ``.card`` has a default ``margin-bottom`` for inline cards
          // in normal flow; inside a flex centered overlay that margin
          // shifts the dialog visibly upward. Override so the modal
          // sits exactly in the viewport center.
          marginBottom: 0,
        }}
      >
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline" }}>
          <h2 style={{ margin: 0 }}>{tIu("title")}</h2>
          <button
            type="button"
            className="ghost"
            onClick={dismiss}
            disabled={phase === "uploading"}
            aria-label={tIu("closeAria")}
          >
            ×
          </button>
        </div>

        <p className="meta" style={{ margin: 0 }}>
          {targetFolderId ? tIu("destFolder") : tIu("destRoot")}
          {" · "}
          {tIu("summaryFiles", {
            n: files.length,
            mib: (totalBytes / 1_048_576).toFixed(1),
          })}
        </p>

        {phase === "preview" || phase === "error" ? (
          <>
            {files.length === 0 ? (
              <p className="meta">{tIu("noFilesQueued")}</p>
            ) : (
              <ul
                style={{
                  margin: 0,
                  padding: 0,
                  listStyle: "none",
                  overflowY: "auto",
                  maxHeight: 280,
                  border: "1px solid var(--color-border, #e5e7eb)",
                  borderRadius: 4,
                }}
              >
                {files.slice(0, 100).map((f, i) => (
                  <li
                    key={`${f.name}-${i}`}
                    style={{
                      display: "flex",
                      alignItems: "center",
                      gap: "0.5rem",
                      padding: "0.35rem 0.5rem",
                      borderBottom: "1px solid var(--color-border, #f3f4f6)",
                    }}
                  >
                    <span style={{ flex: 1, overflow: "hidden", textOverflow: "ellipsis" }}>
                      {f.name}
                    </span>
                    <span className="meta" style={{ fontSize: "0.8rem" }}>
                      {(f.size / 1024).toFixed(1)} KiB
                    </span>
                    <button
                      type="button"
                      className="ghost"
                      onClick={() => removeFile(i)}
                      aria-label={tIu("removeAria", { name: f.name })}
                      style={{ padding: "0 0.4rem" }}
                    >
                      ×
                    </button>
                  </li>
                ))}
                {files.length > 100 && (
                  <li className="meta" style={{ padding: "0.35rem 0.5rem" }}>
                    {tIu("andMore", { n: files.length - 100 })}
                  </li>
                )}
              </ul>
            )}
            {err && <p className="error">{err}</p>}
            <div style={{ display: "flex", justifyContent: "flex-end", gap: "0.5rem" }}>
              <button type="button" className="ghost" onClick={dismiss}>
                {tIu("cancel")}
              </button>
              <button type="button" onClick={startUpload} disabled={files.length === 0}>
                {files.length > 0
                  ? tIu("uploadBtnWithCount", { n: files.length })
                  : tIu("uploadBtn")}
              </button>
            </div>
          </>
        ) : null}

        {phase === "uploading" && (
          <>
            <p>{tIu("uploadingPct", { pct: progress })}</p>
            <div
              style={{
                height: 6,
                background: "var(--color-border, #e5e7eb)",
                borderRadius: 3,
                overflow: "hidden",
              }}
            >
              <div
                role="progressbar"
                tabIndex={-1}
                aria-valuenow={progress}
                aria-valuemin={0}
                aria-valuemax={100}
                style={{
                  width: `${progress}%`,
                  height: "100%",
                  background: "var(--color-accent, #3b82f6)",
                  transition: "width 120ms linear",
                }}
              />
            </div>
            <div style={{ display: "flex", justifyContent: "flex-end" }}>
              <button type="button" className="ghost" onClick={() => xhrRef.current?.abort()}>
                {tIu("interrupt")}
              </button>
            </div>
          </>
        )}

        {phase === "done" && summary && (
          <>
            <SummaryView summary={summary} />
            <div style={{ display: "flex", justifyContent: "flex-end" }}>
              <button type="button" onClick={dismiss}>
                {tIu("closeBtn")}
              </button>
            </div>
          </>
        )}
      </div>
    </NativeDialog>
  );
}

function InlineDoneTitle() {
  const tIu = useTranslations("inlineUploader");
  return <>{tIu("doneTitle")}</>;
}

function SummaryView({ summary }: { summary: BulkUploadSummary }) {
  const tIu = useTranslations("inlineUploader");
  const byKind = summary.routed.reduce<Record<string, number>>((acc, r) => {
    acc[r.kind] = (acc[r.kind] ?? 0) + 1;
    return acc;
  }, {});
  return (
    <div>
      <h3 style={{ marginTop: 0 }}>
        <InlineDoneTitle />
      </h3>
      <ul style={{ marginTop: "0.25rem" }}>
        {summary.studies_created.length > 0 && (
          <li>{tIu("studiesCreated", { n: summary.studies_created.length })}</li>
        )}
        {summary.documents_created.length > 0 && (
          <li>{tIu("documentsAdded", { n: summary.documents_created.length })}</li>
        )}
        {summary.instances_created > 0 && (
          <li>
            {tIu("instancesNew", { n: summary.instances_created })}
            {summary.instances_existing > 0 && (
              <span className="meta">
                {tIu("instancesExisting", { n: summary.instances_existing })}
              </span>
            )}
          </li>
        )}
        {Object.entries(byKind).map(([kind, n]) => (
          <li key={kind} className="meta" style={{ fontSize: "0.85rem" }}>
            {kind}: {n}
          </li>
        ))}
        {summary.errors.length > 0 && (
          <li className="error">
            <strong>{summary.errors.length}</strong> error
            {summary.errors.length === 1 ? "e" : "i"}:
            <ul>
              {summary.errors.slice(0, 5).map((e) => (
                <li key={`${e.filename}:${e.message}`}>
                  <code>{e.filename}</code>: {e.message}
                </li>
              ))}
              {summary.errors.length > 5 && (
                <li className="meta">...e altri {summary.errors.length - 5}</li>
              )}
            </ul>
          </li>
        )}
      </ul>
    </div>
  );
}
