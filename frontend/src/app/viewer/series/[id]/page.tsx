"use client";

import { useTranslations } from "next-intl";
import dynamic from "next/dynamic";
import Link from "next/link";
import { useParams, useSearchParams } from "next/navigation";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import BrowserSupportGate from "@/components/BrowserSupportGate";
import FiducialPanel, { useFiducials } from "@/components/FiducialMarkers3D";
import FindingPanel from "@/components/FindingPanel";
import FusionControls from "@/components/FusionControls";
import HangingProtocolPicker from "@/components/HangingProtocolPicker";
import HotSpotsPanel from "@/components/HotSpotsPanel";
import HotkeyHelpOverlay, { type HotkeyHelpSection } from "@/components/HotkeyHelpOverlay";
import type { MPRLayoutHandle } from "@/components/MPRLayoutTypes";
import MarkerListPanel from "@/components/MarkerListPanel";
import type { MarkerOverlayItem } from "@/components/MarkerOverlay";
import { markerIsManagedByCornerstone, markerToOverlayItem } from "@/components/MarkerOverlay";
import PetInfoPanel from "@/components/PetInfoPanel";
import ReportComposer from "@/components/ReportComposer";
import SegmentationControls from "@/components/SegmentationControls";
import SegmentationImporter from "@/components/SegmentationImporter";
import Series2DViewer from "@/components/Series2DViewer";
import SidebarSection from "@/components/SidebarSection";
import SidebarSectionNav from "@/components/SidebarSectionNav";
import SimilarCasesPanelViewer from "@/components/SimilarCasesPanelViewer";
import TransferFunctionEditor from "@/components/TransferFunctionEditor";
import ViewerIdentityBanner from "@/components/ViewerIdentityBanner";
import type {
  BlendMode,
  ColorPreset,
  VolumeData,
  VolumeViewerHandle,
} from "@/components/VolumeViewer";
import ViewerToolPalette from "@/components/viewer/ViewerToolPalette";
import type { Tool } from "@/components/viewer/toolTypes";
import { type AnnotationCommand, createAnnotationHistory } from "@/lib/annotationHistory";
import {
  ApiError,
  type AppSetting,
  type DisplayMetadata,
  type Marker,
  type MarkerKind,
  type Patient,
  type Series,
  type Study,
  fetchVolume,
  findingsApi,
  getStoredToken,
  markersApi,
  parseFloatVector,
  patientsApi,
  request,
  segmentationsApi,
  settingsApi,
  studiesApi,
} from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import { clearCornerstoneAnnotations, purgeCornerstoneCache } from "@/lib/cornerstoneSetup";
import { type HangingProtocol, type LayoutId, pickDefaultProtocol } from "@/lib/hangingProtocols";
import {
  type HotkeyBinding,
  dispatchViewportResetView,
  dispatchViewportZoom,
  useHotkeys,
} from "@/lib/hotkeys";
import { extractAndDownloadStl } from "@/lib/isosurfaceSTL";
import { useFullscreen } from "@/lib/useFullscreen";
import { useIsMobile } from "@/lib/useIsMobile";
import { resetViewerProbe, updateViewerProbe, useViewerDebug } from "@/lib/viewerProbe";
import { type ViewportStateBlob, useViewportState } from "@/lib/viewportState";
import { computeAutoWL, modalityDefaults, suggestedFromDicom } from "@/lib/windowing";

import Vr3DColorEditor from "./Vr3DColorEditor";
import Vr3DCropBox from "./Vr3DCropBox";
import Vr3DRangeControl from "./Vr3DRangeControl";
import WLPresetBar from "./WLPresetBar";
import { WL_BTN_STYLE } from "./viewerStyles";

// Cornerstone3D-backed MPR layout — the only viewer path. Replaced
// the legacy hand-rolled MPRLayout once Cornerstone reached feature
// parity (crosshair, fusion, tools, MIP, fade markers, suv-aware
// labels, ROI stats, ...).
const CornerstoneMPRLayout = dynamic(() => import("@/components/CornerstoneMPRLayout"), {
  ssr: false,
  loading: () => <div style={{ padding: "2rem", color: "#bbb" }}>loading…</div>,
});

/** SUV variant the user can pick from the PET sidebar (Addendum C §5.3). */
type SuvVariant = "bw" | "lbm-janma" | "lbm-james" | "bsa-mosteller" | "bsa-dubois" | "raw";

/** Resolve the SUV scaling factor for the currently selected variant.
 *  ``null`` means "no SUV factor available, render raw pixel values"
 *  (the consumer falls back to displaying Bq/mL or counts). */
function pickActiveSuvFactor(meta: DisplayMetadata | null, variant: SuvVariant): number | null {
  if (!meta || !meta.is_pet) return null;
  switch (variant) {
    case "bw":
      return meta.suv_factor_bw;
    case "lbm-janma":
      return meta.suv_factor_lbm_janmahasatian;
    case "lbm-james":
      return meta.suv_factor_lbm_james;
    case "bsa-mosteller":
      return meta.suv_factor_bsa_mosteller;
    case "bsa-dubois":
      return meta.suv_factor_bsa_dubois;
    case "raw":
      return null;
  }
}

const PRESETS_CT: ColorPreset[] = [
  "ct-bone",
  "ct-soft-tissue",
  "ct-lung",
  "ct-angio",
  "ct-liver",
  "ct-liver-vessels",
  "ct-kidney",
  "ct-pancreas",
  "ct-spleen",
  "ct-cardiac",
];
const PRESETS_MR: ColorPreset[] = ["mri-t1", "mri-t2", "mri-edge"];
const PRESETS_PT: ColorPreset[] = ["pet-suv-hot", "pet-suv-rainbow"];
const PRESETS_GENERIC: ColorPreset[] = [
  "bone",
  "grayscale",
  "skin",
  "tissue",
  "muscle",
  "cool-warm",
  "hot",
  "ice",
  "spectral",
  "jet",
  "viridis",
  "magma",
];
const ALL_PRESETS: ColorPreset[] = [
  ...PRESETS_CT,
  ...PRESETS_MR,
  ...PRESETS_PT,
  ...PRESETS_GENERIC,
];

/** Modality-aware short list of presets. Audit 2026-05-13 showed the
 *  full 27-button grid was the single worst UX item in the right rail
 *  (CT users had to scan past MR / PET / artistic presets to find
 *  ct-bone). The filtered list keeps the modality block + the small
 *  ``Generic`` set; the ``Show all`` toggle in the UI restores the
 *  legacy full grid for power users. */
function presetsForModality(modality: string | null | undefined): ColorPreset[] {
  const m = (modality ?? "").toUpperCase().trim();
  if (m === "CT") return [...PRESETS_CT, ...PRESETS_GENERIC];
  if (m === "MR" || m === "MRI") return [...PRESETS_MR, ...PRESETS_GENERIC];
  if (m === "PT" || m === "PET" || m === "NM") return [...PRESETS_PT, ...PRESETS_GENERIC];
  return ALL_PRESETS;
}

// W/L presets are now sourced from ``lib/windowing.ts`` so the list
// auto-adapts to the series' modality + body-part.

// ---------------------------------------------------------------------------
// Helpers used by the Markers API sync (TODO 43)
// ---------------------------------------------------------------------------

function mapToolToMarkerKind(tool: string): MarkerKind {
  // The MPRViewport finishMeasurement uses short tool names ("dist",
  // "angle", "area", "ellipse", "freehand", "arrow", "text", "probe").
  // The marker kind enum uses dot-namespaced values.
  const map: Record<string, MarkerKind> = {
    dist: "measurement.distance",
    distance: "measurement.distance",
    angle: "measurement.angle",
    area: "measurement.area",
    ellipse: "measurement.ellipse",
    freehand: "measurement.freehand",
    arrow: "measurement.arrow",
    text: "measurement.text",
    probe: "measurement.probe",
    bbox: "measurement.bbox",
    // CircleROI is surfaced by the MPR layout under the legacy "sphere"
    // tool name (the backend treats it as the equator of a PERCIST
    // sphere). Without this entry it fell through to the default and
    // was mis-persisted as ``measurement.distance``.
    sphere: "measurement.sphere",
  };
  return map[tool] ?? "measurement.distance";
}

function parseMeasurementValue(value: string): { value: number; unit: string } | null {
  // The local Measurement.value is a pre-formatted string like
  // "24.3 mm" / "87°" / "240 HU" / "(area) 12.4 mm²". We try to
  // recover (number, unit) for the markers.computed payload.
  // If parsing fails, we store null and the body fallback kicks in.
  const m = /^\s*(-?\d+(?:\.\d+)?)\s*(.*)$/.exec(value || "");
  if (!m) return null;
  const num = Number.parseFloat(m[1]);
  if (!Number.isFinite(num)) return null;
  return { value: num, unit: (m[2] ?? "").trim() };
}

// ``Tool`` now lives in @/components/viewer/toolTypes (shared with the
// multiphase contrast grid). Imported above.

function toolIcon(tool: string): string {
  switch (tool) {
    case "dist":
      return "📏";
    case "angle":
      return "📐";
    case "area":
      return "▱";
    case "ellipse":
      return "◯";
    case "freehand":
      return "✎";
    case "arrow":
      return "➤";
    case "text":
      return "T";
    case "probe":
      return "+";
    default:
      return "📎";
  }
}

