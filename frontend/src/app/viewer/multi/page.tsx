"use client";

// Multi-series viewer: hosts up to 4 ``CornerstoneMPRLayout`` panes
// side-by-side, each loading its own series. Replaces the cripple
// ``/viewer/compare`` route that used canvas-based MPRViewport with
// no tool support — every pane here has the full Cornerstone tool
// group (crosshair, W/L, measurements, fusion, voxel HUD, ...).
//
// Sync across panes is intentionally NOT enabled by default: each
// series is its own frame of reference, and global slice-sync only
// makes clinical sense when the volumes are co-registered (PET/CT
// of the same scan). For that case the user should use the dedicated
// ``Open as PET-CT fusion`` flow from the study page; this multi
// route is the "look at multiple unrelated series" tool.

import dynamic from "next/dynamic";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense, useEffect, useMemo, useRef, useState } from "react";

import type { MPRLayoutHandle } from "@/components/MPRLayoutTypes";
import type { VolumeData } from "@/components/VolumeViewer";
import { ApiError, type Series, fetchVolume, studiesApi } from "@/lib/api";
import { modalityDefaults } from "@/lib/windowing";

const CornerstoneMPRLayout = dynamic(() => import("@/components/CornerstoneMPRLayout"), {
  ssr: false,
});

const MAX_PANES = 4;

interface PaneState {
  seriesId: string;
  series: Series | null;
  volume: VolumeData | null;
  err: string | null;
}

export default function MultiViewerPage() {
  return (
    <Suspense
      fallback={
        <main>
          <p className="meta">Loading…</p>
        </main>
      }
    >
      <MultiViewerInner />
    </Suspense>
  );
}

function MultiViewerInner() {
  const search = useSearchParams();
  const seriesIds = useMemo(() => {
    const all: string[] = [];
    for (const v of search.getAll("s")) if (v) all.push(v);
    // Dedupe + cap at MAX_PANES.
    const seen = new Set<string>();
    const out: string[] = [];
    for (const id of all) {
      if (seen.has(id)) continue;
      seen.add(id);
      out.push(id);
    }
    return out.slice(0, MAX_PANES);
  }, [search]);

  const [panes, setPanes] = useState<PaneState[]>(() =>
    seriesIds.map((seriesId) => ({
      seriesId,
      series: null,
      volume: null,
      err: null,
    })),
  );

  // Re-init the pane list when the URL changes.
  useEffect(() => {
    setPanes((prev) => {
      const byId = new Map(prev.map((p) => [p.seriesId, p]));
      return seriesIds.map(
        (id) =>
          byId.get(id) ?? {
            seriesId: id,
            series: null,
            volume: null,
            err: null,
          },
      );
    });
  }, [seriesIds]);

  // Per-pane loader: fetches the series metadata + the packed volume.
  // Each pane owns its load lifecycle so a slow series doesn't block
  // siblings; partial failures degrade to an inline error in the
  // affected pane.
  useEffect(() => {
    const cancellers: Array<() => void> = [];
    for (const pane of panes) {
      if (pane.volume || pane.err) continue;
      let cancelled = false;
      cancellers.push(() => {
        cancelled = true;
      });
      void (async () => {
        try {
          const series = await studiesApi.series(pane.seriesId);
          const { header, scalars } = await fetchVolume(pane.seriesId);
          if (cancelled) return;
          setPanes((prev) =>
            prev.map((p) =>
              p.seriesId === pane.seriesId
                ? {
                    ...p,
                    series,
                    volume: {
                      dimensions: [header.nx, header.ny, header.nz],
                      spacing: header.spacing,
                      scalars,
                      range: header.valueRange,
                    },
                  }
                : p,
            ),
          );
        } catch (e) {
          if (cancelled) return;
          setPanes((prev) =>
            prev.map((p) =>
              p.seriesId === pane.seriesId
                ? {
                    ...p,
                    err: e instanceof ApiError ? e.message : String(e),
                  }
                : p,
            ),
          );
        }
      })();
    }
    return () => {
      for (const c of cancellers) c();
    };
  }, [panes]);

  const removePane = (seriesId: string) => {
    setPanes((prev) => prev.filter((p) => p.seriesId !== seriesId));
  };

  const count = panes.length;
  if (count === 0) {
    return (
      <main style={{ padding: "2rem" }}>
        <Link href="/studies" style={{ color: "#9cb9e6" }}>
          ← studies
        </Link>
        <h1>Multi-series viewer</h1>
        <p className="meta">
          No series selected. Pick 2+ series in a study and choose “Open as multi-series”.
        </p>
      </main>
    );
  }

  // Grid: 1×N for ≤2 panes, 2×N for 3-4 (mirrors CT-MPR conventions).
  const cols = count <= 2 ? count : 2;
  const rows = count <= 2 ? 1 : 2;

  return (
    <div
      style={{
        margin: 0,
        padding: 0,
        height: "calc(100vh - 56px)",
        background: "#000",
        display: "grid",
        gridTemplateColumns: `repeat(${cols}, 1fr)`,
        gridTemplateRows: `repeat(${rows}, 1fr)`,
        gap: 4,
      }}
    >
      {panes.map((pane) => (
        <Pane key={pane.seriesId} pane={pane} onRemove={() => removePane(pane.seriesId)} />
      ))}
    </div>
  );
}

