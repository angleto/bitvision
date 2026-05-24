"use client";

import { useTranslations } from "next-intl";
import { useCallback, useEffect, useMemo, useState } from "react";

import { patientsApi } from "@/lib/api";
import { useIsMobile } from "@/lib/useIsMobile";
import Markdown from "./Markdown";
import PDFViewer from "./PDFViewer";

export interface DocumentPreviewProps {
  documentId: string;
  patientId: string;
  /** Free-form document_type slug (e.g. ``consent``); shown in the header. */
  documentType?: string | null;
  /** MIME type as stored on the document row — drives the render branch. */
  contentType: string | null;
  filename: string;
  /** Optional size in bytes; shown in the header for ``binary`` blobs. */
  sizeBytes?: number | null;
  onClose?: () => void;
}

type RenderKind = "pdf" | "image" | "text" | "markdown" | "binary" | "unknown";

function detectKind(contentType: string | null, filename: string): RenderKind {
  const ct = (contentType ?? "").toLowerCase();
  const ext = filename.toLowerCase().split(".").pop() ?? "";
  if (ct === "application/pdf" || ext === "pdf") return "pdf";
  if (ct.startsWith("image/")) return "image";
  if (ext === "md" || ext === "markdown" || ct === "text/markdown") return "markdown";
  if (ct.startsWith("text/") || ext === "txt") return "text";
  // Binary archives we should NEVER pull through the browser as a
  // blob: CD/DVD ISO images sit at 500 MiB-5 GiB and previewing one
  // means scrolling 1+ GiB through `fetch` + `URL.createObjectURL`,
  // which OOM-kills the tab and offers nothing to look at. The user
  // wants a download link plus the metadata sidebar, full stop.
  if (
    ct === "application/x-iso9660-image" ||
    ct === "application/x-iso9660" ||
    ct === "application/octet-stream" ||
    ext === "iso" ||
    ext === "img" ||
    ext === "dmg" ||
    ext === "zip" ||
    ext === "7z" ||
    ext === "tar" ||
    ext === "gz"
  ) {
    return "binary";
  }
  return "unknown";
}

