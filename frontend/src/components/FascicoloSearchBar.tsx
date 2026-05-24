"use client";

// Debounced search bar for a patient's fascicolo. Calls
// `/api/patients/{id}/search` 300ms after the user stops typing (min 2
// chars) and surfaces results either inline as a dropdown or to the
// parent via `onResults` / `onSelect`.

import { useTranslations } from "next-intl";
import { useEffect, useId, useRef, useState } from "react";

import {
  ApiError,
  type PatientSearchItem,
  type PatientSearchResult,
  patientSearchApi,
} from "@/lib/api";

const DEBOUNCE_MS = 300;
const MIN_QUERY_LEN = 2;

export interface FascicoloSearchBarProps {
  patientId: string;
  /** Called every time results change (including null when cleared). */
  onResults?: (result: PatientSearchResult | null) => void;
  /** Called when the user clicks a result. */
  onSelect?: (item: PatientSearchItem) => void;
  /** Hide the inline dropdown. Useful when parent renders its own list. */
  inlineResults?: boolean;
  placeholder?: string;
}

export default function FascicoloSearchBar({
  patientId,
  onResults,
  onSelect,
  inlineResults = true,
  placeholder,
}: FascicoloSearchBarProps) {
  const tUi = useTranslations("uiCommon");
  const tFasc = useTranslations("fascicolo");
  const tFsearch = useTranslations("fascicoloSearch");
  const resolvedPlaceholder = placeholder ?? tFasc("searchPlaceholder");
  const [query, setQuery] = useState("");
  const [semantic, setSemantic] = useState(false);
  const [result, setResult] = useState<PatientSearchResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  const containerRef = useRef<HTMLDivElement | null>(null);
  const inputId = useId();

  // Debounced search. The `cancelled` flag drops stale responses when a
  // fresher query lands before the previous fetch resolves.
  // biome-ignore lint/correctness/useExhaustiveDependencies: ``onResults`` is omitted on purpose — callers pass a fresh callback each render and we'd refetch on every parent rerender otherwise.
  useEffect(() => {
    const q = query.trim();
    if (q.length < MIN_QUERY_LEN) {
      setResult(null);
      setErr(null);
      setLoading(false);
      onResults?.(null);
      return;
    }
    setLoading(true);
    let cancelled = false;
    const handle = setTimeout(async () => {
      try {
        const res = await patientSearchApi.run(patientId, { q, semantic });
        if (cancelled) return;
        setResult(res);
        setErr(null);
        onResults?.(res);
      } catch (e) {
        if (cancelled) return;
        setErr(e instanceof ApiError ? e.message : "search failed");
        setResult(null);
        onResults?.(null);
      } finally {
        if (!cancelled) setLoading(false);
      }
    }, DEBOUNCE_MS);
    return () => {
      cancelled = true;
      clearTimeout(handle);
    };
  }, [patientId, query, semantic]);

  // Close dropdown on outside click.
  useEffect(() => {
    if (!open) return;
    function onDocClick(e: MouseEvent) {
      if (!containerRef.current) return;
      if (!containerRef.current.contains(e.target as Node)) setOpen(false);
    }
    document.addEventListener("mousedown", onDocClick);
    return () => document.removeEventListener("mousedown", onDocClick);
  }, [open]);

  function clearQuery() {
    setQuery("");
    setResult(null);
    setErr(null);
    setOpen(false);
    onResults?.(null);
  }

  const grouped = result ? groupBySection(result.items) : [];
  const showDropdown = inlineResults && open && query.trim().length >= MIN_QUERY_LEN;

  return (
    <div ref={containerRef} style={{ position: "relative", width: "100%" }}>
      <div style={{ display: "flex", gap: "0.5rem", alignItems: "center" }}>
        <input
          id={inputId}
          type="search"
          value={query}
          onChange={(e) => {
            setQuery(e.target.value);
            setOpen(true);
          }}
          onFocus={() => setOpen(true)}
          onKeyDown={(e) => {
            if (e.key === "Escape") clearQuery();
          }}
          placeholder={resolvedPlaceholder}
          aria-label={placeholder}
          style={{
            flex: 1,
            padding: "0.4rem 0.6rem",
            border: "1px solid var(--color-border, #d1d5db)",
            borderRadius: 6,
            fontSize: "0.9rem",
          }}
        />
        <label
          style={{
            display: "inline-flex",
            alignItems: "center",
            gap: "0.3rem",
            fontSize: "0.8rem",
            whiteSpace: "nowrap",
          }}
          title={tFsearch("semanticToggleTitle")}
        >
          <input
            type="checkbox"
            checked={semantic}
            onChange={(e) => setSemantic(e.target.checked)}
          />
          {tFsearch("semanticToggleLabel")}
        </label>
        {query && (
          <button
            type="button"
            className="ghost"
            onClick={clearQuery}
            style={{ fontSize: "0.8rem" }}
            title={tFsearch("esc")}
          >
            {tFsearch("clearBtn")}
          </button>
        )}
      </div>

      {showDropdown && (
        <div
          // biome-ignore lint/a11y/useSemanticElements: custom combobox listbox; native <select> cannot host the inline filter / chip UI
          role="listbox"
          // The input above drives navigation via Arrow/Enter/Esc;
          // the listbox itself is mouse-only, so ``tabIndex={-1}``
          // keeps it focusable for screen-reader navigation without
          // putting it in the natural tab order.
          tabIndex={-1}
          style={{
            position: "absolute",
            top: "100%",
            left: 0,
            right: 0,
            marginTop: "0.25rem",
            maxHeight: 480,
            overflowY: "auto",
            background: "var(--color-bg, #fff)",
            border: "1px solid var(--color-border, #e5e7eb)",
            borderRadius: 6,
            boxShadow: "0 4px 16px rgba(0,0,0,0.08)",
            zIndex: 20,
          }}
        >
          {loading && (
            <p className="meta" style={{ padding: "0.6rem" }}>
              {tFsearch("searching")}
            </p>
          )}
          {err && (
            <p className="error" style={{ padding: "0.6rem" }}>
              {err}
            </p>
          )}
          {!loading && !err && result && result.items.length === 0 && (
            <p className="meta" style={{ padding: "0.6rem" }}>
              {tUi("noResultsForQuery", { q: result.query })}
            </p>
          )}
          {!loading && !err && grouped.length > 0 && (
            <>
              <div
                className="meta"
                style={{
                  padding: "0.4rem 0.6rem",
                  fontSize: "0.75rem",
                  borderBottom: "1px solid var(--color-border, #e5e7eb)",
                }}
              >
                {result?.total ?? 0} risultat{(result?.total ?? 0) === 1 ? "o" : "i"}
              </div>
              {grouped.map((group) => (
                <div key={group.section}>
                  <div
                    style={{
                      padding: "0.3rem 0.6rem",
                      fontSize: "0.75rem",
                      fontWeight: 600,
                      textTransform: "uppercase",
                      letterSpacing: "0.03em",
                      background: "var(--color-muted, #f9fafb)",
                    }}
                  >
                    {sectionTranslationKey(group.section) === group.section
                      ? group.section
                      : tFsearch(
                          sectionTranslationKey(group.section) as
                            | "kindStudies"
                            | "kindReports"
                            | "kindAnnotations"
                            | "kindDocuments"
                            | "kindConsultations",
                        )}{" "}
                    ({group.items.length})
                  </div>
                  {group.items.map((item) => (
                    <SearchRow
                      key={`${item.section}-${item.id}`}
                      item={item}
                      onSelect={(it) => {
                        setOpen(false);
                        onSelect?.(it);
                      }}
                    />
                  ))}
                </div>
              ))}
            </>
          )}
        </div>
      )}
    </div>
  );
}