export default function SeriesViewerPage() {
  const params = useParams<{ id: string }>();
  const search = useSearchParams();
  // ``?fusion=<series-id>`` triggers an auto-fusion at viewer mount,
  // bypassing the manual ``FusionControls`` picker. Used by the
  // study page's "Open as PET-CT fusion" deep link: the user selects
  // CT + PT together, lands on the CT viewer with the PT volume
  // already overlaid.
  const fusionParam = search.get("fusion");
  // Cache isolation cross-paziente / cross-studio / cross-serie. La cache
  // Cornerstone3D è globale al processo JS; le sue voci sono keyed per
  // ``seriesId`` (i ``volumeId`` hanno la forma ``bvp-vol-{primary,
  // fusion}:<seriesId>``) quindi i lookup con un seriesId diverso non
  // potrebbero comunque colpire l'entry di un altro paziente. Ma per
  // policy esplicita di isolamento (la cache NON deve mai essere letta
  // inter-paziente / inter-studio / inter-serie) e per eliminare edge
  // case di timing — back-button, hot-reload dev, leak residuo via
  // ``vtkOpenGLTexture`` GPU che persiste oltre l'unmount del React
  // component — purgiamo esplicitamente la cache su transizione del
  // route key (``params.id``).
  //
  // La cleanup di questa effect parte PRIMA delle effect del nuovo
  // ``params.id`` (semantica React: cleanup → re-render → nuovi
  // effect), quindi il viewer successivo trova sempre cache pulita
  // prima di chiamare ``makeLocalVolume`` per le sue serie.
  // biome-ignore lint/correctness/useExhaustiveDependencies: viewer lifecycle effect — re-running on derived deps would tear down GPU resources.
  useEffect(() => {
    return () => {
      purgeCornerstoneCache();
      // Also drop the process-global annotation state so annotations from this
      // visit don't leak into the next viewer and get re-POSTed as duplicate
      // markers (task cde63ced follow-up: the diff-sync keys on an unstable
      // array-index id, so a leaked annotation re-enters as "new").
      clearCornerstoneAnnotations();
    };
  }, [params.id]);
  const { user } = useAuth();
  const tv = useTranslations("viewer");
  const [series, setSeries] = useState<Series | null>(null);
  const [volume, setVolume] = useState<VolumeData | null>(null);
  const [volumeLoading, setVolumeLoading] = useState(false);
  const [loadProgress, setLoadProgress] = useState<{
    loaded: number;
    total: number;
    elapsed: number;
  } | null>(null);
  const [err, setErr] = useState<string | null>(null);
  // True when the backend has no packed volume for this series (404).
  // The series is still readable slice-by-slice, so we fall back to
  // the lightweight 2D PACS-style viewer instead of showing the error
  // card. Radiographs / CR / DX / mammography land here by design,
  // mixed-SOP test series too.
  const [volumeUnavailable, setVolumeUnavailable] = useState(false);
  const [elapsedSec, setElapsedSec] = useState(0);
  // On mobile we collapse the sidebar by default and force the
  // viewport to a single axial pane (the only plane usable on a
  // 6-inch screen). The hook is SSR-safe (returns false initially).
  const isMobile = useIsMobile();
  const [sidebarOpen, setSidebarOpen] = useState(true);
  // Phone viewports can't host the 340px sidebar without crowding the
  // canvas down to a useless thumbnail. Collapse it on the first
  // mobile detection and re-open if the user resizes back to desktop;
  // explicit user clicks on the toggle still win because we only
  // re-sync when the breakpoint itself changes.
  useEffect(() => {
    setSidebarOpen(!isMobile);
  }, [isMobile]);

  // Viewport toggles
  const [showAxial, setShowAxial] = useState(true);
  const [showSagittal, setShowSagittal] = useState(false);
  const [showCoronal, setShowCoronal] = useState(false);
  const [show3D, setShow3D] = useState(false);
  const [showMip, setShowMip] = useState(false);
  const [showOblique, setShowOblique] = useState(false);

  // The grid maxes out at a 2x2 (the largest layout the protocols
  // ever populate). Allowing a 5th pane to be enabled would either
  // overflow the grid or shrink existing panes below the readable
  // size for diagnostic review, so toggles refuse to enable past the
  // cap. Turning a pane OFF is always allowed; the user simply has
  // to deselect something else before adding a new pane.
  const MAX_PANES = 4;
  const paneCount =
    (showAxial ? 1 : 0) +
    (showSagittal ? 1 : 0) +
    (showCoronal ? 1 : 0) +
    (show3D ? 1 : 0) +
    (showMip ? 1 : 0) +
    (showOblique ? 1 : 0);
  const atPaneCap = paneCount >= MAX_PANES;
  const togglePane = (current: boolean, setter: React.Dispatch<React.SetStateAction<boolean>>) => {
    if (current) {
      setter(false);
      return;
    }
    if (atPaneCap) return;
    setter(true);
  };

  // Hanging protocol
  const [layout, setLayout] = useState<LayoutId | undefined>(undefined);
  const [protocolId, setProtocolId] = useState<string>("axial-only");
  const [autoFuseModality, setAutoFuseModality] = useState<string | null>(null);
  const applyProtocol = useCallback((p: HangingProtocol) => {
    setProtocolId(p.id);
    setLayout(p.layout);
    const planes = new Set(p.planes);
    setShowAxial(planes.has("axial"));
    setShowSagittal(planes.has("sagittal"));
    setShowCoronal(planes.has("coronal"));
    setShow3D(planes.has("3d"));
    // ``mip`` was added as a first-class plane so the PT-CT review
    // protocols can specify a rotating MIP pane in place of (or
    // alongside) the 3D one. The MIP toggle was previously
    // user-only state; now hanging protocols drive it too.
    setShowMip(planes.has("mip"));
    // Oblique is a power-user toggle that no shipped protocol
    // includes; force it off on protocol activation, otherwise an
    // earlier user-enabled oblique pane stays mounted on top of the
    // protocol's 4 panes and the user sees 5 panes squeezed into a
    // 2x2 grid (the 5th overflows or replaces another).
    setShowOblique(false);
    setAutoFuseModality(p.autoFuseModality ?? null);
  }, []);

  // Crosshair + tools
  const [mprCrosshair, setMprCrosshair] = useState<[number, number, number]>([0, 0, 0]);
  // Default: no toolbar mode selected. The MPR layout interprets
  // ``activeTool === null`` as "Primary mouse drives the crosshair";
  // W/L, Pan and the measure tools are opt-in via the toolbar.
  const [activeTool, setActiveTool] = useState<Tool | null>(null);
  // Interactive click-to-segment (MedSAM-2, task 3af7a33d): busy spinner while
  // the ~3-10s CPU inference runs, a friendly error string, and a bump counter
  // that tells SegmentationImporter to re-list once a new mask is persisted.
  const [segBusy, setSegBusy] = useState(false);
  const [segError, setSegError] = useState<string | null>(null);
  const [segReload, setSegReload] = useState(0);
  const [cineActive, setCineActive] = useState(false);
  const [cineSpeed, setCineSpeed] = useState(5);
  const [allMeasurements, setAllMeasurements] = useState<
    Array<{
      id: number;
      tool: string;
      value: string;
      // User-supplied free-text label, populated by the prompt that
      // fires after each measurement is completed and editable later
      // via the side panel. Persisted as the marker's ``body``.
      label?: string;
      points: Array<{ x: number; y: number }>;
      // Axial slice (Z voxel index) where the measurement was placed.
      // Optional: legacy entries from viewport-state-persistence may
      // not have it; the slice-fade renderer treats null as "always
      // visible" to avoid hiding pre-existing data.
      sliceIndex?: number;
      // Server-side UUID once the marker has been synced to the
      // markers API. Set by the syncing useEffect below; used to
      // DELETE the right server row when the user removes the
      // measurement locally.
      markerId?: string;
      // Cornerstone3D tool name (e.g. ``CircleROITool``). The PERCIST
      // helper filters on this to pick the most recent sphere ROI;
      // ``tool`` is the legacy short name and would be ambiguous.
      csToolName?: string;
      // Server-side SUV stats from ``/roi-stats``, present only for
      // ROIs drawn on PT series with a computable suv_factor_bw.
      suv?: {
        mean?: number;
        sd?: number;
        max?: number;
        peak?: number;
      };
      // Patient/world (LPS) coordinates of the annotation handles, kept
      // alongside the voxel ``points`` so the marker survives a re-pack
      // and is comparable across series/modalities. Emitted by the MPR
      // layout (Cornerstone-native world coords).
      worldPoints?: Array<[number, number, number]>;
      // FrameOfReferenceUID of the volume the annotation was drawn on —
      // the anchor that makes ``worldPoints`` meaningful across series.
      frameOfReferenceUID?: string;
    }>
  >([]);
  // Tracks ids that have already been synced to the markers API so
  // the diff effect doesn't re-POST them on every render. Using a
  // ref instead of state to avoid extra renders.
  const syncedIdsRef = useRef<Set<number>>(new Set());
  // Tracks the local-id → server-id mapping for DELETE on removal.
  const markerIdMapRef = useRef<Map<number, string>>(new Map());
  const prevMeasurementIdsRef = useRef<Set<number>>(new Set());
  const initialMarkersLoadedRef = useRef<boolean>(false);
  // Undo/redo (task cde63ced), scoped to annotations CREATED this session
  // (keyed on the stable Cornerstone annotationUID). Recording is pure (ref
  // stacks); apply reuses the existing safe handle methods, so it never
  // corrupts marker persistence. ``userUidsRef`` is the set of session-drawn
  // uids so we never treat a persisted-loaded marker as undoable.
  const historyRef = useRef(createAnnotationHistory());
  const applyingHistoryUidsRef = useRef<Set<string>>(new Set());
  const annotationSnapshotsRef = useRef<Map<string, unknown>>(new Map());
  const userUidsRef = useRef<Set<string>>(new Set());
  const [historyVersion, setHistoryVersion] = useState(0);
  // markerId → FindingType.category, so the overlay/panels colour a marker by
  // the CLASS of the finding it belongs to. Ref + version so a late findings
  // load recolours without threading through every marker render.
  const catByMarkerRef = useRef<Map<string, string>>(new Map());
  const [catMapVersion, setCatMapVersion] = useState(0);
  // Bumped by the sync effect so MarkerListPanel re-fetches when the
  // user adds/removes measurements via the canvas.
  const [markerListRefreshKey, setMarkerListRefreshKey] = useState(0);
  // v3.5.1 — Marker overlay (SVG) state. ``overlayMarkers`` is what
  // CornerstoneMPRLayout draws on the axial pane for the kinds
  // Cornerstone itself does not render (bbox.lesion, fiducial,
  // text-overlay). MarkerListPanel lifts the persisted markers up via
  // ``onMarkersLoaded``; we filter out the cornerstone-managed kinds
  // here so the overlay never double-draws an outline.
  const [overlayMarkers, setOverlayMarkers] = useState<MarkerOverlayItem[]>([]);
  // Toggle for the SVG overlay (bbox.lesion / fiducial / text-overlay
  // markers projected onto the axial canvas). Default on so AI
  // annotations are visible by default — the radiologist explicitly
  // opted into computing them, hiding them silently would be more
  // surprising than the toolbar chip. Persisted in localStorage so
  // the preference survives navigation.
  const [showAiOverlay, setShowAiOverlay] = useState<boolean>(true);
  useEffect(() => {
    const stored =
      typeof window === "undefined" ? null : window.localStorage.getItem("bvp.viewer.aiOverlay");
    if (stored != null) setShowAiOverlay(stored === "1");
  }, []);
  const toggleAiOverlay = useCallback(() => {
    setShowAiOverlay((prev) => {
      const next = !prev;
      try {
        window.localStorage.setItem("bvp.viewer.aiOverlay", next ? "1" : "0");
      } catch {
        // localStorage may throw in private mode; the toggle still works for this session.
      }
      return next;
    });
  }, []);
  const [focusedMarkerId, setFocusedMarkerId] = useState<string | null>(null);
  // Auto-clear the focus after the user has had time to find it on
  // the canvas. The animation is infinite while focused, so without
  // the clear the pulse would stay on forever.
  const focusedMarkerTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(
    () => () => {
      if (focusedMarkerTimerRef.current) clearTimeout(focusedMarkerTimerRef.current);
    },
    [],
  );
  const focusMarker = useCallback((markerId: string | null) => {
    setFocusedMarkerId(markerId);
    if (focusedMarkerTimerRef.current) {
      clearTimeout(focusedMarkerTimerRef.current);
      focusedMarkerTimerRef.current = null;
    }
    if (markerId) {
      focusedMarkerTimerRef.current = setTimeout(() => {
        setFocusedMarkerId(null);
      }, 6000);
    }
  }, []);
  // Slice-fade rendering parameters sourced from the public app
  // settings (``viewer.marker.fade.*``). Defaults match the seed.
  const [markerFade, setMarkerFade] = useState<{
    enabled: boolean;
    range: number;
    opacity: number;
  }>({ enabled: true, range: 2, opacity: 0.5 });

  // 3D controls
  const [vrPreset, setVrPreset] = useState<ColorPreset>("bone");
  /** Show the full 27-preset legacy list (true) vs. the modality-tailored
   *  shortlist (false, default). Audit 2026-05-13: the 27-button grid
   *  forced the user to scan past unrelated presets every time. */
  const [vrShowAllPresets, setVrShowAllPresets] = useState(false);
  const [vrBlend, setVrBlend] = useState<BlendMode>("composite");
  const [vrOpacity, setVrOpacity] = useState(0);
  const [vrSample, setVrSample] = useState(0.7);
  const [vrShade, setVrShade] = useState(true);
  const [vrCinematic, setVrCinematic] = useState(false);
  // Threshold pushes the OTF cutoff up the histogram in real-time.
  // 0 = preset default, 0.3 = render only the top 70% of the
  // displayRange (kills "filled cylinder" PET artefacts), -0.2 =
  // render more of the lower tissue. UI exposes it as a slider in
  // the 3D control panel so the user doesn't have to fight presets.
  const [vrThreshold, setVrThreshold] = useState(0);
  // Custom opacity stops emitted by the inline TF editor. Null means
  // "use the preset's curve"; an array overrides the preset entirely.
  const [vrCustomOpacity, setVrCustomOpacity] = useState<Array<{
    t: number;
    alpha: number;
  }> | null>(null);
  // HU / scalar range override for the 3D rendering. ``null`` falls
  // back to the preset's resolved ``displayRange`` (rangeHint for
  // HU-based presets, percentile-based otherwise). Driven by the
  // dedicated W/L 3D + min/max sliders below.
  const [vrRangeOverride, setVrRangeOverride] = useState<[number, number] | null>(null);
  // Custom color transfer function stops emitted by the new CTF
  // editor. ``null`` = preset colours; an array of {pos, [r,g,b]}
  // replaces the preset's RGB ramp entirely.
  const [vrCustomColors, setVrCustomColors] = useState<Array<{
    pos: number;
    color: [number, number, number];
  }> | null>(null);
  // Axis-aligned crop box in normalised volume space. ``null`` =
  // render the full volume; otherwise clip planes are applied.
  // Components in [0, 1] per axis; lower < upper.
  const [vrCropBox, setVrCropBox] = useState<{
    lower: [number, number, number];
    upper: [number, number, number];
  } | null>(null);
  // Cornerstone-side fusion mirror state: the viewer page tracks the
  // active fusion volume + series id so ``CornerstoneMPRLayout``
  // can register and overlay it. The vtk-based
  // ``VolumeViewer`` continues to receive the same fusion via its
  // imperative ``setFusionVolume`` for the 3D pane.
  const [csFusionVolume, setCsFusionVolume] = useState<{
    dimensions: [number, number, number];
    spacing: [number, number, number];
    scalars: Float32Array;
    range: [number, number];
  } | null>(null);
  const [csFusionSeriesId, setCsFusionSeriesId] = useState<string | null>(null);
  // Flag di fallimento del fetch della fusion (URL ?fusion=). Quando
  // ``fetchVolume`` rigetta (404 / network), si setta ``true`` così MPR
  // e MIP — entrambi gattati su ``fusionExpected`` — possono uscire dal
  // wait e procedere primary-only invece di restare appesi nello stato
  // ``fusionPending=true`` per sempre. Speculare al gate
  // architetturale che chiude la double-pass del shared engine.
  const [csFusionFailed, setCsFusionFailed] = useState<boolean>(false);
  // Reset esplicito quando l'utente naviga ad un URL senza ?fusion= (la
  // useEffect del fetch non gira, quindi non avrebbe occasione di
  // resettare). Mantiene pulito lo stato tra navigazioni successive.
  useEffect(() => {
    if (!fusionParam) setCsFusionFailed(false);
  }, [fusionParam]);
  // PET overlay visibility — local source of truth so the toolbar
  // button re-renders on toggle. The MPR layout exposes a setter via
  // its imperative handle (``setFusionVisible``); reading the state
  // back from ``mprRef.current?.fusionVisible`` doesn't trigger a
  // React re-render, which is why the button used to "stick off"
  // after the first click. Default ``true``: PET visible at start.
  const [petOn, setPetOn] = useState(true);
  // Reset to "PET visible" whenever a new fusion series is loaded
  // (otherwise a fusion swap would silently inherit the previous
  // toggle state).
  useEffect(() => {
    if (csFusionSeriesId) setPetOn(true);
  }, [csFusionSeriesId]);
  // Tracks the in-flight fusion overlay load so the viewport can
  // render a "Loading fusion overlay" banner while bytes stream in
  // and the post-fetch decode finishes. ``null`` = idle. Populated
  // by the ``onLoadingChange`` callback wired to ``FusionControls``.
  const [fusionLoading, setFusionLoading] = useState<{
    state: "downloading" | "decoding";
    loaded: number;
    total: number;
    percent: number | null;
  } | null>(null);
  // Fusion series metadata (modality, description, instance count)
  // for the second card in the Study chip — without this the
  // sidebar only listed the primary series, hiding the fact that a
  // PET-CT view actually has two series loaded. Fetched whenever
  // ``csFusionSeriesId`` changes (FusionControls picker or URL
  // ``?fusion=`` deep link).
  const [csFusionSeriesMeta, setCsFusionSeriesMeta] = useState<Series | null>(null);
  // SUV variant the operator wants to see in the HUD / ROI labels
  // (Addendum C §5.3). Defaults to BW (the EANM-canonical), can be
  // switched to SUL Janmahasatian (PERCIST), SUL James, BSA Mosteller,
  // BSA Du Bois, or "raw" (no SUV scaling — display Bq/mL directly).
  const [suvVariant, setSuvVariant] = useState<SuvVariant>("bw");
  // Active SUV display window in SUV units (spec §3.3). ``null`` means
  // "auto" — the layout's auto-WL on mount picks the percentile-based
  // default. When the user clicks a preset (0-5 / 0-10 / 0-15 / 0-25)
  // we store the SUV range here; a useEffect downstream converts it to
  // volume-space (Bq/mL) using the active SUV variant's factor and
  // applies it to whichever volume (primary or fusion) is the PT.
  // Critically, the window also re-applies whenever ``suvVariant``
  // changes, so flipping BW → SUL → BSA visibly re-windows the PT.
  const [suvDisplayRange, setSuvDisplayRange] = useState<{ lo: number; hi: number } | null>(null);
  // SUV threshold filter (spec §3.4). Independent of the display
  // range above: the range controls W/L (contrast); this controls
  // VISIBILITY by zeroing the volume's opacity transfer function
  // outside [lo, hi]. Useful to suppress physiological uptake
  // (kidneys, bladder, brain, liver background) and leave only the
  // metabolically relevant voxels. PERCIST 1.0 (Wahl et al., J Nucl
  // Med 2009) uses 1.5 × liver_SUVmean + 2 × liver_SD as the
  // measurable-lesion floor; the helper input below computes that
  // straight from a hand-placed liver ROI's mean/SD.
  //   lo: lower SUV cut (pixels below → transparent)
  //   hi: upper SUV cut, or null = no upper bound (clamp to volume max)
  // ``null`` for the whole struct = filter disabled.
  const [suvHide, setSuvHide] = useState<{ lo: number; hi: number | null } | null>(null);
  const [suvHideDraftLo, setSuvHideDraftLo] = useState<string>("2.5");
  const [suvHideDraftHi, setSuvHideDraftHi] = useState<string>("");
  const [percistLiverMean, setPercistLiverMean] = useState<string>("");
  const [percistLiverSd, setPercistLiverSd] = useState<string>("");
  // PET LUT for the fusion overlay (spec §3.2). Drives the RGB
  // transfer function on the fusion volume actor.
  const [petColormap, setPetColormap] = useState<
    "hot-iron" | "rainbow" | "inverse-gray" | "hot-metal"
  >("hot-iron");
  // EARL Gaussian harmonisation target FWHM in mm (Addendum C §7).
  // 0 = off (default), 7 = EANM EARL1, 5 = EARL2. Re-fetches the
  // volume at the new FWHM via the cached derivative path.
  const [earlFwhmMm, setEarlFwhmMm] = useState<number>(0);
  // Selected sub-stack of a multi-stack series (Philips mDIXON
  // Water/Fat/In-phase/Out-of-phase, multi-echo, DWI). 0 = primary.
  // The available stacks + labels come from ``displayMeta.sub_stacks``;
  // switching re-fetches the volume at ``volume.raw?stack=<idx>``.
  const [selectedStackIndex, setSelectedStackIndex] = useState<number>(0);
  // Fusion display metadata (SUV factors / units / tracer / EARL
  // input) — needed when the PRIMARY is a CT but the fusion overlay
  // is a PET, otherwise the SUV / tracer / EARL controls would be
  // hidden behind ``displayMeta.is_pet`` (= primary modality).
  const [csFusionDisplayMeta, setCsFusionDisplayMeta] = useState<DisplayMetadata | null>(null);
  useEffect(() => {
    if (!csFusionSeriesId) {
      setCsFusionSeriesMeta(null);
      setCsFusionDisplayMeta(null);
      return;
    }
    let cancelled = false;
    studiesApi
      .series(csFusionSeriesId)
      .then((s) => {
        if (!cancelled) setCsFusionSeriesMeta(s);
      })
      .catch(() => {
        if (!cancelled) setCsFusionSeriesMeta(null);
      });
    studiesApi
      .displayMetadata(csFusionSeriesId)
      .then((dm) => {
        if (!cancelled) setCsFusionDisplayMeta(dm);
      })
      .catch(() => {
        if (!cancelled) setCsFusionDisplayMeta(null);
      });
    return () => {
      cancelled = true;
    };
  }, [csFusionSeriesId]);
  const [stlBusy, setStlBusy] = useState(false);
  const [stlIso, setStlIso] = useState<number | null>(null);
  const [stlStatus, setStlStatus] = useState<string | null>(null);

  // Fiducials live in client-side state for now (V6 scope bound).
  const {
    fiducials,
    add: addFiducial,
    remove: removeFiducial,
    rename: renameFiducial,
    clear: clearFiducials,
  } = useFiducials();

  const volumeViewerRef = useRef<VolumeViewerHandle | null>(null);
  const layoutRef = useRef<HTMLDivElement | null>(null);
  const { isFullscreen, isSupported: fsSupported, toggle: toggleFs } = useFullscreen(layoutRef);
  const mprRef = useRef<MPRLayoutHandle | null>(null);

  // Viewer instrumentation: dormant unless an admin enabled the
  // ``viewer.debug.instrumentation`` flag (see /admin/settings). When
  // off, every probe call below is a no-op and behaviour is unchanged.
  const viewerDebug = useViewerDebug();

  const [helpOpen, setHelpOpen] = useState(false);

  // patient_id of the parent study, populated lazily after the series
  // loads. Used by ViewerNotesOverlay to scope the quick-note input.
  const [studyPatientId, setStudyPatientId] = useState<string | null>(null);
  // Parent study + patient identity for the always-on safety banner
  // (never-doubt-which-patient). Both lazily fetched after series load;
  // ``patient`` stays null when the reader isn't authorised to see the
  // demographics (the banner then shows study-only identity).
  const [study, setStudy] = useState<Study | null>(null);
  const [patient, setPatient] = useState<Patient | null>(null);
  // Display metadata (photometric, pixel spacing, AND PET SUV factors
  // when the series is PT modality). Lazily fetched after series load.
  const [displayMeta, setDisplayMeta] = useState<DisplayMetadata | null>(null);
  // PET-aware metadata bag the sidebar uses to drive SUV / EARL /
  // tracer controls. Prefer the primary when it's already PT, else
  // fall back to the fusion's metadata so CT-primary + PT-fusion deep
  // links still expose the PET controls.
  const petDisplayMeta = displayMeta?.is_pet
    ? displayMeta
    : csFusionDisplayMeta?.is_pet
      ? csFusionDisplayMeta
      : null;
  // Which slot holds the PET volume — drives where the SUV display
  // range is applied. ``"primary"`` when the user opened a PT series
  // directly, ``"fusion"`` for the typical "open as PET-CT" deep link
  // that lands on the CT primary with the PT as overlay. ``null`` when
  // there is no PT in either slot.
  const petSlot: "primary" | "fusion" | null = displayMeta?.is_pet
    ? "primary"
    : csFusionDisplayMeta?.is_pet
      ? "fusion"
      : null;
  // Apply the active SUV display range to whichever volume is the PT.
  // Re-runs when the user picks a different range or flips the SUV
  // variant: the variant change is the bit that was previously
  // invisible, since the volume's scalars stay raw Bq/mL and only the
  // wc/ww re-derived from ``factor`` reflects the variant choice.
  useEffect(() => {
    if (!suvDisplayRange || !petSlot) return;
    const f = pickActiveSuvFactor(petDisplayMeta, suvVariant);
    if (!f || f <= 0) return;
    const wc = (suvDisplayRange.lo + suvDisplayRange.hi) / 2 / f;
    const ww = (suvDisplayRange.hi - suvDisplayRange.lo) / f;
    if (petSlot === "primary") {
      mprRef.current?.setWC(wc);
      mprRef.current?.setWW(ww);
    } else {
      mprRef.current?.setFusionWC?.(wc);
      mprRef.current?.setFusionWW?.(ww);
    }
  }, [suvDisplayRange, suvVariant, petSlot, petDisplayMeta]);
  // Ref to the right sidebar so the section nav can scroll within it.
  const sidebarRef = useRef<HTMLElement | null>(null);
  // Scrollable content area inside the sidebar — separate from the
  // sticky chip-nav that stays at the top. SidebarSectionNav uses
  // this ref so its scrollIntoView jumps the right element.
  const sidebarScrollRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    let cancelled = false;
    studiesApi
      .series(params.id)
      .then((s) => {
        if (cancelled) return;
        setSeries(s);
        // Parallel fetch of the parent study to surface its patient_id
        // AND the patient/study identity for the safety banner.
        studiesApi
          .detail(s.study_id)
          .then((st) => {
            if (cancelled) return;
            setStudyPatientId(st.patient_id);
            setStudy(st);
            // Patient identity for the banner. The backend gates this
            // per-caller, so a successful read means the reader is
            // authorised to see who the study belongs to; on 403/404 we
            // simply leave the patient half of the banner blank.
            if (st.patient_id) {
              patientsApi
                .detail(st.patient_id)
                .then((p) => {
                  if (!cancelled) setPatient(p);
                })
                .catch(() => {
                  /* identity stays partial (study-only) when not authorised */
                });
            }
          })
          .catch(() => {
            // Non-fatal: overlay just stays hidden if we can't resolve.
          });
        // Fetch display metadata (PET SUV factors, photometric, ...) so
        // PetInfoPanel can render the SUV provenance card. Independent
        // of the volume fetch — works for 2D fallback series too.
        studiesApi
          .displayMetadata(s.id)
          .then((dm) => {
            if (!cancelled) {
              setDisplayMeta(dm);
              // Open on the primary contrast (Water for mDIXON). The
              // volume fetch is already in flight for stack 0; only
              // re-fetch if the default isn't the primary.
              if (typeof dm.default_stack_index === "number" && dm.default_stack_index !== 0) {
                setSelectedStackIndex(dm.default_stack_index);
                setVolume(null);
              }
            }
          })
          .catch(() => {
            /* PET HUD just stays hidden if we can't resolve. */
          });
      })
      .catch((e) => {
        if (!cancelled) setErr(e instanceof ApiError ? e.message : "load failed");
      });
    return () => {
      cancelled = true;
    };
  }, [params.id]);

  useEffect(() => {
    if (!series) return;
    // Apply once with whatever we know now (modality + instance count).
    // The displayMeta-driven re-pick (just below) refines the choice
    // when the IOP-derived primary plane lands.
    applyProtocol(
      pickDefaultProtocol(series.modality, {
        instanceCount: series.received_instance_count ?? null,
      }),
    );
  }, [series, applyProtocol]);

  // Refine the protocol once display metadata returns: that is when we
  // learn the acquisition plane (axial vs sagittal vs coronal) and can
  // promote the right pane to primary, plus collapse to a 2D layout
  // for non-volumetric / single-slice series.
  useEffect(() => {
    if (!series || !displayMeta) return;
    applyProtocol(
      pickDefaultProtocol(series.modality, {
        instanceCount: displayMeta.instance_count || (series.received_instance_count ?? null),
        primaryPlane: displayMeta.primary_plane,
      }),
    );
  }, [series, displayMeta, applyProtocol]);

  // Load public app settings once. The slice-fade rendering params
  // are admin-tunable from /admin/settings; we read the cached
  // version here without polling.
  useEffect(() => {
    let cancelled = false;
    settingsApi
      .listPublic()
      .then((rows: AppSetting[]) => {
        if (cancelled) return;
        const next = { enabled: true, range: 2, opacity: 0.5 };
        for (const r of rows) {
          if (r.key === "viewer.marker.fade.enabled" && typeof r.value === "boolean") {
            next.enabled = r.value;
          } else if (r.key === "viewer.marker.fade.range" && typeof r.value === "number") {
            next.range = Math.max(0, Math.round(r.value));
          } else if (r.key === "viewer.marker.fade.opacity" && typeof r.value === "number") {
            next.opacity = Math.min(1, Math.max(0, r.value));
          }
        }
        setMarkerFade(next);
      })
      .catch(() => {
        /* keep defaults if the settings endpoint is unreachable */
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // -------- Markers API sync (TODO 43) --------
  //
  // 1. On mount (once the parent study + patient are known), fetch
  //    persisted measurement markers from the server and seed
  //    ``allMeasurements`` so the radiologist resumes where they
  //    left off — even on a different device. The fetched ids go
  //    into ``syncedIdsRef`` so the diff effect doesn't re-POST.
  // 2. After every state change, diff added vs removed ids and
  //    POST/DELETE accordingly. Local id stays a number (legacy);
  //    server uuid lives in ``markerIdMapRef``.

  // Reconcile local canvas state with the server marker list. Runs
  // on mount AND on a 15s poll, so measurements written by other
  // clients (MCP agent, another browser tab, future SSE) surface on
  // the canvas without a page reload. The reconcile is idempotent:
  // any server marker whose UUID is already mapped via
  // ``markerIdMapRef`` is skipped, so re-fetches only seed newly
  // appeared rows. Deletions are intentionally NOT mirrored here to
  // avoid racing the user's in-flight canvas removal; the sidebar
  // panel handles that path with explicit refresh.
  useEffect(() => {
    if (!studyPatientId || !series?.study_id) return;
    const studyId = series.study_id;
    let cancelled = false;
    let seedCounter = 0;

    const knownServerIds = (): Set<string> => {
      const out = new Set<string>();
      for (const v of markerIdMapRef.current.values()) out.add(v);
      return out;
    };

    const reconcile = async () => {
      try {
        // Fetch study- and series-scoped markers in parallel so the
        // canvas matches what the sidebar panel sees. MCP / external
        // agents sometimes anchor to the series; the canvas used to
        // miss those entirely.
        const [studyMarkers, seriesMarkers] = await Promise.all([
          markersApi.list(studyPatientId, {
            target_kind: "study",
            target_id: studyId,
            kind: undefined,
          }),
          markersApi.list(studyPatientId, {
            target_kind: "series",
            target_id: params.id,
            kind: undefined,
          }),
        ]);
        if (cancelled) return;

        const known = knownServerIds();
        const incoming = [...studyMarkers, ...seriesMarkers].filter(
          (m: Marker) => m.kind.startsWith("measurement.") && !known.has(m.id),
        );
        if (incoming.length === 0) {
          initialMarkersLoadedRef.current = true;
          return;
        }

        const seeded = incoming
          .map((m) => {
            const g = m.geometry as { points?: number[][] } | null;
            const pts = g?.points ?? [];
            if (pts.length === 0) return null;
            const sliceZ = pts[0][2];
            const value = (() => {
              const c = m.computed as { value?: unknown; unit?: unknown } | null;
              if (c?.value != null) {
                return `${c.value}${c.unit ? ` ${c.unit}` : ""}`;
              }
              return m.body ?? "";
            })();
            seedCounter += 1;
            const localId = Number.MAX_SAFE_INTEGER - 1_000_000 - seedCounter;
            const persistedLabel =
              typeof m.body === "string" && m.body.trim() ? m.body.trim() : undefined;
            return {
              id: localId,
              tool: m.kind.replace("measurement.", "") || "dist",
              value,
              label: persistedLabel,
              points: pts.map((p) => ({ x: p[0], y: p[1] })),
              sliceIndex: typeof sliceZ === "number" ? Math.round(sliceZ) : undefined,
              markerId: m.id,
            };
          })
          .filter((x): x is NonNullable<typeof x> => x !== null);

        for (const sm of seeded) {
          syncedIdsRef.current.add(sm.id);
          if (sm.markerId) markerIdMapRef.current.set(sm.id, sm.markerId);
          prevMeasurementIdsRef.current.add(sm.id);
        }
        initialMarkersLoadedRef.current = true;
        if (seeded.length > 0) {
          setAllMeasurements((prev) => [...seeded, ...prev]);
          // Tell the sidebar panel too (it depends on this key for
          // its own refresh, and the user opened the page expecting
          // both surfaces to converge).
          setMarkerListRefreshKey((k) => k + 1);
        }
      } catch {
        // Network blip during poll; don't block the viewer.
        // eslint-disable-next-line no-console
        console.warn("markers reconcile failed");
      }
    };

    reconcile();
    const t = setInterval(reconcile, 15000);
    return () => {
      cancelled = true;
      clearInterval(t);
    };
  }, [studyPatientId, series?.study_id, params.id]);

  useEffect(() => {
    if (!studyPatientId || !series?.study_id) return;
    if (!initialMarkersLoadedRef.current) return;
    const currentIds = new Set(allMeasurements.map((m) => m.id));
    const previousIds = prevMeasurementIdsRef.current;
    // Local ``id`` is an ARRAY INDEX (see CornerstoneMPRLayout onMeasurementsChange
    // ``id: i``), so it is NOT stable across list changes: a persisted marker that
    // the reconcile seeded with a synthetic id can re-enter here with a fresh index
    // and slip past the id-keyed guards. The STABLE key is ``markerId`` (the server
    // uuid for a loaded marker, the Cornerstone annotationUID for a fresh draw).
    // Exclude anything whose markerId is already a known server marker so a page
    // load can't re-POST loaded markers as duplicates (this study had accumulated
    // 100+ such dupes) nor record them as undoable "creates" in the annotation
    // history (task cde63ced). A genuine draw's annotationUID is never a server id,
    // so real user draws still persist + become undoable.
    const knownServerIds = new Set(markerIdMapRef.current.values());
    const added = allMeasurements.filter(
      (m) =>
        !previousIds.has(m.id) &&
        !syncedIdsRef.current.has(m.id) &&
        !(m.markerId != null && knownServerIds.has(m.markerId)),
    );
    const removedIds: number[] = [];
    for (const id of previousIds) {
      if (!currentIds.has(id)) removedIds.push(id);
    }

    const mutated = added.length > 0 || removedIds.length > 0;

    for (const m of added) {
      // Capture sliceIndex from the current axial crosshair Z if the
      // measurement was created without one (the older finishMeasurement
      // path doesn't fill it in).
      const z =
        m.sliceIndex ?? (Number.isFinite(mprCrosshair[2]) ? Math.round(mprCrosshair[2]) : 0);
      const kind: MarkerKind = mapToolToMarkerKind(m.tool);
      const points: number[][] = m.points.map((p) => [p.x, p.y, z]);
      const computed = parseMeasurementValue(m.value);
      syncedIdsRef.current.add(m.id);
      // ``body`` carries the human-readable label across all
      // measurement kinds. Historically only ``measurement.text``
      // populated it (from the auto-computed ``value``); now any
      // measurement can carry a user-supplied label, falling back to
      // ``value`` for the legacy ``measurement.text`` path so old
      // markers keep round-tripping.
      const persistedLabel = m.label?.trim()
        ? m.label.trim()
        : kind === "measurement.text"
          ? m.value
          : null;
      // Persist the patient/world coordinates (already computed by the
      // MPR layer) + the FrameOfReferenceUID alongside the voxel
      // ``points`` so the annotation is recoverable and cross-series
      // comparable, not just voxel indices meaningless outside this
      // packed volume.
      const geometry: Record<string, unknown> = { axis: "axial", points };
      if (m.worldPoints?.length) geometry.world_points = m.worldPoints;
      if (m.frameOfReferenceUID) geometry.frame_of_reference_uid = m.frameOfReferenceUID;
      markersApi
        .create(studyPatientId, {
          target_kind: "study",
          target_id: series.study_id,
          kind,
          geometry,
          body: persistedLabel,
          computed,
        })
        .then((server) => {
          markerIdMapRef.current.set(m.id, server.id);
          setMarkerListRefreshKey((k) => k + 1);
        })
        .catch(() => {
          syncedIdsRef.current.delete(m.id);
        });
    }

    for (const id of removedIds) {
      const markerId = markerIdMapRef.current.get(id);
      syncedIdsRef.current.delete(id);
      markerIdMapRef.current.delete(id);
      if (markerId) {
        markersApi
          .remove(markerId)
          .then(() => setMarkerListRefreshKey((k) => k + 1))
          .catch(() => {
            /* ignore */
          });
      }
    }

    if (mutated) {
      prevMeasurementIdsRef.current = currentIds;
    }

    // Undo/redo history (task cde63ced), scoped to session-drawn annotations
    // and keyed on the stable Cornerstone annotationUID. ``added`` already
    // excludes persisted-loaded markers (pre-added to syncedIdsRef), so a
    // create here is a genuine user draw. Recording is side-effect-free; the
    // apply path reuses the existing safe handle methods, so it cannot corrupt
    // marker persistence. Covers create + delete (move/label are a later pass).
    const handle = mprRef.current;
    if (handle) {
      let histChanged = false;
      const curUids = new Set(
        allMeasurements.map((m) => m.markerId).filter((u): u is string => !!u),
      );
      for (const uid of curUids) {
        const snap = handle.getAnnotation?.(uid);
        if (snap) annotationSnapshotsRef.current.set(uid, snap);
      }
      for (const m of added) {
        if (!m.markerId || applyingHistoryUidsRef.current.has(m.markerId)) continue;
        userUidsRef.current.add(m.markerId);
        historyRef.current.push({
          type: "create",
          csId: m.markerId,
          annotation: annotationSnapshotsRef.current.get(m.markerId) ?? null,
        });
        histChanged = true;
      }
      for (const uid of [...userUidsRef.current]) {
        if (curUids.has(uid)) continue;
        userUidsRef.current.delete(uid);
        const snap = annotationSnapshotsRef.current.get(uid) ?? null;
        annotationSnapshotsRef.current.delete(uid);
        if (applyingHistoryUidsRef.current.has(uid)) continue;
        historyRef.current.push({ type: "remove", csId: uid, annotation: snap });
        histChanged = true;
      }
      if (histChanged) setHistoryVersion((v) => v + 1);
    }
  }, [allMeasurements, studyPatientId, series?.study_id, mprCrosshair]);

  // Apply an undo/redo command against Cornerstone, guarded by
  // ``applyingHistoryUidsRef`` so the resulting allMeasurements change is not
  // re-recorded as a fresh user command (task cde63ced).
  const applyHistory = useCallback((cmd: AnnotationCommand, forward: boolean) => {
    const h = mprRef.current;
    if (!h) return;
    const uid = cmd.csId;
    applyingHistoryUidsRef.current.add(uid);
    if (cmd.type === "label") {
      h.updateAnnotationLabel(uid, forward ? cmd.nextLabel : cmd.prevLabel);
    } else {
      // undo(create)=remove, redo(create)=restore; remove is the mirror.
      const restore = cmd.type === "create" ? forward : !forward;
      if (restore) {
        if (cmd.annotation) {
          h.restoreAnnotation?.(cmd.annotation);
          userUidsRef.current.add(uid);
        }
      } else {
        h.removeAnnotation(uid);
        userUidsRef.current.delete(uid);
      }
    }
    setMarkerListRefreshKey((k) => k + 1);
    setHistoryVersion((v) => v + 1);
    // Release the guard after the resulting allMeasurements effect has run.
    window.setTimeout(() => applyingHistoryUidsRef.current.delete(uid), 0);
  }, []);

  const undoAnnotation = useCallback(() => {
    const cmd = historyRef.current.undo();
    if (cmd) applyHistory(cmd, false);
  }, [applyHistory]);
  const redoAnnotation = useCallback(() => {
    const cmd = historyRef.current.redo();
    if (cmd) applyHistory(cmd, true);
  }, [applyHistory]);

  // markerId → finding-class map (task cde63ced): colour a marker by the CLASS
  // of the finding it belongs to. Best-effort; a failure leaves markers on
  // their geometry-kind colour. Refreshes with the marker list.
  // biome-ignore lint/correctness/useExhaustiveDependencies: series.study_id is the reactive key; findingsApi is a stable module import.
  useEffect(() => {
    if (!studyPatientId || !series?.study_id) return;
    let cancelled = false;
    void (async () => {
      try {
        const [vocab, findings] = await Promise.all([
          findingsApi.getVocab(),
          findingsApi.list(studyPatientId, { study_id: series.study_id, limit: 500 }),
        ]);
        if (cancelled) return;
        const catByType = new Map<string, string>();
        for (const ft of vocab.finding_types) catByType.set(ft.key, ft.category);
        const map = new Map<string, string>();
        for (const f of findings) {
          const cat = catByType.get(f.type);
          if (!cat) continue;
          for (const g of f.geometry ?? []) {
            if (g.marker_id) map.set(g.marker_id, cat);
          }
        }
        catByMarkerRef.current = map;
        setCatMapVersion((v) => v + 1);
      } catch {
        /* markers keep their geometry-kind colour */
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [studyPatientId, series?.study_id, markerListRefreshKey]);

  // Recolour existing overlay markers when the class map updates.
  // biome-ignore lint/correctness/useExhaustiveDependencies: catMapVersion is the refresh trigger.
  useEffect(() => {
    setOverlayMarkers((prev) =>
      prev.map((it) => ({ ...it, category: catByMarkerRef.current.get(it.id) ?? null })),
    );
  }, [catMapVersion]);

  useEffect(() => {
    if (!volumeLoading) {
      setElapsedSec(0);
      return;
    }
    const t = setInterval(() => setElapsedSec((s) => s + 1), 1000);
    return () => clearInterval(t);
  }, [volumeLoading]);

  // Cine mode
  useEffect(() => {
    if (!cineActive || !volume) return;
    const maxZ = volume.dimensions[2] - 1;
    const t = setInterval(() => {
      setMprCrosshair((prev) => {
        const next: [number, number, number] = [prev[0], prev[1], (prev[2] + 1) % (maxZ + 1)];
        mprRef.current?.setCrosshair(next);
        return next;
      });
    }, 1000 / cineSpeed);
    return () => clearInterval(t);
  }, [cineActive, cineSpeed, volume]);

  const loadVolume = useCallback((): Promise<VolumeData | null> => {
    if (volume) return Promise.resolve(volume);
    setVolumeLoading(true);
    setErr(null);
    setVolumeUnavailable(false);
    setLoadProgress(null);
    const startTime = Date.now();
    return (async () => {
      // Progressive first paint: show the ~1/8 low-res preview immediately
      // (≈3s) so the panes aren't blank while the full-res volume (≈26s over
      // the throttled egress) streams in below and swaps in. Best-effort — any
      // preview failure (404 / network) falls straight through to full-res
      // with zero user-visible change. The preview carries no geometry; the
      // full-res payload is authoritative.
      try {
        const ptoken = getStoredToken();
        const ph: Record<string, string> = {};
        if (ptoken) ph.authorization = `Bearer ${ptoken}`;
        const presp = await fetch(studiesApi.volumePreviewUrl(params.id), {
          credentials: "include",
          headers: ph,
        });
        if (presp.ok) {
          const pbuf = await presp.arrayBuffer();
          const pdv = new DataView(pbuf);
          const pnx = pdv.getUint32(0, true);
          const pny = pdv.getUint32(4, true);
          const pnz = pdv.getUint32(8, true);
          const pv: VolumeData = {
            dimensions: [pnx, pny, pnz],
            spacing: [pdv.getFloat32(12, true), pdv.getFloat32(16, true), pdv.getFloat32(20, true)],
            scalars: new Float32Array(pbuf, 32, pnx * pny * pnz),
            range: [pdv.getFloat32(24, true), pdv.getFloat32(28, true)],
            resolution: "preview",
          };
          setVolume(pv);
          setMprCrosshair([Math.floor(pnx / 2), Math.floor(pny / 2), Math.floor(pnz / 2)]);
          if (viewerDebug) {
            updateViewerProbe({
              notes: ["preview rendered"],
              timings: { previewMs: Date.now() - startTime },
            });
          }
        }
      } catch {
        /* preview is best-effort — fall through to the full-res load */
      }
      try {
        const token = getStoredToken();
        const headers: Record<string, string> = {};
        if (token) headers.authorization = `Bearer ${token}`;
        const resp = await fetch(
          studiesApi.volumeUrl(params.id, {
            earlFwhmMm: earlFwhmMm > 0 ? earlFwhmMm : undefined,
            stackIndex: selectedStackIndex > 0 ? selectedStackIndex : undefined,
          }),
          { credentials: "include", headers },
        );
        if (!resp.ok) throw new ApiError(resp.status, await resp.text());
        // Real DICOM geometry rides on X-Volume-* headers (the blob's
        // binary header is frozen). Absent/partial → identity-frame
        // fallback downstream in makeLocalVolume.
        const geomOrigin = parseFloatVector(resp.headers.get("x-volume-origin"), 3);
        const geomDirection = parseFloatVector(resp.headers.get("x-volume-direction"), 9);
        const geomFor = resp.headers.get("x-volume-frame-of-reference") || undefined;
        const contentLength = Number(resp.headers.get("content-length") || 0);
        // gzip Content-Length is the COMPRESSED size; the browser hands us
        // decompressed chunks, so it can't drive a % bar. Derive the true
        // uncompressed total from the 32-byte packed header (nx,ny,nz) the
        // moment we have it (after the first chunk); until then the raw MB
        // counter still moves, so it's never a bare spinner.
        const reader = resp.body?.getReader();
        if (!reader) throw new Error("no response body");
        const chunks: Uint8Array[] = [];
        let loaded = 0;
        let expectedTotal = 0;
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          chunks.push(value);
          loaded += value.length;
          if (!expectedTotal && loaded >= 32) {
            let head = chunks[0];
            if (head.length < 32) {
              head = new Uint8Array(32);
              let o = 0;
              for (const c of chunks) {
                const n = Math.min(c.length, 32 - o);
                head.set(c.subarray(0, n), o);
                o += n;
                if (o >= 32) break;
              }
            }
            const hdv = new DataView(head.buffer, head.byteOffset, 32);
            const hnx = hdv.getUint32(0, true);
            const hny = hdv.getUint32(4, true);
            const hnz = hdv.getUint32(8, true);
            if (hnx > 0 && hny > 0 && hnz > 0) expectedTotal = 32 + hnx * hny * hnz * 4;
          }
          setLoadProgress({
            loaded,
            total: expectedTotal || contentLength || loaded,
            elapsed: (Date.now() - startTime) / 1000,
          });
        }
        const buf = new ArrayBuffer(loaded);
        const view = new Uint8Array(buf);
        let offset = 0;
        for (const chunk of chunks) {
          view.set(chunk, offset);
          offset += chunk.length;
        }
        const dv = new DataView(buf);
        const v: VolumeData = {
          dimensions: [dv.getUint32(0, true), dv.getUint32(4, true), dv.getUint32(8, true)],
          spacing: [dv.getFloat32(12, true), dv.getFloat32(16, true), dv.getFloat32(20, true)],
          scalars: new Float32Array(
            buf,
            32,
            dv.getUint32(0, true) * dv.getUint32(4, true) * dv.getUint32(8, true),
          ),
          range: [dv.getFloat32(24, true), dv.getFloat32(28, true)],
          origin: geomOrigin as [number, number, number] | undefined,
          direction: geomDirection as VolumeData["direction"],
          frameOfReferenceUid: geomFor,
          resolution: "full",
        };
        setVolume(v);
        setMprCrosshair([
          Math.floor(v.dimensions[0] / 2),
          Math.floor(v.dimensions[1] / 2),
          Math.floor(v.dimensions[2] / 2),
        ]);
        if (viewerDebug) {
          updateViewerProbe({
            notes: ["full-res swapped in"],
            timings: { fullMs: Date.now() - startTime },
          });
        }
        return v;
      } catch (e) {
        // 404 from the volume endpoint is the legitimate "no 3D pack
        // available" signal — radiographs (CR/DX, single frame),
        // mammography, mixed-SOP test data, or a series that hasn't
        // gone through pack_volume yet. We don't treat it as a hard
        // error: the render branch falls through to ``<Series2DViewer>``
        // which scrolls slice-by-slice via the per-instance JPEG
        // endpoint. Keep ``err`` clean so the viewer chrome shows the
        // 2D fallback rather than the red error card.
        if (e instanceof ApiError && e.status === 404) {
          setVolumeUnavailable(true);
        } else {
          setErr(e instanceof ApiError ? e.message : tv("volumeErrorFallback"));
        }
        return null;
      } finally {
        setVolumeLoading(false);
      }
    })();
  }, [volume, params.id, tv, earlFwhmMm, selectedStackIndex, viewerDebug]);

  useEffect(() => {
    loadVolume();
  }, [loadVolume]);

  // Switch the displayed sub-stack (mDIXON contrast / echo / b-value).
  // Drop the current volume so ``loadVolume`` re-fetches at the new
  // ``?stack=`` index; the Cornerstone cache is keyed per stack so the
  // panes rebuild cleanly.
  const selectStack = useCallback((idx: number) => {
    setSelectedStackIndex((cur) => {
      if (cur === idx) return cur;
      setVolume(null);
      return idx;
    });
  }, []);

  // ``?fusion=<series-id>`` URL flow: fetch the fusion volume once
  // and push it into both the legacy vtk fusion path (so the 3D
  // viewer overlays it) and the Cornerstone state mirror (so the
  // MPR layout overlays it). The lazy fetch waits for the primary
  // volume so the camera is ready before the second volume binds.
  useEffect(() => {
    if (!fusionParam || !volume) return;
    // Reset esplicito del fail-flag all'inizio di ogni tentativo (gestisce
    // il caso "URL fusion B dopo URL fusion A fallita": senza reset il
    // flag stantio terrebbe il gate del MPR/MIP rilasciato col bypass
    // failure-path anche per il nuovo tentativo, mascherando un eventuale
    // secondo errore).
    setCsFusionFailed(false);
    let cancelled = false;
    // Watchdog: se ``fetchVolume`` non risolve né rigetta entro 30s (rete
    // lenta o congelata), abortiamo la richiesta e forziamo il
    // fail-flag. Senza questo timeout il gate ``fusionExpected``
    // resterebbe ``fusionPending=true`` indefinitamente e MPR/MIP
    // resterebbero appesi senza render. 30s è abbondante per un volume
    // PET tipico (50-150 MB su connessione domestica) ma sufficiente
    // per eliminare il rischio "appeso per sempre".
    const controller = new AbortController();
    const watchdog = setTimeout(() => controller.abort(), 30_000);
    fetchVolume(fusionParam, { signal: controller.signal })
      .then(({ header, scalars }) => {
        if (cancelled) return;
        const fv = {
          dimensions: [header.nx, header.ny, header.nz] as [number, number, number],
          spacing: header.spacing as [number, number, number],
          scalars,
          range: header.valueRange as [number, number],
          origin: header.origin,
          direction: header.direction,
          frameOfReferenceUid: header.frameOfReferenceUid,
        };
        setCsFusionVolume(fv);
        setCsFusionSeriesId(fusionParam);
        // Mirror to vtk so the 3D pane gets the overlay too.
        volumeViewerRef.current?.setFusionVolume({
          ...fv,
          colormap: "hot",
          opacity: 0.05,
        });
      })
      .catch(() => {
        // La fusion resta best-effort: il primary continua a renderizzare
        // senza overlay. Settiamo ``csFusionFailed=true`` così il gate
        // ``fusionExpected`` sul MPR e sulla MIP (che altrimenti
        // aspetterebbero ``csFusionVolume`` per sempre) si rilascia ed
        // entrambi proseguono primary-only invece di restare appesi.
        // Cattura sia errori HTTP/network reali che AbortError dal
        // watchdog: in entrambi i casi l'UX corretta è "rinuncia alla
        // fusion, mostra il primary".
        if (!cancelled) setCsFusionFailed(true);
      })
      .finally(() => {
        clearTimeout(watchdog);
      });
    return () => {
      cancelled = true;
      controller.abort();
      clearTimeout(watchdog);
    };
  }, [fusionParam, volume]);

  // EARL re-fetch: when the user changes the FWHM target the
  // current volume is no longer correct (it was filtered to a
  // different / no FWHM). Drop the cached state so ``loadVolume``
  // re-fetches with the new query param.
  // biome-ignore lint/correctness/useExhaustiveDependencies: viewer lifecycle effect — re-running on derived deps would tear down GPU resources.
  useEffect(() => {
    setVolume(null);
    setVolumeUnavailable(false);
  }, [earlFwhmMm]);

  // PT-specific autopilot: when a PET volume finishes loading, jump
  // to a clinically meaningful default (50-99.5 percentile window +
  // inverted grayscale, matching how a PACS opens a PT series). The
  // generic ``Auto W/L`` button already does the right thing thanks
  // to the modality-aware ``computeAutoWL``, but most users don't
  // know they need to click it for PT — without this kick they see
  // a uniform white slab and conclude the viewer is broken.
  const ptAutoAppliedRef = useRef<string | null>(null);
  useEffect(() => {
    if (!volume || !series) return;
    if ((series.modality || "").toUpperCase() !== "PT") return;
    // Only fire once per series.id, not on every re-render.
    if (ptAutoAppliedRef.current === series.id) return;
    if (!mprRef.current) return;
    const { wc, ww } = computeAutoWL(volume.scalars, "PT");
    mprRef.current.setWC(wc);
    mprRef.current.setWW(ww);
    if (!mprRef.current.invert) {
      mprRef.current.setInvert(true);
    }
    ptAutoAppliedRef.current = series.id;
  }, [volume, series]);

  const stepSlice = useCallback(
    (delta: number) => {
      if (!volume) return;
      const maxZ = volume.dimensions[2] - 1;
      setMprCrosshair((prev) => {
        const nextZ = Math.max(0, Math.min(maxZ, prev[2] + delta));
        if (nextZ === prev[2]) return prev;
        const next: [number, number, number] = [prev[0], prev[1], nextZ];
        mprRef.current?.setCrosshair(next);
        return next;
      });
    },
    [volume],
  );

  // setMprCrosshair with a fresh array reference forces the sidebar to
  // re-read the mutable mprRef WC/WW values (they aren't React state here).
  const nudgeRerender = useCallback(() => setMprCrosshair((c) => [...c]), []);

  const adjustWindow = useCallback(
    (dWc: number, dWw: number) => {
      if (!mprRef.current) return;
      if (dWc) mprRef.current.setWC(mprRef.current.wc + dWc);
      if (dWw) mprRef.current.setWW(Math.max(1, mprRef.current.ww + dWw));
      nudgeRerender();
    },
    [nudgeRerender],
  );

  const hotkeyBindings: HotkeyBinding[] = [
    {
      key: "z",
      ctrl: true,
      shift: false,
      description: "Undo annotation",
      handler: () => undoAnnotation(),
    },
    {
      key: "z",
      ctrl: true,
      shift: true,
      description: "Redo annotation",
      handler: () => redoAnnotation(),
    },
    {
      key: "ArrowUp",
      description: "Previous slice",
      handler: () => stepSlice(-1),
      disabled: !volume,
    },
    {
      key: "ArrowDown",
      description: "Next slice",
      handler: () => stepSlice(1),
      disabled: !volume,
    },
    {
      key: "PageUp",
      description: "Jump back 10 slices",
      handler: () => stepSlice(-10),
      disabled: !volume,
    },
    {
      key: "PageDown",
      description: "Jump forward 10 slices",
      handler: () => stepSlice(10),
      disabled: !volume,
    },
    {
      key: "+",
      description: "Zoom in",
      handler: () => dispatchViewportZoom(1.1),
    },
    {
      key: "=",
      description: "Zoom in (same row as +)",
      handler: () => dispatchViewportZoom(1.1),
    },
    {
      key: "-",
      description: "Zoom out",
      handler: () => dispatchViewportZoom(0.9),
    },
    {
      key: "0",
      description: "Reset zoom & pan",
      handler: () => dispatchViewportResetView(),
    },
    {
      key: "ArrowLeft",
      description: "Narrow window width",
      handler: () => adjustWindow(0, -25),
      disabled: !volume,
    },
    {
      key: "ArrowRight",
      description: "Widen window width",
      handler: () => adjustWindow(0, 25),
      disabled: !volume,
    },
    {
      key: "[",
      description: "Lower window center",
      handler: () => adjustWindow(-25, 0),
      disabled: !volume,
    },
    {
      key: "]",
      description: "Raise window center",
      handler: () => adjustWindow(25, 0),
      disabled: !volume,
    },
    {
      key: "w",
      description: "Reset window / level",
      handler: () => {
        mprRef.current?.resetWL();
        nudgeRerender();
      },
    },
    {
      key: "i",
      description: "Invert colors",
      handler: () => {
        if (!mprRef.current) return;
        mprRef.current.setInvert(!mprRef.current.invert);
        nudgeRerender();
      },
    },
    {
      key: "r",
      description: "Rotate 90°",
      handler: () => mprRef.current?.rotateAll(),
    },
    {
      key: "f",
      description: "Flip horizontally",
      handler: () => mprRef.current?.flipHAll(),
    },
    {
      key: "f",
      shift: true,
      description: "Flip vertically",
      handler: () => mprRef.current?.flipVAll(),
    },
    {
      key: "1",
      description: "Toggle axial viewport",
      handler: () => togglePane(showAxial, setShowAxial),
    },
    {
      key: "2",
      description: "Toggle sagittal viewport",
      handler: () => togglePane(showSagittal, setShowSagittal),
    },
    {
      key: "3",
      description: "Toggle coronal viewport",
      handler: () => togglePane(showCoronal, setShowCoronal),
    },
    {
      key: "4",
      description: "Toggle 3D viewport",
      handler: () => togglePane(show3D, setShow3D),
    },
    {
      key: " ",
      description: "Play / pause cine",
      handler: () => setCineActive((v) => !v),
      disabled: !volume,
    },
    {
      key: "Escape",
      description: "Close help / blur focus",
      handler: () => {
        if (helpOpen) {
          setHelpOpen(false);
          return;
        }
        if (document.activeElement instanceof HTMLElement) document.activeElement.blur();
      },
    },
    {
      key: "?",
      description: "Show this help overlay",
      handler: () => setHelpOpen((v) => !v),
    },
    {
      key: "/",
      description: "Show this help overlay",
      handler: () => setHelpOpen((v) => !v),
    },
    {
      key: "l",
      description: "Toggle Lens probe",
      handler: () => setActiveTool((cur) => (cur === "measure-lens" ? null : "measure-lens")),
      disabled: !volume,
    },
    {
      key: "s",
      description: "Toggle click-to-segment (MedSAM)",
      handler: () => setActiveTool((cur) => (cur === "segment" ? null : "segment")),
      disabled: !volume,
    },
  ];

  useHotkeys(hotkeyBindings);

  const helpSections: HotkeyHelpSection[] = [
    {
      title: "Navigation",
      bindings: hotkeyBindings.filter((b) =>
        ["ArrowUp", "ArrowDown", "PageUp", "PageDown", " "].includes(b.key),
      ),
    },
    {
      title: "Zoom & pan",
      bindings: hotkeyBindings.filter((b) => ["+", "=", "-", "0"].includes(b.key)),
    },
    {
      title: "Window / Level",
      bindings: hotkeyBindings.filter((b) =>
        ["ArrowLeft", "ArrowRight", "[", "]", "w", "i"].includes(b.key),
      ),
    },
    {
      title: "Orientation",
      bindings: hotkeyBindings.filter((b) => ["r", "f"].includes(b.key)),
    },
    {
      title: "Layout",
      bindings: hotkeyBindings.filter((b) => ["1", "2", "3", "4"].includes(b.key)),
    },
    {
      title: "General",
      bindings: hotkeyBindings.filter((b) => ["Escape", "?", "/"].includes(b.key)),
    },
  ];
  const persistableState = useMemo<ViewportStateBlob>(
    () => ({
      showAxial,
      showSagittal,
      showCoronal,
      show3D,
      activeTool,
      mprCrosshair,
      vrPreset,
      vrBlend,
      vrOpacity,
      vrSample,
      vrShade,
      sidebarOpen,
    }),
    [
      showAxial,
      showSagittal,
      showCoronal,
      show3D,
      activeTool,
      mprCrosshair,
      vrPreset,
      vrBlend,
      vrOpacity,
      vrSample,
      vrShade,
      sidebarOpen,
    ],
  );
  useViewportState(params.id, persistableState, {
    debounceMs: 1000,
    enabled: !!user,
    onRestore: (s) => {
      if (typeof s.showAxial === "boolean") setShowAxial(s.showAxial);
      if (typeof s.showSagittal === "boolean") setShowSagittal(s.showSagittal);
      if (typeof s.showCoronal === "boolean") setShowCoronal(s.showCoronal);
      if (typeof s.show3D === "boolean") setShow3D(s.show3D);
      if (typeof s.sidebarOpen === "boolean") setSidebarOpen(s.sidebarOpen);
      if (typeof s.activeTool === "string") setActiveTool(s.activeTool as Tool);
      if (Array.isArray(s.mprCrosshair) && s.mprCrosshair.length === 3) {
        const c = s.mprCrosshair as [number, number, number];
        setMprCrosshair(c);
        mprRef.current?.setCrosshair(c);
      }
      if (typeof s.vrPreset === "string") setVrPreset(s.vrPreset as ColorPreset);
      if (typeof s.vrBlend === "string") setVrBlend(s.vrBlend as BlendMode);
      if (typeof s.vrOpacity === "number") setVrOpacity(s.vrOpacity);
      if (typeof s.vrSample === "number") setVrSample(s.vrSample);
      if (typeof s.vrShade === "boolean") setVrShade(s.vrShade);
    },
  });

  // Quick-highlight macros: bundle preset + HU range + crop reset
  // into one click. The radiologist gets a "I want to see liver
  // parenchyma right now" button instead of having to dial preset,
  // window, and crop separately. Each macro can also clear the
  // overrides it doesn't touch so a previous macro doesn't bleed
  // into the next pick.
  // Each macro carries its translation key (resolved via ``tv()``)
  // alongside the preset + HU range. The key lookup keeps the
  // English UI from leaking Italian copy and vice versa; the macro
  // ids stay locale-agnostic so persisted-state restores match
  // regardless of the user's current locale.
  const HIGHLIGHT_MACROS: Array<{
    id: string;
    labelKey:
      | "vrHighlightLiver"
      | "vrHighlightLiverVessels"
      | "vrHighlightKidney"
      | "vrHighlightPancreas"
      | "vrHighlightSpleen"
      | "vrHighlightHeart"
      | "vrHighlightLung"
      | "vrHighlightAngio"
      | "vrHighlightBone"
      | "vrHighlightSoftTissue";
    preset: ColorPreset;
    range: [number, number];
  }> = [
    { id: "liver", labelKey: "vrHighlightLiver", preset: "ct-liver", range: [30, 200] },
    {
      id: "liver-vessels",
      labelKey: "vrHighlightLiverVessels",
      preset: "ct-liver-vessels",
      range: [60, 300],
    },
    { id: "kidney", labelKey: "vrHighlightKidney", preset: "ct-kidney", range: [20, 250] },
    {
      id: "pancreas",
      labelKey: "vrHighlightPancreas",
      preset: "ct-pancreas",
      range: [20, 150],
    },
    { id: "spleen", labelKey: "vrHighlightSpleen", preset: "ct-spleen", range: [30, 200] },
    { id: "cardiac", labelKey: "vrHighlightHeart", preset: "ct-cardiac", range: [30, 400] },
    { id: "lung", labelKey: "vrHighlightLung", preset: "ct-lung", range: [-1000, -300] },
    {
      id: "angio",
      labelKey: "vrHighlightAngio",
      preset: "ct-angio",
      range: [100, 600],
    },
    { id: "bone", labelKey: "vrHighlightBone", preset: "ct-bone", range: [200, 1500] },
    {
      id: "soft-tissue",
      labelKey: "vrHighlightSoftTissue",
      preset: "ct-soft-tissue",
      range: [-100, 300],
    },
  ];
  const applyHighlight = (m: (typeof HIGHLIGHT_MACROS)[number]) => {
    setVrPreset(m.preset);
    volumeViewerRef.current?.setColorPreset(m.preset);
    setVrRangeOverride(m.range);
    // Drop unrelated overrides so the macro lands on a clean state
    // (otherwise a leftover crop or custom CTF from the previous
    // session keeps modifying what the macro means).
    setVrCustomColors(null);
    setVrCustomOpacity(null);
    setVrCropBox(null);
    setVrThreshold(0);
    volumeViewerRef.current?.setThreshold(0);
  };

  const render3DControls = () => (
    <>
      <h2>{tv("vrHighlightTitle")}</h2>
      <p className="meta" style={{ marginTop: "-0.4rem", fontSize: "0.7rem" }}>
        {tv("vrHighlightHelp")}
      </p>
      <div style={{ display: "flex", gap: "0.3rem", flexWrap: "wrap" }}>
        {HIGHLIGHT_MACROS.map((m) => (
          <button
            key={m.id}
            type="button"
            className={
              vrPreset === m.preset &&
              vrRangeOverride &&
              vrRangeOverride[0] === m.range[0] &&
              vrRangeOverride[1] === m.range[1]
                ? "viewer-btn viewer-btn--active"
                : "viewer-btn"
            }
            style={{ fontSize: "0.7rem", padding: "0.2rem 0.45rem" }}
            onClick={() => applyHighlight(m)}
            title={tv("vrHighlightTooltip", {
              preset: m.preset,
              lo: m.range[0],
              hi: m.range[1],
            })}
          >
            {tv(m.labelKey)}
          </button>
        ))}
      </div>
      <h2>Color map</h2>
      <div style={{ display: "flex", gap: "0.3rem", flexWrap: "wrap", alignItems: "center" }}>
        {(vrShowAllPresets ? ALL_PRESETS : presetsForModality(series?.modality)).map((p) => (
          <button
            key={p}
            type="button"
            style={{ fontSize: "0.7rem", padding: "0.2rem 0.45rem" }}
            className={vrPreset === p ? "viewer-btn viewer-btn--active" : "viewer-btn"}
            onClick={() => {
              setVrPreset(p);
              volumeViewerRef.current?.setColorPreset(p);
            }}
          >
            {p}
          </button>
        ))}
        <button
          type="button"
          className={vrShowAllPresets ? "viewer-btn viewer-btn--active" : "viewer-btn"}
          style={{ fontSize: "0.65rem", padding: "0.15rem 0.4rem", opacity: 0.75 }}
          onClick={() => setVrShowAllPresets(!vrShowAllPresets)}
          title={
            vrShowAllPresets
              ? "Restore the modality-tailored shortlist"
              : `Show all ${ALL_PRESETS.length} presets (CT + MR + PET + generic)`
          }
        >
          {vrShowAllPresets ? "− less" : "+ all"}
        </button>
      </div>
      <h2>Blend mode</h2>
      <div style={{ display: "flex", gap: "0.4rem", flexWrap: "wrap" }}>
        {(["composite", "mip", "minip", "average"] as BlendMode[]).map((m) => (
          <button
            key={m}
            type="button"
            className={vrBlend === m ? "viewer-btn viewer-btn--active" : "viewer-btn"}
            onClick={() => {
              setVrBlend(m);
              volumeViewerRef.current?.setBlendMode(m);
            }}
          >
            {m === "mip" ? "MIP" : m === "minip" ? "MinIP" : m}
          </button>
        ))}
      </div>
      <h2>Opacity</h2>
      <input
        type="range"
        min={-50}
        max={50}
        value={vrOpacity}
        onChange={(e) => {
          const v = Number(e.target.value);
          setVrOpacity(v);
          volumeViewerRef.current?.setOpacityShift(v);
        }}
      />
      <h2 style={{ marginTop: "0.5rem" }}>{tv("vrThresholdTitle")}</h2>
      <p className="meta" style={{ marginTop: "-0.4rem", fontSize: "0.7rem" }}>
        {tv("vrThresholdHelp", { value: vrThreshold.toFixed(2) })}
      </p>
      <input
        type="range"
        min={-0.5}
        max={0.5}
        step={0.01}
        value={vrThreshold}
        onChange={(e) => {
          const v = Number(e.target.value);
          setVrThreshold(v);
          volumeViewerRef.current?.setThreshold(v);
        }}
      />
      {volume && (
        <Vr3DRangeControl
          fullRange={volume.range}
          value={vrRangeOverride}
          onChange={setVrRangeOverride}
        />
      )}
      <h2 style={{ marginTop: "0.5rem" }}>{tv("vrTfEditorTitle")}</h2>
      <p className="meta" style={{ marginTop: "-0.4rem", fontSize: "0.7rem" }}>
        {tv("vrTfEditorHelp")}
      </p>
      {volume && (
        <TransferFunctionEditor
          scalars={volume.scalars}
          stops={vrCustomOpacity}
          onChange={(stops) => setVrCustomOpacity(stops)}
          onReset={() => setVrCustomOpacity(null)}
        />
      )}
      <Vr3DColorEditor value={vrCustomColors} onChange={setVrCustomColors} />
      {volume && <Vr3DCropBox value={vrCropBox} onChange={setVrCropBox} />}
      <h2>Ray quality</h2>
      <input
        type="range"
        min={0.2}
        max={3}
        step={0.1}
        value={vrSample}
        onChange={(e) => {
          const v = Number(e.target.value);
          setVrSample(v);
          volumeViewerRef.current?.setSampleDistance(v);
        }}
      />
      <h2>Lighting</h2>
      <div style={{ display: "flex", gap: "0.5rem", alignItems: "center", flexWrap: "wrap" }}>
        <button
          type="button"
          className={vrShade ? "viewer-btn viewer-btn--active" : "viewer-btn"}
          onClick={() => {
            setVrShade(!vrShade);
            volumeViewerRef.current?.setShade(!vrShade);
          }}
        >
          {vrShade ? "Shading ON" : "Shading OFF"}
        </button>
        <button
          type="button"
          className={vrCinematic ? "viewer-btn viewer-btn--active" : "viewer-btn"}
          onClick={() => {
            const next = !vrCinematic;
            setVrCinematic(next);
            volumeViewerRef.current?.setCinematic(next);
            // Cinematic mode implies shading — force it on so gradient-
            // opacity + specular reads correctly.
            if (next && !vrShade) {
              setVrShade(true);
              volumeViewerRef.current?.setShade(true);
            }
          }}
          title="Gradient-opacity + stronger specular — approximation of cinematic rendering"
        >
          {vrCinematic ? "Cinematic ON" : "Cinematic OFF"}
        </button>
        <button
          type="button"
          className="viewer-btn"
          onClick={() => volumeViewerRef.current?.resetCamera()}
        >
          Reset camera
        </button>
      </div>
      <h2>Camera presets</h2>
      <div style={{ display: "flex", gap: "0.3rem", flexWrap: "wrap" }}>
        {["anterior", "posterior", "left", "right", "superior", "inferior"].map((p) => (
          <button
            key={p}
            type="button"
            className="viewer-btn"
            style={{ fontSize: "0.65rem", padding: "0.15rem 0.4rem" }}
            onClick={() => volumeViewerRef.current?.setCameraPreset(p)}
          >
            {p}
          </button>
        ))}
      </div>
      <h2>Background</h2>
      <input
        type="color"
        value="#0a0d14"
        onChange={(e) => {
          const c = e.target.value;
          const r = Number.parseInt(c.slice(1, 3), 16) / 255;
          const g = Number.parseInt(c.slice(3, 5), 16) / 255;
          const b = Number.parseInt(c.slice(5, 7), 16) / 255;
          volumeViewerRef.current?.setBackground(r, g, b);
        }}
        style={{ width: 40, height: 24, padding: 0, border: "1px solid #444" }}
      />
      {volume && (
        <SegmentationControls
          volume={volume}
          onMaskChange={(mask) => {
            volumeViewerRef.current?.setSegmentationMask(
              mask ? { data: mask.data, color: mask.color } : null,
            );
          }}
        />
      )}
      {volume && series && (
        <FusionControls
          studyId={series.study_id}
          primarySeriesId={series.id}
          autoFuseModality={autoFuseModality}
          onFusionChange={(fusion) => {
            // Dual-route: vtk legacy 3D viewer keeps its fusion
            // actor pipeline; the Cornerstone MPR layout receives a
            // ``VolumeData`` shape (no colormap/opacity, those are
            // applied in the layout's TF setup).
            volumeViewerRef.current?.setFusionVolume(fusion);
            setCsFusionVolume(
              fusion
                ? {
                    dimensions: fusion.dimensions,
                    spacing: fusion.spacing,
                    scalars: fusion.scalars,
                    range: fusion.range,
                  }
                : null,
            );
          }}
          onOpacityChange={(v) => volumeViewerRef.current?.setFusionOpacity(v)}
          onColormapChange={(c) => volumeViewerRef.current?.setFusionColormap(c)}
          onThresholdChange={(v) => volumeViewerRef.current?.setFusionThreshold(v)}
          onActiveSeriesIdChange={(id) => setCsFusionSeriesId(id)}
          onLoadingChange={(info) => {
            setFusionLoading(info.state === "idle" ? null : info);
          }}
        />
      )}
      {volume && (segBusy || segError) && (
        <output
          className="card"
          style={{
            display: "block",
            marginBottom: "0.5rem",
            fontSize: "0.75rem",
            color: segError ? "var(--bv-danger, #d9534f)" : "var(--bv-fg-muted, #888)",
          }}
        >
          {segBusy ? tv("segmentRunning") : segError}
        </output>
      )}
      {volume && (
        <SegmentationImporter
          seriesId={params.id}
          reloadSignal={segReload}
          onMaskLoaded={(data, color) => {
            volumeViewerRef.current?.setSegmentationMask({ data, color });
          }}
          onMaskCleared={() => volumeViewerRef.current?.setSegmentationMask(null)}
        />
      )}
      {volume && (
        <>
          <h2>Export STL</h2>
          <div className="card" style={{ display: "flex", flexDirection: "column", gap: "0.4rem" }}>
            <label className="meta" style={{ fontSize: "0.7rem" }}>
              Iso value: {stlIso ?? Math.round((volume.range[0] + volume.range[1]) / 2)}
              <input
                type="range"
                min={Math.floor(volume.range[0])}
                max={Math.ceil(volume.range[1])}
                value={stlIso ?? Math.round((volume.range[0] + volume.range[1]) / 2)}
                onChange={(e) => setStlIso(Number(e.target.value))}
              />
            </label>
            <button
              type="button"
              className="viewer-btn"
              disabled={stlBusy}
              onClick={async () => {
                if (!volume) return;
                setStlBusy(true);
                setStlStatus("extracting isosurface…");
                try {
                  const iso = stlIso ?? (volume.range[0] + volume.range[1]) / 2;
                  const res = await extractAndDownloadStl(
                    {
                      scalars: volume.scalars,
                      dimensions: volume.dimensions,
                      spacing: volume.spacing,
                      isoValue: iso,
                    },
                    `series-${params.id}-iso${Math.round(iso)}.stl`,
                  );
                  setStlStatus(
                    `exported ${res.triangleCount.toLocaleString()} triangles · ` +
                      `${(res.bytes / 1024).toFixed(0)} KiB`,
                  );
                } catch (e) {
                  setStlStatus(`failed: ${e instanceof Error ? e.message : String(e)}`);
                } finally {
                  setStlBusy(false);
                }
              }}
              style={{ opacity: stlBusy ? 0.6 : 1 }}
            >
              {stlBusy ? "Working…" : "Export isosurface as STL"}
            </button>
            {stlStatus && (
              <div className="meta" style={{ fontSize: "0.65rem" }}>
                {stlStatus}
              </div>
            )}
          </div>
        </>
      )}
      {volume && (
        <FiducialPanel
          fiducials={fiducials}
          crosshair={mprCrosshair}
          spacing={volume.spacing}
          onAdd={addFiducial}
          onRemove={removeFiducial}
          onRename={renameFiducial}
          onClear={clearFiducials}
          onJumpTo={(pos) => {
            setMprCrosshair(pos);
            mprRef.current?.setCrosshair(pos);
          }}
        />
      )}
    </>
  );

  // Reset the instrumentation probe when the study/series changes so an
  // audit never reads stale panes/notes from a previous volume. params.id
  // is an intentional re-fire trigger, not read in the body.
  // biome-ignore lint/correctness/useExhaustiveDependencies: re-fire on study switch
  useEffect(() => {
    if (viewerDebug) resetViewerProbe("series");
  }, [viewerDebug, params.id]);

  // Populate window.__viewer at 1 Hz (the W/L lives on the imperative
  // handle as a mutable, not React state, so polling keeps it fresh).
  useEffect(() => {
    if (!viewerDebug) return;
    const push = () => {
      const h = mprRef.current;
      const voi =
        h && h.wc != null && h.ww != null
          ? { lower: h.wc - h.ww / 2, upper: h.wc + h.ww / 2 }
          : null;
      const invert = h?.invert ?? undefined;
      updateViewerProbe({
        surface: "series",
        identity: {
          seriesId: params.id,
          studyId: series?.study_id ?? undefined,
          patientId: studyPatientId ?? undefined,
          modality: series?.modality ?? undefined,
        },
        volume: volume
          ? {
              dims: volume.dimensions as [number, number, number],
              hasGeometry: Boolean(
                (series as { frame_of_reference_uid?: string } | null)?.frame_of_reference_uid,
              ),
            }
          : null,
        panes: {
          axial: { visible: true, voi, invert },
          sagittal: { visible: showSagittal, voi, invert },
          coronal: { visible: showCoronal, voi, invert },
          "3d": { visible: show3D, voi: null },
          mip: { visible: showMip, voi, invert },
          oblique: { visible: showOblique, voi, invert },
        },
        activeTool: activeTool ?? null,
        layout: layout ? String(layout) : protocolId,
        measurementCount: allMeasurements.length,
        undoDepth: historyRef.current.undoDepth(),
        redoDepth: historyRef.current.redoDepth(),
        error: err,
      });
    };
    push();
    const t = setInterval(push, 1000);
    return () => clearInterval(t);
  }, [
    viewerDebug,
    params.id,
    series,
    studyPatientId,
    volume,
    activeTool,
    layout,
    protocolId,
    allMeasurements.length,
    err,
    showSagittal,
    showCoronal,
    show3D,
    showMip,
    showOblique,
  ]);

  return (
    <BrowserSupportGate>
      <div
        ref={layoutRef}
        className={`viewer-layout${sidebarOpen ? "" : " viewer-layout--no-sidebar"}${isMobile ? " viewer-layout--mobile" : ""}`}
        style={{ margin: 0, maxWidth: "none" }}
      >
        <div className="viewer-layout__canvas">
          <ViewerIdentityBanner patient={patient} study={study} />
          <div className="viewer-layout__canvas-inner" data-testid="viewer-canvas">
            {fusionLoading && (
              <div
                aria-live="polite"
                style={{
                  position: "absolute",
                  top: 8,
                  left: "50%",
                  transform: "translateX(-50%)",
                  zIndex: 10,
                  background: "rgba(15,20,30,0.85)",
                  color: "#e6ecf3",
                  fontSize: "0.78rem",
                  padding: "0.4rem 0.7rem",
                  borderRadius: 6,
                  border: "1px solid #2a2f3b",
                  boxShadow: "0 2px 8px rgba(0,0,0,0.4)",
                  minWidth: 240,
                  fontFamily: "ui-monospace, monospace",
                  pointerEvents: "none",
                  display: "flex",
                  flexDirection: "column",
                  gap: 4,
                }}
              >
                <div style={{ display: "flex", justifyContent: "space-between", gap: 12 }}>
                  <span>
                    {fusionLoading.state === "decoding"
                      ? tv("decodingFusionOverlay")
                      : tv("loadingFusionOverlay")}
                  </span>
                  {fusionLoading.state === "downloading" && fusionLoading.loaded > 0 && (
                    <span style={{ color: "#94a3b8" }}>
                      {/* MB only, no percentage. ``Content-Length`` is
                        the compressed transport size when the
                        backend serves gzip/brotli; computing a ratio
                        against the decompressed ``loaded`` count
                        produces values past 100% and gaslights the
                        user — drop it and let the indeterminate bar
                        below convey "still working". */}
                      {(fusionLoading.loaded / 1024 / 1024).toFixed(1)} MB
                    </span>
                  )}
                </div>
                <div
                  // Decorative indeterminate bar: the parent already
                  // carries ``aria-live="polite"`` with the textual
                  // state, and there is no meaningful ``aria-valuenow``
                  // to surface (the % was wrong — see comment above).
                  // Treat it as presentation so screen readers don't
                  // announce an empty progressbar.
                  role="presentation"
                  style={{
                    height: 4,
                    borderRadius: 2,
                    background: "rgba(148,163,184,0.25)",
                    overflow: "hidden",
                    position: "relative",
                  }}
                >
                  <div
                    style={{
                      position: "absolute",
                      top: 0,
                      height: "100%",
                      width: "30%",
                      background: "#e96b1f",
                      animation: "bvFusionIndeterminate 1.1s ease-in-out infinite",
                    }}
                  />
                </div>
              </div>
            )}
            {volumeLoading && volume && loadProgress ? (
              // Non-blocking full-res progress: the low-res preview is already
              // visible underneath; this badge proves the HD volume is still
              // downloading AND advancing (real % + MB, not a bare spinner), and
              // flags a stalled transfer so a hung load is evident.
              <div
                aria-live="polite"
                style={{
                  position: "absolute",
                  bottom: 8,
                  right: 8,
                  zIndex: 10,
                  background: "rgba(15,20,30,0.82)",
                  color: "#e6ecf3",
                  fontSize: "0.72rem",
                  padding: "0.35rem 0.6rem",
                  borderRadius: 6,
                  border: "1px solid #2a2f3b",
                  fontFamily: "ui-monospace, monospace",
                  pointerEvents: "none",
                  minWidth: 190,
                  display: "flex",
                  flexDirection: "column",
                  gap: 4,
                }}
              >
                {(() => {
                  const total = loadProgress.total;
                  const pct =
                    total > loadProgress.loaded
                      ? Math.min(99, Math.round((loadProgress.loaded / total) * 100))
                      : total > 0
                        ? 99
                        : null;
                  const mb = (loadProgress.loaded / 1_048_576).toFixed(1);
                  const stalled = elapsedSec - loadProgress.elapsed > 6;
                  return (
                    <>
                      <div style={{ display: "flex", justifyContent: "space-between", gap: 10 }}>
                        <span>Full-res HD</span>
                        <span style={{ color: stalled ? "#f0a868" : "#94a3b8" }}>
                          {pct != null ? `${pct}% · ` : ""}
                          {mb} MB
                          {stalled ? " · rete lenta…" : ""}
                        </span>
                      </div>
                      <div
                        role="presentation"
                        style={{
                          height: 4,
                          borderRadius: 2,
                          background: "rgba(148,163,184,0.25)",
                          overflow: "hidden",
                        }}
                      >
                        <div
                          style={{
                            height: "100%",
                            width: pct != null ? `${pct}%` : "30%",
                            background: stalled ? "#f0a868" : "#e96b1f",
                            transition: "width 0.2s ease",
                            animation:
                              pct == null
                                ? "bvFusionIndeterminate 1.1s ease-in-out infinite"
                                : "none",
                          }}
                        />
                      </div>
                    </>
                  );
                })()}
              </div>
            ) : null}
            {volumeLoading && !volume ? (
              // Big blocking spinner ONLY while there's nothing to show yet.
              // Once the progressive low-res preview has populated ``volume``
              // we render the panes underneath instead (the preview is
              // visible immediately) and let the full-res stream in + swap.
              <div
                style={{
                  display: "flex",
                  flexDirection: "column",
                  alignItems: "center",
                  justifyContent: "center",
                  height: "100%",
                  color: "#bbb",
                  gap: "0.8rem",
                }}
              >
                <div
                  style={{
                    width: 44,
                    height: 44,
                    border: "3px solid #333",
                    borderTop: "3px solid #e96b1f",
                    borderRadius: "50%",
                    animation: "spin 1s linear infinite",
                  }}
                />
                {(() => {
                  const instCount = series?.received_instance_count ?? 0;
                  const estMiB = instCount > 0 ? (instCount * 512 * 512 * 4) / 1_048_576 : 0;
                  // Real % from the UNCOMPRESSED total (header dims, fed into
                  // loadProgress.total) — never the gzip Content-Length. The
                  // download phase is determinate (bar fills, % climbs); only
                  // the brief post-download CPU "building" phase is
                  // indeterminate, and even there the elapsed counter keeps
                  // ticking so a frozen build is still visible.
                  const pct =
                    loadProgress && loadProgress.total > loadProgress.loaded
                      ? Math.min(99, Math.round((loadProgress.loaded / loadProgress.total) * 100))
                      : loadProgress
                        ? 99
                        : null;
                  const stalled = loadProgress != null && elapsedSec - loadProgress.elapsed > 6;
                  return (
                    <>
                      <div style={{ fontSize: "0.95rem" }}>
                        {loadProgress
                          ? `Downloading volume… ${pct != null ? `${pct}% · ` : ""}${(loadProgress.loaded / 1_048_576).toFixed(1)} MB${stalled ? " · rete lenta…" : ""}`
                          : `Building volume from ${instCount} slices… ${elapsedSec}s`}
                      </div>
                      <div
                        style={{
                          width: 260,
                          height: 6,
                          background: "#222",
                          borderRadius: 3,
                          overflow: "hidden",
                          position: "relative",
                        }}
                      >
                        {pct != null ? (
                          <div
                            style={{
                              width: `${pct}%`,
                              height: "100%",
                              background: stalled ? "#f0a868" : "#e96b1f",
                              borderRadius: 3,
                              transition: "width 0.2s ease",
                            }}
                          />
                        ) : (
                          <div
                            style={{
                              width: "100%",
                              height: "100%",
                              background:
                                "linear-gradient(90deg, transparent 0%, #e96b1f 50%, transparent 100%)",
                              animation: "pulse-bar 1.5s ease-in-out infinite",
                              borderRadius: 3,
                            }}
                          />
                        )}
                      </div>
                      <div style={{ fontSize: "0.75rem", color: "#666", fontFamily: "monospace" }}>
                        {loadProgress
                          ? `${(loadProgress.loaded / 1_048_576).toFixed(1)} MB · ${loadProgress.elapsed.toFixed(0)}s`
                          : estMiB > 0
                            ? `estimated ~${estMiB.toFixed(0)} MB · ${elapsedSec}s`
                            : `${elapsedSec}s`}
                      </div>
                    </>
                  );
                })()}
              </div>
            ) : volumeUnavailable && series ? (
              // 2D fallback: this series can't be packed as a 3D volume but
              // each instance can still be rasterised individually. Useful
              // for radiographs, mammography, and any series the volume
              // worker rejected (mixed SOP class, single slice, ...).
              <Series2DViewer
                seriesId={params.id}
                sliceCount={series.received_instance_count || 1}
                caption={
                  series.series_description
                    ? `${series.modality ?? ""} · ${series.series_description}`.trim()
                    : (series.modality ?? undefined)
                }
              />
            ) : err ? (
              <div
                data-testid="viewer-error"
                style={{
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                  height: "100%",
                  padding: "2rem",
                }}
              >
                <div
                  style={{
                    maxWidth: 560,
                    background: "#1a1d25",
                    border: "1px solid #2a2f3b",
                    borderRadius: 10,
                    padding: "1.5rem",
                    color: "#e6ecf3",
                  }}
                >
                  <h2 style={{ marginTop: 0, color: "#f88", fontSize: "1.05rem" }}>
                    {tv("volumeErrorTitle")}
                  </h2>
                  <p style={{ color: "#c5cdd9", fontSize: "0.92rem", marginBottom: "1rem" }}>
                    {err}
                  </p>
                  {series?.study_id && (
                    <Link
                      href={`/studies/${series.study_id}`}
                      style={{
                        display: "inline-block",
                        padding: "0.45rem 0.85rem",
                        background: "#e96b1f",
                        color: "#fff",
                        borderRadius: 6,
                        fontSize: "0.88rem",
                        textDecoration: "none",
                      }}
                    >
                      {tv("backToStudy")}
                    </Link>
                  )}
                </div>
              </div>
            ) : volume ? (
              <CornerstoneMPRLayout
                ref={mprRef}
                volume={volume}
                showAxial={true}
                showSagittal={isMobile ? false : showSagittal}
                showCoronal={isMobile ? false : showCoronal}
                show3D={isMobile ? false : show3D}
                showMip={isMobile ? false : showMip}
                layout={isMobile ? "1x1" : layout}
                showOblique={isMobile ? false : showOblique}
                onCrosshairChange={setMprCrosshair}
                activeTool={activeTool ?? undefined}
                onActiveToolChange={(t) => setActiveTool(t as Tool | null)}
                seriesDescription={series?.series_description ?? undefined}
                seriesId={params.id}
                stackIndex={selectedStackIndex}
                volumeViewerRef={volumeViewerRef}
                onMeasurementsChange={setAllMeasurements}
                measurements={allMeasurements}
                markerFade={markerFade}
                modality={series?.modality ?? null}
                suggestedVoi={suggestedFromDicom(series)}
                customOpacityStops={vrCustomOpacity}
                customRange={vrRangeOverride}
                customColorStops={vrCustomColors}
                cropBox={vrCropBox}
                fusionVolume={csFusionVolume}
                fusionSeriesId={csFusionSeriesId}
                fusionExpected={fusionParam != null}
                fusionFailed={csFusionFailed}
                fusionModality={csFusionSeriesMeta?.modality ?? null}
                suvFactorBw={pickActiveSuvFactor(petDisplayMeta, suvVariant)}
                petColormap={petColormap}
                petSuvHide={suvHide}
                studyId={series?.study_id ?? null}
                overlayMarkers={showAiOverlay ? overlayMarkers : []}
                focusedMarkerId={showAiOverlay ? focusedMarkerId : null}
                onOverlayMarkerClick={(id) => focusMarker(id)}
                onLensPin={async ({ axis, centerIjk, radiusMm, isPet, suvFactor, stats }) => {
                  if (!studyPatientId || !series) return;
                  // Persist as a ``measurement.ellipse`` with the AABB of
                  // the disc projected onto the active slice. ``computed``
                  // carries source="lens-probe", radius, and the client-
                  // side stats; the server-confirmed values land via the
                  // subsequent PATCH.
                  const halfI = Math.max(1, Math.round(radiusMm));
                  const halfJ = Math.max(1, Math.round(radiusMm));
                  const halfK = 0;
                  const min_ijk: [number, number, number] =
                    axis === "axial"
                      ? [centerIjk[0] - halfI, centerIjk[1] - halfJ, centerIjk[2]]
                      : axis === "sagittal"
                        ? [centerIjk[0], centerIjk[1] - halfJ, centerIjk[2] - halfK]
                        : [centerIjk[0] - halfI, centerIjk[1], centerIjk[2] - halfK];
                  const max_ijk: [number, number, number] =
                    axis === "axial"
                      ? [centerIjk[0] + halfI, centerIjk[1] + halfJ, centerIjk[2]]
                      : axis === "sagittal"
                        ? [centerIjk[0], centerIjk[1] + halfJ, centerIjk[2] + halfK]
                        : [centerIjk[0] + halfI, centerIjk[1], centerIjk[2] + halfK];
                  const computedBase: Record<string, unknown> = {
                    radius_mm: radiusMm,
                    voxel_count: stats.count,
                    mean: stats.mean,
                    std: stats.std,
                    min: stats.min,
                    max: stats.max,
                    source: "lens-probe",
                    pending: true,
                  };
                  if (isPet && suvFactor && suvFactor > 0) {
                    computedBase.suv_mean = stats.mean * suvFactor;
                    computedBase.suv_max = stats.max * suvFactor;
                    computedBase.suv_variant = suvVariant;
                  }
                  let created: Marker;
                  try {
                    created = await markersApi.create(studyPatientId, {
                      target_kind: "series",
                      target_id: params.id,
                      kind: "measurement.ellipse",
                      geometry: { axis, points: [min_ijk, max_ijk] },
                      computed: computedBase,
                      body: `Lens ${radiusMm.toFixed(1)} mm`,
                    });
                  } catch (err) {
                    console.error("lens-pin create failed", err);
                    return;
                  }
                  // Server-confirmed roi-stats round-trip → patch the
                  // marker so the report-composer and the SUV columns
                  // get the authoritative numbers.
                  try {
                    const roi = await request<{
                      voxel_count: number;
                      mean: number;
                      std: number;
                      min: number;
                      max: number;
                      peak_1cm3: number | null;
                      suv_mean: number | null;
                      suv_sd: number | null;
                      suv_max: number | null;
                      suv_peak: number | null;
                      suv_variant_used: string | null;
                    }>(`/api/series/${params.id}/roi-stats`, {
                      method: "POST",
                      json: {
                        kind: "sphere",
                        center_ijk: centerIjk,
                        radius_mm: radiusMm,
                        suv_variant: isPet ? suvVariant : undefined,
                      },
                    });
                    const patched: Record<string, unknown> = {
                      ...computedBase,
                      pending: false,
                      voxel_count: roi.voxel_count,
                      mean: roi.mean,
                      std: roi.std,
                      min: roi.min,
                      max: roi.max,
                    };
                    if (roi.suv_mean != null) patched.suv_mean = roi.suv_mean;
                    if (roi.suv_max != null) patched.suv_max = roi.suv_max;
                    if (roi.suv_peak != null) patched.suv_peak = roi.suv_peak;
                    if (roi.suv_sd != null) patched.suv_sd = roi.suv_sd;
                    if (roi.suv_variant_used) patched.suv_variant = roi.suv_variant_used;
                    await markersApi.update(created.id, { computed: patched });
                  } catch (err) {
                    // Non-fatal: the client-side stats are usable; the
                    // ``pending`` flag stays true so the report composer
                    // can flag it.
                    console.warn("lens-pin server confirm failed", err);
                  }
                }}
                onSegmentPredict={async ({ axis, sliceIdx, point }) => {
                  // MedSAM-2 click-to-segment (task 3af7a33d). Persist under a
                  // generated label so the backend embeds the 2D mask into a
                  // full-volume mask; then fetch + display it via the existing
                  // setSegmentationMask path (shown in the 3D pane, consistent
                  // with how imported/auto segmentations render).
                  if (segBusy) return; // one prediction at a time
                  const label = `interactive-${Date.now()}`;
                  setSegBusy(true);
                  setSegError(null);
                  try {
                    const res = await segmentationsApi.interactivePredict(params.id, {
                      axis,
                      slice_idx: sliceIdx,
                      points: [point],
                      label,
                    });
                    const persisted = res.persisted_label ?? label;
                    const bytes = await segmentationsApi.fetchMask(params.id, persisted);
                    volumeViewerRef.current?.setSegmentationMask({
                      data: bytes,
                      color: [1, 0.3, 0.3],
                    });
                    setSegReload((n) => n + 1); // re-list in SegmentationImporter
                    updateViewerProbe({
                      notes: [`segment predict axis=${axis} slice=${sliceIdx} -> ${persisted}`],
                    });
                  } catch (e) {
                    const msg =
                      e instanceof ApiError && e.status === 502
                        ? tv("segmentUnavailable")
                        : e instanceof ApiError && e.status === 504
                          ? tv("segmentTimeout")
                          : tv("segmentFailed");
                    setSegError(msg);
                  } finally {
                    setSegBusy(false);
                  }
                }}
              />
            ) : (
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                  height: "100%",
                  color: "#666",
                }}
              >
                loading…
              </div>
            )}
            <div
              style={{
                position: "absolute",
                bottom: 6,
                right: 6,
                zIndex: 5,
                display: "flex",
                gap: 4,
              }}
            >
              {/* Toggle the AI-annotation overlay (dashed bboxes,
                fiducials, text labels). When the panel reports zero
                non-cornerstone markers the button is dimmed but
                still clickable so the preference can be set before
                an agent writes the first annotation. */}
              <button
                type="button"
                className="viewer-chip-btn"
                onClick={toggleAiOverlay}
                aria-pressed={showAiOverlay}
                aria-label={showAiOverlay ? "Nascondi annotazioni AI" : "Mostra annotazioni AI"}
                title={
                  overlayMarkers.length === 0
                    ? "Nessuna annotazione AI da mostrare"
                    : showAiOverlay
                      ? `Nascondi ${overlayMarkers.length} annotazioni AI`
                      : `Mostra ${overlayMarkers.length} annotazioni AI`
                }
                style={{
                  opacity: overlayMarkers.length === 0 ? 0.55 : 1,
                  ...(showAiOverlay
                    ? { background: "rgba(251,146,60,0.55)", borderColor: "rgba(251,146,60,0.85)" }
                    : {}),
                }}
              >
                {showAiOverlay ? "⬚" : "⬜"}
              </button>
              {fsSupported && (
                <button
                  type="button"
                  className="viewer-chip-btn"
                  onClick={() => {
                    void toggleFs();
                  }}
                  aria-label={isFullscreen ? "Exit fullscreen" : "Enter fullscreen"}
                  title={isFullscreen ? "Exit fullscreen" : "Fullscreen"}
                >
                  {isFullscreen ? "⤢" : "⛶"}
                </button>
              )}
              <button
                type="button"
                className="viewer-chip-btn"
                onClick={() => setSidebarOpen(!sidebarOpen)}
                aria-label={sidebarOpen ? "Hide sidebar" : "Show sidebar"}
                title={sidebarOpen ? "Hide sidebar" : "Show sidebar"}
              >
                {sidebarOpen ? "»" : "«"}
              </button>
            </div>
          </div>
        </div>
        {sidebarOpen && (
          <aside className="viewer-layout__sidebar" ref={sidebarRef} data-testid="viewer-sidebar">
            {series && (
              <>
                <SidebarSectionNav
                  containerRef={sidebarScrollRef}
                  sections={[
                    { label: tv("chipStudio"), match: "study-overview" },
                    ...(displayMeta?.is_pet
                      ? [
                          {
                            label: tv("chipPet"),
                            match: "pet-info",
                            title: "SUV factor + provenance PET",
                          },
                        ]
                      : []),
                    { label: tv("chipMpr"), match: "mpr" },
                    { label: tv("chipWl"), match: "wl" },
                    { label: tv("chipLayout"), match: "layout" },
                    { label: tv("chipTools"), match: "tools" },
                    { label: tv("chipCine"), match: "cine" },
                    { label: tv("chip3d"), match: "3d" },
                    { label: tv("chipAnnot"), match: "marker-list" },
                    { label: tv("chipFindings"), match: "findings" },
                    { label: tv("chipReport"), match: "report-composer" },
                    { label: tv("chipHelp"), match: "shortcuts" },
                  ]}
                />
                <div ref={sidebarScrollRef} className="viewer-layout__sidebar-scroll">
                  {/*
                The Studio chip in the section nav scrolls here (via
                data-section="study-overview"). Wrapping the back-links
                + heading + description card together keeps the
                navigation breadcrumbs visible after the click, instead
                of leaving them hidden above the scroll origin.
              */}
                  <div data-section="study-overview">
                    <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
                      {studyPatientId && (
                        <Link
                          href={`/patients/${studyPatientId}`}
                          style={{ color: "#e96b1f", fontSize: "0.82rem" }}
                        >
                          {tv("backToFascicolo")}
                        </Link>
                      )}
                      <Link
                        href={`/studies/${series.study_id}`}
                        style={{ color: "#9cb9e6", fontSize: "0.82rem" }}
                      >
                        {tv("backToStudyShort")}
                      </Link>
                    </div>
                    <h2>
                      {series.modality ?? "series"}
                      {series.body_part_examined ? ` · ${series.body_part_examined}` : ""}
                    </h2>
                    <div className="card">
                      <div>{series.series_description ?? "(no description)"}</div>
                      <div className="meta" style={{ marginTop: "0.5rem" }}>
                        <span className="badges" style={{ marginLeft: 0 }}>
                          <span
                            className="badge"
                            style={{
                              background: "var(--bv-accent, #e96b1f)",
                              color: "#fff",
                            }}
                          >
                            primary
                          </span>
                          {series.modality && <span className="badge">{series.modality}</span>}
                        </span>{" "}
                        #{series.series_number ?? "?"} · {series.received_instance_count} instance
                        {series.received_instance_count === 1 ? "" : "s"}
                      </div>
                    </div>
                    {csFusionSeriesMeta && (
                      <div
                        className="card"
                        style={{
                          borderColor: "var(--bv-accent, #e96b1f)",
                          borderStyle: "dashed",
                        }}
                      >
                        <div>{csFusionSeriesMeta.series_description ?? "(no description)"}</div>
                        <div className="meta" style={{ marginTop: "0.5rem" }}>
                          <span className="badges" style={{ marginLeft: 0 }}>
                            <span
                              className="badge"
                              style={{
                                background: "transparent",
                                color: "var(--bv-accent, #e96b1f)",
                                border: "1px solid var(--bv-accent, #e96b1f)",
                              }}
                            >
                              fusion overlay
                            </span>
                            {csFusionSeriesMeta.modality && (
                              <span className="badge">{csFusionSeriesMeta.modality}</span>
                            )}
                          </span>{" "}
                          #{csFusionSeriesMeta.series_number ?? "?"} ·{" "}
                          {csFusionSeriesMeta.received_instance_count} instance
                          {csFusionSeriesMeta.received_instance_count === 1 ? "" : "s"}
                        </div>
                      </div>
                    )}
                  </div>

                  {/* 4D dynamic warning (spec §9). Backend keeps only
                  the first temporal frame; the user should know if
                  they're looking at a slice of a dynamic series. */}
                  {petDisplayMeta?.is_dynamic_4d && (
                    <div
                      className="card"
                      style={{
                        padding: "0.5rem 0.75rem",
                        marginTop: "0.5rem",
                        fontSize: "0.78rem",
                        background: "rgba(255, 165, 0, 0.12)",
                        borderLeft: "3px solid #f59e0b",
                      }}
                    >
                      <strong style={{ fontSize: "0.82rem" }}>Dynamic 4D study</strong>
                      <p className="meta" style={{ margin: "0.2rem 0 0", fontSize: "0.7rem" }}>
                        Series carries {petDisplayMeta.n_time_frames} temporal frames. The viewer is
                        showing the first frame only; dynamic time-activity navigation is not yet
                        wired in this build.
                      </p>
                    </div>
                  )}

                  {/* FrameOfReferenceUID mismatch warning (spec §1.2).
                  Fusion is offered without a registration step only
                  when both series share the same FoR. */}
                  {displayMeta?.frame_of_reference_uid &&
                    csFusionDisplayMeta?.frame_of_reference_uid &&
                    displayMeta.frame_of_reference_uid !==
                      csFusionDisplayMeta.frame_of_reference_uid && (
                      <div
                        className="card"
                        style={{
                          padding: "0.5rem 0.75rem",
                          marginTop: "0.5rem",
                          fontSize: "0.78rem",
                          background: "rgba(220, 38, 38, 0.12)",
                          borderLeft: "3px solid #dc2626",
                        }}
                      >
                        <strong style={{ fontSize: "0.82rem" }}>
                          Coregistration not guaranteed
                        </strong>
                        <p className="meta" style={{ margin: "0.2rem 0 0", fontSize: "0.7rem" }}>
                          Primary and fusion series have different FrameOfReferenceUIDs. The overlay
                          is rendered assuming a shared coordinate system, which may misalign
                          anatomy by several voxels. Verify before reading lesion locations.
                        </p>
                      </div>
                    )}

                  {/* Render the PET section as soon as we know the
                  user will look at PET (primary is PT OR we asked for
                  a fusion overlay) — even before ``petDisplayMeta``
                  resolves. Without this preemptive mount the right
                  rail visibly grows mid-load when the metadata fetch
                  lands a few hundred ms after the series fetch:
                  the user perceives sections turning "on and off".
                  We swap in a small loading placeholder while the
                  metadata is in flight, so the section's position +
                  height stay stable. */}
                  {(series?.modality === "PT" || csFusionSeriesId !== null || petDisplayMeta) && (
                    <SidebarSection
                      sectionId="pet-info"
                      title="PET"
                      defaultOpen={true}
                      hint={
                        petDisplayMeta
                          ? "Tracer / SUV variant / colormap / display range / threshold / EARL"
                          : "Loading PET metadata…"
                      }
                    >
                      {!petDisplayMeta && (
                        <p className="meta" style={{ fontSize: "0.78rem", marginTop: "0.4rem" }}>
                          Loading PET metadata…
                        </p>
                      )}
                      {petDisplayMeta && (
                        <>
                          <PetInfoPanel metadata={petDisplayMeta} />
                          <div
                            className="card"
                            style={{
                              padding: "0.5rem 0.75rem",
                              marginTop: "0.5rem",
                              fontSize: "0.78rem",
                            }}
                          >
                            <strong style={{ fontSize: "0.82rem" }}>SUV variant</strong>
                            <p
                              className="meta"
                              style={{ margin: "0.2rem 0 0.4rem", fontSize: "0.7rem" }}
                            >
                              Choose how the viewer normalises pixel activity for HUD and ROI stats.
                              {petDisplayMeta.tracer ? ` Tracer: ${petDisplayMeta.tracer}.` : ""}
                            </p>
                            <select
                              value={suvVariant}
                              onChange={(e) => setSuvVariant(e.target.value as SuvVariant)}
                              style={{ width: "100%", fontSize: "0.78rem" }}
                            >
                              <option value="bw" disabled={petDisplayMeta.suv_factor_bw == null}>
                                SUV-BW {petDisplayMeta.suv_factor_bw == null ? "(n/a)" : ""}
                              </option>
                              <option
                                value="lbm-janma"
                                disabled={petDisplayMeta.suv_factor_lbm_janmahasatian == null}
                              >
                                SUL · Janmahasatian (PERCIST){" "}
                                {petDisplayMeta.suv_factor_lbm_janmahasatian == null ? "(n/a)" : ""}
                              </option>
                              <option
                                value="lbm-james"
                                disabled={petDisplayMeta.suv_factor_lbm_james == null}
                              >
                                SUL · James{" "}
                                {petDisplayMeta.suv_factor_lbm_james == null ? "(n/a)" : ""}
                              </option>
                              <option
                                value="bsa-mosteller"
                                disabled={petDisplayMeta.suv_factor_bsa_mosteller == null}
                              >
                                SUV-BSA · Mosteller{" "}
                                {petDisplayMeta.suv_factor_bsa_mosteller == null ? "(n/a)" : ""}
                              </option>
                              <option
                                value="bsa-dubois"
                                disabled={petDisplayMeta.suv_factor_bsa_dubois == null}
                              >
                                SUV-BSA · Du Bois{" "}
                                {petDisplayMeta.suv_factor_bsa_dubois == null ? "(n/a)" : ""}
                              </option>
                              <option value="raw">Raw pixel value (Bq/mL)</option>
                            </select>
                            {petDisplayMeta.suv_warnings &&
                              petDisplayMeta.suv_warnings.length > 0 && (
                                <ul
                                  style={{
                                    marginTop: "0.4rem",
                                    paddingLeft: "1rem",
                                    fontSize: "0.7rem",
                                    color: "#fca5a5",
                                  }}
                                >
                                  {petDisplayMeta.suv_warnings.map((w) => (
                                    <li key={w}>{w}</li>
                                  ))}
                                </ul>
                              )}
                            {/* Active SUV scaling factor — shown so the user
                      can see the variant selector is actually doing
                      something even before they hover the volume.
                      The variant pickers above feed this through
                      ``pickActiveSuvFactor``; the same number is sent
                      to the layout as ``suvFactorBw`` and is used by
                      the voxel HUD + ROI labels. */}
                            {(() => {
                              // RAW is not an error: the user explicitly opted
                              // out of SUV scaling. The volume keeps rendering
                              // pixel values in their native units (Bq/mL) and
                              // the SUV-domain controls below (range, threshold)
                              // gate themselves off. Only flag a true "factor
                              // missing" condition for the BW/LBM/BSA branches,
                              // which need a numeric factor and silently fall
                              // back to raw if the DICOM header is incomplete.
                              if (suvVariant === "raw") {
                                return (
                                  <div
                                    className="meta"
                                    style={{
                                      marginTop: "0.4rem",
                                      fontSize: "0.7rem",
                                      color: "#fbbf24",
                                    }}
                                  >
                                    Showing raw pixel values (Bq/mL). SUV scaling is disabled, so
                                    SUV-based display range and threshold controls are unavailable.
                                    Pick a SUV variant (BW / SUL / BSA) to re-enable them.
                                  </div>
                                );
                              }
                              const f = pickActiveSuvFactor(petDisplayMeta, suvVariant);
                              if (f == null || f <= 0) {
                                return (
                                  <div
                                    className="meta"
                                    style={{
                                      marginTop: "0.4rem",
                                      fontSize: "0.7rem",
                                      color: "#fca5a5",
                                    }}
                                  >
                                    No factor for {suvVariant.toUpperCase()} — variant unavailable
                                    for this series. Required DICOM tags (height for LBM/BSA, sex
                                    for LBM) are missing in the header.
                                  </div>
                                );
                              }
                              return (
                                <div
                                  className="meta"
                                  style={{ marginTop: "0.4rem", fontSize: "0.7rem" }}
                                >
                                  Scaling factor: {f.toExponential(3)} (Bq/mL → SUV). Variant
                                  changes also re-window the PT volume when a SUV display range or
                                  threshold is active.
                                </div>
                              );
                            })()}

                            {/* PET LUT (spec §3.2). The four colormaps that
                      nuclear-medicine readers expect; hot-iron is the
                      historical FDG default, inverse-gray is the
                      reading convention for MIP. */}
                            <div style={{ marginTop: "0.6rem" }}>
                              <div
                                // Section heading for the colormap radio
                                // group below — was a misused <label> (no
                                // htmlFor / no nested control), which biome
                                // flags as ``noLabelWithoutControl``. Plain
                                // <div> keeps the visual style and is the
                                // honest semantic.
                                style={{
                                  display: "block",
                                  fontSize: "0.78rem",
                                  fontWeight: 600,
                                }}
                              >
                                PET colormap
                              </div>
                              <div
                                style={{
                                  display: "flex",
                                  gap: 4,
                                  flexWrap: "wrap",
                                  marginTop: 4,
                                }}
                              >
                                {[
                                  ["hot-iron", "Hot iron"],
                                  ["rainbow", "Rainbow"],
                                  ["inverse-gray", "Inverse gray"],
                                  ["hot-metal", "Hot metal"],
                                ].map(([id, label]) => (
                                  <button
                                    key={id}
                                    type="button"
                                    className={
                                      petColormap === id
                                        ? "viewer-btn viewer-btn--active"
                                        : "viewer-btn"
                                    }
                                    style={{
                                      fontSize: "0.7rem",
                                      padding: "0.15rem 0.45rem",
                                    }}
                                    onClick={() =>
                                      setPetColormap(
                                        id as "hot-iron" | "rainbow" | "inverse-gray" | "hot-metal",
                                      )
                                    }
                                  >
                                    {label}
                                  </button>
                                ))}
                              </div>
                            </div>

                            {/* SUV display range presets (spec §3.3). The
                      sidebar pushes the chosen [lo,hi] window onto
                      the fusion volume via the layout's imperative
                      handle; the underlying volume scalars stay in
                      Bq/mL so we divide by the active SUV factor to
                      land in volume space. */}
                            <div style={{ marginTop: "0.6rem" }}>
                              <div
                                style={{
                                  display: "block",
                                  fontSize: "0.78rem",
                                  fontWeight: 600,
                                }}
                              >
                                SUV display range
                              </div>
                              <div
                                style={{
                                  display: "flex",
                                  gap: 4,
                                  flexWrap: "wrap",
                                  marginTop: 4,
                                }}
                              >
                                {(
                                  [
                                    ["0–5", 0, 5],
                                    ["0–10", 0, 10],
                                    ["0–15", 0, 15],
                                    ["0–25", 0, 25],
                                  ] as const
                                ).map(([label, lo, hi]) => {
                                  const isActive =
                                    suvDisplayRange?.lo === lo && suvDisplayRange?.hi === hi;
                                  return (
                                    <button
                                      key={label}
                                      type="button"
                                      className={
                                        isActive ? "viewer-btn viewer-btn--active" : "viewer-btn"
                                      }
                                      style={{
                                        fontSize: "0.7rem",
                                        padding: "0.15rem 0.45rem",
                                      }}
                                      onClick={() => setSuvDisplayRange({ lo, hi })}
                                    >
                                      {label}
                                    </button>
                                  );
                                })}
                                <button
                                  type="button"
                                  className={
                                    suvDisplayRange === null
                                      ? "viewer-btn viewer-btn--active"
                                      : "viewer-btn"
                                  }
                                  style={{
                                    fontSize: "0.7rem",
                                    padding: "0.15rem 0.45rem",
                                  }}
                                  onClick={() => {
                                    setSuvDisplayRange(null);
                                    // Reset only the volume that holds the PT.
                                    // Touching the wrong slot is what made the
                                    // CT primary go fully black previously
                                    // (defaultWC/WW for the CT volume.range
                                    // lands far from any clinical preset).
                                    if (petSlot === "primary") {
                                      mprRef.current?.resetWL?.();
                                    } else if (petSlot === "fusion") {
                                      mprRef.current?.resetFusionWL?.();
                                    }
                                  }}
                                >
                                  Auto
                                </button>
                              </div>
                            </div>

                            {/* SUV threshold filter (spec §3.4) — hides voxels
                      outside the chosen [lo, hi] SUV interval by zeroing
                      the volume's opacity transfer function. Backbone
                      use case: review FDG-PET for lesions above
                      physiological background (kidneys, bladder, brain,
                      liver hepatocellular uptake). Display-only — does
                      not alter ROI stats, hot-spot detection, or the
                      stored pixel values.
                      Disabled when the active variant is RAW (no SUV
                      factor → cannot map SUV bounds to scalar bounds);
                      the user has to pick a quantitative variant first. */}
                            <div style={{ marginTop: "0.6rem" }}>
                              <div
                                style={{
                                  display: "block",
                                  fontSize: "0.78rem",
                                  fontWeight: 600,
                                }}
                              >
                                SUV threshold (hide outside range)
                              </div>
                              <p
                                className="meta"
                                style={{ margin: "0.2rem 0", fontSize: "0.7rem" }}
                              >
                                Suppresses voxels with SUV outside [lo, hi]. Use it to mute
                                physiological uptake (kidneys / bladder ≳ 10, brain ≳ 8, liver
                                background ≈ 2-3) and isolate metabolically relevant findings.
                                Display-only — ROI stats and hot-spot detection are unaffected.
                              </p>
                              {suvVariant === "raw" ||
                              pickActiveSuvFactor(petDisplayMeta, suvVariant) == null ? (
                                <div
                                  className="meta"
                                  style={{
                                    fontSize: "0.7rem",
                                    color: "#fbbf24",
                                    padding: "0.3rem 0.4rem",
                                    borderLeft: "2px solid #fbbf24",
                                    background: "rgba(251, 191, 36, 0.08)",
                                  }}
                                >
                                  SUV threshold needs a quantitative variant (BW / SUL / BSA).
                                  Switch the variant above to enable this control.
                                </div>
                              ) : (
                                <>
                                  <div
                                    style={{
                                      display: "flex",
                                      gap: 4,
                                      flexWrap: "wrap",
                                      marginTop: 4,
                                    }}
                                  >
                                    {(
                                      [
                                        { label: "Off", lo: null, hi: null },
                                        { label: "≥ 1.5 (clean MIP)", lo: 1.5, hi: null },
                                        { label: "≥ 2.5 (PERCIST)", lo: 2.5, hi: null },
                                        { label: "≥ 4 (high uptake)", lo: 4.0, hi: null },
                                        { label: "2.5 – 25", lo: 2.5, hi: 25 },
                                      ] as const
                                    ).map((preset) => {
                                      const isActive =
                                        preset.lo === null
                                          ? suvHide === null
                                          : suvHide?.lo === preset.lo && suvHide?.hi === preset.hi;
                                      return (
                                        <button
                                          key={preset.label}
                                          type="button"
                                          className={
                                            isActive
                                              ? "viewer-btn viewer-btn--active"
                                              : "viewer-btn"
                                          }
                                          style={{
                                            fontSize: "0.7rem",
                                            padding: "0.15rem 0.45rem",
                                          }}
                                          onClick={() => {
                                            if (preset.lo === null) {
                                              setSuvHide(null);
                                            } else {
                                              setSuvHide({ lo: preset.lo, hi: preset.hi });
                                              setSuvHideDraftLo(String(preset.lo));
                                              setSuvHideDraftHi(
                                                preset.hi == null ? "" : String(preset.hi),
                                              );
                                            }
                                          }}
                                        >
                                          {preset.label}
                                        </button>
                                      );
                                    })}
                                  </div>
                                  {/* Manual numeric inputs. Empty hi = no upper
                            bound (clamps to the volume's max scalar);
                            standard for "show everything above N SUV". */}
                                  <div
                                    style={{
                                      display: "flex",
                                      gap: 6,
                                      alignItems: "center",
                                      marginTop: 6,
                                      fontSize: "0.7rem",
                                      flexWrap: "wrap",
                                    }}
                                  >
                                    <label
                                      style={{ display: "flex", alignItems: "center", gap: 3 }}
                                    >
                                      SUV lo
                                      <input
                                        type="number"
                                        min={0}
                                        step={0.1}
                                        value={suvHideDraftLo}
                                        onChange={(e) => setSuvHideDraftLo(e.target.value)}
                                        style={{ width: 56, fontSize: "0.7rem" }}
                                      />
                                    </label>
                                    <label
                                      style={{ display: "flex", alignItems: "center", gap: 3 }}
                                    >
                                      SUV hi
                                      <input
                                        type="number"
                                        min={0}
                                        step={0.1}
                                        placeholder="∞"
                                        value={suvHideDraftHi}
                                        onChange={(e) => setSuvHideDraftHi(e.target.value)}
                                        style={{ width: 56, fontSize: "0.7rem" }}
                                      />
                                    </label>
                                    <button
                                      type="button"
                                      className="viewer-btn"
                                      style={{ fontSize: "0.7rem", padding: "0.15rem 0.45rem" }}
                                      onClick={() => {
                                        const lo = Number.parseFloat(suvHideDraftLo);
                                        const hiRaw = suvHideDraftHi.trim();
                                        const hi = hiRaw === "" ? null : Number.parseFloat(hiRaw);
                                        if (!Number.isFinite(lo) || lo < 0) return;
                                        if (hi !== null && (!Number.isFinite(hi) || hi <= lo))
                                          return;
                                        setSuvHide({ lo, hi });
                                      }}
                                    >
                                      Apply
                                    </button>
                                    {suvHide && (
                                      <button
                                        type="button"
                                        className="viewer-btn"
                                        style={{ fontSize: "0.7rem", padding: "0.15rem 0.45rem" }}
                                        onClick={() => setSuvHide(null)}
                                      >
                                        Clear
                                      </button>
                                    )}
                                  </div>
                                  {/* PERCIST helper. The user reads SUVmean and
                            SUVsd from a 3 cm spherical liver ROI in
                            unaffected right-lobe parenchyma (PERCIST
                            1.0 §4.3) and the helper computes
                            1.5 × mean + 2 × SD as the measurable-lesion
                            floor, then applies it as ``lo`` (open hi). */}
                                  <div
                                    style={{
                                      marginTop: 8,
                                      padding: "0.4rem 0.5rem",
                                      border: "1px solid rgba(96,165,250,0.35)",
                                      borderRadius: 4,
                                      background: "rgba(96,165,250,0.06)",
                                      fontSize: "0.7rem",
                                    }}
                                  >
                                    <div style={{ fontWeight: 600, marginBottom: 4 }}>
                                      PERCIST helper
                                    </div>
                                    <div
                                      className="meta"
                                      style={{ fontSize: "0.68rem", marginBottom: 6 }}
                                    >
                                      Draw a Sphere ROI (~15 mm radius) in unaffected right-lobe
                                      liver, then click "Use last sphere". Computes the PERCIST
                                      measurable-lesion floor (1.5 × mean + 2 × SD). Manual paste
                                      still works when no PET SUV factor is available.
                                    </div>
                                    {(() => {
                                      // Pick the most-recent sphere ROI that has
                                      // server-side SUV stats. The mapping
                                      // ``measure-sphere`` → ``CircleROITool`` is
                                      // canonical (see CornerstoneMPRLayout); we
                                      // also accept the legacy ``"sphere"`` short
                                      // name for measurements restored from the
                                      // markers API.
                                      const sphereWithSuv = [...allMeasurements]
                                        .reverse()
                                        .find(
                                          (mm) =>
                                            (mm.csToolName === "CircleROI" ||
                                              mm.tool === "sphere") &&
                                            mm.suv?.mean !== undefined &&
                                            mm.suv?.sd !== undefined,
                                        );
                                      const disabled = sphereWithSuv === undefined;
                                      return (
                                        <button
                                          type="button"
                                          className="viewer-btn"
                                          disabled={disabled}
                                          title={
                                            disabled
                                              ? "Draw a Sphere ROI on a PET series to enable"
                                              : `Pull SUVmean ${sphereWithSuv?.suv?.mean?.toFixed(
                                                  2,
                                                )} ± ${sphereWithSuv?.suv?.sd?.toFixed(
                                                  2,
                                                )} from the last sphere ROI`
                                          }
                                          style={{
                                            fontSize: "0.7rem",
                                            padding: "0.15rem 0.45rem",
                                            marginBottom: 4,
                                            opacity: disabled ? 0.5 : 1,
                                          }}
                                          onClick={() => {
                                            if (!sphereWithSuv?.suv) return;
                                            const m = sphereWithSuv.suv.mean;
                                            const sd = sphereWithSuv.suv.sd;
                                            if (m == null || sd == null) return;
                                            setPercistLiverMean(m.toFixed(2));
                                            setPercistLiverSd(sd.toFixed(2));
                                          }}
                                        >
                                          Use last sphere as liver ref
                                        </button>
                                      );
                                    })()}
                                    <div
                                      style={{
                                        display: "flex",
                                        gap: 6,
                                        alignItems: "center",
                                        flexWrap: "wrap",
                                      }}
                                    >
                                      <label
                                        style={{ display: "flex", alignItems: "center", gap: 3 }}
                                      >
                                        Liver SUVmean
                                        <input
                                          type="number"
                                          min={0}
                                          step={0.1}
                                          value={percistLiverMean}
                                          onChange={(e) => setPercistLiverMean(e.target.value)}
                                          style={{ width: 60, fontSize: "0.7rem" }}
                                        />
                                      </label>
                                      <label
                                        style={{ display: "flex", alignItems: "center", gap: 3 }}
                                      >
                                        Liver SD
                                        <input
                                          type="number"
                                          min={0}
                                          step={0.05}
                                          value={percistLiverSd}
                                          onChange={(e) => setPercistLiverSd(e.target.value)}
                                          style={{ width: 60, fontSize: "0.7rem" }}
                                        />
                                      </label>
                                      <button
                                        type="button"
                                        className="viewer-btn"
                                        style={{
                                          fontSize: "0.7rem",
                                          padding: "0.15rem 0.45rem",
                                        }}
                                        onClick={() => {
                                          const m = Number.parseFloat(percistLiverMean);
                                          const sd = Number.parseFloat(percistLiverSd);
                                          if (!Number.isFinite(m) || m <= 0) return;
                                          if (!Number.isFinite(sd) || sd < 0) return;
                                          const lo = 1.5 * m + 2 * sd;
                                          setSuvHide({ lo, hi: null });
                                          setSuvHideDraftLo(lo.toFixed(2));
                                          setSuvHideDraftHi("");
                                        }}
                                      >
                                        Apply 1.5×mean + 2×SD
                                      </button>
                                    </div>
                                    {(() => {
                                      const m = Number.parseFloat(percistLiverMean);
                                      const sd = Number.parseFloat(percistLiverSd);
                                      if (!Number.isFinite(m) || m <= 0) return null;
                                      if (!Number.isFinite(sd) || sd < 0) return null;
                                      return (
                                        <div
                                          className="meta"
                                          style={{ marginTop: 4, fontSize: "0.68rem" }}
                                        >
                                          Threshold: SUV ≥ {(1.5 * m + 2 * sd).toFixed(2)}
                                        </div>
                                      );
                                    })()}
                                  </div>
                                  {suvHide && (
                                    <div
                                      className="meta"
                                      style={{
                                        marginTop: 6,
                                        fontSize: "0.68rem",
                                        color: "#a3e635",
                                      }}
                                    >
                                      Active: hide SUV {"<"} {suvHide.lo.toFixed(2)}
                                      {suvHide.hi != null
                                        ? ` and SUV > ${suvHide.hi.toFixed(2)}`
                                        : ""}
                                      .
                                    </div>
                                  )}
                                </>
                              )}
                            </div>

                            <div style={{ marginTop: "0.6rem" }}>
                              <div
                                style={{
                                  display: "block",
                                  fontSize: "0.78rem",
                                  fontWeight: 600,
                                }}
                              >
                                EARL harmonisation
                              </div>
                              <p
                                className="meta"
                                style={{ margin: "0.2rem 0", fontSize: "0.7rem" }}
                              >
                                Server-side Gaussian filter to match a target effective PSF FWHM
                                (EANM/EARL accreditation). Improves cross-scanner SUV
                                reproducibility on lesions {">"} 2 cm. 0 = off · 7 mm = EARL1 · 5 mm
                                = EARL2.
                              </p>
                              <div
                                style={{
                                  display: "flex",
                                  gap: 4,
                                  flexWrap: "wrap",
                                  marginBottom: 4,
                                }}
                              >
                                {[0, 5, 7, 10].map((v) => (
                                  <button
                                    key={v}
                                    type="button"
                                    className={
                                      earlFwhmMm === v
                                        ? "viewer-btn viewer-btn--active"
                                        : "viewer-btn"
                                    }
                                    style={{
                                      fontSize: "0.7rem",
                                      padding: "0.15rem 0.45rem",
                                    }}
                                    onClick={() => setEarlFwhmMm(v)}
                                  >
                                    {v === 0 ? "off" : `${v} mm`}
                                  </button>
                                ))}
                              </div>
                              <input
                                type="range"
                                min={0}
                                max={15}
                                step={0.5}
                                value={earlFwhmMm}
                                onChange={(e) => setEarlFwhmMm(Number(e.target.value))}
                                style={{ width: "100%" }}
                              />
                              <div className="meta" style={{ fontSize: "0.7rem" }}>
                                Current: {earlFwhmMm.toFixed(1)} mm
                                {earlFwhmMm > 0 ? " (re-fetches volume)" : ""}
                              </div>
                            </div>
                          </div>

                          {/* Hot spots lives inside the PET section: it
                      reads the PT volume directly (connected-component
                      lesion finder on SUV) so it has no meaning outside
                      a PET review. Keeping it as a sub-block here
                      (instead of a sibling SidebarSection) matches the
                      mental model "PET → SUV controls → hot-spots". */}
                          {(() => {
                            const primaryIsPet = (series?.modality ?? "").toUpperCase() === "PT";
                            const fusionIsPet =
                              (csFusionSeriesMeta?.modality ?? "").toUpperCase() === "PT";
                            const hotSpotSeriesId =
                              fusionIsPet && csFusionSeriesId ? csFusionSeriesId : params.id;
                            if (!primaryIsPet && !fusionIsPet) return null;
                            return (
                              <div
                                className="card"
                                style={{
                                  padding: "0.5rem 0.75rem",
                                  marginTop: "0.5rem",
                                  fontSize: "0.78rem",
                                }}
                              >
                                <strong style={{ fontSize: "0.82rem" }}>Hot spots</strong>
                                <p
                                  className="meta"
                                  style={{ margin: "0.2rem 0 0.4rem", fontSize: "0.7rem" }}
                                >
                                  Connected-component lesion finder · PERCIST 1.0
                                </p>
                                <HotSpotsPanel
                                  seriesId={hotSpotSeriesId}
                                  isPet
                                  patientId={studyPatientId}
                                  onJumpTo={(voxel) => {
                                    const ok =
                                      mprRef.current?.setCrosshairFromSourceSeries?.(
                                        hotSpotSeriesId,
                                        voxel,
                                      ) ?? false;
                                    if (!ok) {
                                      setMprCrosshair(voxel);
                                      mprRef.current?.setCrosshair(voxel);
                                    }
                                  }}
                                />
                              </div>
                            );
                          })()}
                        </>
                      )}
                    </SidebarSection>
                  )}

                  <SidebarSection
                    sectionId="layout"
                    title="Layout"
                    defaultOpen={false}
                    hint="Hanging protocol · quick presets · individual planes"
                  >
                    <HangingProtocolPicker
                      value={protocolId}
                      modality={series.modality}
                      onChange={applyProtocol}
                    />

                    {/* PET-CT spec §2.1 quick layouts. Maps each preset
                    onto the existing showAxial / showSagittal /
                    showCoronal / showMip / show3D toggles so the same
                    rendering pipeline serves both the protocol picker
                    and these one-click presets. */}
                    <p
                      className="meta"
                      style={{ marginTop: "0.6rem", marginBottom: "0.25rem", fontSize: "0.7rem" }}
                    >
                      Quick presets
                    </p>
                    <div style={{ display: "flex", gap: "0.4rem", flexWrap: "wrap" }}>
                      <button
                        type="button"
                        className="viewer-btn"
                        onClick={() => {
                          setShowAxial(false);
                          setShowSagittal(false);
                          setShowCoronal(false);
                          setShow3D(false);
                          setShowMip(true);
                          setShowOblique(false);
                        }}
                        title="1×1 PET MIP — overview cine"
                      >
                        1×1 MIP
                      </button>
                      <button
                        type="button"
                        className="viewer-btn"
                        onClick={() => {
                          setShowAxial(true);
                          setShowSagittal(true);
                          setShowCoronal(true);
                          setShow3D(false);
                          setShowMip(true);
                          setShowOblique(false);
                        }}
                        title="2×2 fusion — axial + sag + cor + MIP"
                      >
                        2×2 fusion
                      </button>
                      <button
                        type="button"
                        className="viewer-btn"
                        onClick={() => {
                          setShowAxial(true);
                          setShowSagittal(true);
                          setShowCoronal(true);
                          setShow3D(true);
                          setShowMip(false);
                          setShowOblique(false);
                        }}
                        title="3×3 MPR — full axial/sagittal/coronal review"
                      >
                        3×3 MPR
                      </button>
                      <button
                        type="button"
                        className="viewer-btn"
                        onClick={() => {
                          setShowAxial(true);
                          setShowSagittal(false);
                          setShowCoronal(false);
                          setShow3D(false);
                          setShowMip(false);
                          setShowOblique(false);
                          setCsFusionSeriesId(null);
                          setCsFusionFailed(false);
                        }}
                        title="Primary only — fusion overlay disabled"
                      >
                        Primary only
                      </button>
                    </div>

                    <p
                      className="meta"
                      style={{ marginTop: "0.7rem", marginBottom: "0.25rem", fontSize: "0.7rem" }}
                    >
                      Individual planes
                    </p>
                    <div style={{ display: "flex", gap: "0.4rem", flexWrap: "wrap" }}>
                      <button
                        type="button"
                        className={showAxial ? "viewer-btn viewer-btn--active" : "viewer-btn"}
                        onClick={() => togglePane(showAxial, setShowAxial)}
                        disabled={!showAxial && atPaneCap}
                        title={
                          !showAxial && atPaneCap
                            ? `Max ${MAX_PANES} viewports — deselect one first`
                            : undefined
                        }
                      >
                        Axial
                      </button>
                      <button
                        type="button"
                        className={showSagittal ? "viewer-btn viewer-btn--active" : "viewer-btn"}
                        onClick={() => togglePane(showSagittal, setShowSagittal)}
                        disabled={!showSagittal && atPaneCap}
                        title={
                          !showSagittal && atPaneCap
                            ? `Max ${MAX_PANES} viewports — deselect one first`
                            : undefined
                        }
                      >
                        Sagittal
                      </button>
                      <button
                        type="button"
                        className={showCoronal ? "viewer-btn viewer-btn--active" : "viewer-btn"}
                        onClick={() => togglePane(showCoronal, setShowCoronal)}
                        disabled={!showCoronal && atPaneCap}
                        title={
                          !showCoronal && atPaneCap
                            ? `Max ${MAX_PANES} viewports — deselect one first`
                            : undefined
                        }
                      >
                        Coronal
                      </button>
                      <button
                        type="button"
                        className={show3D ? "viewer-btn viewer-btn--active" : "viewer-btn"}
                        onClick={() => togglePane(show3D, setShow3D)}
                        disabled={!show3D && atPaneCap}
                        title={
                          !show3D && atPaneCap
                            ? `Max ${MAX_PANES} viewports — deselect one first`
                            : undefined
                        }
                      >
                        3D
                      </button>
                      <button
                        type="button"
                        className={showMip ? "viewer-btn viewer-btn--active" : "viewer-btn"}
                        onClick={() => togglePane(showMip, setShowMip)}
                        disabled={!showMip && atPaneCap}
                        title={
                          !showMip && atPaneCap
                            ? `Max ${MAX_PANES} viewports — deselect one first`
                            : "Rotating MIP — server-rendered cine sprite, distinct from the 3D-volume MIP blend mode"
                        }
                      >
                        R/MIP
                      </button>
                      <button
                        type="button"
                        className={showOblique ? "viewer-btn viewer-btn--active" : "viewer-btn"}
                        onClick={() => togglePane(showOblique, setShowOblique)}
                        disabled={!showOblique && atPaneCap}
                        title={
                          !showOblique && atPaneCap
                            ? `Max ${MAX_PANES} viewports — deselect one first`
                            : undefined
                        }
                      >
                        Oblique
                      </button>
                    </div>
                  </SidebarSection>

                  {volume && (
                    <>
                      <SidebarSection sectionId="mpr" title="Slice navigation" defaultOpen={true}>
                        <label className="meta" style={{ display: "block", fontSize: "0.7rem" }}>
                          Axial (Z): {mprCrosshair[2] + 1}/{volume.dimensions[2]}
                          <input
                            type="range"
                            min={0}
                            max={volume.dimensions[2] - 1}
                            value={mprCrosshair[2]}
                            onChange={(e) => {
                              const v = Number(e.target.value);
                              const pos: [number, number, number] = [
                                mprCrosshair[0],
                                mprCrosshair[1],
                                v,
                              ];
                              setMprCrosshair(pos);
                              mprRef.current?.setCrosshair(pos);
                            }}
                          />
                        </label>
                        <label className="meta" style={{ display: "block", fontSize: "0.7rem" }}>
                          Sagittal (X): {mprCrosshair[0] + 1}/{volume.dimensions[0]}
                          <input
                            type="range"
                            min={0}
                            max={volume.dimensions[0] - 1}
                            value={mprCrosshair[0]}
                            onChange={(e) => {
                              const v = Number(e.target.value);
                              const pos: [number, number, number] = [
                                v,
                                mprCrosshair[1],
                                mprCrosshair[2],
                              ];
                              setMprCrosshair(pos);
                              mprRef.current?.setCrosshair(pos);
                            }}
                          />
                        </label>
                        <label className="meta" style={{ display: "block", fontSize: "0.7rem" }}>
                          Coronal (Y): {mprCrosshair[1] + 1}/{volume.dimensions[1]}
                          <input
                            type="range"
                            min={0}
                            max={volume.dimensions[1] - 1}
                            value={mprCrosshair[1]}
                            onChange={(e) => {
                              const v = Number(e.target.value);
                              const pos: [number, number, number] = [
                                mprCrosshair[0],
                                v,
                                mprCrosshair[2],
                              ];
                              setMprCrosshair(pos);
                              mprRef.current?.setCrosshair(pos);
                            }}
                          />
                        </label>
                      </SidebarSection>

                      {displayMeta?.sub_stacks && displayMeta.sub_stacks.length > 1 && (
                        <SidebarSection
                          sectionId="substacks"
                          title="Contrast / sub-stack"
                          defaultOpen={true}
                          hint={`${displayMeta.sub_stacks.length} co-located volumes`}
                        >
                          <p className="meta" style={{ marginTop: "-0.2rem", fontSize: "0.7rem" }}>
                            This series packs several co-located volumes (mDIXON / multi-echo /
                            diffusion) under one acquisition. Pick which one to view — each
                            reconstructs independently.
                          </p>
                          <div
                            style={{
                              display: "flex",
                              gap: 4,
                              flexWrap: "wrap",
                              marginTop: 4,
                            }}
                          >
                            {displayMeta.sub_stacks.map((s) => (
                              <button
                                key={s.stack_index}
                                type="button"
                                className={
                                  selectedStackIndex === s.stack_index
                                    ? "viewer-btn viewer-btn--active"
                                    : "viewer-btn"
                                }
                                style={{ fontSize: "0.7rem", padding: "0.15rem 0.5rem" }}
                                onClick={() => selectStack(s.stack_index)}
                                title={`${s.label} · ${s.instance_count} slices`}
                                disabled={volumeLoading && selectedStackIndex === s.stack_index}
                              >
                                {s.label}
                              </button>
                            ))}
                          </div>
                        </SidebarSection>
                      )}

                      <SidebarSection
                        sectionId="wl"
                        title="Window / Level"
                        defaultOpen={true}
                        hint={`Presets · sliders${csFusionSeriesMeta && csFusionVolume ? " · fusion overlay window" : ""}`}
                      >
                        <h3 className="viewer-rail-section__sub">W/L presets · primary</h3>
                        <p className="meta" style={{ marginTop: "-0.3rem", fontSize: "0.7rem" }}>
                          Modality: {series?.modality ?? "—"}
                        </p>
                        <WLPresetBar
                          series={series}
                          volume={volume}
                          onApply={(wc, ww) => {
                            mprRef.current?.setWC(wc);
                            mprRef.current?.setWW(Math.max(1, ww));
                            setMprCrosshair([...mprCrosshair]);
                          }}
                          onReset={() => {
                            mprRef.current?.resetWL();
                            setMprCrosshair([...mprCrosshair]);
                          }}
                        />
                        {csFusionSeriesMeta && csFusionVolume && (
                          <>
                            <h3
                              className="viewer-rail-section__sub"
                              style={{ marginTop: "0.75rem" }}
                            >
                              W/L presets · fusion overlay
                            </h3>
                            <p
                              className="meta"
                              style={{ marginTop: "-0.3rem", fontSize: "0.7rem" }}
                            >
                              Modality: {csFusionSeriesMeta.modality ?? "—"} · sets the SUV /
                              overlay window independently of the primary
                            </p>
                            <WLPresetBar
                              series={csFusionSeriesMeta}
                              volume={
                                // Reuse the fusion volume range for the slider
                                // bounds; the per-modality presets come from
                                // ``modalityDefaults(fusion.modality)``.
                                csFusionVolume as VolumeData
                              }
                              onApply={(wc, ww) => {
                                mprRef.current?.setFusionWC?.(wc);
                                mprRef.current?.setFusionWW?.(Math.max(1, ww));
                              }}
                              onReset={() => {
                                if (!csFusionVolume) return;
                                const [lo, hi] = csFusionVolume.range;
                                mprRef.current?.setFusionWC?.((lo + hi) / 2);
                                mprRef.current?.setFusionWW?.(Math.max(1, hi - lo));
                              }}
                            />
                          </>
                        )}

                        <h3 className="viewer-rail-section__sub" style={{ marginTop: "0.75rem" }}>
                          Window / Level (sliders)
                        </h3>
                        <div className="meta" style={{ fontSize: "0.7rem" }}>
                          WC: {Math.round(mprRef.current?.wc ?? 0)} · WW:{" "}
                          {Math.round(mprRef.current?.ww ?? 0)}
                        </div>
                        <label className="meta" style={{ display: "block", fontSize: "0.65rem" }}>
                          Center
                          <input
                            type="range"
                            min={Math.floor(volume.range[0])}
                            max={Math.ceil(volume.range[1])}
                            value={Math.round(mprRef.current?.wc ?? 0)}
                            onChange={(e) => {
                              mprRef.current?.setWC(Number(e.target.value));
                              setMprCrosshair([...mprCrosshair]);
                            }}
                          />
                        </label>
                        <label className="meta" style={{ display: "block", fontSize: "0.65rem" }}>
                          Width
                          <input
                            type="range"
                            min={1}
                            max={Math.ceil(volume.range[1] - volume.range[0]) * 2}
                            value={Math.round(mprRef.current?.ww ?? 1)}
                            onChange={(e) => {
                              mprRef.current?.setWW(Math.max(1, Number(e.target.value)));
                              setMprCrosshair([...mprCrosshair]);
                            }}
                          />
                        </label>
                        <div
                          style={{
                            display: "flex",
                            gap: "0.4rem",
                            flexWrap: "wrap",
                            marginTop: "0.3rem",
                          }}
                        >
                          <button
                            type="button"
                            className="viewer-btn"
                            onClick={() => {
                              mprRef.current?.resetWL();
                              setMprCrosshair([...mprCrosshair]);
                            }}
                          >
                            Reset W/L
                          </button>
                          <button
                            type="button"
                            className="viewer-btn"
                            onClick={() => {
                              if (!volume) return;
                              const z = mprCrosshair[2];
                              const [nxV, nyV] = volume.dimensions;
                              let min = Number.POSITIVE_INFINITY;
                              let max = Number.NEGATIVE_INFINITY;
                              for (
                                let i = z * nxV * nyV;
                                i < (z + 1) * nxV * nyV && i < volume.scalars.length;
                                i++
                              ) {
                                const v = volume.scalars[i];
                                if (v < min) min = v;
                                if (v > max) max = v;
                              }
                              if (min < max) {
                                mprRef.current?.setWC((min + max) / 2);
                                mprRef.current?.setWW(max - min);
                                setMprCrosshair([...mprCrosshair]);
                              }
                            }}
                          >
                            Auto W/L
                          </button>
                          <button
                            type="button"
                            className={
                              mprRef.current?.invert
                                ? "viewer-btn viewer-btn--active"
                                : "viewer-btn"
                            }
                            onClick={() => {
                              mprRef.current?.setInvert(!mprRef.current?.invert);
                              setMprCrosshair([...mprCrosshair]);
                            }}
                          >
                            Invert
                          </button>
                          {csFusionSeriesId && (
                            <button
                              type="button"
                              className={petOn ? "viewer-btn viewer-btn--active" : "viewer-btn"}
                              title={
                                petOn
                                  ? "Hide PET overlay — see CT alone to compare against the metabolic finding"
                                  : "Show PET fusion overlay over the CT (default review)"
                              }
                              onClick={() => {
                                const next = !petOn;
                                setPetOn(next);
                                mprRef.current?.setFusionVisible?.(next);
                                setMprCrosshair([...mprCrosshair]);
                              }}
                            >
                              PET {petOn ? "on" : "off"}
                            </button>
                          )}
                        </div>
                      </SidebarSection>

                      <SidebarSection
                        sectionId="tools"
                        title="Tools"
                        defaultOpen={true}
                        hint="Mouse bindings · measurement tools"
                      >
                        <div data-testid="viewer-toolbar">
                          <ViewerToolPalette
                            activeTool={activeTool}
                            onChange={(t) => setActiveTool(t)}
                            onUndo={undoAnnotation}
                            onRedo={redoAnnotation}
                            canUndo={historyVersion >= 0 && historyRef.current.canUndo()}
                            canRedo={historyVersion >= 0 && historyRef.current.canRedo()}
                            onClearAll={() => {
                              // Drop the SVG overlays first so the next
                              // ANNOTATION_REMOVED → onMeasurementsChange pass
                              // doesn't immediately replay them back into state.
                              mprRef.current?.clearAnnotations();
                              setAllMeasurements([]);
                            }}
                          />
                        </div>

                        {/* "Measurements" widget rimosso: era duplicato di
                      ``MarkerListPanel`` (sezione Annotazioni in
                      basso) e leggeva un altro shadow-state, quindi
                      cancellare da qui non ripuliva il pannello né
                      la annotation Cornerstone. Tutto passa ora dal
                      pannello "Annotazioni": label, edit, delete. */}
                      </SidebarSection>

                      {activeTool === "measure-lens" && (
                        <SidebarSection
                          sectionId="lens"
                          title="Lens probe"
                          defaultOpen={true}
                          hint="Live circular ROI · Shift+Wheel = radius"
                        >
                          <div style={{ display: "flex", flexDirection: "column", gap: "0.4rem" }}>
                            <div style={{ display: "flex", gap: "0.4rem", alignItems: "center" }}>
                              <label
                                htmlFor="lens-radius"
                                className="meta"
                                style={{ fontSize: "0.7rem" }}
                              >
                                Radius (mm)
                              </label>
                              <input
                                id="lens-radius"
                                type="number"
                                min={1}
                                max={50}
                                step={0.5}
                                value={mprRef.current?.lensRadiusMm ?? 5}
                                onChange={(e) => {
                                  const v = Number(e.target.value);
                                  if (Number.isFinite(v)) {
                                    mprRef.current?.setLensRadiusMm?.(v);
                                  }
                                }}
                                style={{ width: 70, padding: "1px 4px" }}
                              />
                              <input
                                type="range"
                                min={1}
                                max={50}
                                step={0.5}
                                value={mprRef.current?.lensRadiusMm ?? 5}
                                onChange={(e) => {
                                  const v = Number(e.target.value);
                                  if (Number.isFinite(v)) {
                                    mprRef.current?.setLensRadiusMm?.(v);
                                  }
                                }}
                                style={{ flex: 1 }}
                              />
                            </div>
                            <button
                              type="button"
                              className="viewer-btn"
                              onClick={() => mprRef.current?.pinCurrentLens?.()}
                              title="Persist the current readout as a measurement"
                            >
                              Pin current reading
                            </button>
                            <p className="meta" style={{ fontSize: "0.65rem", margin: 0 }}>
                              Move cursor over the canvas to sample; Shift+Wheel grows / shrinks the
                              disc; left-click pins it as a marker.
                            </p>
                          </div>
                        </SidebarSection>
                      )}

                      <SidebarSection sectionId="cine" title="Cine" defaultOpen={false}>
                        <div style={{ display: "flex", gap: "0.4rem", alignItems: "center" }}>
                          <button
                            type="button"
                            className={cineActive ? "viewer-btn viewer-btn--active" : "viewer-btn"}
                            onClick={() => setCineActive(!cineActive)}
                          >
                            {cineActive ? "Stop" : "Play"}
                          </button>
                          <input
                            type="range"
                            min={1}
                            max={30}
                            value={cineSpeed}
                            onChange={(e) => setCineSpeed(Number(e.target.value))}
                            style={{ width: 80 }}
                          />
                          <span className="meta" style={{ fontSize: "0.7rem" }}>
                            {cineSpeed} fps
                          </span>
                        </div>
                      </SidebarSection>
                    </>
                  )}

                  {show3D && (
                    <SidebarSection
                      sectionId="3d"
                      title="3D rendering"
                      defaultOpen={false}
                      hint="Color map · blend mode · opacity · transfer functions · lighting · camera"
                    >
                      {render3DControls()}
                    </SidebarSection>
                  )}

                  <SidebarSection sectionId="shortcuts" title="Shortcuts" defaultOpen={false}>
                    <button
                      type="button"
                      className="viewer-btn"
                      style={{ fontSize: "0.7rem" }}
                      onClick={() => setHelpOpen(true)}
                      title="Show keyboard shortcuts (press ? or /)"
                    >
                      Keyboard shortcuts (?)
                    </button>
                  </SidebarSection>

                  <SidebarSection
                    sectionId="similar-cases"
                    title="Similar cases"
                    defaultOpen={false}
                    hint="Vector-similarity matches across the corpus"
                  >
                    <SimilarCasesPanelViewer seriesId={params.id} />
                  </SidebarSection>

                  {/* Hot spots now lives inside the PET section above
                  (mental model: PET → SUV → hot-spots is one workflow).
                  Kept the comment as a breadcrumb in case the panel
                  ever moves back out: the rendering condition is
                  ``primaryIsPet || fusionIsPet`` and the cross-volume
                  jump API matters whenever the MPR primary is a CT
                  with PT fusion. */}

                  {studyPatientId && series?.study_id && (
                    <SidebarSection
                      sectionId="marker-list"
                      title="Annotazioni"
                      defaultOpen={true}
                      hint="Measurements · markers · clinical notes"
                    >
                      <MarkerListPanel
                        patientId={studyPatientId}
                        studyId={series.study_id}
                        seriesIds={[params.id, csFusionSeriesId].filter((s): s is string => !!s)}
                        refreshKey={markerListRefreshKey}
                        focusedMarkerId={focusedMarkerId}
                        onMarkersLoaded={(loaded) => {
                          // Drop the cornerstone-managed kinds — those
                          // already render through CS itself. The
                          // overlay only paints what would otherwise
                          // have no visual feedback on the canvas.
                          const items: MarkerOverlayItem[] = loaded
                            .filter((m) => !markerIsManagedByCornerstone(m))
                            .map((m) => ({
                              ...markerToOverlayItem(m),
                              // Colour by the CLASS of the finding this marker
                              // belongs to (task cde63ced); null → kind colour.
                              category: catByMarkerRef.current.get(m.id) ?? null,
                            }));
                          setOverlayMarkers(items);
                        }}
                        onJumpTo={(voxel, markerId, sourceSeriesId) => {
                          // Round-trip IJK through world coords when the
                          // marker lives on a different series than the
                          // MPR primary (typical PT-on-CT fusion: marker
                          // bbox written in PT-IJK while the user is
                          // looking at the CT). Without the round-trip,
                          // ``setCrosshair`` would interpret PT-IJK as
                          // primary-IJK; with mismatched dimensions the
                          // crosshair lands outside the primary's bounds
                          // and the sag/cor planes go black.
                          const seriesForVoxel = sourceSeriesId ?? params.id;
                          const ok =
                            mprRef.current?.setCrosshairFromSourceSeries?.(seriesForVoxel, voxel) ??
                            false;
                          if (!ok) {
                            // Fallback for older builds without the
                            // cross-volume API: best-effort direct jump
                            // (correct when the row's source matches the
                            // primary, off-target otherwise).
                            setMprCrosshair(voxel);
                            mprRef.current?.setCrosshair(voxel);
                          }
                          if (markerId) focusMarker(markerId);
                        }}
                        onMarkerDeleted={(markerId) => {
                          // Strip the Cornerstone overlay AND drop the
                          // matching React entry. Without both, the next
                          // ANNOTATION_ADDED event re-syncs every still-
                          // alive Cornerstone annotation back into
                          // ``allMeasurements``, including the one the
                          // user just deleted.
                          mprRef.current?.removeAnnotation(markerId);
                          setAllMeasurements((prev) =>
                            prev.filter((m) => {
                              const localId = m.id;
                              const mappedServerId = markerIdMapRef.current.get(localId);
                              return m.markerId !== markerId && mappedServerId !== markerId;
                            }),
                          );
                        }}
                        onMarkerLabelChanged={(markerId, label) => {
                          // Mirror the panel-side edit into the canvas
                          // (so the on-image text refreshes) and into
                          // ``allMeasurements`` (so the report-composer
                          // and any future re-render see the new label).
                          mprRef.current?.updateAnnotationLabel(markerId, label);
                          setAllMeasurements((prev) =>
                            prev.map((m) => {
                              const mapped = markerIdMapRef.current.get(m.id) ?? m.markerId;
                              if (mapped !== markerId && m.markerId !== markerId) {
                                return m;
                              }
                              return { ...m, label };
                            }),
                          );
                        }}
                      />
                    </SidebarSection>
                  )}

                  {studyPatientId && series?.study_id && (
                    <SidebarSection
                      sectionId="findings"
                      title="Reperti"
                      defaultOpen={false}
                      hint="Structured findings · type / anatomy / size / SUV"
                    >
                      <FindingPanel
                        patientId={studyPatientId}
                        studyId={series.study_id}
                        seriesId={params.id}
                        frameOfReferenceUid={displayMeta?.frame_of_reference_uid ?? null}
                        refreshKey={markerListRefreshKey}
                      />
                    </SidebarSection>
                  )}

                  {studyPatientId && series?.study_id && (
                    <SidebarSection
                      sectionId="report-composer"
                      title="Refertazione"
                      defaultOpen={false}
                      hint="Title · technique · findings · impression · recommendations"
                    >
                      <ReportComposer
                        patientId={studyPatientId}
                        studyId={series.study_id}
                        measurements={allMeasurements.map((m) => ({
                          id: String(m.id),
                          tool: m.tool,
                          value: m.value,
                        }))}
                      />
                    </SidebarSection>
                  )}
                </div>
              </>
            )}
          </aside>
        )}
        <HotkeyHelpOverlay
          open={helpOpen}
          onClose={() => setHelpOpen(false)}
          sections={helpSections}
        />
      </div>
    </BrowserSupportGate>
  );
}
