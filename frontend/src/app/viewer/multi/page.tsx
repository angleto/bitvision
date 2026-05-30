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
import ViewerIdentityBanner from "@/components/ViewerIdentityBanner";
import type { VolumeData } from "@/components/VolumeViewer";
import {
  ApiError,
  type Patient,
  type Series,
  type Study,
  fetchVolume,
  patientsApi,
  studiesApi,
} from "@/lib/api";
import { modalityDefaults } from "@/lib/windowing";

// Distinct, high-contrast accent per patient so a 2- or 4-pane grid makes
// "these two panes are the SAME patient / DIFFERENT patients" obvious at a
// glance — the colour backs up the textual correct-patient guard.
const PATIENT_ACCENTS = ["#38bdf8", "#f472b6", "#a3e635", "#fbbf24"];

const CornerstoneMPRLayout = dynamic(() => import("@/components/CornerstoneMPRLayout"), {
  ssr: false,
});

const MAX_PANES = 4;

interface PaneState {
  seriesId: string;
  series: Series | null;
  volume: VolumeData | null;
  study: Study | null;
  patient: Patient | null;
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
      study: null,
      patient: null,
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
            study: null,
            patient: null,
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
          // Volume + study/patient identity in parallel. Identity is
          // best-effort (the correct-patient guard degrades gracefully);
          // the volume fetch drives the pane's success/error.
          const [{ header, scalars }, study] = await Promise.all([
            fetchVolume(pane.seriesId),
            studiesApi.detail(series.study_id).catch(() => null),
          ]);
          let patient: Patient | null = null;
          if (study?.patient_id) {
            patient = await patientsApi.detail(study.patient_id).catch(() => null);
          }
          if (cancelled) return;
          setPanes((prev) =>
            prev.map((p) =>
              p.seriesId === pane.seriesId
                ? {
                    ...p,
                    series,
                    study,
                    patient,
                    volume: {
                      dimensions: [header.nx, header.ny, header.nz],
                      spacing: header.spacing,
                      scalars,
                      range: header.valueRange,
                      origin: header.origin,
                      direction: header.direction,
                      frameOfReferenceUid: header.frameOfReferenceUid,
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

  // Correct-patient guard: assign a stable accent per distinct patient and
  // flag when the grid mixes more than one. Comparing series from
  // different patients side-by-side is almost always a mistake; the rare
  // legitimate case (e.g. teaching) is still allowed, just loudly marked.
  const patientColor = new Map<string, string>();
  for (const p of panes) {
    const pid = p.patient?.id;
    if (pid && !patientColor.has(pid)) {
      patientColor.set(pid, PATIENT_ACCENTS[patientColor.size % PATIENT_ACCENTS.length]);
    }
  }
  const mixedPatients = patientColor.size > 1;

  return (
    <div
      style={{
        margin: 0,
        padding: 0,
        height: "calc(100vh - 56px)",
        background: "#000",
        display: "flex",
        flexDirection: "column",
      }}
    >
      {mixedPatients && (
        <div
          role="alert"
          style={{
            flex: "0 0 auto",
            background: "#7f1d1d",
            color: "#fee2e2",
            fontFamily: "ui-monospace, monospace",
            fontSize: "0.8rem",
            fontWeight: 700,
            letterSpacing: "0.04em",
            padding: "5px 12px",
            borderBottom: "2px solid #ef4444",
            textShadow: "0 1px 2px rgba(0,0,0,0.6)",
          }}
        >
          ⚠ DIFFERENT PATIENTS — this layout shows {patientColor.size} different patients
          side-by-side. Verify every measurement and finding against the correct study.
        </div>
      )}
      <div
        style={{
          flex: "1 1 auto",
          minHeight: 0,
          display: "grid",
          gridTemplateColumns: `repeat(${cols}, 1fr)`,
          gridTemplateRows: `repeat(${rows}, 1fr)`,
          gap: 4,
        }}
      >
        {panes.map((pane) => (
          <Pane
            key={pane.seriesId}
            pane={pane}
            accentColor={pane.patient?.id ? patientColor.get(pane.patient.id) : undefined}
            onRemove={() => removePane(pane.seriesId)}
          />
        ))}
      </div>
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

function Pane({
  pane,
  onRemove,
  accentColor,
}: {
  pane: PaneState;
  onRemove: () => void;
  accentColor?: string;
}) {
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
        // Accent the border with the per-patient colour so SAME vs
        // DIFFERENT patient is obvious across panes (correct-patient guard).
        border: accentColor ? `2px solid ${accentColor}` : "1px solid #1a1f2b",
        display: "grid",
        gridTemplateRows: "auto auto 1fr",
      }}
    >
      {(pane.patient || pane.study) && (
        <ViewerIdentityBanner
          patient={pane.patient}
          study={pane.study}
          accentColor={accentColor}
          compact
        />
      )}
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
