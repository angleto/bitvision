"use client";

import { useLocale, useTranslations } from "next-intl";
import { useState } from "react";

import NativeDialog from "@/components/NativeDialog";
import { ApiError } from "@/lib/api";
import { request } from "@/lib/api";
import { entryLabel, useDocumentCatalog } from "@/lib/useDocumentCatalog";

interface Props {
  patientId: string;
  open: boolean;
  onClose: () => void;
  onIngested?: (documentId: string) => void;
}

// i18n override: a few catalog ids have shorter, picker-tuned labels
// under ``ingestDoc.kindOverrides`` (e.g. "Referral" reads better as
// "Impegnativa / Richiesta" in this dialog). When the override key
// exists we prefer it; otherwise we fall back to the catalog's
// localised ``display_name``.
const KIND_OVERRIDES = new Set<string>([
  "referral",
  "consent",
  "emergency_report",
  "progress_note",
  "history_physical",
  "imaging_study_bundle",
]);

async function fileToBase64(f: File): Promise<string> {
  const buf = await f.arrayBuffer();
  const bytes = new Uint8Array(buf);
  const CHUNK = 0x8000;
  let bin = "";
  for (let i = 0; i < bytes.length; i += CHUNK) {
    bin += String.fromCharCode.apply(null, Array.from(bytes.subarray(i, i + CHUNK)));
  }
  return btoa(bin);
}

export default function IngestDocumentDialog({ patientId, open, onClose, onIngested }: Props) {
  const t = useTranslations("ingestDoc");
  const locale = useLocale();
  const tKindOverrides = useTranslations("ingestDoc.kindOverrides");
  const { catalog } = useDocumentCatalog();

  const [file, setFile] = useState<File | null>(null);
  const [kind, setKind] = useState("unclassified");
  const [provenance, setProvenance] = useState("digital_native_pdf");
  const [authority, setAuthority] = useState("original");
  const [title, setTitle] = useState("");
  const [documentDate, setDocumentDate] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!open) return null;

  const activeKinds = (catalog?.kinds ?? []).filter((k) => k.is_active);
  const activeProvenances = (catalog?.provenances ?? []).filter((p) => p.is_active);
  const activeAuthorities = (catalog?.authorities ?? []).filter((a) => a.is_active);

  const kindLabel = (value: string) => {
    if (KIND_OVERRIDES.has(value)) {
      try {
        return tKindOverrides(value);
      } catch {
        // Override key missing — fall through to catalog.
      }
    }
    const entry = catalog?.kinds.find((k) => k.id === value);
    return entry ? entryLabel(entry, locale) : value;
  };

  async function ingest() {
    if (!file) {
      setError(t("errorNoFile"));
      return;
    }
    setError(null);
    setBusy(true);
    try {
      const b64 = await fileToBase64(file);
      const result = await request<{
        document_id: string;
        kind_id: string;
        provenance_id: string;
        authority_id: string;
      }>("/api/documents/ingest", {
        method: "POST",
        json: {
          patient_id: patientId,
          filename: file.name,
          content_base64: b64,
          content_type: file.type || undefined,
          kind_id: kind,
          provenance_id: provenance,
          authority_id: authority,
          title: title || undefined,
          document_date: documentDate || undefined,
        },
      });
      onIngested?.(result.document_id);
      setFile(null);
      setTitle("");
      setDocumentDate("");
      onClose();
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
    <NativeDialog open={open} onClose={onClose} ariaLabel={t("ariaLabel")} className="bv-dialog">
      <div
        style={{
          background: "var(--card-bg, white)",
          borderRadius: "0.5rem",
          padding: "1.5rem",
          maxWidth: "600px",
          width: "90%",
          maxHeight: "85vh",
          overflow: "auto",
        }}
      >
        <h2 style={{ marginTop: 0 }}>{t("title")}</h2>
        <p style={{ color: "var(--muted-fg, #666)" }}>{t("intro")}</p>

        <label style={{ display: "block", margin: "1rem 0" }}>
          {t("fieldFile")}
          <input
            type="file"
            onChange={(e) => {
              const f = e.target.files?.[0] ?? null;
              setFile(f);
              if (f && !title) setTitle(f.name);
            }}
            style={{ display: "block", marginTop: "0.25rem" }}
          />
          {file && (
            <small style={{ color: "var(--muted-fg, #666)" }}>
              {file.name} — {Math.round(file.size / 1024)} KB
            </small>
          )}
        </label>

        <label style={{ display: "block", margin: "0.75rem 0" }}>
          {t("fieldTitle")}
          <input
            type="text"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            style={{ width: "100%", marginTop: "0.25rem" }}
            placeholder={t("titlePlaceholder")}
          />
        </label>

        <label style={{ display: "block", margin: "0.75rem 0" }}>
          {t("fieldDate")}
          <input
            type="date"
            value={documentDate}
            onChange={(e) => setDocumentDate(e.target.value)}
            style={{ marginTop: "0.25rem" }}
          />
        </label>

        <div
          style={{
            display: "grid",
            gridTemplateColumns: "1fr 1fr 1fr",
            gap: "0.75rem",
            margin: "1rem 0",
          }}
        >
          <label>
            {t("fieldKind")}
            <select
              value={kind}
              onChange={(e) => setKind(e.target.value)}
              disabled={activeKinds.length === 0}
              style={{ width: "100%", marginTop: "0.25rem" }}
            >
              {activeKinds.map((k) => (
                <option key={k.id} value={k.id}>
                  {kindLabel(k.id)}
                </option>
              ))}
            </select>
          </label>
          <label>
            {t("fieldProvenance")}
            <select
              value={provenance}
              onChange={(e) => setProvenance(e.target.value)}
              disabled={activeProvenances.length === 0}
              style={{ width: "100%", marginTop: "0.25rem" }}
            >
              {activeProvenances.map((p) => (
                <option key={p.id} value={p.id}>
                  {entryLabel(p, locale)}
                </option>
              ))}
            </select>
          </label>
          <label>
            {t("fieldAuthority")}
            <select
              value={authority}
              onChange={(e) => setAuthority(e.target.value)}
              disabled={activeAuthorities.length === 0}
              style={{ width: "100%", marginTop: "0.25rem" }}
            >
              {activeAuthorities.map((a) => (
                <option key={a.id} value={a.id}>
                  {entryLabel(a, locale)}
                </option>
              ))}
            </select>
          </label>
        </div>

        {error && (
          <p role="alert" style={{ color: "#c00" }}>
            {error}
          </p>
        )}

        <div
          style={{
            display: "flex",
            gap: "0.5rem",
            justifyContent: "flex-end",
          }}
        >
          <button type="button" onClick={onClose} disabled={busy}>
            {t("cancel")}
          </button>
          <button
            type="button"
            disabled={!file || busy}
            onClick={() => void ingest()}
            style={{
              background: file ? "#2563eb" : undefined,
              color: file ? "white" : undefined,
            }}
          >
            {busy ? t("submitBusy") : t("submit")}
          </button>
        </div>
      </div>
    </NativeDialog>
  );
}
