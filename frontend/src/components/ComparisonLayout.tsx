"use client";

// Side-by-side comparison of up to 4 series. Each pane loads its own volume
// and hosts an independent MPRViewport. Sync of slice scroll and W/L is
// opt-in; when enabled, changes on one pane are mirrored to the others
// (slice sync applies the delta and clamps per-pane).

import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError, fetchVolume } from "@/lib/api";
import MPRViewport from "./MPRViewport";
import type { VolumeData } from "./VolumeViewer";

type Axis = "axial" | "sagittal" | "coronal";

export interface ComparisonPane {
  seriesId: string;
  label: string;
}

interface Props {
  panes: ComparisonPane[];
  axis?: Axis;
  syncScroll: boolean;
  syncWL: boolean;
  onRemove?: (index: number) => void;
}

interface PaneState {
  volume: VolumeData | null;
  loading: boolean;
  err: string | null;
  crosshair: [number, number, number];
  wc: number;
  ww: number;
}

async function loadVolume(seriesId: string): Promise<VolumeData> {
  const { header, scalars } = await fetchVolume(seriesId);
  return {
    dimensions: [header.nx, header.ny, header.nz],
    spacing: header.spacing,
    scalars,
    range: header.valueRange,
  };
}

function axisSliceIndex(cross: [number, number, number], axis: Axis): number {
  if (axis === "axial") return cross[2];
  if (axis === "sagittal") return cross[0];
  return cross[1];
}

function axisMaxIndex(dims: [number, number, number], axis: Axis): number {
  if (axis === "axial") return dims[2] - 1;
  if (axis === "sagittal") return dims[0] - 1;
  return dims[1] - 1;
}

function withAxisSlice(
  cross: [number, number, number],
  axis: Axis,
  value: number,
): [number, number, number] {
  const out: [number, number, number] = [cross[0], cross[1], cross[2]];
  if (axis === "axial") out[2] = value;
  else if (axis === "sagittal") out[0] = value;
  else out[1] = value;
  return out;
}

function initialStateFor(volume: VolumeData): PaneState {
  return {
    volume,
    loading: false,
    err: null,
    crosshair: [
      Math.floor(volume.dimensions[0] / 2),
      Math.floor(volume.dimensions[1] / 2),
      Math.floor(volume.dimensions[2] / 2),
    ],
    wc: (volume.range[0] + volume.range[1]) / 2,
    ww: Math.max(1, volume.range[1] - volume.range[0]),
  };
}

