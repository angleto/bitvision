"use client";

import { useTranslations } from "next-intl";
import { useState } from "react";

import NativeDialog from "@/components/NativeDialog";
import { ApiError } from "@/lib/api";
import { type MergeAliasesResult, documentsApi } from "@/lib/api_records";

interface DocumentLike {
  id: string;
  title: string;
  content_sha256?: string | null;
  created_at?: string;
  document_type?: string;
}

interface Props {
  candidates: DocumentLike[];
  initialSelected?: string[];
  open: boolean;
  onClose: () => void;
  onMerged?: (result: MergeAliasesResult) => void;
}

export default function MergeAliasesDialog({
  candidates,
  initialSelected = [],
  open,
  onClose,
  onMerged,
}: Props) {
  const t = useTranslations("mergeAliases");
  const [selected, setSelected] = useState<Set<string>>(new Set(initialSelected));
  const [canonicalId, setCanonicalId] = useState<string>(initialSelected[0] ?? "");
  const [reason, setReason] = useState<string>("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!open) return null;

  const validSelection = selected.size >= 2 && (canonicalId === "" || selected.has(canonicalId));

  function toggle(id: string) {
    const next = new Set(selected);
    if (next.has(id)) {
      next.delete(id);
      if (canonicalId === id) setCanonicalId("");
    } else {
      next.add(id);
      if (!canonicalId) setCanonicalId(id);
    }
    setSelected(next);
  }

  async function run() {
    setError(null);
    setBusy(true);
    try {
      const result = await documentsApi.merge({
        document_ids: Array.from(selected),
        canonical_id: canonicalId || undefined,
        reason: reason.trim() || undefined,
      });
      onMerged?.(result);
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
          maxWidth: "700px",
          width: "90%",
          maxHeight: "85vh",
          overflow: "auto",
        }}
      >
        <h2 style={{ marginTop: 0 }}>{t("title")}</h2>
        <p style={{ color: "var(--muted-fg, #666)" }}>{t("intro")}</p>

        <table style={{ width: "100%", marginTop: "1rem" }}>
          <thead>
            <tr>
              <th />
              <th style={{ textAlign: "left" }}>{t("colTitle")}</th>
              <th style={{ textAlign: "left" }}>{t("colType")}</th>
              <th style={{ textAlign: "left" }}>{t("colHash")}</th>
              <th style={{ textAlign: "left" }}>{t("colCanonical")}</th>
            </tr>
          </thead>
          <tbody>
            {candidates.map((d) => (
              <tr key={d.id}>
                <td>
                  <input
                    type="checkbox"
                    checked={selected.has(d.id)}
                    onChange={() => toggle(d.id)}
                  />
                </td>
                <td>{d.title}</td>
                <td>
                  <small>{d.document_type ?? "—"}</small>
                </td>
                <td style={{ fontFamily: "monospace", fontSize: "0.75em" }}>
                  {d.content_sha256 ? `${d.content_sha256.slice(0, 12)}…` : "—"}
                </td>
                <td>
                  <input
                    type="radio"
                    name="canonical"
                    disabled={!selected.has(d.id) || !d.content_sha256}
                    title={d.content_sha256 ? t("setCanonicalTitle") : t("noHashTitle")}
                    checked={canonicalId === d.id}
                    onChange={() => setCanonicalId(d.id)}
                  />
                </td>
              </tr>
            ))}
          </tbody>
        </table>

        <label style={{ display: "block", marginTop: "1rem" }}>
          {t("reasonLabel")}
          <textarea
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            rows={2}
            style={{ width: "100%", marginTop: "0.25rem" }}
            placeholder={t("reasonPlaceholder")}
          />
        </label>

        {error && (
          <p role="alert" style={{ color: "#c00", marginTop: "0.5rem" }}>
            {error}
          </p>
        )}

        <div
          style={{
            marginTop: "1rem",
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
            disabled={!validSelection || busy}
            onClick={() => void run()}
            style={{
              background: validSelection ? "#2563eb" : undefined,
              color: validSelection ? "white" : undefined,
            }}
          >
            {busy
              ? t("submitBusy")
              : t("submit", {
                  count: selected.size,
                  canonicalSuffix: canonicalId ? "" : t("canonicalDefault"),
                })}
          </button>
        </div>
      </div>
    </NativeDialog>
  );
}
