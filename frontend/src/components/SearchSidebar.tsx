"use client";

/*
 * SearchSidebar — left rail for /search.
 *
 * Three sections:
 *
 * 1. Scope segmented control (Public / Mine / All) — drives the
 *    ``scope`` query param on /api/search and /api/search/hybrid.
 *    Anonymous callers can only see "Public" (the auth filter already
 *    enforces this, the toggle is hidden).
 *
 * 2. Tag chip palette — populated from ``facets.top_tags`` returned
 *    by /api/search?facets=true. Clicking a chip toggles it into
 *    the active tag filter set. Active chips show a remove-X.
 *
 * 3. Faceted filters — modality / body_part / year, rendered as
 *    chip rows. Same toggle semantics as tags.
 *
 * All state is lifted: parent owns the filter object and re-runs
 * search on change. The sidebar is purely presentational + emits
 * onChange.
 *
 * MCP parity: the same scope + tag combos are exposed in the MCP
 * tools (search_studies, search_hybrid, search_by_tags) per
 * memory ``feedback-mcp-must-be-gui-superset``.
 */

import { useTranslations } from "next-intl";

import type { SearchFacets } from "@/lib/api";

export type SearchScope = "all" | "public" | "mine";

export interface SearchFilters {
  scope: SearchScope;
  tags: string[]; // entries shaped 'namespace:value'
  modality: string | null;
  body_part: string | null;
  year: string | null;
}

export const EMPTY_FILTERS: SearchFilters = {
  scope: "all",
  tags: [],
  modality: null,
  body_part: null,
  year: null,
};

interface Props {
  filters: SearchFilters;
  facets: SearchFacets | null | undefined;
  onChange: (next: SearchFilters) => void;
  /** When true, hide the "Mine" scope option (anonymous session). */
  hideMineScope?: boolean;
}

export default function SearchSidebar({ filters, facets, onChange, hideMineScope }: Props) {
  const t = useTranslations("searchSidebar");

  const setScope = (s: SearchScope) => onChange({ ...filters, scope: s });
  const toggleTag = (tag: string) => {
    const set = new Set(filters.tags);
    if (set.has(tag)) set.delete(tag);
    else set.add(tag);
    onChange({ ...filters, tags: Array.from(set) });
  };
  const setModality = (m: string | null) =>
    onChange({ ...filters, modality: filters.modality === m ? null : m });
  const setBodyPart = (b: string | null) =>
    onChange({ ...filters, body_part: filters.body_part === b ? null : b });
  const setYear = (y: string | null) =>
    onChange({ ...filters, year: filters.year === y ? null : y });

  return (
    <aside
      style={{
        flex: "0 0 240px",
        display: "flex",
        flexDirection: "column",
        gap: "1.1rem",
        fontSize: "0.85rem",
      }}
      aria-label={t("ariaLabel")}
    >
      <FilterSection title={t("scope")}>
        <ScopeSegmented value={filters.scope} onChange={setScope} hideMine={hideMineScope} t={t} />
      </FilterSection>

      {facets?.top_tags && facets.top_tags.length > 0 && (
        <FilterSection title={t("tags")}>
          <ChipRow>
            {facets.top_tags.map((row) => {
              const tag = `${row.namespace}:${row.value}`;
              const active = filters.tags.includes(tag);
              return (
                <Chip
                  key={tag}
                  active={active}
                  onClick={() => toggleTag(tag)}
                  count={row.count}
                  label={`${row.namespace}/${row.value}`}
                />
              );
            })}
          </ChipRow>
        </FilterSection>
      )}

      {facets?.modality && Object.keys(facets.modality).length > 0 && (
        <FilterSection title={t("modality")}>
          <ChipRow>
            {Object.entries(facets.modality).map(([m, n]) => (
              <Chip
                key={m}
                active={filters.modality === m}
                onClick={() => setModality(m)}
                count={n}
                label={m}
              />
            ))}
          </ChipRow>
        </FilterSection>
      )}

      {facets?.body_part && Object.keys(facets.body_part).length > 0 && (
        <FilterSection title={t("bodyPart")}>
          <ChipRow>
            {Object.entries(facets.body_part).map(([b, n]) => (
              <Chip
                key={b}
                active={filters.body_part === b}
                onClick={() => setBodyPart(b)}
                count={n}
                label={b}
              />
            ))}
          </ChipRow>
        </FilterSection>
      )}

      {facets?.year && Object.keys(facets.year).length > 0 && (
        <FilterSection title={t("year")}>
          <ChipRow>
            {Object.entries(facets.year)
              .sort((a, b) => b[0].localeCompare(a[0]))
              .map(([y, n]) => (
                <Chip
                  key={y}
                  active={filters.year === y}
                  onClick={() => setYear(y)}
                  count={n}
                  label={y}
                />
              ))}
          </ChipRow>
        </FilterSection>
      )}

      {(filters.tags.length > 0 ||
        filters.modality ||
        filters.body_part ||
        filters.year ||
        filters.scope !== "all") && (
        <button
          type="button"
          onClick={() => onChange(EMPTY_FILTERS)}
          style={{
            padding: "0.35rem 0.6rem",
            fontSize: "0.78rem",
            background: "transparent",
            border: "1px solid var(--bv-card-border)",
            borderRadius: 4,
            cursor: "pointer",
          }}
        >
          {t("clearAll")}
        </button>
      )}
    </aside>
  );
}

