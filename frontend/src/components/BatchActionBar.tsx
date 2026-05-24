"use client";

// Sticky bar shown at the top of ContentPane when >= 1 item is selected.
// Exposes batch Download (ZIP), Delete, Move, Share, plus "Clear selection".
// When the backend endpoint is missing (404) we surface a clear message so
// the user knows it's not wired yet, rather than failing silently.

import { useTranslations } from "next-intl";
import { useCallback, useEffect, useMemo, useState } from "react";

import { useModal } from "@/components/ModalHost";
import NativeDialog from "@/components/NativeDialog";
import {
  ApiError,
  type BulkItemRef,
  type ItemKind,
  bulkApi,
  downloadJobResult,
  foldersApi,
} from "@/lib/api";
import { jobsStorage } from "@/lib/jobs";
import { useJob } from "@/lib/useJob";

function useMissingEndpointMessage() {
  const tBatch = useTranslations("batchBar");
  return (e: unknown, path: string, fallback: string): string => {
    if (e instanceof ApiError && e.status === 404) {
      return tBatch("endpointMissing", { path });
    }
    return e instanceof Error ? e.message : fallback;
  };
}

export interface BatchItem {
  id: string;
  kind: ItemKind;
  name: string;
}

interface Props {
  items: BatchItem[];
  onClear: () => void;
  /** Refresh the parent list after a mutating batch action. */
  onChanged?: () => void;
}