export default function ComparisonLayout({
  panes,
  axis = "axial",
  syncScroll,
  syncWL,
  onRemove,
}: Props) {
  // Cache pane state by seriesId so add/remove doesn't trigger refetches for
  // panes already loaded. loadedRef tracks which ids have begun loading to
  // avoid the effect racing with itself across renders.
  const [cache, setCache] = useState<Record<string, PaneState>>({});
  const loadedRef = useRef<Set<string>>(new Set());

  useEffect(() => {
    let cancelled = false;
    const toLoad = panes.filter((p) => !loadedRef.current.has(p.seriesId));
    if (toLoad.length === 0) return;
    for (const p of toLoad) loadedRef.current.add(p.seriesId);
    setCache((prev) => {
      const next = { ...prev };
      for (const p of toLoad) {
        next[p.seriesId] = {
          volume: null,
          loading: true,
          err: null,
          crosshair: [0, 0, 0],
          wc: 0,
          ww: 1,
        };
      }
      return next;
    });
    for (const p of toLoad) {
      loadVolume(p.seriesId)
        .then((v) => {
          if (cancelled) return;
          setCache((prev) => ({ ...prev, [p.seriesId]: initialStateFor(v) }));
        })
        .catch((e) => {
          if (cancelled) return;
          // Allow retry on next mount by dropping from loadedRef.
          loadedRef.current.delete(p.seriesId);
          setCache((prev) => ({
            ...prev,
            [p.seriesId]: {
              ...(prev[p.seriesId] ?? { volume: null, crosshair: [0, 0, 0], wc: 0, ww: 1 }),
              loading: false,
              err: e instanceof ApiError ? e.message : "load failed",
            },
          }));
        });
    }
    return () => {
      cancelled = true;
    };
  }, [panes]);

  const handleCrosshair = useCallback(
    (seriesId: string, newPos: [number, number, number]) => {
      setCache((prev) => {
        const current = prev[seriesId];
        if (!current) return prev;
        const delta = axisSliceIndex(newPos, axis) - axisSliceIndex(current.crosshair, axis);
        const next: Record<string, PaneState> = {
          ...prev,
          [seriesId]: { ...current, crosshair: newPos },
        };
        if (syncScroll && delta !== 0) {
          for (const p of panes) {
            if (p.seriesId === seriesId) continue;
            const other = next[p.seriesId];
            if (!other?.volume) continue;
            const maxIdx = axisMaxIndex(other.volume.dimensions, axis);
            const target = Math.max(
              0,
              Math.min(maxIdx, axisSliceIndex(other.crosshair, axis) + delta),
            );
            next[p.seriesId] = {
              ...other,
              crosshair: withAxisSlice(other.crosshair, axis, target),
            };
          }
        }
        return next;
      });
    },
    [axis, syncScroll, panes],
  );

  const handleWindowChange = useCallback(
    (seriesId: string, wc: number, ww: number) => {
      setCache((prev) => {
        if (!prev[seriesId]) return prev;
        const next: Record<string, PaneState> = {
          ...prev,
          [seriesId]: { ...prev[seriesId], wc, ww },
        };
        if (syncWL) {
          for (const p of panes) {
            if (p.seriesId === seriesId) continue;
            if (!next[p.seriesId]) continue;
            next[p.seriesId] = { ...next[p.seriesId], wc, ww };
          }
        }
        return next;
      });
    },
    [syncWL, panes],
  );

  if (panes.length === 0) {
    return <div style={{ ...centered, color: "#666" }}>Add a series to start comparing.</div>;
  }

  const cols = panes.length <= 1 ? 1 : 2;
  const rows = panes.length <= 2 ? 1 : 2;

  return (
    <div
      style={{
        display: "grid",
        gridTemplateColumns: `repeat(${cols}, 1fr)`,
        gridTemplateRows: `repeat(${rows}, 1fr)`,
        width: "100%",
        height: "100%",
        gap: 1,
        background: "#1c2230",
      }}
    >
      {panes.map((pane, i) => {
        const st = cache[pane.seriesId];
        return (
          <div
            key={`${pane.seriesId}-${i}`}
            style={{ position: "relative", overflow: "hidden", background: "#000" }}
          >
            {!st || st.loading ? (
              <div style={centered}>loading {pane.label}…</div>
            ) : st.err ? (
              <div style={{ ...centered, color: "#f88" }}>error: {st.err}</div>
            ) : st.volume ? (
              <MPRViewport
                volume={st.volume}
                axis={axis}
                sliceIndex={axisSliceIndex(st.crosshair, axis)}
                crosshair={st.crosshair}
                onCrosshairChange={(p) => handleCrosshair(pane.seriesId, p)}
                windowCenter={st.wc}
                windowWidth={st.ww}
                onWindowChange={(wc, ww) => handleWindowChange(pane.seriesId, wc, ww)}
                seriesDescription={pane.label}
              />
            ) : null}
            {onRemove && (
              <button
                type="button"
                onClick={() => onRemove(i)}
                title="Remove pane"
                style={{
                  position: "absolute",
                  top: 4,
                  right: 4,
                  zIndex: 3,
                  background: "rgba(0,0,0,0.7)",
                  color: "#ccc",
                  border: "1px solid #444",
                  borderRadius: 4,
                  width: 22,
                  height: 22,
                  padding: 0,
                  cursor: "pointer",
                  fontSize: "0.8rem",
                }}
              >
                ×
              </button>
            )}
          </div>
        );
      })}
    </div>
  );
}

const centered: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  width: "100%",
  height: "100%",
  color: "#bbb",
  fontSize: "0.8rem",
};