function FilterSection({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section>
      <h3
        style={{
          margin: "0 0 0.4rem",
          fontSize: "0.72rem",
          textTransform: "uppercase",
          letterSpacing: "0.04em",
          color: "var(--bv-muted)",
        }}
      >
        {title}
      </h3>
      {children}
    </section>
  );
}

function ChipRow({ children }: { children: React.ReactNode }) {
  return <div style={{ display: "flex", flexWrap: "wrap", gap: "0.3rem" }}>{children}</div>;
}

function Chip({
  active,
  onClick,
  count,
  label,
}: {
  active: boolean;
  onClick: () => void;
  count: number;
  label: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      style={{
        padding: "2px 8px",
        fontSize: "0.74rem",
        borderRadius: 12,
        border: `1px solid ${active ? "var(--bv-info, #6366f1)" : "var(--bv-card-border)"}`,
        background: active ? "var(--bv-info-soft, #eef2ff)" : "transparent",
        color: active ? "var(--bv-info, #4338ca)" : "inherit",
        cursor: "pointer",
        fontFamily: "inherit",
        lineHeight: 1.4,
      }}
      aria-pressed={active}
    >
      {label} <span style={{ opacity: 0.65 }}>· {count}</span>
    </button>
  );
}

function ScopeSegmented({
  value,
  onChange,
  hideMine,
  t,
}: {
  value: SearchScope;
  onChange: (s: SearchScope) => void;
  hideMine?: boolean;
  t: (key: string) => string;
}) {
  const items: { v: SearchScope; label: string }[] = [
    { v: "all", label: t("scopeAll") },
    { v: "public", label: t("scopePublic") },
  ];
  if (!hideMine) items.push({ v: "mine", label: t("scopeMine") });
  return (
    <div
      role="radiogroup"
      aria-label={t("scope")}
      style={{
        display: "inline-flex",
        border: "1px solid var(--bv-card-border)",
        borderRadius: 4,
        overflow: "hidden",
      }}
    >
      {items.map((it, i) => (
        <button
          key={it.v}
          type="button"
          role="radio"
          aria-checked={value === it.v}
          onClick={() => onChange(it.v)}
          style={{
            padding: "0.35rem 0.7rem",
            fontSize: "0.78rem",
            border: "none",
            borderLeft: i === 0 ? "none" : "1px solid var(--bv-card-border)",
            background: value === it.v ? "var(--bv-info-soft, #eef2ff)" : "transparent",
            color: value === it.v ? "var(--bv-info, #4338ca)" : "inherit",
            cursor: "pointer",
            fontFamily: "inherit",
          }}
        >
          {it.label}
        </button>
      ))}
    </div>
  );
}