type PaneTool =
  | "wl"
  | "measure-dist"
  | "measure-angle"
  | "measure-ellipse"
  | "measure-rect"
  | "measure-arrow"
  | "measure-probe"
  | "measure-area";

function Pane({ pane, onRemove }: { pane: PaneState; onRemove: () => void }) {
  const mprRef = useRef<MPRLayoutHandle | null>(null);
  const [tool, setTool] = useState<PaneTool>("wl");
  const presets = pane.series
    ? modalityDefaults(pane.series.modality ?? "", pane.series.body_part_examined ?? undefined)
    : [];

  return (
    <div
      style={{
        position: "relative",
        background: "#000",
        overflow: "hidden",
        border: "1px solid #1a1f2b",
        display: "grid",
        gridTemplateRows: "auto 1fr",
      }}
    >
      {/* Compact per-pane toolbar — gives the multi-series viewer
          the controls a single-pane viewer has, without dragging in
          the full sidebar (which would crowd a 2- or 4-pane grid).
          The toolbar is local to this pane: tool selection, W/L
          preset, reset button. Other features (per-pane crosshair,
          measurements, voxel HUD, flip / rotate) come from the
          embedded ``CornerstoneMPRLayout`` itself. */}
      <div
        style={{
          display: "flex",
          gap: 4,
          alignItems: "center",
          padding: "4px 6px",
          background: "rgba(15,20,30,0.85)",
          borderBottom: "1px solid #1a1f2b",
          fontFamily: "ui-monospace, monospace",
          fontSize: "0.7rem",
        }}
      >
        <span
          style={{
            color: "#e96b1f",
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: "nowrap",
            flex: 1,
            minWidth: 0,
          }}
          title={
            pane.series
              ? `${pane.series.modality ?? "?"} · ${pane.series.series_description ?? "(no description)"}`
              : pane.seriesId
          }
        >
          {pane.series
            ? `${pane.series.modality ?? "?"} · ${
                pane.series.series_description ?? `series ${pane.seriesId.slice(0, 8)}`
              }`
            : pane.seriesId.slice(0, 8)}
        </span>
        <select
          value={tool}
          onChange={(e) => setTool(e.target.value as PaneTool)}
          style={{
            background: "#1a1f2b",
            color: "#e6ecf3",
            border: "1px solid #2a2f3b",
            borderRadius: 3,
            fontSize: "0.7rem",
            padding: "1px 4px",
          }}
          title="Active mouse tool (primary button)"
        >
          <option value="wl">Crosshair / WL</option>
          <option value="measure-dist">Length</option>
          <option value="measure-angle">Angle</option>
          <option value="measure-ellipse">Ellipse ROI</option>
          <option value="measure-rect">Rectangle ROI</option>
          <option value="measure-arrow">Arrow</option>
          <option value="measure-probe">Probe</option>
          <option value="measure-area">Freehand ROI</option>
        </select>
        {presets.length > 0 && (
          <select
            defaultValue=""
            onChange={(e) => {
              const idx = Number(e.target.value);
              if (Number.isNaN(idx) || !mprRef.current) return;
              const p = presets[idx];
              if (!p) return;
              mprRef.current.setWC(p.wc);
              mprRef.current.setWW(p.ww);
            }}
            style={{
              background: "#1a1f2b",
              color: "#e6ecf3",
              border: "1px solid #2a2f3b",
              borderRadius: 3,
              fontSize: "0.7rem",
              padding: "1px 4px",
            }}
            title="W/L preset"
          >
            <option value="">W/L preset…</option>
            {presets.map((p, i) => (
              <option key={p.label} value={i}>
                {p.label} ({p.wc}/{p.ww})
              </option>
            ))}
          </select>
        )}
        <button
          type="button"
          className="ghost"
          onClick={() => mprRef.current?.resetWL()}
          style={{
            background: "transparent",
            color: "#cbd5e1",
            border: "1px solid #2a2f3b",
            borderRadius: 3,
            padding: "1px 6px",
            cursor: "pointer",
            fontSize: "0.7rem",
          }}
          title="Reset W/L to volume range"
        >
          R
        </button>
        <button
          type="button"
          onClick={onRemove}
          title="Remove pane"
          style={{
            background: "transparent",
            color: "#fca5a5",
            border: "1px solid #2a2f3b",
            borderRadius: 3,
            padding: "1px 6px",
            cursor: "pointer",
            fontSize: "0.7rem",
          }}
        >
          ×
        </button>
      </div>
      <div style={{ position: "relative", overflow: "hidden" }}>
        {pane.err ? (
          <div
            style={{
              position: "absolute",
              inset: 0,
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              color: "#fca5a5",
              fontSize: "0.85rem",
              padding: "1rem",
              textAlign: "center",
            }}
          >
            {pane.err}
          </div>
        ) : !pane.volume ? (
          <div
            style={{
              position: "absolute",
              inset: 0,
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              color: "#888",
              fontSize: "0.85rem",
            }}
          >
            loading…
          </div>
        ) : (
          <CornerstoneMPRLayout
            ref={mprRef}
            volume={pane.volume}
            showAxial
            showSagittal
            showCoronal
            show3D={false}
            layout="2x2"
            seriesId={pane.seriesId}
            modality={pane.series?.modality ?? null}
            seriesDescription={pane.series?.series_description ?? undefined}
            activeTool={tool}
          />
        )}
      </div>
    </div>
  );
}
