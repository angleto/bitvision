"use client";

// Multiphase contrast-CT viewer: the phases of one study (non-contrast,
// arterial, portal-venous, delayed, ...) side by side, crosshair-synced by
// anatomical position. Intra-study phases almost always share one
// FrameOfReferenceUID, so sync is exact and needs no registration; a phase
// in a different frame gets a per-pane "Align" (rigid registration) like
// the followup viewer. Each pane auto-windows to the preset matching its
// phase. Reuses CornerstoneMPRLayout (one axial viewport per phase) and the
// shared ``useWorldSyncGrid`` hook.

import ContrastPhaseTab from "@/components/ContrastPhaseTab";
import type { MPRLayoutHandle } from "@/components/MPRLayoutTypes";
import ViewerIdentityBanner from "@/components/ViewerIdentityBanner";
import type { VolumeData } from "@/components/VolumeViewer";
import WashoutPanel from "@/components/WashoutPanel";
import PaneWLControl from "@/components/viewer/PaneWLControl";
import ViewerToolPalette from "@/components/viewer/ViewerToolPalette";
import { CONTRAST_TOOLS, type Tool } from "@/components/viewer/toolTypes";
import { useWorldSyncGrid } from "@/hooks/useWorldSyncGrid";
import { isMat4 } from "@/lib/affine";
import {
  ApiError,
  type Patient,
  type PhaseRoiInput,
  type PhaseRoiStats,
  type SeriesPhase,
  type StudyDetail,
  fetchVolume,
  patientsApi,
  registrationsApi,
  studiesApi,
} from "@/lib/api";
import { defaultPhasePanes, reviewableSeries } from "@/lib/contrastPhases";
import { dispatchViewportResetView, dispatchViewportZoom, useHotkeys } from "@/lib/hotkeys";
import {
  type ViewerPaneProbe,
  resetViewerProbe,
  updateViewerProbe,
  useViewerDebug,
} from "@/lib/viewerProbe";
import { presetForPhase } from "@/lib/windowing";
import { useTranslations } from "next-intl";
import dynamic from "next/dynamic";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { type CSSProperties, Suspense, useEffect, useMemo, useRef, useState } from "react";

const CornerstoneMPRLayout = dynamic(() => import("@/components/CornerstoneMPRLayout"), {
  ssr: false,
});

const MAX_PANES = 6;

// Inline reset so a <button> reads as an accent text link in the toolbar.
const LINK_BTN: CSSProperties = {
  background: "none",
  border: "none",
  padding: 0,
  font: "inherit",
  cursor: "pointer",
  color: "var(--bv-accent, #e96b1f)",
  textDecoration: "underline",
};

type AlignState = "same" | "needs" | "aligning" | "aligned" | "error";

// Minimal subset of a CornerstoneMPRLayout measurement we consume. A circle
// ROI carries its handle points in world (LPS) space. ``markerId`` is the
// Cornerstone annotation UID (``a.annotationUID``) — the key we delete by
// (``removeAnnotation``). ``value`` is the auto-computed measurement string.
interface DrawnMeasurement {
  markerId?: string;
  csToolName?: string;
  tool: string;
  value?: string;
  label?: string;
  worldPoints?: Array<[number, number, number]>;
  frameOfReferenceUID?: string;
}

interface PaneReady {
  handle: MPRLayoutHandle | null;
  scalars: Float32Array | null;
}

