"use client";

/*
 * /search — unified cross-fascicolo search.
 *
 * Single text input + a "type" selector that branches between three
 * existing backends:
 *
 * - "studies" → ``/api/search/hybrid`` — DICOM tag + image-similarity
 *   + report text RRF over imaging studies. The strongest signal for
 *   "find me the CT abdomen with contrast from 2024".
 *
 * - "metadata" → ``/api/search`` (the legacy text-on-metadata path).
 *   Lighter, useful when the user knows exactly what to type. This
 *   mode also hosts the SearchSidebar (scope + tag chip palette +
 *   facets) because /api/search is the one that returns facets.
 *
 * - "visual" → link to the dedicated ``/search/visual`` page that
 *   takes a study/series exemplar (the existing visual search lives
 *   on its own page because the input shape is fundamentally
 *   different — you point at a series, not at a string).
 *
 * Privacy guarantee: every result links to ``/patients/<id>?…`` /
 * ``/studies/<id>``; no S3 keys, no raw PHI in the response. The
 * server-side endpoints already enforce per-row visibility against
 * the caller's grants. The sidebar's ``scope`` filter can only
 * narrow that set, never widen it.
 */

import { useTranslations } from "next-intl";
import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import SearchSidebar, { EMPTY_FILTERS, type SearchFilters } from "@/components/SearchSidebar";
import { useAuth } from "@/lib/auth-context";
import { ApiError, type HybridSearchOut, type Paginated, type Study, searchApi } from "@/lib/api";

type Mode = "studies" | "metadata";

interface UnifiedResults {
  hybrid?: HybridSearchOut;
  meta?: Paginated<Study>;
}

export default function UnifiedSearchPage() {
  const t = useTranslations("searchPage");
  const { user } = useAuth();
  const [q, setQ] = useState("");
  const [mode, setMode] = useState<Mode>("studies");
  const [filters, setFilters] = useState<SearchFilters>(EMPTY_FILTERS);
  const [results, setResults] = useState<UnifiedResults | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const hasActiveFilter = useCallback((f: SearchFilters): boolean => {
    return (
      f.scope !== "all" ||
      f.tags.length > 0 ||
      f.modality !== null ||
      f.body_part !== null ||
      f.year !== null
    );
  }, []);

  const runSearch = useCallback(
    async (qq: string, m: Mode, f: SearchFilters) => {
      const trimmed = qq.trim();
      // "Browse" semantics: with no query but with at least one filter
      // active (scope=Public is the common case — "show me the OpenData
      // library"), fall through to the metadata endpoint which accepts
      // a missing ``q`` and returns the auth-filtered set straight up.
      // Without this guard the old code returned ``setResults(null)``
      // and the page rendered an empty state, making the OpenData
      // catalogue invisible through /search.
      const hasFilter = hasActiveFilter(f);
      if (!trimmed && !hasFilter) {
        setResults(null);
        return;
      }
      setBusy(true);
      setErr(null);
      try {
        // Hybrid requires a query (signals rank against ``q``). Drop
        // to the metadata path whenever the user is browsing by
        // filter alone, regardless of the mode toggle.
        const effectiveMode: Mode = m === "studies" && trimmed ? "studies" : "metadata";
        if (effectiveMode === "studies") {
          const out = await searchApi.hybrid({
            q: trimmed,
            k: 30,
            scope: f.scope === "all" ? undefined : f.scope,
          });
          setResults({ hybrid: out });
        } else {
          // Metadata path: every filter is a first-class query param.
          // ``q`` is optional — an empty value with filters set yields
          // the unfiltered catalogue under the chosen scope.
          const out = await searchApi.run({
            q: trimmed || undefined,
            limit: 50,
            facets: true,
            scope: f.scope === "all" ? undefined : f.scope,
            tag: f.tags.length > 0 ? f.tags : undefined,
            modality: f.modality ?? undefined,
            body_part: f.body_part ?? undefined,
          });
          setResults({ meta: out });
        }
      } catch (e) {
        setErr(e instanceof ApiError ? e.message : "search failed");
      } finally {
        setBusy(false);
      }
    },
    [hasActiveFilter],
  );

  // Re-run when mode or filters change if EITHER the query is non-
  // empty OR at least one filter is active (browse semantics).
  useEffect(() => {
    if (q.trim() || hasActiveFilter(filters)) runSearch(q, mode, filters);
    else setResults(null);
  }, [mode, q, filters, runSearch, hasActiveFilter]);

  const onSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    runSearch(q, mode, filters);
  };

  // Anonymous users have no "mine" scope (nothing they own).
  const hideMineScope = user === null;

  return (
    <main style={{ padding: "1.25rem", maxWidth: 1400 }}>
      <header style={{ marginBottom: "1rem" }}>
        <h1 style={{ margin: 0 }}>{t("title")}</h1>
        <p className="meta" style={{ marginTop: "0.4rem", fontSize: "0.9rem" }}>
          {t("intro")} <Link href="/search/visual">{t("visualSearchLink")} →</Link>.
        </p>
        <p className="meta" style={{ fontSize: "0.78rem" }}>
          {t("privacyNote")}
        </p>
      </header>

      <form
        onSubmit={onSubmit}
        style={{
          display: "flex",
          gap: "0.5rem",
          alignItems: "center",
          flexWrap: "wrap",
          marginBottom: "0.75rem",
        }}
      >
        <input
          type="search"
          autoFocus
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder={t("inputPlaceholder")}
          style={{ flex: "1 1 360px", minWidth: 300, padding: "0.5rem 0.7rem" }}
        />
        <select
          value={mode}
          onChange={(e) => setMode(e.target.value as Mode)}
          style={{ padding: "0.5rem" }}
          aria-label={t("modeAria")}
        >
          <option value="studies">{t("modeHybrid")}</option>
          <option value="metadata">{t("modeMetadata")}</option>
        </select>
        <button type="submit" disabled={busy || !q.trim()}>
          {busy ? "…" : t("submit")}
        </button>
      </form>

      {err && <p style={{ color: "var(--bv-error, #cf6e6e)", margin: "0.5rem 0" }}>{err}</p>}

      <div style={{ display: "flex", gap: "1.5rem", alignItems: "flex-start" }}>
        <SearchSidebar
          filters={filters}
          facets={results?.meta?.facets ?? null}
          onChange={setFilters}
          hideMineScope={hideMineScope}
        />

        <div style={{ flex: 1, minWidth: 0 }}>
          {results === null && !busy && q.trim() === "" && (
            <p className="meta">{t("emptyState")}</p>
          )}
          {results?.hybrid && <HybridList q={q} out={results.hybrid} t={t} />}
          {results?.meta && <MetaList q={q} out={results.meta} t={t} />}
        </div>
      </div>
    </main>
  );
}

