"use client";

import { useTranslations } from "next-intl";
import { useState } from "react";

import { API_BASE_URL, ApiError, getStoredToken } from "@/lib/api";

interface Props {
  documentId: string;
  filename?: string;
  /** Caller can override the label (e.g. localised "Download DVD" vs "Download PDF"). */
  label?: string;
}

/** Bit-perfect download of a Document blob through the backend proxy.
 *
 * Storage isolation: the bucket / endpoint / key never leak to the
 * client. The button issues an authenticated fetch (Bearer header from
 * localStorage), receives the blob into memory, and triggers a browser
 * save with the original filename. Suited to ISO/DVD originals the
 * radiologist wants to load on a professional workstation, but works
 * for any document blob (PDF, DICOM, image, …).
 *
 * For very large files (multi-GB ISO) the in-memory blob is OK on
 * desktops but would OOM a phone; in that case the radiologist uses
 * the dedicated workstation anyway, and the download fits.
 */
export default function IsoDownloadButton({ documentId, filename, label }: Props) {
  const t = useTranslations("isoDownload");
  const buttonLabel = label ?? t("defaultLabel");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function download() {
    setError(null);
    setBusy(true);
    try {
      const token = getStoredToken();
      const headers: HeadersInit = {};
      if (token) headers.authorization = `Bearer ${token}`;
      const resp = await fetch(`${API_BASE_URL}/api/documents/${documentId}/download`, {
        credentials: "include",
        headers,
        cache: "no-store",
      });
      if (!resp.ok) {
        let detail: unknown;
        try {
          detail = await resp.json();
        } catch {
          detail = await resp.text();
        }
        throw new ApiError(resp.status, detail);
      }
      const blob = await resp.blob();
      // Try to read the filename from Content-Disposition first; fall
      // back to the prop, then to a generic name.
      const cd = resp.headers.get("content-disposition") ?? "";
      const m = /filename\*?=(?:UTF-8'')?"?([^";]+)"?/.exec(cd);
      const finalName =
        (m ? decodeURIComponent(m[1]) : null) ?? filename ?? `document-${documentId}`;
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = finalName;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (e: unknown) {
      setError(
        e instanceof ApiError
          ? `${e.status}: ${e.message}`
          : e instanceof Error
            ? e.message
            : t("errorGeneric"),
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <span>
      <button type="button" onClick={() => void download()} disabled={busy}>
        {busy ? t("downloading") : buttonLabel}
      </button>
      {error && (
        <p role="alert" style={{ color: "#c00", margin: "0.25rem 0" }}>
          {error}
        </p>
      )}
    </span>
  );
}