function _formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KiB`;
  if (n < 1024 * 1024 * 1024) return `${(n / (1024 * 1024)).toFixed(1)} MiB`;
  return `${(n / (1024 * 1024 * 1024)).toFixed(2)} GiB`;
}

export default function DocumentPreview({
  documentId,
  patientId,
  documentType,
  contentType,
  filename,
  sizeBytes,
  onClose,
}: DocumentPreviewProps) {
  const tUi = useTranslations("uiCommon");
  const tDoc = useTranslations("documentPreview");
  const kind = useMemo(() => detectKind(contentType, filename), [contentType, filename]);
  // Mobile browsers (iOS Safari, Chrome Android, in-app webviews) do
  // not render application/pdf inline in iframes — they prompt for
  // download or show a broken plugin slot. Treat PDFs as binary on
  // mobile so the user gets the explicit "Apri / Scarica" affordance
  // instead of a black panel that does nothing on tap.
  const isMobile = useIsMobile();
  const contentUrl = useMemo(
    () => patientsApi.documentContentUrl(patientId, documentId),
    [patientId, documentId],
  );

  const [textBody, setTextBody] = useState<string | null>(null);
  const [blobUrl, setBlobUrl] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [downloadErr, setDownloadErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(kind !== "binary");
  const [downloading, setDownloading] = useState(false);

  // The "Scarica" affordances all funnel through here so the
  // browser uses native streaming-to-disk via signed-token anchor —
  // critical for multi-GiB DVD ISOs which would OOM a fetch+Blob
  // path. The bare ``<a href={contentUrl}>`` form was broken in
  // prod (``optional_user`` rejects anchor clicks under the global
  // auth gate) — see ``feedback_storage_isolation`` + the
  // ``download_tokens`` service.
  const handleDownload = useCallback(async () => {
    if (downloading) return;
    setDownloading(true);
    setDownloadErr(null);
    try {
      await patientsApi.downloadDocument(documentId, filename);
    } catch (e) {
      // Inline error instead of ``window.alert`` (UX downgrade: blocks
      // the tab, prefixed by "localhost:3000 says", can't be styled
      // or dismissed alongside the rest of the panel state). The
      // banner sits above the preview content in ``downloadErr``.
      setDownloadErr(e instanceof Error ? e.message : String(e));
    } finally {
      setDownloading(false);
    }
  }, [documentId, filename, downloading]);

  useEffect(() => {
    // ``binary`` is the "do not preview" branch (ISO bundles, archives,
    // generic octet-stream). Streaming a multi-GiB blob into the
    // browser to render a download link would OOM the tab, so we skip
    // the fetch entirely and let the header's Download link do its
    // job. Kept inside the effect so toggling the prop still flips
    // the loading state cleanly.
    if (kind === "binary") {
      setLoading(false);
      setErr(null);
      setTextBody(null);
      setBlobUrl(null);
      return;
    }

    // The API requires a bearer token, so we can't just point an <iframe>
    // or <img> at the endpoint — instead we fetch it, which causes the
    // browser to follow the 307 to the (presigned) S3 URL that *doesn't*
    // need auth, and we hand the resulting blob URL to the renderer.
    let cancelled = false;
    let currentBlob: string | null = null;

    async function load() {
      setLoading(true);
      setErr(null);
      setTextBody(null);
      setBlobUrl(null);
      try {
        const resp = await fetch(contentUrl, {
          credentials: "include",
          cache: "no-store",
        });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        if (kind === "text" || kind === "markdown") {
          const body = await resp.text();
          if (!cancelled) setTextBody(body);
        } else {
          const blob = await resp.blob();
          if (cancelled) return;
          currentBlob = URL.createObjectURL(blob);
          setBlobUrl(currentBlob);
        }
      } catch (e) {
        if (!cancelled) setErr(e instanceof Error ? e.message : "load failed");
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    load();
    return () => {
      cancelled = true;
      if (currentBlob) URL.revokeObjectURL(currentBlob);
    };
  }, [contentUrl, kind]);

  return (
    <aside
      style={{
        display: "flex",
        flexDirection: "column",
        width: "100%",
        height: "100%",
        minHeight: 400,
        border: "1px solid var(--color-border, #d4d4d8)",
        borderRadius: 6,
        background: "var(--color-surface, #fff)",
        overflow: "hidden",
      }}
    >
      <header
        style={{
          display: "flex",
          alignItems: "center",
          gap: "0.5rem",
          padding: "0.5rem 0.75rem",
          borderBottom: "1px solid var(--color-border, #d4d4d8)",
          background: "var(--color-surface-alt, #f4f4f5)",
          flexWrap: "wrap",
        }}
      >
        <strong style={{ flex: 1, minWidth: 0, overflow: "hidden", textOverflow: "ellipsis" }}>
          {filename}
        </strong>
        {documentType && <span className="badge">{documentType}</span>}
        <button
          type="button"
          className="ghost"
          disabled={downloading}
          onClick={handleDownload}
          style={{
            fontSize: "0.8rem",
            cursor: downloading ? "wait" : "pointer",
          }}
          title={tDoc("downloadTitle")}
        >
          {downloading ? tDoc("downloadingShort") : tDoc("downloadShort")}
        </button>
        {onClose && (
          <button
            type="button"
            onClick={onClose}
            className="ghost"
            aria-label={tDoc("closeAriaLabel")}
            style={{ fontSize: "1rem", lineHeight: 1, padding: "0.2rem 0.5rem" }}
          >
            ×
          </button>
        )}
      </header>
      <div style={{ flex: 1, minHeight: 0, position: "relative", overflow: "auto" }}>
        {downloadErr && (
          <div
            role="alert"
            style={{
              padding: "0.5rem 0.75rem",
              borderBottom: "1px solid var(--color-border, #d4d4d8)",
              background: "var(--bv-danger-soft, #fee2e2)",
              color: "var(--bv-danger, #b91c1c)",
              fontSize: "0.85rem",
              display: "flex",
              alignItems: "center",
              gap: "0.5rem",
            }}
          >
            <span style={{ flex: 1 }}>{tDoc("downloadError", { error: downloadErr })}</span>
            <button
              type="button"
              className="ghost"
              onClick={() => setDownloadErr(null)}
              aria-label={tDoc("closeAriaLabel")}
              style={{ fontSize: "0.85rem", padding: "0.1rem 0.4rem" }}
            >
              ×
            </button>
          </div>
        )}
        {loading && (
          <p className="meta" style={{ padding: "1rem" }}>
            {tUi("loading")}
          </p>
        )}
        {err && !loading && (
          <div style={{ padding: "1rem" }}>
            <p className="error">{tDoc("errorPreview", { error: err })}</p>
            <DownloadFallback filename={filename} busy={downloading} onDownload={handleDownload} />
          </div>
        )}
        {!loading && !err && kind === "pdf" && blobUrl && !isMobile && <PDFViewer url={blobUrl} />}
        {!loading && !err && kind === "pdf" && blobUrl && isMobile && (
          <div
            style={{
              padding: "1.25rem",
              display: "flex",
              flexDirection: "column",
              gap: "0.75rem",
              alignItems: "flex-start",
            }}
          >
            <p style={{ margin: 0, fontWeight: 500 }}>{tDoc("mobilePdfTitle")}</p>
            <p className="meta" style={{ margin: 0, fontSize: "0.85rem" }}>
              {tDoc("mobilePdfHint")}
            </p>
            <div style={{ display: "flex", gap: "0.5rem", flexWrap: "wrap" }}>
              <a
                href={blobUrl}
                target="_blank"
                rel="noreferrer noopener"
                className="primary"
                style={{
                  display: "inline-block",
                  padding: "0.5rem 0.85rem",
                  background: "var(--bv-accent, #1a73e8)",
                  color: "#fff",
                  borderRadius: 6,
                  textDecoration: "none",
                  fontSize: "0.9rem",
                }}
              >
                {tDoc("openInNewTab")}
              </a>
              <button
                type="button"
                className="ghost"
                disabled={downloading}
                onClick={handleDownload}
              >
                {downloading ? tDoc("downloading") : tDoc("download", { filename })}
              </button>
            </div>
          </div>
        )}
        {!loading && !err && kind === "image" && blobUrl && (
          <img
            src={blobUrl}
            alt={filename}
            style={{
              display: "block",
              width: "100%",
              height: "100%",
              objectFit: "contain",
              background: "#000",
            }}
          />
        )}
        {!loading && !err && kind === "text" && textBody !== null && (
          <pre
            style={{
              margin: 0,
              padding: "1rem",
              whiteSpace: "pre-wrap",
              wordBreak: "break-word",
              fontFamily:
                "ui-monospace, SFMono-Regular, Menlo, Consolas, 'Liberation Mono', monospace",
              fontSize: "0.85rem",
            }}
          >
            {textBody}
          </pre>
        )}
        {!loading && !err && kind === "markdown" && textBody !== null && (
          <div style={{ padding: "1rem", fontSize: "0.9rem" }}>
            <Markdown text={textBody} />
          </div>
        )}
        {!loading && !err && kind === "binary" && (
          <div
            style={{
              padding: "1.5rem",
              display: "flex",
              flexDirection: "column",
              gap: "0.75rem",
              alignItems: "flex-start",
            }}
          >
            <p style={{ margin: 0, fontWeight: 500 }}>{tDoc("binaryArchive")}</p>
            <p className="meta" style={{ margin: 0, fontSize: "0.85rem" }}>
              {contentType ? tDoc("typeLabel", { type: contentType }) : null}
              {contentType && sizeBytes ? " — " : null}
              {typeof sizeBytes === "number" && sizeBytes > 0
                ? tDoc("sizeLabel", { size: _formatBytes(sizeBytes) })
                : null}
            </p>
            <p className="meta" style={{ margin: 0, fontSize: "0.85rem" }}>
              {tDoc("binaryHelp")}
            </p>
            <button
              type="button"
              className="primary"
              disabled={downloading}
              onClick={handleDownload}
              style={{
                cursor: downloading ? "wait" : "pointer",
              }}
            >
              {downloading ? tDoc("downloading") : tDoc("download", { filename })}
            </button>
          </div>
        )}
        {!loading && !err && kind === "unknown" && (
          <div style={{ padding: "1rem" }}>
            <p className="meta">{tDoc("unknownPreview")}</p>
            <DownloadFallback filename={filename} busy={downloading} onDownload={handleDownload} />
          </div>
        )}
      </div>
    </aside>
  );
}

function DownloadFallback({
  filename,
  busy,
  onDownload,
}: {
  filename: string;
  busy: boolean;
  onDownload: () => void;
}) {
  const t = useTranslations("documentPreview");
  return (
    <p>
      <button
        type="button"
        className="ghost"
        disabled={busy}
        onClick={onDownload}
        style={{
          background: "none",
          border: "none",
          padding: 0,
          color: "var(--bv-link, #1a73e8)",
          textDecoration: "underline",
          cursor: busy ? "wait" : "pointer",
          font: "inherit",
        }}
      >
        {busy ? t("downloadingFile", { filename }) : t("download", { filename })}
      </button>
    </p>
  );
}
