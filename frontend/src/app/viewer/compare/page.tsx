"use client";

import dynamic from "next/dynamic";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense, useEffect, useMemo, useState } from "react";

import type { ComparisonPane } from "@/components/ComparisonLayout";
import SeriesPicker from "@/components/SeriesPicker";
import { ApiError, studiesApi } from "@/lib/api";

const ComparisonLayout = dynamic(() => import("@/components/ComparisonLayout"), {
  ssr: false,
  loading: () => <div style={{ padding: "2rem", color: "#bbb" }}>loading…</div>,
});

const MAX_PANES = 4;
type Axis = "axial" | "sagittal" | "coronal";

export default function ComparePage() {
  return (
    <Suspense
      fallback={
        <main>
          <p className="meta">Loading…</p>
        </main>
      }
    >
      <CompareView />
    </Suspense>
  );
}

function CompareView() {
  const search = useSearchParams();
  const [panes, setPanes] = useState<ComparisonPane[]>([]);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [syncScroll, setSyncScroll] = useState(false);
  const [syncWL, setSyncWL] = useState(false);
  const [axis, setAxis] = useState<Axis>("axial");

  // ?left, ?right, and ?s all feed the same list; left/right are just a
  // naming convention for the 2-pane (new vs prior) case.
  const initialIds = useMemo(() => {
    const ids: string[] = [];
    const collect = (key: string) => {
      for (const v of search.getAll(key)) if (v) ids.push(v);
    };
    collect("left");
    collect("right");
    collect("s");
    // Dedupe while preserving order.
    const seen = new Set<string>();
    const out: string[] = [];
    for (const id of ids) {
      if (seen.has(id)) continue;
      seen.add(id);
      out.push(id);
    }
    return out.slice(0, MAX_PANES);
  }, [search]);

  useEffect(() => {
    let cancelled = false;
    if (initialIds.length === 0) {
      setPanes([]);
      return;
    }
    Promise.all(
      initialIds.map((id) =>
        studiesApi
          .series(id)
          .then((s) => ({
            seriesId: s.id,
            label: `${s.modality ?? "?"} · ${s.series_description ?? `series #${s.series_number ?? "?"}`}`,
          }))
          .catch((e) => ({
            seriesId: id,
            label: `series ${id.slice(0, 8)} (${e instanceof ApiError ? e.message : "error"})`,
          })),
      ),
    ).then((resolved) => {
      if (!cancelled) setPanes(resolved);
    });
    return () => {
      cancelled = true;
    };
  }, [initialIds]);

  const addPane = (seriesId: string, label: string) => {
    setPanes((prev) => {
      if (prev.length >= MAX_PANES) return prev;
      if (prev.some((p) => p.seriesId === seriesId)) return prev;
      return [...prev, { seriesId, label }];
    });
  };

  const removePane = (index: number) => {
    setPanes((prev) => prev.filter((_, i) => i !== index));
  };

  return (
    <div className="viewer-layout" style={{ margin: 0, maxWidth: "none" }}>
      <div className="viewer-layout__canvas">
        <ComparisonLayout
          panes={panes}
          axis={axis}
          syncScroll={syncScroll}
          syncWL={syncWL}
          onRemove={removePane}
        />
      </div>
      <aside className="viewer-layout__sidebar">
        <Link href="/studies" style={{ color: "#9cb9e6" }}>
          ← studies
        </Link>
        <h2>Compare</h2>
        <p className="meta" style={{ fontSize: "0.7rem" }}>
          Up to {MAX_PANES} series side-by-side. Panes are independent by default; toggle the
          options below to sync slice scroll or window/level across all panes.
        </p>

        <h2>
          Panes ({panes.length}/{MAX_PANES})
        </h2>
        <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
          {panes.map((p, i) => (
            <div
              key={`${p.seriesId}-${i}`}
              style={{
                display: "flex",
                justifyContent: "space-between",
                alignItems: "center",
                border: "1px solid #2a2f3b",
                borderRadius: 4,
                padding: "0.3rem 0.5rem",
                fontSize: "0.75rem",
              }}
            >
              <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                {p.label}
              </span>
              <button
                type="button"
                onClick={() => removePane(i)}
                style={{
                  background: "none",
                  border: "none",
                  color: "#f66",
                  cursor: "pointer",
                  fontSize: "0.9rem",
                }}
                title="Remove"
              >
                ×
              </button>
            </div>
          ))}
          <button
            type="button"
            className="viewer-btn"
            disabled={panes.length >= MAX_PANES}
            onClick={() => setPickerOpen(true)}
            style={{ fontSize: "0.75rem", marginTop: 4 }}
          >
            {panes.length >= MAX_PANES ? "Max panes reached" : "+ Add series"}
          </button>
        </div>

        <h2>Axis</h2>
        <div style={{ display: "flex", gap: "0.3rem", flexWrap: "wrap" }}>
          {(["axial", "sagittal", "coronal"] as Axis[]).map((a) => (
            <button
              key={a}
              type="button"
              className={axis === a ? "viewer-btn viewer-btn--active" : "viewer-btn"}
              style={{ fontSize: "0.7rem", padding: "0.2rem 0.45rem" }}
              onClick={() => setAxis(a)}
            >
              {a}
            </button>
          ))}
        </div>

        <h2>Sync</h2>
        <label
          className="meta"
          style={{ display: "flex", alignItems: "center", gap: 6, fontSize: "0.75rem" }}
        >
          <input
            type="checkbox"
            checked={syncScroll}
            onChange={(e) => setSyncScroll(e.target.checked)}
          />
          Sync slice scroll
        </label>
        <label
          className="meta"
          style={{ display: "flex", alignItems: "center", gap: 6, fontSize: "0.75rem" }}
        >
          <input type="checkbox" checked={syncWL} onChange={(e) => setSyncWL(e.target.checked)} />
          Sync window / level
        </label>
        <p className="meta" style={{ fontSize: "0.65rem", marginTop: "0.3rem" }}>
          Scroll = slice · Drag = W/L · Middle-drag = pan · Ctrl+Scroll = zoom
        </p>
      </aside>

      <SeriesPicker
        open={pickerOpen}
        onClose={() => setPickerOpen(false)}
        onPick={addPane}
        excludeSeriesIds={panes.map((p) => p.seriesId)}
      />
    </div>
  );
}