function PhasePane({
  phase,
  registerHandle,
  onCrosshair,
  activeTool,
  onMeasurements,
  onReady,
}: {
  phase: SeriesPhase;
  registerHandle: (h: MPRLayoutHandle | null) => void;
  onCrosshair: (pos: [number, number, number]) => void;
  activeTool: string;
  onMeasurements?: (m: DrawnMeasurement[]) => void;
  /** Report the pane's handle + voxel scalars once the volume is loaded, so
   *  the parent can drive per-pane W/L presets and keyboard navigation. */
  onReady?: (info: PaneReady) => void;
}) {
  const t = useTranslations("contrast");
  const [vol, setVol] = useState<VolumeData | null>(null);
  const [err, setErr] = useState<string | null>(null);
  // Bytes of the FULL-res volume streamed in behind the preview. A moving
  // number proves the HD load is progressing (frozen = stalled), so the pane
  // is never a mute "loading…" or a content-less spinner.
  const [fullProgress, setFullProgress] = useState<{ loaded: number } | null>(null);
  const localRef = useRef<MPRLayoutHandle | null>(null);
  const lastPhaseRef = useRef<string | null | undefined>(undefined);
  const sid = phase.series_id;

  useEffect(() => {
    let cancelled = false;
    setVol(null);
    setErr(null);
    setFullProgress(null);
    lastPhaseRef.current = undefined;
    void (async () => {
      // Progressive: paint the ~1/8 low-res preview first (≈2s) so the pane
      // isn't blank while the full-res streams in over the throttled egress
      // (≈17-26s/phase). Best-effort — any preview failure is silent and we
      // fall straight through to the authoritative full-res load below.
      try {
        const pv = await fetchVolume(sid, { preview: true });
        if (!cancelled) {
          setVol({
            dimensions: [pv.header.nx, pv.header.ny, pv.header.nz],
            spacing: pv.header.spacing,
            scalars: pv.scalars,
            range: pv.header.valueRange,
            origin: pv.header.origin,
            direction: pv.header.direction,
            frameOfReferenceUid: pv.header.frameOfReferenceUid,
            resolution: "preview",
          });
          updateViewerProbe({ notes: ["contrast preview rendered"] });
        }
      } catch {
        /* preview is optional — fall through to the full-res load */
      }
      try {
        const { header, scalars } = await fetchVolume(sid, {
          onProgress: (p) => {
            if (!cancelled) setFullProgress({ loaded: p.loaded });
          },
        });
        if (cancelled) return;
        setFullProgress(null);
        // Re-arm the phase-preset guard so it re-applies across the
        // preview→full swap: ``vol`` changes but ``acquisition_phase`` does
        // not, and the volumeId flip (:preview→full) re-fits/auto-windows —
        // the phase-specific preset must win on the full-res volume.
        lastPhaseRef.current = undefined;
        setVol({
          dimensions: [header.nx, header.ny, header.nz],
          spacing: header.spacing,
          scalars,
          range: header.valueRange,
          origin: header.origin,
          direction: header.direction,
          frameOfReferenceUid: header.frameOfReferenceUid,
          resolution: "full",
        });
      } catch (e) {
        if (!cancelled) setErr(e instanceof ApiError ? e.message : String(e));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [sid]);

  // Auto-window to the preset matching the phase. Re-applies only when the
  // phase label changes (not on every render) so a manual W/L drag is not
  // clobbered until the radiologist re-classifies the series.
  useEffect(() => {
    if (!vol) return;
    if (lastPhaseRef.current === phase.acquisition_phase) return;
    lastPhaseRef.current = phase.acquisition_phase;
    const preset = presetForPhase(phase.acquisition_phase, phase.body_part_examined);
    if (!preset) return;
    const id = requestAnimationFrame(() => {
      const h = localRef.current;
      if (h?.setWC && h?.setWW) {
        h.setWC(preset.wc);
        h.setWW(preset.ww);
      }
    });
    return () => cancelAnimationFrame(id);
  }, [vol, phase.acquisition_phase, phase.body_part_examined]);

  const setRef = (h: MPRLayoutHandle | null) => {
    localRef.current = h;
    registerHandle(h);
  };

  // Lift handle + scalars once the volume is loaded (ref is attached before
  // effects run, so localRef.current is valid here). onReady is a stable
  // per-pane closure; re-run only when the volume changes.
  // biome-ignore lint/correctness/useExhaustiveDependencies: onReady stable; key on vol intentionally
  useEffect(() => {
    if (vol) onReady?.({ handle: localRef.current, scalars: vol.scalars });
  }, [vol]);

  // Mark this pane "out of coverage": when a synced world point falls outside
  // this phase's z-extent the crosshair is clamped to the nearest valid slice
  // (no more black pane) — but that's NOT the anatomy at the synced point, so
  // signal it instead of showing the edge slice silently. The flag lives on the
  // imperative handle, so poll it.
  const [outOfCov, setOutOfCov] = useState(false);
  useEffect(() => {
    const id = setInterval(() => {
      const oc = localRef.current?.getProbeState?.()?.outOfCoverage ?? false;
      setOutOfCov((prev) => (prev === oc ? prev : oc));
    }, 400);
    return () => clearInterval(id);
  }, []);

  if (err) {
    return (
      <div className="error" style={{ padding: "1rem" }}>
        {t("loadError", { detail: err })}
      </div>
    );
  }
  if (!vol) {
    return <div style={{ color: "#6b7280", padding: "1rem" }}>{t("loading")}</div>;
  }
  return (
    <div style={{ position: "relative", width: "100%", height: "100%" }}>
      <CornerstoneMPRLayout
        ref={setRef}
        volume={vol}
        showAxial
        showSagittal={false}
        showCoronal={false}
        show3D={false}
        layout="1x1"
        seriesId={sid}
        modality={phase.modality ?? null}
        seriesDescription={phase.series_description ?? undefined}
        activeTool={activeTool}
        onCrosshairChange={onCrosshair}
        onMeasurementsChange={onMeasurements}
      />
      {vol.resolution === "preview" && fullProgress ? (
        // The pane already shows the low-res preview; this badge proves the
        // full-res is still streaming in AND advancing (moving MB = progress,
        // frozen = stalled) rather than a silent low-res image.
        <div
          aria-live="polite"
          style={{
            position: "absolute",
            bottom: 6,
            right: 6,
            zIndex: 5,
            background: "rgba(15,20,30,0.78)",
            color: "#cbd5e1",
            fontSize: "0.66rem",
            padding: "0.2rem 0.45rem",
            borderRadius: 5,
            border: "1px solid #2a2f3b",
            fontFamily: "ui-monospace, monospace",
            pointerEvents: "none",
          }}
        >
          Full-res · {(fullProgress.loaded / 1_048_576).toFixed(1)} MB
        </div>
      ) : null}
      {outOfCov ? (
        <div
          aria-live="polite"
          style={{
            position: "absolute",
            top: 6,
            left: 6,
            zIndex: 5,
            background: "rgba(120,72,16,0.9)",
            color: "#fde68a",
            fontSize: "0.66rem",
            fontWeight: 600,
            padding: "0.2rem 0.5rem",
            borderRadius: 5,
            border: "1px solid #d97706",
            pointerEvents: "none",
          }}
          title="Questa fase non copre la posizione sincronizzata; mostrata la slice più vicina."
        >
          ⚠ fuori copertura
        </div>
      ) : null}
    </div>
  );
}

// Manual series picker: the explicit override for "your classification is
// wrong — let me open the right series". Lists the study's reviewable axial
// phase volumes (scouts / reformats / dose reports hidden), with a "show all
// series" escape hatch for the rare case the radiologist wants one of those.
// The chosen set becomes the authoritative phase panes.
function PhaseSeriesPicker({
  series,
  hiddenCount,
  showAll,
  onToggleShowAll,
  initialSelected,
  onConfirm,
  onCancel,
}: {
  series: SeriesPhase[];
  hiddenCount: number;
  showAll: boolean;
  onToggleShowAll: (v: boolean) => void;
  initialSelected: string[];
  onConfirm: (ids: string[]) => void;
  onCancel?: () => void;
}) {
  const t = useTranslations("contrast");
  const [sel, setSel] = useState<Set<string>>(() => new Set(initialSelected));
  const toggle = (id: string) =>
    setSel((s) => {
      const n = new Set(s);
      if (n.has(id)) n.delete(id);
      else n.add(id);
      return n;
    });
  // Derive from the selection set itself, NOT the visible list: a series that
  // is selected but hidden (e.g. a non-reviewable one carried in from a URL
  // pick while "show all" is off) must survive a toggle and the confirm.
  const chosen = [...sel];

  return (
    <div
      style={{
        flex: "1 1 auto",
        overflowY: "auto",
        padding: "1rem 1.25rem",
        color: "#e6ecf3",
        minHeight: 0,
      }}
    >
      <h2 style={{ fontSize: "0.95rem", margin: "0 0 0.25rem" }}>{t("pickTitle")}</h2>
      <p className="meta" style={{ margin: "0 0 0.75rem", fontSize: "0.8rem" }}>
        {t("pickSubtitle")}
      </p>
      {series.length === 0 ? (
        <p className="meta">{t("noPhases")}</p>
      ) : (
        <ul style={{ listStyle: "none", padding: 0, margin: 0, display: "grid", gap: 4 }}>
          {series.map((p) => {
            const reformat = !showAll
              ? false
              : !(p.is_reviewable_phase ?? true) ||
                (!!p.series_plane && p.series_plane !== "axial");
            return (
              <li key={p.series_id}>
                <label
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: 8,
                    padding: "4px 8px",
                    background: "#11151c",
                    border: "1px solid #1a1f2b",
                    borderRadius: 6,
                    cursor: "pointer",
                    opacity: reformat ? 0.7 : 1,
                  }}
                >
                  <input
                    type="checkbox"
                    checked={sel.has(p.series_id)}
                    onChange={() => toggle(p.series_id)}
                  />
                  <span style={{ flex: 1, minWidth: 0 }}>
                    {p.acquisition_phase && (
                      <span style={{ color: "var(--bv-accent, #e96b1f)", marginRight: 6 }}>
                        {t(`phase.${p.acquisition_phase}`)}
                      </span>
                    )}
                    {p.series_description || `series ${p.series_number ?? "?"}`}
                    {p.series_plane && p.series_plane !== "axial" && (
                      <span className="meta" style={{ marginLeft: 6, fontSize: "0.72rem" }}>
                        · {t(`plane.${p.series_plane}`)}
                      </span>
                    )}
                  </span>
                  <span className="meta" style={{ fontSize: "0.72rem", whiteSpace: "nowrap" }}>
                    {p.instance_count ?? 0} img
                  </span>
                </label>
              </li>
            );
          })}
        </ul>
      )}
      {(hiddenCount > 0 || showAll) && (
        <label
          style={{
            display: "inline-flex",
            alignItems: "center",
            gap: 6,
            marginTop: 10,
            fontSize: "0.78rem",
          }}
        >
          <input
            type="checkbox"
            checked={showAll}
            onChange={(e) => onToggleShowAll(e.target.checked)}
          />
          {t("showAllSeries", { count: hiddenCount })}
        </label>
      )}
      <div style={{ display: "flex", gap: 8, marginTop: 12 }}>
        <button
          type="button"
          className="viewer-btn viewer-btn--active"
          disabled={chosen.length === 0}
          onClick={() => onConfirm(chosen)}
        >
          {t("pickOpen", { count: chosen.length })}
        </button>
        {onCancel && (
          <button type="button" className="ghost" onClick={onCancel}>
            {t("close")}
          </button>
        )}
      </div>
    </div>
  );
}

export default function ContrastViewerPage() {
  return (
    <Suspense
      fallback={
        <main style={{ padding: "2rem" }}>
          <p className="meta">Loading…</p>
        </main>
      }
    >
      <ContrastViewerInner />
    </Suspense>
  );
}

function ContrastViewerInner() {
  const t = useTranslations("contrast");
  const search = useSearchParams();
  const studyParam = search.get("study");
  const studyId = studyParam ?? search.getAll("s")[0] ?? null;
  // Explicit series selection passed in the URL (``?study=X&s=id&s=id``) —
  // e.g. from the study page's "Contrast phases" button after the user ticked
  // the right series. This is an authoritative manual pick: open exactly these
  // series as phases. Only honoured alongside ``study`` (otherwise a bare
  // ``s=`` is the legacy "study id" fallback above).
  const urlSeriesKey = studyParam ? search.getAll("s").join(",") : "";
  const urlSeries = useMemo(() => (urlSeriesKey ? urlSeriesKey.split(",") : []), [urlSeriesKey]);

  const grid = useWorldSyncGrid();
  const [phases, setPhases] = useState<SeriesPhase[]>([]);
  const [study, setStudy] = useState<StudyDetail | null>(null);
  const [patient, setPatient] = useState<Patient | null>(null);
  const [loadErr, setLoadErr] = useState<string | null>(null);
  const [detecting, setDetecting] = useState(false);
  const [busySeries, setBusySeries] = useState<string | null>(null);
  const [aligning, setAligning] = useState<Record<string, boolean>>({});
  const [alignError, setAlignError] = useState<Record<string, boolean>>({});
  const [alignErrMsg, setAlignErrMsg] = useState<Record<string, string | null>>({});
  const [alignStage, setAlignStage] = useState<Record<string, string | null>>({});
  const [activeReg, setActiveReg] = useState<Record<string, string | null>>({});
  const alignCancelRef = useRef<Record<string, boolean>>({});
  const [alignedSet, setAlignedSet] = useState<Record<string, boolean>>({});
  // In-session decision about which series to open as panes:
  //   null    = none yet -> follow the URL selection, else auto-classification;
  //   "auto"  = the user explicitly reset to auto -> ignore the URL selection;
  //   string[] = an explicit pick from the picker -> takes precedence.
  const [pick, setPick] = useState<string[] | "auto" | null>(null);
  const [showPicker, setShowPicker] = useState(false);
  // Picker escape hatch: reveal non-reviewable series (reformats, scouts, dose
  // reports) for the rare case the radiologist wants one of them.
  const [showAllSeries, setShowAllSeries] = useState(false);
  // ---- Diagnostic controls (toolbar / keyboard / per-pane W/L) ----
  // The measurement tool applied to every pane (radiologist picks from the
  // palette). "wl" = window/level drag, the sensible default for reading.
  const [activeTool, setActiveTool] = useState<Tool>("wl");
  // Which pane the keyboard drives (arrows scroll it; sync propagates to the
  // rest). Click a pane to make it active.
  const [activePane, setActivePane] = useState(0);
  // Apply W/L presets/Auto to every phase at once. ON by default: a radiologist
  // compares phases at the SAME window, so picking a preset should change all
  // panes; turn it off to tune one phase independently.
  const [linkWL, setLinkWL] = useState(true);
  // Single-pane reading mode (one phase full-size + a phase-flip strip) vs the
  // synced side-by-side grid (default).
  const [layoutMode, setLayoutMode] = useState<"grid" | "single">("grid");
  const [showMarkers, setShowMarkers] = useState(false);
  // Per-pane handle + scalars, lifted from each PhasePane once its volume
  // loads. ``paneHandlesRef`` is the click-time source of truth for keyboard /
  // delete; ``paneInfo`` mirrors it in state so PaneWLControl re-renders when a
  // pane becomes ready.
  const paneHandlesRef = useRef<(MPRLayoutHandle | null)[]>([]);
  const [paneInfo, setPaneInfo] = useState<Record<number, PaneReady>>({});
  // ROIs drawn on each pane (from the measurement stream) — the source for the
  // deletable-ROI rail.
  const [paneMeasurements, setPaneMeasurements] = useState<Record<number, DrawnMeasurement[]>>({});
  // Wash-out measurement state.
  const [measureMode, setMeasureMode] = useState(false);
  const [washout, setWashout] = useState<PhaseRoiStats | null>(null);
  const [washoutBusy, setWashoutBusy] = useState(false);
  const [washoutErr, setWashoutErr] = useState<string | null>(null);
  const [washoutSaved, setWashoutSaved] = useState(false);
  const lastRoiRef = useRef<string | null>(null);
  const savedRoiRef = useRef<{
    center_lps: [number, number, number];
    radius_mm: number;
    frame_of_reference_uid: string | null;
  } | null>(null);
  // Region scoping the wash-out interpretation (adrenal | liver | other). For
  // the liver workflow a second "parenchyma" reference ROI is sampled so the
  // panel reports the lesion-vs-parenchyma relative wash-out (LI-RADS) instead
  // of the adrenal APW/RPW indices.
  // Liver is the primary use of the multiphase viewer, so default to the guided
  // lesion + parenchyma workflow (the operator can switch to adrenal/other).
  const [region, setRegion] = useState<"adrenal" | "liver" | "other">("liver");
  const [roiTarget, setRoiTarget] = useState<"lesion" | "parenchyma">("lesion");
  const [hasLesion, setHasLesion] = useState(false);
  const [hasParenchyma, setHasParenchyma] = useState(false);
  type RoiCapture = {
    center_lps: [number, number, number];
    radius_mm: number;
    forUid: string | null;
    key: string;
    // The Cornerstone annotation UID + the pane it was drawn on, so a single ROI
    // can be deleted individually (removeAnnotation) instead of "clear all".
    markerId?: string;
    paneIndex?: number;
  };
  const lesionRoiRef = useRef<RoiCapture | null>(null);
  const parenchymaRoiRef = useRef<RoiCapture | null>(null);

  useEffect(() => {
    if (!studyId) return;
    let cancelled = false;
    void (async () => {
      try {
        const detail = await studiesApi.detail(studyId);
        if (cancelled) return;
        setStudy(detail);
        if (detail.patient_id) {
          patientsApi
            .detail(detail.patient_id)
            .then((p) => {
              if (!cancelled) setPatient(p);
            })
            .catch(() => {});
        }
        let manifest = await studiesApi.phases(studyId);
        // Best-effort auto-detect when nothing is classified yet (idempotent;
        // a non-owner gets 403 and we just show the unclassified manifest).
        const ct = manifest.phases.filter((p) => (p.modality || "").toUpperCase() === "CT");
        if (ct.length > 0 && manifest.phases.every((p) => !p.acquisition_phase)) {
          try {
            manifest = await studiesApi.detectPhases(studyId);
          } catch {
            /* not owner / detect unavailable — manual button stays */
          }
        }
        if (!cancelled) setPhases(manifest.phases);
      } catch (e) {
        if (!cancelled) setLoadErr(e instanceof ApiError ? e.message : String(e));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [studyId]);

  // Default panes when there is no manual pick: one reviewable axial series
  // per classified phase, in clinical order. Scouts / recon-kernel duplicates
  // / MPR reformats / dose reports are excluded even if a stale label sits on
  // them, so two acquired phases never open as six panes.
  const autoPanes = useMemo(() => defaultPhasePanes(phases), [phases]);

  // The reviewable axial phase volumes the picker offers first (clutter
  // hidden); the "show all" toggle reveals the rest.
  const reviewable = useMemo(() => reviewableSeries(phases), [phases]);
  const allCt = useMemo(
    () => phases.filter((p) => (p.modality || "").toUpperCase() === "CT"),
    [phases],
  );
  const pickerSeries = showAllSeries ? allCt : reviewable.length > 0 ? reviewable : allCt;
  const hiddenCount = Math.max(0, allCt.length - reviewable.length);

  // The active selection: an in-session picker choice wins, else the URL
  // selection, else auto-classification. ``null`` => auto.
  const selection = pick === "auto" ? null : (pick ?? (urlSeries.length > 0 ? urlSeries : null));
  const selectionActive = selection != null;

  // Panes = the active selection mapped to series (preserving the chosen
  // order), else the auto phases. Never every CT.
  const panes = useMemo(() => {
    const base =
      selection != null
        ? selection
            .map((id) => phases.find((p) => p.series_id === id))
            .filter((p): p is SeriesPhase => p != null)
        : autoPanes;
    return base.slice(0, MAX_PANES);
  }, [selection, autoPanes, phases]);
  const truncated = (selection?.length ?? autoPanes.length) > MAX_PANES;
  const referenceFoR = panes[0]?.frame_of_reference_uid ?? null;

  // Apply a manual pick: drive the panes now AND reflect it in the URL so a
  // reload / bookmark / shared link keeps exactly these phases.
  function applySelection(ids: string[]) {
    setPick(ids);
    setShowPicker(false);
    if (typeof window !== "undefined") {
      const sp = new URLSearchParams();
      if (studyId) sp.set("study", studyId);
      for (const id of ids) sp.append("s", id);
      window.history.replaceState(null, "", `${window.location.pathname}?${sp.toString()}`);
    }
  }

  // Close a phase pane the radiologist doesn't need (e.g. keep 2 of 4 loaded
  // phases). Re-pins the remaining panes as the manual selection (+ URL) so the
  // choice survives a reload. Never closes the last pane.
  function closePane(seriesId: string) {
    const remaining = panes.map((p) => p.series_id).filter((id) => id !== seriesId);
    if (remaining.length === 0) return;
    applySelection(remaining);
  }

  // Keep the active (keyboard-driven) pane in range as the pane set changes.
  useEffect(() => {
    if (activePane >= panes.length && panes.length > 0) setActivePane(0);
  }, [activePane, panes.length]);

  // ---- keyboard navigation, radiological read ----
  // Arrow keys scroll the ACTIVE pane; because setCrosshair fires the layout's
  // onCrosshairChange, the world-sync grid then drives every other phase to the
  // same anatomical level (linked scroll). W/L keys act on the active pane, or
  // on all same-frame panes when "Link W/L" is on. Zoom/reset broadcast to
  // every viewport (linked by construction).
  const applyWL = (wc: number, ww: number) => {
    const targets = linkWL ? paneHandlesRef.current : [paneHandlesRef.current[activePane]];
    for (const h of targets) {
      h?.setWC(wc);
      h?.setWW(Math.max(1, ww));
    }
  };
  // Per-pane W/L action that reads the live handle(s) AT CLICK TIME (never a
  // stale lifted prop) and honours Link W/L: act on pane ``i`` alone, or on
  // every pane when linked.
  const forPaneWL = (i: number, fn: (h: MPRLayoutHandle) => void) => {
    const targets = linkWL ? paneHandlesRef.current : [paneHandlesRef.current[i]];
    for (const h of targets) if (h) fn(h);
  };
  const applyWLForPane = (i: number, wc: number, ww: number) =>
    forPaneWL(i, (h) => {
      h.setWC(wc);
      h.setWW(Math.max(1, ww));
    });
  const stepSlice = (delta: number) => {
    const h = paneHandlesRef.current[activePane];
    if (!h) return;
    const cur = Math.round(h.crosshair[2]);
    const maxZ = (h.dims?.[2] ?? 1) - 1;
    const z = Math.max(0, Math.min(maxZ, cur + delta));
    if (z !== cur) h.setCrosshair([h.crosshair[0], h.crosshair[1], z]);
  };
  const activeHandle = () => paneHandlesRef.current[activePane];
  useHotkeys([
    { key: "ArrowUp", handler: () => stepSlice(-1) },
    { key: "ArrowDown", handler: () => stepSlice(1) },
    { key: "PageUp", handler: () => stepSlice(-10) },
    { key: "PageDown", handler: () => stepSlice(10) },
    {
      key: "ArrowLeft",
      handler: () => {
        const h = activeHandle();
        if (h) applyWL(h.wc, h.ww - 25);
      },
    },
    {
      key: "ArrowRight",
      handler: () => {
        const h = activeHandle();
        if (h) applyWL(h.wc, h.ww + 25);
      },
    },
    {
      key: "[",
      handler: () => {
        const h = activeHandle();
        if (h) applyWL(h.wc - 25, h.ww);
      },
    },
    {
      key: "]",
      handler: () => {
        const h = activeHandle();
        if (h) applyWL(h.wc + 25, h.ww);
      },
    },
    {
      key: "i",
      handler: () => {
        const h = activeHandle();
        h?.setInvert(!h.invert);
      },
    },
    { key: "+", handler: () => dispatchViewportZoom(1.1) },
    { key: "=", handler: () => dispatchViewportZoom(1.1) },
    { key: "-", handler: () => dispatchViewportZoom(0.9) },
    { key: "0", handler: () => dispatchViewportResetView() },
    { key: "f", handler: () => activeHandle()?.flipHAll() },
    { key: "f", shift: true, handler: () => activeHandle()?.flipVAll() },
    // Esc interrupts an in-progress ROI draw on every pane (the operator can
    // always abort a measurement and start over / switch tool).
    { key: "Escape", handler: () => cancelActiveDraws() },
  ]);

  // Cancel any half-drawn ROI across all panes.
  function cancelActiveDraws() {
    for (const h of paneHandlesRef.current) h?.cancelDraw?.();
  }

  // No panes to show (nothing auto-classified, or a URL/stale selection that
  // matched no current series) -> open the picker so the user chooses rather
  // than facing an empty viewer.
  useEffect(() => {
    if (phases.length > 0 && panes.length === 0) {
      setShowPicker(true);
    }
  }, [phases.length, panes.length]);

  // If the open picker carries a selection that includes a non-reviewable
  // series (a deliberate URL/manual pick of a reformat etc.), reveal the full
  // list so the user actually sees what is selected.
  useEffect(() => {
    if (!showPicker || selection == null || reviewable.length === 0) return;
    const revIds = new Set(reviewable.map((p) => p.series_id));
    if (selection.some((id) => !revIds.has(id) && phases.some((p) => p.series_id === id))) {
      setShowAllSeries(true);
    }
  }, [showPicker, selection, reviewable, phases]);

  // Assign each pane's sync transform from its FrameOfReferenceUID: same FoR
  // as the reference pane => identity (null, syncable). Different FoR are
  // left unset in the hook (skipped in sync) until the user aligns them.
  // grid.setTransform is stable (useCallback); rerun only when the pane set
  // or reference frame changes.
  // biome-ignore lint/correctness/useExhaustiveDependencies: grid is stable; keying on panes/referenceFoR is intentional
  useEffect(() => {
    let allSameFoR = panes.length > 0;
    panes.forEach((p, i) => {
      const same = !!p.frame_of_reference_uid && p.frame_of_reference_uid === referenceFoR;
      if (i === 0 || same) grid.setTransform(i, null);
      if (i > 0 && !same) allSameFoR = false;
    });
    // When every phase shares the acquisition frame (the normal multiphase
    // contrast case) sync by SLICE INDEX, not world coordinates: a scroll to
    // slice k drives all panes to slice k (clamped). This matches aligning them
    // by hand and avoids the world round-trip that mis-mapped the synced panes.
    grid.setIndexSync(allSameFoR);
  }, [panes, referenceFoR]);

  function alignStateOf(p: SeriesPhase, i: number): AlignState {
    const same =
      i === 0 || (!!p.frame_of_reference_uid && p.frame_of_reference_uid === referenceFoR);
    if (same) return "same";
    if (aligning[p.series_id]) return "aligning";
    if (alignedSet[p.series_id]) return "aligned";
    if (alignError[p.series_id]) return "error";
    return "needs";
  }

  async function detect() {
    if (!studyId) return;
    setDetecting(true);
    try {
      const manifest = await studiesApi.detectPhases(studyId, false);
      setPhases(manifest.phases);
    } catch {
      /* surfaced via the badge; keep prior manifest */
    } finally {
      setDetecting(false);
    }
  }

  async function setPhase(seriesId: string, acquisitionPhase: string | null) {
    setBusySeries(seriesId);
    try {
      const updated = await studiesApi.setSeriesPhase(seriesId, acquisitionPhase);
      setPhases((prev) => prev.map((p) => (p.series_id === seriesId ? { ...p, ...updated } : p)));
    } catch {
      /* leave unchanged on failure */
    } finally {
      setBusySeries(null);
    }
  }

  async function align(paneIndex: number) {
    const pane = panes[paneIndex];
    const ref = panes[0];
    if (!pane || !ref) return;
    const sid = pane.series_id;
    setAlignError((s) => ({ ...s, [sid]: false }));
    setAlignErrMsg((s) => ({ ...s, [sid]: null }));
    setAlignStage((s) => ({ ...s, [sid]: "queued" }));
    setAligning((s) => ({ ...s, [sid]: true }));
    alignCancelRef.current[sid] = false;
    const fail = (msg?: string) => {
      setAlignError((s) => ({ ...s, [sid]: true }));
      setAlignErrMsg((s) => ({ ...s, [sid]: msg ?? t("alignFailed") }));
    };
    try {
      const reg = await registrationsApi.create({
        fixed_series_id: ref.series_id,
        moving_series_id: sid,
        kind: "rigid",
      });
      setActiveReg((s) => ({ ...s, [sid]: reg.id }));
      // Poll until terminal. No blind short cap: rigid registration legitimately
      // runs a few minutes under worker load. ~10 min backstop, live stage shown.
      for (let i = 0; i < 300; i++) {
        if (alignCancelRef.current[sid]) return;
        const cur = await registrationsApi.get(reg.id);
        setAlignStage((s) => ({
          ...s,
          [sid]: cur.status === "running" ? (cur.stage ?? "running") : cur.status,
        }));
        if (cur.status === "succeeded") {
          const m = cur.result_meta?.lps_matrix;
          if (isMat4(m)) {
            grid.setTransform(paneIndex, m);
            setAlignedSet((s) => ({ ...s, [sid]: true }));
          } else {
            fail(t("noMatrix"));
          }
          return;
        }
        if (cur.status === "failed") {
          fail(cur.error ?? undefined);
          return;
        }
        if (cur.status === "cancelled") return;
        await new Promise((r) => setTimeout(r, 2000));
      }
      fail(t("alignTimeout"));
    } catch (e) {
      fail(e instanceof ApiError ? e.message : undefined);
    } finally {
      setAligning((s) => ({ ...s, [sid]: false }));
      setAlignStage((s) => ({ ...s, [sid]: null }));
      setActiveReg((s) => ({ ...s, [sid]: null }));
    }
  }

  async function cancelAlign(seriesId: string) {
    alignCancelRef.current[seriesId] = true;
    const id = activeReg[seriesId];
    if (id) {
      try {
        await registrationsApi.cancel(id);
      } catch {
        /* best-effort */
      }
    }
    setAligning((s) => ({ ...s, [seriesId]: false }));
    setAlignStage((s) => ({ ...s, [seriesId]: null }));
    setActiveReg((s) => ({ ...s, [seriesId]: null }));
  }

  // The newest circle ROI in the stream that is NOT already captured (``exclude``
  // = markerIds of ROIs we already hold). handle[0] = centre, handle[1] = a
  // point on the perimeter, both in world (LPS); radius = their distance (mm).
  // Excluding known markerIds is what lets us delete one ROI without the OTHER,
  // still-on-screen circle getting re-captured as the freshly-drawn one.
  function roiFromMeasurements(ms: DrawnMeasurement[], exclude?: Set<string>): RoiCapture | null {
    const circle = [...ms]
      .reverse()
      .find(
        (m) =>
          (m.csToolName === "CircleROI" || m.tool === "sphere") &&
          (m.worldPoints?.length ?? 0) >= 2 &&
          !(m.markerId && exclude?.has(m.markerId)),
      );
    const pts = circle?.worldPoints;
    if (!pts || pts.length < 2) return null;
    const c = pts[0];
    const e = pts[1];
    const radius = Math.hypot(e[0] - c[0], e[1] - c[1], e[2] - c[2]);
    if (!(radius > 0)) return null;
    const forUid = circle?.frameOfReferenceUID ?? referenceFoR ?? null;
    const key = `${c[0].toFixed(3)},${c[1].toFixed(3)},${c[2].toFixed(3)}|${radius.toFixed(3)}`;
    return { center_lps: c, radius_mm: radius, forUid, key, markerId: circle?.markerId };
  }

  // POST the lesion ROI (and, for the liver workflow, the parenchyma ROI)
  // with the current region. Called on a new draw and whenever the region
  // changes; the backend gates the indices/flags by region.
  function runWashout() {
    const lesion = lesionRoiRef.current;
    if (!studyId || !lesion) return;
    const input: PhaseRoiInput = {
      kind: "sphere",
      center_lps: lesion.center_lps,
      radius_mm: lesion.radius_mm,
      frame_of_reference_uid: lesion.forUid,
      region,
    };
    const paren = parenchymaRoiRef.current;
    if (region === "liver" && paren) {
      input.parenchyma_center_lps = paren.center_lps;
      input.parenchyma_radius_mm = paren.radius_mm;
    }
    setWashoutBusy(true);
    setWashoutErr(null);
    setWashoutSaved(false);
    studiesApi
      .phaseRoiStats(studyId, input)
      .then((r) => setWashout(r))
      .catch((err) => setWashoutErr(err instanceof ApiError ? err.message : String(err)))
      .finally(() => setWashoutBusy(false));
  }

  // Per-voxel wash-out / subtraction heat map for the current lesion ROI; the
  // panel renders the returned PNG. Null when there is no lesion ROI yet.
  function fetchWashoutMap(metric: "washout" | "subtraction") {
    const lesion = lesionRoiRef.current;
    if (!studyId || !lesion) return Promise.resolve(null);
    return studiesApi.washoutMap(studyId, {
      center_lps: lesion.center_lps,
      radius_mm: lesion.radius_mm,
      metric,
    });
  }

  // Capture a circle ROI drawn on ANY pane (panes share the frame of reference,
  // so a circle on pane 2 is a valid world ROI just like one on pane 0). The
  // previous code only listened to pane 0, so drawing on another phase silently
  // did nothing — a big source of "I drew a ROI and nothing happened".
  function handleMeasurements(ms: DrawnMeasurement[], paneIndex: number) {
    if (!studyId) return;
    // Exclude the ROIs we already hold so a freshly drawn circle is the one we
    // pick up (and so deleting one ROI never re-captures the other).
    const held = new Set<string>();
    if (lesionRoiRef.current?.markerId) held.add(lesionRoiRef.current.markerId);
    if (parenchymaRoiRef.current?.markerId) held.add(parenchymaRoiRef.current.markerId);
    const roi = roiFromMeasurements(ms, held);
    if (!roi) return;
    roi.paneIndex = paneIndex;
    // In the liver workflow the next circle fills the active target (lesion ->
    // then parenchyma). Dedup per target so the repeating measurement stream
    // does not re-post the same draw.
    const target = region === "liver" ? roiTarget : "lesion";
    const dedupKey = `${target}|${roi.key}`;
    if (lastRoiRef.current === dedupKey) return;
    lastRoiRef.current = dedupKey;
    if (target === "parenchyma") {
      parenchymaRoiRef.current = roi;
      setHasParenchyma(true);
    } else {
      lesionRoiRef.current = roi;
      savedRoiRef.current = {
        center_lps: roi.center_lps,
        radius_mm: roi.radius_mm,
        frame_of_reference_uid: roi.forUid,
      };
      setHasLesion(true);
      // Guided auto-advance: after the lesion, the liver workflow needs the
      // reference-parenchyma ROI next, so flip the active target automatically
      // (the operator no longer has to find a toggle).
      if (region === "liver" && !parenchymaRoiRef.current) setRoiTarget("parenchyma");
    }
    runWashout();
  }

  // Delete one ROI (lesion or parenchyma) individually: remove its Cornerstone
  // annotation from the pane it was drawn on and clear the captured ref + state.
  function deleteRoi(which: "lesion" | "parenchyma") {
    const ref = which === "lesion" ? lesionRoiRef : parenchymaRoiRef;
    const roi = ref.current;
    if (roi?.markerId != null && roi.paneIndex != null) {
      paneHandlesRef.current[roi.paneIndex]?.removeAnnotation(roi.markerId);
    }
    ref.current = null;
    lastRoiRef.current = null;
    if (which === "lesion") {
      setHasLesion(false);
      savedRoiRef.current = null;
      setWashout(null);
      setRoiTarget("lesion");
    } else {
      setHasParenchyma(false);
      setRoiTarget("parenchyma");
    }
    if (which === "parenchyma" && lesionRoiRef.current) runWashout();
  }

  // Clear every ROI + result and restart the guided flow.
  function resetRois() {
    for (const h of paneHandlesRef.current) h?.clearAnnotations();
    lesionRoiRef.current = null;
    parenchymaRoiRef.current = null;
    savedRoiRef.current = null;
    lastRoiRef.current = null;
    setHasLesion(false);
    setHasParenchyma(false);
    setWashout(null);
    setRoiTarget("lesion");
  }

  // Re-run the wash-out when the region changes (the backend gates the
  // indices/flags by region) so the displayed result refreshes in place.
  // biome-ignore lint/correctness/useExhaustiveDependencies: re-run only on region change; runWashout reads current refs.
  useEffect(() => {
    if (lesionRoiRef.current) runWashout();
  }, [region]);

  async function saveWashout() {
    const roi = savedRoiRef.current;
    if (!studyId || !washout || !roi) return;
    try {
      await studiesApi.createPhaseEnhancementSet(studyId, {
        roi_kind: "sphere",
        roi: {
          center_lps: roi.center_lps,
          radius_mm: roi.radius_mm,
          frame_of_reference_uid: roi.frame_of_reference_uid,
        },
        samples: washout.samples.map((s) => ({
          series_id: s.series_id,
          acquisition_phase: s.acquisition_phase,
          hu_mean: s.hu_mean,
          hu_std: s.hu_std,
          voxel_count: s.voxel_count,
        })),
      });
      setWashoutSaved(true);
    } catch (e) {
      setWashoutErr(e instanceof ApiError ? e.message : String(e));
    }
  }

  // Viewer instrumentation: dormant unless an admin enabled the
  // ``viewer.debug.instrumentation`` flag (see /admin/settings). Strictly
  // additive — every probe call is a no-op when the flag is off. Polled at
  // 1 Hz because each pane's W/L lives on its imperative handle (mutable),
  // not React state.
  const viewerDebug = useViewerDebug();
  useEffect(() => {
    if (!viewerDebug) return;
    resetViewerProbe("contrast");
    const push = () => {
      const paneProbe: Record<string, ViewerPaneProbe> = {};
      panes.forEach((phase, i) => {
        const h = paneHandlesRef.current[i];
        const voi =
          h && h.wc != null && h.ww != null
            ? { lower: h.wc - h.ww / 2, upper: h.wc + h.ww / 2 }
            : null;
        // Live per-pane crosshair/coverage/canvas for the radiological sync
        // test: all synced panes should report the SAME crosshairLps, none
        // outOfCoverage on the liver overlap, none with a 0×0 canvas.
        const ps = h?.getProbeState?.();
        const key = phase.acquisition_phase ?? phase.series_id;
        paneProbe[key] = {
          visible: layoutMode === "single" ? i === activePane : true,
          voi,
          invert: h?.invert ?? undefined,
          crosshairLps: ps?.crosshairLps ?? null,
          crosshairIjk: ps?.crosshairIjk ?? null,
          sliceIndex: ps?.sliceIndex ?? null,
          canvas: ps?.canvas ?? null,
          outOfCoverage: ps?.outOfCoverage ?? null,
          cameraFocalLps: ps?.cameraFocalLps ?? null,
          volOrigin: ps?.volOrigin ?? null,
          volSpacing: ps?.volSpacing ?? null,
          volDims: ps?.volDims ?? null,
          volumeIdUsed: ps?.volumeIdUsed ?? null,
        };
      });
      const w = washout;
      updateViewerProbe({
        surface: "contrast",
        identity: {
          studyId,
          patientId: patient?.id ?? undefined,
          modality: panes[0]?.modality ?? undefined,
        },
        panes: paneProbe,
        activeTool,
        measurementCount: Object.values(paneMeasurements).reduce(
          (n, ms) => n + (ms?.length ?? 0),
          0,
        ),
        error: loadErr,
        washoutResult: w
          ? {
              region: w.washout.region,
              apw: w.washout.apw,
              rpw: w.washout.rpw,
              curve: w.washout.curve,
              parenchymaCurve: w.washout.parenchyma_curve,
              relativeCurve: w.washout.relative_curve,
              samples: w.samples,
              skipped: w.skipped,
            }
          : null,
      });
    };
    push();
    const timer = setInterval(push, 1000);
    return () => clearInterval(timer);
  }, [
    viewerDebug,
    studyId,
    panes,
    patient,
    activeTool,
    measureMode,
    layoutMode,
    activePane,
    paneMeasurements,
    loadErr,
    washout,
  ]);

  // Test-only driver hooks for the autonomous radiological E2E (gated by the
  // debug flag). Lets Playwright drive all panes to a world point and run a
  // wash-out with a known ROI without faking Cornerstone mouse drags — so the
  // gate actually exercises the radiological workflow, not just rendering.
  useEffect(() => {
    if (!viewerDebug) return;
    const w = window as unknown as { __viewerTest?: Record<string, unknown> };
    w.__viewerTest = {
      // Push a world LPS point to every pane; returns key -> inCoverage.
      setCrosshairWorldAll: (lps: [number, number, number]) => {
        const out: Record<string, boolean> = {};
        panes.forEach((phase, i) => {
          const h = paneHandlesRef.current[i];
          const key = phase.acquisition_phase ?? phase.series_id;
          out[key] = h?.setCrosshairWorld?.(lps) ?? false;
        });
        return out;
      },
      // Compute wash-out for a known lesion (+ optional parenchyma) ROI.
      runWashout: async (args: {
        lesionCenterLps: [number, number, number];
        lesionRadiusMm: number;
        parenchymaCenterLps?: [number, number, number];
        parenchymaRadiusMm?: number;
        region?: "adrenal" | "liver" | "other";
      }) => {
        if (!studyId) return null;
        const reg = args.region ?? region;
        const input: PhaseRoiInput = {
          kind: "sphere",
          center_lps: args.lesionCenterLps,
          radius_mm: args.lesionRadiusMm,
          frame_of_reference_uid: referenceFoR ?? null,
          region: reg,
        };
        if (reg === "liver" && args.parenchymaCenterLps && args.parenchymaRadiusMm) {
          input.parenchyma_center_lps = args.parenchymaCenterLps;
          input.parenchyma_radius_mm = args.parenchymaRadiusMm;
        }
        const r = await studiesApi.phaseRoiStats(studyId, input);
        setWashout(r);
        return r;
      },
    };
    return () => {
      w.__viewerTest = undefined;
    };
  }, [viewerDebug, panes, studyId, referenceFoR, region]);

  if (!studyId) {
    return (
      <main style={{ padding: "2rem" }}>
        <Link href="/studies">← studies</Link>
        <h1>{t("title")}</h1>
        <p className="meta">{t("pickHint")}</p>
      </main>
    );
  }

  const cols = panes.length <= 1 ? 1 : panes.length <= 4 ? 2 : 3;

  return (
    <div
      className="viewer-chrome"
      style={{
        height: "calc(100vh - 56px)",
        background: "#000",
        display: "flex",
        flexDirection: "column",
      }}
    >
      <ViewerIdentityBanner patient={patient} study={study} />
      <div
        style={{
          flex: "0 0 auto",
          display: "flex",
          alignItems: "center",
          gap: "0.75rem",
          padding: "0.4rem 0.75rem",
          // Viewer chrome is always-dark; pin the toolbar dark (not the
          // theme-aware --bv-card-bg, which is white in light mode and made the
          // light-on-dark ghost buttons invisible on a white bar).
          background: "#11151c",
          color: "#e6ecf3",
          borderBottom: "1px solid #1a1f2b",
          flexWrap: "wrap",
        }}
      >
        <Link
          href={`/studies/${studyId}`}
          className="ghost"
          style={{ fontSize: "0.82rem", textDecoration: "none", whiteSpace: "nowrap" }}
          title={t("backToStudy")}
        >
          ← {t("backToStudy")}
        </Link>
        <strong style={{ fontSize: "0.9rem" }}>{t("title")}</strong>
        <label
          style={{ display: "inline-flex", alignItems: "center", gap: 4, fontSize: "0.82rem" }}
        >
          <input
            type="checkbox"
            checked={grid.sync}
            onChange={(e) => grid.setSync(e.target.checked)}
          />
          {t("sync")}
        </label>
        <button type="button" className="ghost" disabled={detecting} onClick={detect}>
          {detecting ? t("detecting") : t("detect")}
        </button>
        <button
          type="button"
          data-testid="washout-run"
          className={measureMode ? "viewer-btn viewer-btn--active" : "ghost"}
          aria-pressed={measureMode}
          onClick={() => {
            if (measureMode) {
              // Leaving measure mode: abort any half-drawn ROI, clear the
              // transient result, and return to the window/level tool.
              cancelActiveDraws();
              setWashout(null);
              lastRoiRef.current = null;
              setActiveTool("wl");
            } else {
              // Entering: arm the circle ROI as the default, but the palette
              // stays usable so the operator can switch to pan/zoom and back.
              setActiveTool("measure-sphere");
            }
            setMeasureMode((v) => !v);
          }}
          title={t("washoutHint")}
        >
          {t("measureWashout")}
        </button>
        {/* Full measurement/tool palette (window-level, pan, HU/enhancement
            ROIs, distance, angle, probe) applied to every phase pane. */}
        <ViewerToolPalette
          activeTool={activeTool}
          onChange={(tool) => setActiveTool(tool ?? "wl")}
          tools={CONTRAST_TOOLS}
          compact
        />
        <button
          type="button"
          className="viewer-btn"
          style={{ fontSize: "0.7rem", color: "#f66" }}
          title={t("clearRoisHint")}
          onClick={() => {
            for (const h of paneHandlesRef.current) h?.clearAnnotations();
          }}
        >
          {t("clearRois")}
        </button>
        <label
          style={{ display: "inline-flex", alignItems: "center", gap: 4, fontSize: "0.82rem" }}
          title={t("linkWlHint")}
        >
          <input type="checkbox" checked={linkWL} onChange={(e) => setLinkWL(e.target.checked)} />
          {t("linkWl")}
        </label>
        <button
          type="button"
          className={layoutMode === "single" ? "viewer-btn viewer-btn--active" : "ghost"}
          aria-pressed={layoutMode === "single"}
          onClick={() => setLayoutMode((m) => (m === "grid" ? "single" : "grid"))}
          title={t("layoutHint")}
        >
          {layoutMode === "grid" ? t("layoutSingle") : t("layoutGrid")}
        </button>
        <button
          type="button"
          className={showMarkers ? "viewer-btn viewer-btn--active" : "ghost"}
          aria-pressed={showMarkers}
          onClick={() => setShowMarkers((v) => !v)}
        >
          {t("markers")}
        </button>
        <button
          type="button"
          className="viewer-btn viewer-btn--active"
          aria-pressed={showPicker}
          onClick={() => setShowPicker((v) => !v)}
          title={t("pickSubtitle")}
        >
          {t("chooseSeries")}
        </button>
        {/* Provenance of the open panes + the one-click correction the user
            explicitly asked for: "if your classification is wrong let me pick
            the right series". */}
        {!showPicker && panes.length > 0 && (
          <span className="meta" style={{ fontSize: "0.76rem" }}>
            {selectionActive ? (
              <>
                {t("manualSelection", { count: panes.length })}{" "}
                <button
                  type="button"
                  onClick={() => {
                    setPick("auto");
                    if (typeof window !== "undefined" && studyId) {
                      window.history.replaceState(
                        null,
                        "",
                        `${window.location.pathname}?study=${studyId}`,
                      );
                    }
                  }}
                  style={LINK_BTN}
                >
                  {t("resetToAuto")}
                </button>
              </>
            ) : (
              <>
                {t("autoClassified", { count: panes.length })}{" "}
                <button type="button" onClick={() => setShowPicker(true)} style={LINK_BTN}>
                  {t("wrongPickSeries")}
                </button>
              </>
            )}
          </span>
        )}
        {loadErr && <span style={{ color: "var(--bv-danger, #f87171)" }}>{loadErr}</span>}
        {truncated && (
          <span className="meta" style={{ fontSize: "0.76rem" }}>
            {t("showingFirst", { count: MAX_PANES })}
          </span>
        )}
      </div>

      <div style={{ flex: "1 1 auto", display: "flex", minHeight: 0, position: "relative" }}>
        {showPicker ? (
          <PhaseSeriesPicker
            series={pickerSeries}
            hiddenCount={hiddenCount}
            showAll={showAllSeries}
            onToggleShowAll={setShowAllSeries}
            initialSelected={selection ?? autoPanes.map((p) => p.series_id)}
            onConfirm={applySelection}
            onCancel={panes.length > 0 ? () => setShowPicker(false) : undefined}
          />
        ) : panes.length === 0 ? (
          <div style={{ color: "#6b7280", padding: "1.5rem" }}>{t("noPhases")}</div>
        ) : (
          <div style={{ flex: "1 1 auto", display: "flex", flexDirection: "column", minHeight: 0 }}>
            {layoutMode === "single" && (
              <div
                style={{
                  display: "flex",
                  gap: 4,
                  padding: "0.25rem 0.5rem",
                  background: "#0b0e13",
                  borderBottom: "1px solid #1a1f2b",
                  flexWrap: "wrap",
                }}
              >
                {panes.map((p, i) => (
                  <button
                    key={p.series_id}
                    type="button"
                    className={i === activePane ? "viewer-btn viewer-btn--active" : "viewer-btn"}
                    style={{ fontSize: "0.72rem", padding: "1px 8px" }}
                    onClick={() => setActivePane(i)}
                  >
                    {p.acquisition_phase
                      ? t(`phase.${p.acquisition_phase}`)
                      : p.series_description || `#${p.series_number ?? "?"}`}
                  </button>
                ))}
              </div>
            )}
            <div
              style={{
                flex: "1 1 auto",
                display: "grid",
                gridTemplateColumns: `repeat(${layoutMode === "single" ? 1 : cols}, 1fr)`,
                gap: 2,
                minHeight: 0,
              }}
            >
              {panes.map((phase, i) => {
                const a = alignStateOf(phase, i);
                const hidden = layoutMode === "single" && i !== activePane;
                return (
                  <div
                    key={phase.series_id}
                    data-testid={`contrast-phase-${phase.acquisition_phase ?? phase.series_id}`}
                    onPointerDown={() => setActivePane(i)}
                    style={{
                      position: "relative",
                      background: "#000",
                      border: "1px solid #1a1f2b",
                      outline:
                        i === activePane && layoutMode === "grid"
                          ? "2px solid var(--bv-accent, #e96b1f)"
                          : undefined,
                      outlineOffset: -2,
                      display: hidden ? "none" : "grid",
                      gridTemplateRows: "auto 1fr",
                      overflow: "hidden",
                      minHeight: 0,
                    }}
                  >
                    <div
                      style={{
                        display: "flex",
                        alignItems: "center",
                        gap: 6,
                        padding: "0.25rem 0.5rem",
                        fontSize: "0.78rem",
                        color: "#cbd5e1",
                        background: "#0b0e13",
                        borderBottom: "1px solid #1a1f2b",
                        flexWrap: "wrap",
                      }}
                    >
                      <ContrastPhaseTab
                        phase={phase}
                        busy={busySeries === phase.series_id}
                        onSet={(p) => setPhase(phase.series_id, p)}
                      />
                      {a === "needs" && (
                        <button
                          type="button"
                          className="ghost"
                          title={t("needsAlign")}
                          onClick={() => align(i)}
                          style={{ fontSize: "0.72rem", padding: "1px 6px" }}
                        >
                          {t("align")}
                        </button>
                      )}
                      {a === "aligning" && (
                        <>
                          <span className="meta">
                            {alignStage[phase.series_id] &&
                            t.has(`alignStage.${alignStage[phase.series_id]}`)
                              ? t(`alignStage.${alignStage[phase.series_id]}`)
                              : t("aligning")}
                          </span>
                          <button
                            type="button"
                            className="ghost"
                            onClick={() => cancelAlign(phase.series_id)}
                            style={{ fontSize: "0.72rem", padding: "1px 6px" }}
                          >
                            {t("alignCancel")}
                          </button>
                        </>
                      )}
                      {a === "aligned" && (
                        <span style={{ color: "var(--bv-success, #047857)" }}>{t("aligned")}</span>
                      )}
                      {a === "error" && (
                        <span
                          style={{ color: "var(--bv-danger, #f87171)", fontSize: "0.72rem" }}
                          title={alignErrMsg[phase.series_id] ?? undefined}
                        >
                          {alignErrMsg[phase.series_id] ?? t("alignFailed")}
                        </span>
                      )}
                      {a === "error" && (
                        <button
                          type="button"
                          className="ghost"
                          onClick={() => align(i)}
                          style={{ fontSize: "0.72rem", padding: "1px 6px" }}
                        >
                          {t("alignRetry")}
                        </button>
                      )}
                      {phase.series_description && (
                        <span
                          style={{
                            color: "#64748b",
                            overflow: "hidden",
                            textOverflow: "ellipsis",
                            whiteSpace: "nowrap",
                            minWidth: 0,
                          }}
                          title={phase.series_description}
                        >
                          {phase.series_description}
                        </span>
                      )}
                      <span
                        style={{
                          marginLeft: "auto",
                          display: "inline-flex",
                          alignItems: "center",
                          gap: 4,
                        }}
                      >
                        <PaneWLControl
                          modality={phase.modality}
                          bodyPart={phase.body_part_examined}
                          scalars={paneInfo[i]?.scalars ?? null}
                          onApplyWL={(wc, ww) => applyWLForPane(i, wc, ww)}
                          onReset={() => forPaneWL(i, (h) => h.resetWL())}
                          onInvert={() => forPaneWL(i, (h) => h.setInvert(!h.invert))}
                        />
                        {panes.length > 1 && (
                          <button
                            type="button"
                            className="ghost"
                            data-testid={`contrast-close-${phase.series_id}`}
                            title={t("closePane")}
                            aria-label={t("closePane")}
                            onClick={() => closePane(phase.series_id)}
                            style={{ fontSize: "0.8rem", padding: "1px 6px", lineHeight: 1 }}
                          >
                            ✕
                          </button>
                        )}
                      </span>
                    </div>
                    <PhasePane
                      phase={phase}
                      registerHandle={(h) => {
                        grid.registerPane(i)(h);
                        paneHandlesRef.current[i] = h;
                      }}
                      onCrosshair={(pos) => grid.onCrosshairChange(i, pos)}
                      activeTool={activeTool}
                      onMeasurements={(ms) => {
                        setPaneMeasurements((prev) => ({ ...prev, [i]: ms }));
                        handleMeasurements(ms, i);
                      }}
                      onReady={(info) => {
                        paneHandlesRef.current[i] = info.handle;
                        setPaneInfo((prev) => ({ ...prev, [i]: info }));
                      }}
                    />
                  </div>
                );
              })}
            </div>
          </div>
        )}
        {/* The wash-out + ROI panels float OVER the grid (absolute), they do
            NOT take layout width: a side panel shrinking the flex row would
            resize the Cornerstone canvas and the re-fit makes the drawn ROI
            jump ("panel shrinks, ROI shifts"). The wash-out card is anchored
            bottom-CENTRE so it never breaks the left/right symmetry of the two
            phase panes. */}
        {(measureMode || washout) && (
          <div
            data-testid="washout-panel"
            style={{
              position: "absolute",
              bottom: 10,
              left: "50%",
              transform: "translateX(-50%)",
              zIndex: 7,
              maxHeight: "46%",
              display: "flex",
              borderRadius: 8,
              overflow: "hidden",
              boxShadow: "0 8px 28px rgba(0,0,0,0.6)",
            }}
          >
            <WashoutPanel
              result={washout}
              busy={washoutBusy}
              error={washoutErr}
              saved={washoutSaved}
              region={region}
              onRegionChange={setRegion}
              roiTarget={roiTarget}
              onRoiTargetChange={setRoiTarget}
              hasLesion={hasLesion}
              hasParenchyma={hasParenchyma}
              onRequestMap={fetchWashoutMap}
              onSave={saveWashout}
              onDeleteRoi={deleteRoi}
              onReset={resetRois}
              onCancelDraw={cancelActiveDraws}
              onClose={() => {
                cancelActiveDraws();
                setMeasureMode(false);
                setWashout(null);
                lastRoiRef.current = null;
                lesionRoiRef.current = null;
                parenchymaRoiRef.current = null;
                setHasLesion(false);
                setHasParenchyma(false);
                setRoiTarget("lesion");
              }}
            />
          </div>
        )}
        {showMarkers && (
          <div
            style={{
              position: "absolute",
              top: 0,
              bottom: 0,
              right: 0,
              zIndex: 6,
              width: 240,
              overflowY: "auto",
              padding: "0.5rem 0.75rem",
              // Always-dark (not theme-aware --bv-card-bg, white in light mode).
              background: "#11151c",
              borderLeft: "1px solid #1a1f2b",
              boxShadow: "-8px 0 24px rgba(0,0,0,0.5)",
              color: "#e6ecf3",
              minHeight: 0,
            }}
          >
            <h3 style={{ fontSize: "0.85rem", margin: "0 0 0.5rem" }}>{t("roisTitle")}</h3>
            {panes.every((_, i) => (paneMeasurements[i]?.length ?? 0) === 0) ? (
              <p className="meta" style={{ fontSize: "0.76rem" }}>
                {t("roisEmpty")}
              </p>
            ) : (
              panes.map((phase, i) => {
                const ms = paneMeasurements[i] ?? [];
                if (ms.length === 0) return null;
                return (
                  <div key={phase.series_id} style={{ marginBottom: "0.6rem" }}>
                    <div
                      style={{
                        fontSize: "0.74rem",
                        color: "var(--bv-accent, #e96b1f)",
                        marginBottom: 2,
                      }}
                    >
                      {phase.acquisition_phase
                        ? t(`phase.${phase.acquisition_phase}`)
                        : phase.series_description}
                    </div>
                    {ms.map((m, k) => (
                      <div
                        key={m.markerId ?? `${i}-${k}`}
                        style={{
                          display: "flex",
                          alignItems: "center",
                          gap: 6,
                          padding: "2px 4px",
                          fontSize: "0.74rem",
                          borderBottom: "1px solid #1a1f2b",
                        }}
                      >
                        <span
                          style={{
                            flex: 1,
                            minWidth: 0,
                            overflow: "hidden",
                            textOverflow: "ellipsis",
                            whiteSpace: "nowrap",
                          }}
                        >
                          {m.label || m.csToolName || m.tool}
                          {m.value && <span className="meta"> · {m.value}</span>}
                        </span>
                        <button
                          type="button"
                          className="ghost"
                          title={t("roiDelete")}
                          style={{ color: "#f66", padding: "0 6px" }}
                          disabled={!m.markerId}
                          onClick={() => {
                            if (m.markerId) paneHandlesRef.current[i]?.removeAnnotation(m.markerId);
                          }}
                        >
                          ×
                        </button>
                      </div>
                    ))}
                  </div>
                );
              })
            )}
          </div>
        )}
      </div>
    </div>
  );
}