export default function BatchActionBar({ items, onClear, onChanged }: Props) {
  const modal = useModal();
  const tBatch = useTranslations("batchBar");
  const tA = useTranslations("actions");
  const missingEndpointMessage = useMissingEndpointMessage();
  const [busy, setBusy] = useState<null | "download" | "delete" | "share">(null);
  const [error, setError] = useState<string | null>(null);
  const [moveOpen, setMoveOpen] = useState(false);
  const [shareInfo, setShareInfo] = useState<string | null>(null);
  // Active download Job. Once enqueued we poll via ``useJob`` and
  // trigger the browser download from ``result_download_url`` on
  // success. The legacy "anchor click to a never-implemented GET"
  // path was 404'ing silently.
  const [downloadJobId, setDownloadJobId] = useState<string | null>(null);
  const [downloaded, setDownloaded] = useState(false);

  const refs: BulkItemRef[] = useMemo(
    () => items.map((i) => ({ id: i.id, kind: i.kind })),
    [items],
  );
  const count = items.length;

  const downloadable = useMemo(
    () => refs.filter((r) => r.kind === "study" || r.kind === "document"),
    [refs],
  );
  const handleDownload = useCallback(async () => {
    setError(null);
    setDownloaded(false);
    setBusy("download");
    try {
      if (downloadable.length === 0) {
        setError(tBatch("downloadNoEligible"));
        setBusy(null);
        return;
      }
      const job = await bulkApi.requestDownload(downloadable);
      jobsStorage.add({ id: job.id, kind: job.kind });
      setDownloadJobId(job.id);
    } catch (e) {
      setError(missingEndpointMessage(e, "/api/bulk/download", "download failed"));
    } finally {
      setBusy(null);
    }
  }, [downloadable, missingEndpointMessage, tBatch]);

  const { job: downloadJob, error: downloadPollError } = useJob(downloadJobId);
  useEffect(() => {
    if (!downloadJob || downloaded) return;
    if (downloadJob.status !== "succeeded" || !downloadJob.result_download_url) return;
    let cancelled = false;
    void downloadJobResult(downloadJob.id).then(() => {
      if (!cancelled) setDownloaded(true);
    });
    return () => {
      cancelled = true;
    };
  }, [downloadJob, downloaded]);

  const handleDelete = useCallback(async () => {
    const ok = await modal.confirm({
      message: tBatch("deleteConfirm", { n: count }),
      destructive: true,
      confirmLabel: tBatch("delete"),
    });
    if (!ok) return;
    setError(null);
    setBusy("delete");
    try {
      const result = await bulkApi.remove({ items: refs });
      if (result.failed?.length) {
        setError(tBatch("deleteFailed", { n: result.failed.length }));
      }
      onChanged?.();
      onClear();
    } catch (e) {
      setError(missingEndpointMessage(e, "/api/bulk/delete", "delete failed"));
    } finally {
      setBusy(null);
    }
  }, [count, refs, onChanged, onClear, modal, tBatch, missingEndpointMessage]);

  const handleShare = useCallback(async () => {
    setError(null);
    setBusy("share");
    setShareInfo(null);
    try {
      const link = await bulkApi.share({
        items: refs,
        permissions: ["read:metadata", "read:pixels", "read:annotations"],
        expires_in_hours: 168,
        password: null,
        label: `${count} items`,
      });
      const url = `${window.location.origin}/shared/${link.token}`;
      await navigator.clipboard.writeText(url).catch(() => {});
      setShareInfo(tBatch("linkCopied", { url }));
    } catch (e) {
      setError(missingEndpointMessage(e, "/api/bulk/share", "share failed"));
    } finally {
      setBusy(null);
    }
  }, [refs, count, tBatch, missingEndpointMessage]);

  return (
    <>
      <div
        role="toolbar"
        aria-label={tBatch("toolbarLabel")}
        style={{
          position: "sticky",
          top: 0,
          zIndex: 15,
          display: "flex",
          alignItems: "center",
          gap: "0.75rem",
          padding: "0.6rem 1rem",
          margin: "0 0 1rem",
          background: "var(--bv-header-bg, #fff)",
          border: "1px solid var(--bv-card-border, #e5e7eb)",
          borderRadius: 8,
          boxShadow: "0 1px 6px rgba(0,0,0,0.05)",
        }}
      >
        <button
          type="button"
          className="ghost"
          aria-label="Deseleziona tutto"
          onClick={onClear}
          style={{ padding: "0.3rem 0.55rem" }}
        >
          ✕
        </button>
        <span style={{ fontWeight: 500 }}>{tBatch("selectedCount", { n: count })}</span>
        <div style={{ flex: 1 }} />
        <button
          type="button"
          className="ghost"
          disabled={busy !== null || downloadJob?.status === "running"}
          onClick={handleDownload}
        >
          {busy === "download"
            ? tA("loadingShort")
            : downloadJob && (downloadJob.status === "queued" || downloadJob.status === "running")
              ? `${tBatch("downloadZip")} · ${
                  downloadJob.progress_total && downloadJob.progress_total > 0
                    ? `${downloadJob.progress_done}/${downloadJob.progress_total}`
                    : "…"
                }`
              : tBatch("downloadZip")}
        </button>
        <button
          type="button"
          className="ghost"
          disabled={busy !== null}
          onClick={() => setMoveOpen(true)}
        >
          {tBatch("moveTo")}
        </button>
        <button type="button" className="ghost" disabled={busy !== null} onClick={handleShare}>
          {busy === "share" ? tA("loadingShort") : tBatch("share")}
        </button>
        <button
          type="button"
          style={{ background: "#b42318", color: "#fff" }}
          disabled={busy !== null}
          onClick={handleDelete}
        >
          {busy === "delete" ? tA("loadingShort") : tBatch("delete")}
        </button>
      </div>
      {error && (
        <p className="error" style={{ color: "#b42318", marginTop: "-0.5rem" }}>
          {error}
        </p>
      )}
      {downloadJob?.status === "failed" && downloadJob.error?.message && (
        <p className="error" style={{ color: "#b42318", marginTop: "-0.5rem" }}>
          {downloadJob.error.message}
        </p>
      )}
      {downloadPollError instanceof ApiError && downloadJob?.status !== "succeeded" && (
        <p className="error" style={{ color: "#b42318", marginTop: "-0.5rem" }}>
          {downloadPollError.message}
        </p>
      )}
      {downloaded && downloadJob?.result_download_url && (
        <p className="meta" style={{ marginTop: "-0.5rem" }}>
          {tBatch("downloadStarted")}{" "}
          <button
            type="button"
            className="ghost"
            onClick={() => {
              if (downloadJob?.id) void downloadJobResult(downloadJob.id);
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
            {tBatch("downloadAgain")}
          </button>
        </p>
      )}
      {shareInfo && (
        <p className="meta" style={{ marginTop: "-0.5rem" }}>
          {shareInfo}
        </p>
      )}
      {moveOpen && (
        <MoveDialog
          items={refs}
          onClose={() => setMoveOpen(false)}
          onDone={() => {
            setMoveOpen(false);
            onChanged?.();
            onClear();
          }}
          onError={setError}
        />
      )}
    </>
  );
}

// ---- Move dialog ------------------------------------------------------------
// Lightweight modal: loads the current user's folders, shows them as a flat
// list (the backend already enforces the owner filter) plus a "Root" option,
// then POSTs `bulkApi.move`. Not a full tree walker — keeping it simple since
// most users only have a handful of folders. If a future iteration needs
// nested selection, swap the `ul` for a recursive component.

function MoveDialog({
  items,
  onClose,
  onDone,
  onError,
}: {
  items: BulkItemRef[];
  onClose: () => void;
  onDone: () => void;
  onError: (msg: string | null) => void;
}) {
  const tBatch = useTranslations("batchBar");
  const tA = useTranslations("actions");
  const missingEndpointMessage = useMissingEndpointMessage();
  const [folders, setFolders] = useState<Array<{ id: string; name: string }>>([]);
  const [loading, setLoading] = useState(true);
  const [target, setTarget] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const rows = await foldersApi.list();
        if (!cancelled) setFolders(rows.map((f) => ({ id: f.id, name: f.name })));
      } catch {
        if (!cancelled) setFolders([]);
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  async function submit() {
    setSaving(true);
    onError(null);
    try {
      await bulkApi.move({ items, target_folder_id: target });
      onDone();
    } catch (e) {
      onError(missingEndpointMessage(e, "/api/bulk/move", "move failed"));
    } finally {
      setSaving(false);
    }
  }

  return (
    <NativeDialog
      open
      onClose={onClose}
      ariaLabel={tBatch("moveDialogLabel")}
      className="bv-dialog"
    >
      <div
        style={{
          background: "var(--bv-card-bg,#fff)",
          borderRadius: 10,
          padding: "1.25rem",
          minWidth: 360,
          maxHeight: "70vh",
          overflowY: "auto",
        }}
      >
        <h2 style={{ marginTop: 0 }}>{tBatch("moveDialogTitle", { n: items.length })}</h2>
        {loading ? (
          <p className="meta">{tBatch("moveDialogLoading")}</p>
        ) : (
          <ul style={{ listStyle: "none", padding: 0, margin: "0 0 1rem" }}>
            <li>
              <label style={{ display: "flex", gap: "0.5rem", padding: "0.4rem 0" }}>
                <input
                  type="radio"
                  name="target"
                  checked={target === null}
                  onChange={() => setTarget(null)}
                />
                <span>{tBatch("moveDialogRoot")}</span>
              </label>
            </li>
            {folders.map((f) => (
              <li key={f.id}>
                <label style={{ display: "flex", gap: "0.5rem", padding: "0.4rem 0" }}>
                  <input
                    type="radio"
                    name="target"
                    checked={target === f.id}
                    onChange={() => setTarget(f.id)}
                  />
                  <span>{f.name}</span>
                </label>
              </li>
            ))}
          </ul>
        )}
        <div style={{ display: "flex", gap: "0.5rem", justifyContent: "flex-end" }}>
          <button type="button" className="ghost" onClick={onClose}>
            {tBatch("moveDialogCancel")}
          </button>
          <button type="button" disabled={saving || loading} onClick={submit}>
            {saving ? tA("loadingShort") : tBatch("moveDialogSubmit")}
          </button>
        </div>
      </div>
    </NativeDialog>
  );
}
