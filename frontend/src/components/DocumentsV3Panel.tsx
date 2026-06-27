"use client";

import { useLocale, useTranslations } from "next-intl";
import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";

import IngestDocumentDialog from "@/components/IngestDocumentDialog";
import IsoDownloadButton from "@/components/IsoDownloadButton";
import MergeAliasesDialog from "@/components/MergeAliasesDialog";
import { ApiError, type PatientDocument, patientsApi } from "@/lib/api";
import { entryLabel, useDocumentCatalog } from "@/lib/useDocumentCatalog";

interface Props {
  patientId: string;
  isOwner: boolean;
}

const KIND_OVERRIDES = new Set<string>([
  "referral",
  "consent",
  "emergency_report",
  "progress_note",
  "history_physical",
  "imaging_study_bundle",
]);

const AUTHORITY_COLOR: Record<string, string> = {
  original: "#059669",
  derived: "#d97706",
  canonical_synthesis: "#2563eb",
  stale: "#9ca3af",
};

export default function DocumentsV3Panel({ patientId, isOwner }: Props) {
  const t = useTranslations("documentsPanel");
  const locale = useLocale();
  const tKindOverrides = useTranslations("ingestDoc.kindOverrides");
  const tAuthority = useTranslations("documentsPanel.authority");
  const { catalog } = useDocumentCatalog();

  const [docs, setDocs] = useState<PatientDocument[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [ingestOpen, setIngestOpen] = useState(false);
  const [mergeOpen, setMergeOpen] = useState(false);
  // Date is the natural primary axis (most recent first by default);
  // ``kind`` lets the user batch-review by clinical type (all referrals
  // together, then all consents, etc.). Keeping it client-side is fine
  // here because the panel always loads the full document list — no
  // pagination — and switching axes must feel instant.
  type SortKey = "date" | "kind";
  type SortDir = "asc" | "desc";
  const [sortKey, setSortKey] = useState<SortKey>("date");
  const [sortDir, setSortDir] = useState<SortDir>("desc");

  const cycleSort = useCallback(
    (key: SortKey) => {
      if (sortKey !== key) {
        // First click on a different column: keep the sensible default
        // direction for that column (desc = newest first for date,
        // asc = A→Z for kind labels).
        setSortKey(key);
        setSortDir(key === "date" ? "desc" : "asc");
        return;
      }
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    },
    [sortKey],
  );

  const refresh = useCallback(async () => {
    setError(null);
    try {
      const rows = await patientsApi.listDocuments(patientId);
      // The list endpoint must return an array; if the API surface
      // ever drifts (or a stale catch-all in tests serves a non-JSON
      // body), treat the response as empty rather than letting
      // ``rows.filter`` / ``rows.length`` blow up later in render.
      setDocs(Array.isArray(rows) ? rows : []);
    } catch (e: unknown) {
      setError(e instanceof ApiError ? `${e.status}: ${e.message}` : "error");
    }
  }, [patientId]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  function toggle(id: string) {
    const next = new Set(selected);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    setSelected(next);
  }

  // Memoised so the function identity is stable across renders and
  // can be a clean ``useMemo`` dep below; the deps are the only inputs
  // it reads, so the resolved label is correct whenever any of them
  // changes (catalog finishes loading, locale flips via the Language
  // switcher, i18n message bundle hot-reloads in dev).
  const kindLabel = useCallback(
    (kindId: string): string => {
      if (KIND_OVERRIDES.has(kindId)) {
        try {
          return tKindOverrides(kindId);
        } catch {
          // Override key missing — fall through to catalog.
        }
      }
      const entry = catalog?.kinds.find((k) => k.id === kindId);
      return entry ? entryLabel(entry, locale) : kindId;
    },
    [catalog, locale, tKindOverrides],
  );

  function authorityLabel(authId: string): string {
    try {
      return tAuthority(authId);
    } catch {
      return authId;
    }
  }

  // Memoised sorted list. Date keying prefers ``document_date`` (the
  // clinical date the user cares about) and falls back to ``created_at``
  // — same rule the cell render uses, so the sort always agrees with
  // the visible value. Empty / null dates sink to the bottom in both
  // directions: an undated row never has useful position relative to
  // a dated row, so flipping it to the top in ascending mode would
  // defeat the affordance.
  const sortedDocs = useMemo<PatientDocument[]>(() => {
    // Guard against a malformed response: a non-array value is
    // truthy enough to slip past the null-check, then ``[...obj]``
    // throws "is not iterable" inside the useMemo, which Next surfaces
    // as the bare "Application error" boundary in production. Treating
    // it as empty matches the loading-state UX and lets the user
    // recover by reloading.
    if (!docs || !Array.isArray(docs)) return [];
    const dirSign = sortDir === "asc" ? 1 : -1;
    const out = [...docs];
    if (sortKey === "kind") {
      const collator = new Intl.Collator(locale, { sensitivity: "base", numeric: true });
      out.sort((a, b) => {
        const A = kindLabel(a.kind_id);
        const B = kindLabel(b.kind_id);
        const cmp = collator.compare(A, B);
        if (cmp !== 0) return dirSign * cmp;
        // Tie-break on date desc so equal-kind groups stay
        // chronologically reasonable.
        const dateA = a.document_date ?? a.created_at.slice(0, 10);
        const dateB = b.document_date ?? b.created_at.slice(0, 10);
        return dateB.localeCompare(dateA);
      });
      return out;
    }
    out.sort((a, b) => {
      const A = a.document_date ?? a.created_at.slice(0, 10);
      const B = b.document_date ?? b.created_at.slice(0, 10);
      if (!A && !B) return 0;
      if (!A) return 1;
      if (!B) return -1;
      return dirSign * A.localeCompare(B);
    });
    return out;
    // ``kindLabel`` is the only indirection: it captures ``catalog`` +
    // i18n internally and the useCallback above re-creates it when
    // either changes, so listing it here is sufficient (catalog /
    // locale don't need to be listed twice).
  }, [docs, sortKey, sortDir, locale, kindLabel]);

  if (error) {
    return (
      <p role="alert" style={{ color: "#c00" }}>
        {error}
      </p>
    );
  }
  if (docs === null) return <p>{t("loading")}</p>;

  return (
    <section>
      <header
        style={{
          display: "flex",
          gap: "0.5rem",
          alignItems: "center",
          marginBottom: "0.75rem",
        }}
      >
        <strong style={{ flex: 1 }}>
          {t("countLabel", { n: docs.length })}
          {selected.size > 0 ? ` ${t("selectedSuffix", { n: selected.size })}` : ""}
        </strong>
        {isOwner && selected.size >= 2 && (
          <button type="button" onClick={() => setMergeOpen(true)}>
            {t("mergeAlias")}
          </button>
        )}
        {isOwner && (
          <button type="button" onClick={() => setIngestOpen(true)}>
            {t("addDocument")}
          </button>
        )}
      </header>

      {docs.length === 0 ? (
        <p style={{ color: "var(--bv-muted)" }}>{t("empty")}</p>
      ) : (
        <div style={{ overflowX: "auto" }}>
          <table style={{ width: "100%", fontSize: "0.9rem" }}>
            <thead>
              <tr>
                {isOwner && <th />}
                <th style={{ textAlign: "left" }}>{t("colTitle")}</th>
                <th style={{ textAlign: "left" }}>
                  <SortableHeader
                    active={sortKey === "kind"}
                    dir={sortDir}
                    onClick={() => cycleSort("kind")}
                    label={t("colKind")}
                    ariaLabel={t("sortByKind")}
                  />
                </th>
                <th style={{ textAlign: "left" }}>{t("colAuthority")}</th>
                <th style={{ textAlign: "left" }}>
                  <SortableHeader
                    active={sortKey === "date"}
                    dir={sortDir}
                    onClick={() => cycleSort("date")}
                    label={t("colDate")}
                    ariaLabel={t("sortByDate")}
                  />
                </th>
                <th style={{ textAlign: "left" }}>{t("colHash")}</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {sortedDocs.map((d) => (
                <tr key={d.id}>
                  {isOwner && (
                    <td>
                      <input
                        type="checkbox"
                        checked={selected.has(d.id)}
                        onChange={() => toggle(d.id)}
                      />
                    </td>
                  )}
                  <td>
                    <Link
                      href={`/patients/${patientId}/documents/${d.id}?from=documents`}
                      style={{ textDecoration: "none" }}
                    >
                      {d.title}
                    </Link>
                  </td>
                  <td>
                    <small>{kindLabel(d.kind_id)}</small>
                  </td>
                  <td>
                    <span
                      style={{
                        fontSize: "0.7rem",
                        padding: "0.1rem 0.4rem",
                        borderRadius: "999px",
                        background: AUTHORITY_COLOR[d.authority_id] ?? "#9ca3af",
                        color: "white",
                      }}
                    >
                      {authorityLabel(d.authority_id)}
                    </span>
                  </td>
                  <td>{d.document_date ?? d.created_at.slice(0, 10)}</td>
                  <td
                    style={{
                      fontFamily: "monospace",
                      fontSize: "0.75em",
                      color: "var(--bv-muted)",
                    }}
                  >
                    {d.content_sha256 ? `${d.content_sha256.slice(0, 8)}…` : "—"}
                  </td>
                  <td style={{ textAlign: "right" }}>
                    <Link
                      href={`/provenance/document/${d.id}`}
                      style={{ marginRight: "0.5rem", fontSize: "0.85rem" }}
                    >
                      {t("history")}
                    </Link>
                    {(d.kind_id === "imaging_study_bundle" ||
                      d.provenance_id === "dicom_dvd_iso") && (
                      <IsoDownloadButton documentId={d.id} filename={d.title} label="ISO" />
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <IngestDocumentDialog
        patientId={patientId}
        open={ingestOpen}
        onClose={() => setIngestOpen(false)}
        onIngested={() => {
          setIngestOpen(false);
          void refresh();
        }}
      />
      <MergeAliasesDialog
        candidates={docs.filter((d) => selected.has(d.id))}
        initialSelected={Array.from(selected)}
        open={mergeOpen}
        onClose={() => setMergeOpen(false)}
        onMerged={() => {
          setMergeOpen(false);
          setSelected(new Set());
          void refresh();
        }}
      />
    </section>
  );
}

/**
 * Click-to-sort table header. Renders the column label plus a triangle
 * indicator that points up (asc) or down (desc) when this header drives
 * the current sort. Inactive headers render with a muted ↕ glyph so the
 * affordance is discoverable without dominating the row.
 */
function SortableHeader({
  active,
  dir,
  onClick,
  label,
  ariaLabel,
}: {
  active: boolean;
  dir: "asc" | "desc";
  onClick: () => void;
  label: string;
  ariaLabel: string;
}) {
  const indicator = active ? (dir === "asc" ? "▲" : "▼") : "↕";
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label={ariaLabel}
      aria-sort={active ? (dir === "asc" ? "ascending" : "descending") : "none"}
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: "0.3rem",
        padding: 0,
        background: "transparent",
        border: "none",
        color: "inherit",
        font: "inherit",
        fontWeight: 600,
        cursor: "pointer",
      }}
    >
      <span>{label}</span>
      <span
        aria-hidden
        style={{
          fontSize: "0.7em",
          color: active ? "var(--bv-fg, #0f172a)" : "var(--bv-muted, #94a3b8)",
        }}
      >
        {indicator}
      </span>
    </button>
  );
}