function HybridList({
  q,
  out,
  t,
}: {
  q: string;
  out: HybridSearchOut;
  t: (key: string, vals?: Record<string, string | number | Date>) => string;
}) {
  if (out.items.length === 0) {
    return <p className="meta">{t("noHybridResults", { q })}</p>;
  }
  return (
    <div>
      <p className="meta" style={{ fontSize: "0.82rem", margin: "0.5rem 0 0.75rem" }}>
        {out.items.length} studi · pesi:{" "}
        {Object.entries(out.weights_used)
          .map(([k, v]) => `${k}=${v}`)
          .join(", ")}
      </p>
      <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "grid", gap: "0.5rem" }}>
        {out.items.map((it) => (
          <li
            key={it.study.id}
            className="card"
            style={{ padding: "0.6rem 0.85rem", display: "flex", gap: "0.85rem" }}
          >
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ display: "flex", justifyContent: "space-between", gap: "1rem" }}>
                <Link
                  href={`/studies/${it.study.id}`}
                  style={{ fontWeight: 600, textDecoration: "none" }}
                >
                  {it.study.study_description || it.study.modalities?.join(", ") || "Studio"}
                </Link>
                <span className="meta" style={{ fontSize: "0.78rem" }}>
                  score {it.score.toFixed(3)}
                </span>
              </div>
              <div className="meta" style={{ fontSize: "0.78rem", marginTop: "0.15rem" }}>
                {it.study.modalities?.join(" / ")}{" "}
                {it.study.study_date ? `· ${it.study.study_date}` : ""}{" "}
                {it.study.patient_id ? (
                  <>
                    · <Link href={`/patients/${it.study.patient_id}`}>fascicolo</Link>
                  </>
                ) : null}
                {it.study.is_opendata && (
                  <>
                    {" · "}
                    <span style={{ color: "var(--bv-info, #4338ca)" }}>{t("openDataBadge")}</span>
                  </>
                )}
              </div>
              <div
                className="meta"
                style={{
                  fontSize: "0.74rem",
                  marginTop: "0.2rem",
                  display: "flex",
                  gap: "0.5rem",
                  flexWrap: "wrap",
                }}
              >
                {(["tag", "text", "image"] as const).map((sig) => (
                  <span
                    key={sig}
                    style={{
                      padding: "1px 6px",
                      borderRadius: 3,
                      background: "var(--bv-card-bg-soft, transparent)",
                      border: "1px solid var(--bv-card-border)",
                      opacity: it.signals[sig] > 0 ? 1 : 0.4,
                    }}
                    title={`Contributo ${sig}: ${it.signals[sig].toFixed(3)}`}
                  >
                    {sig} {it.signals[sig].toFixed(2)}
                  </span>
                ))}
              </div>
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}

function MetaList({
  q,
  out,
  t,
}: {
  q: string;
  out: Paginated<Study>;
  t: (key: string, vals?: Record<string, string | number | Date>) => string;
}) {
  if (out.items.length === 0) {
    return <p className="meta">{t("noMetaResults", { q })}</p>;
  }
  return (
    <div>
      <p className="meta" style={{ fontSize: "0.82rem", margin: "0.5rem 0 0.75rem" }}>
        {out.total} {t("metaResultsCount")}
      </p>
      <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "grid", gap: "0.4rem" }}>
        {out.items.map((s) => (
          <li key={s.id} className="card" style={{ padding: "0.55rem 0.85rem" }}>
            <Link href={`/studies/${s.id}`} style={{ fontWeight: 600, textDecoration: "none" }}>
              {s.study_description || s.modalities?.join(", ") || "Studio"}
            </Link>
            <div className="meta" style={{ fontSize: "0.78rem", marginTop: "0.15rem" }}>
              {s.modalities?.join(" / ")} {s.study_date ? `· ${s.study_date}` : ""}{" "}
              {s.patient_id ? (
                <>
                  · <Link href={`/patients/${s.patient_id}`}>fascicolo</Link>
                </>
              ) : null}
              {s.is_opendata && (
                <>
                  {" · "}
                  <span style={{ color: "var(--bv-info, #4338ca)" }}>{t("openDataBadge")}</span>
                </>
              )}
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}