function SearchRow({
  item,
  onSelect,
}: {
  item: PatientSearchItem;
  onSelect: (it: PatientSearchItem) => void;
}) {
  return (
    <button
      type="button"
      // biome-ignore lint/a11y/useSemanticElements: custom combobox option inside a non-<select> listbox
      role="option"
      aria-selected="false"
      onClick={() => onSelect(item)}
      style={{
        display: "block",
        width: "100%",
        textAlign: "left",
        padding: "0.5rem 0.6rem",
        border: "none",
        borderBottom: "1px solid var(--color-border, #f3f4f6)",
        background: "transparent",
        cursor: "pointer",
      }}
    >
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          gap: "0.5rem",
        }}
      >
        <strong
          style={{
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: "nowrap",
            flex: 1,
          }}
        >
          {item.title}
        </strong>
        <span
          className="badge"
          title={`ts_rank ${item.rank.toFixed(3)}`}
          style={{ fontSize: "0.7rem", flexShrink: 0 }}
        >
          {item.rank.toFixed(2)}
        </span>
      </div>
      {item.preview && (
        <p
          className="meta"
          style={{
            margin: "0.15rem 0 0",
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: "nowrap",
            fontSize: "0.8rem",
          }}
        >
          {item.preview}
        </p>
      )}
    </button>
  );
}

function groupBySection(
  items: PatientSearchItem[],
): { section: string; items: PatientSearchItem[] }[] {
  const buckets = new Map<string, PatientSearchItem[]>();
  for (const it of items) {
    const bucket = buckets.get(it.section);
    if (bucket) bucket.push(it);
    else buckets.set(it.section, [it]);
  }
  return Array.from(buckets.entries()).map(([section, entries]) => ({
    section,
    items: entries,
  }));
}

function sectionTranslationKey(section: string): string {
  switch (section) {
    case "studies":
      return "kindStudies";
    case "reports":
      return "kindReports";
    case "annotations":
      return "kindAnnotations";
    case "documents":
      return "kindDocuments";
    case "consultations":
      return "kindConsultations";
    default:
      return section;
  }
}
