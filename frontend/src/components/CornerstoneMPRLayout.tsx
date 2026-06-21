"use client";

// Cornerstone3D-backed MPR layout. Drop-in replacement for the
// hand-rolled ``MPRLayout`` — same prop interface, same imperative
// handle (``MPRLayoutHandle``), so the surrounding viewer page,
// hotkeys, and persistence logic don't change.
//
// What Cornerstone gives us out of the box (vs. the old layout):
//   - True oblique reformat via ``CrosshairsTool``: drag the dashed
//     line to translate, drag the T-handle to rotate the orthogonal
//     planes (the entire OHIF crosshair example).
//   - Multi-volume per viewport for PET-CT fusion (no separate vtk
//     fusion path to maintain).
//   - W/L tool with smooth interactive drag, plus programmatic
//     ``setProperties({ voiRange })`` for the sidebar sliders.
//   - StackScrollTool for slice scroll (mouse wheel) — sliceIndex
//     state still flows through ``crosshair[2]`` so the existing
//     persistence layer keeps working unchanged.
//
// What stays hand-rolled:
//   - The 3D and MIP viewports (handled by ``VolumeViewer`` and
//     ``MipCineViewport`` siblings; Cornerstone3D's volume render
//     pipeline is fine but the existing one is feature-tuned for
//     our presets / TF editor / threshold).
//   - The ``ObliqueMPRViewport`` (when ``showOblique``); kept
//     because the crosshair already does oblique implicitly.
//
// Feature parity status (English-first, no back-compat needed):
//   ✓ Crosshair sync across the 3 ortho viewports
//   ✓ W/L drag + programmatic set
//   ✓ Slice scroll via wheel + ``setCrosshair`` for keyboard nav
//   ✓ Zoom (right-drag) + Pan (middle-drag)
//   ✓ Per-viewport flip H/V + 90° rotate (via Cornerstone camera)
//   ✓ Auto-WL + auto-invert for PT modality
//   ✓ Fusion overlay (PT on CT) with hot colormap
//   ✗ Measurements / annotations — Phase 2c (Cornerstone has
//     LengthTool, BidirectionalTool, EllipticalROITool, etc., but
//     the persistence layer for our markers needs a separate pass)
//   ✗ Marker fade across slices — Phase 2c
//   ✗ Pixel-value HUD overlay — added as a passive listener; will
//     polish in Phase 2c

import * as cs from "@cornerstonejs/core";
import * as csTools from "@cornerstonejs/tools";
import vtkPlane from "@kitware/vtk.js/Common/DataModel/Plane";
import { useTranslations } from "next-intl";
import dynamic from "next/dynamic";
import {
  forwardRef,
  useCallback,
  useEffect,
  useImperativeHandle,
  useMemo,
  useRef,
  useState,
} from "react";

import { request } from "@/lib/api";
import { ensureCornerstoneInit } from "@/lib/cornerstoneSetup";
import { makeSampler, sampleDisk, worldToIjk } from "@/lib/cornerstoneTools/volumeSampling";
import { buildLocalVolume } from "@/lib/cornerstoneVolume";
import { resolveFusionOrder } from "@/lib/fusionVolumeOrder";
import { LAYOUT_DIMS } from "@/lib/hangingProtocols";
import { extractBidirectionalMm } from "@/lib/measurements";
import {
  type EdgeLetters,
  type OrientationCamera,
  type TransformFlags,
  cameraEdgeLetters,
  cameraTransformFlags,
} from "@/lib/orientationMarkers";
import type { MPRLayoutHandle, MPRLayoutProps } from "./MPRLayoutTypes";
import MarkerOverlay, { type MarkerOverlayItem } from "./MarkerOverlay";
import { useModal } from "./ModalHost";
import OrientationMarkers from "./OrientationMarkers";
import type { VolumeData, VolumeViewerHandle } from "./VolumeViewer";

const VolumeViewer = dynamic(() => import("./VolumeViewer"), { ssr: false });
const CornerstoneMipViewport = dynamic(() => import("./CornerstoneMipViewport"), { ssr: false });
const ObliqueMPRViewport = dynamic(() => import("./ObliqueMPRViewport"), {
  ssr: false,
});

// All Cornerstone-side ids are derived per-instance from the
// ``seriesId`` so multiple ``CornerstoneMPRLayout`` panes can co-exist
// on the same page (the multi-series viewer mounts up to four).
// Cache keys for volumes still share a global namespace via
// ``VOLUME_*_PREFIX`` so a series visible in two panes only loads
// once.
const ENGINE_ID_PREFIX = "bvp-mpr-cs-engine:";
const TOOL_GROUP_PREFIX = "bvp-mpr-cs-toolgroup:";
const VP_AXIAL_PREFIX = "bvp-cs-axial:";
const VP_SAG_PREFIX = "bvp-cs-sagittal:";
const VP_COR_PREFIX = "bvp-cs-coronal:";
const VOLUME_PRIMARY_PREFIX = "bvp-vol-primary:";
const VOLUME_FUSION_PREFIX = "bvp-vol-fusion:";

type Axis = "axial" | "sagittal" | "coronal";
type PaneKey = Axis | "3d" | "mip" | "oblique";

const ORIENT: Record<Axis, cs.Enums.OrientationAxis> = {
  axial: cs.Enums.OrientationAxis.AXIAL,
  sagittal: cs.Enums.OrientationAxis.SAGITTAL,
  coronal: cs.Enums.OrientationAxis.CORONAL,
};

/** Per-instance ids. The ``engineId`` is a SINGLE shared constant so
 *  every pane on the page renders through ONE cornerstone3D
 *  ``RenderingEngine`` (one WebGL context) instead of one engine per
 *  pane. A RenderingEngine drives many viewports off a single shared
 *  offscreen context, so the one-engine-per-pane design exhausted the
 *  browser's ~16-context cap as the user navigated phases/studies
 *  (engines were intentionally never destroyed), losing contexts and
 *  crashing the crosshairs tool. The viewport + tool-group ids stay
 *  per-pane (derived from seriesId) so panes never collide on the
 *  shared engine. */
function makeIds(instanceKey: string) {
  return {
    // SHARED engine. Multi-context approaches (one engine per pane; ContextPool)
    // were tried for the multiphase repaint freeze and made it WORSE — both
    // regressed to 0/4 panes following in the headless GL harness (SwiftShader
    // loses float-texture filtering / stalls readPixels across many contexts).
    // The single shared Tiled engine + a full engine.render() on the crosshair
    // sync repaints all panes on a real GPU (whose offscreen-texture cap fits
    // the 4-pane tile); the headless cap clips it to the left column, so this
    // path is validated on a real GPU, not in SwiftShader.
    engineId: `${ENGINE_ID_PREFIX}shared`,
    toolGroupId: TOOL_GROUP_PREFIX + instanceKey,
    vpAxial: VP_AXIAL_PREFIX + instanceKey,
    vpSag: VP_SAG_PREFIX + instanceKey,
    vpCor: VP_COR_PREFIX + instanceKey,
  };
}

interface ExtendedProps extends MPRLayoutProps {
  /** Optional pre-loaded fusion volume. When set, Cornerstone renders
   *  it as a translucent color overlay on top of the primary volume
   *  (the standard PET-CT fusion view). For loose convenience the
   *  prop accepts the same ``VolumeData`` shape the rest of the app
   *  passes around — we wrap it into a Cornerstone local volume
   *  internally. */
  fusionVolume?: VolumeData | null;
  /** Stable id used to derive the cache key for the fusion volume. */
  fusionSeriesId?: string | null;
  /** "Fusion is expected" hint, derived synchronously from the URL
   *  search param (``?fusion=<id>``) at page mount. Distinct from
   *  ``fusionSeriesId``, which is published by the parent only after
   *  ``fetchVolume`` resolves so the MPR's ``setVolumesForViewports``
   *  call sees the FoR-shared local volume already in the Cornerstone
   *  cache (publishing the seriesId before the volume causes Cornerstone
   *  to register a stub entry with default FoR = volumeId, and the next
   *  layered ``setVolumes`` call rejects two volumes with mismatched
   *  FoRs).
   *
   *  This prop is forwarded to ``CornerstoneMipViewport`` so the MIP can
   *  suspend its ``setup()`` from the very first render — without it,
   *  the MIP would run setup with the CT primary, then re-run with the
   *  PT fusion when the data lands, leaving the renderer on a stale
   *  frame ("white MIP at first mount", chased across beta.72-.79). */
  fusionExpected?: boolean;
  /** Failure flag for the fusion fetch (URL ``?fusion=`` flow). When the
   *  parent's ``fetchVolume(fusionParam)`` rejects (404 / network), it
   *  flips this to ``true`` so the MPR setup gate (and its mirror in
   *  ``CornerstoneMipViewport``) can release without a successful
   *  ``fusionVolume`` payload — both proceed primary-only instead of
   *  hanging in ``fusionPending=true`` forever waiting for a volume
   *  that will never arrive. ``false`` (default) means "in progress
   *  or successful". The page resets it to ``false`` at every retry
   *  and at every ``setCsFusionSeriesId(null)`` clear so the flag
   *  doesn't carry stale failure state across navigations. */
  fusionFailed?: boolean;
  /** Sub-stack index of a multi-stack series (mDIXON W/F/IP/OP, multi-
   *  echo, DWI). Folded into the Cornerstone volume cache key so each
   *  contrast of the same series gets its OWN cache entry and switching
   *  stacks rebuilds the panes instead of re-showing the stale volume.
   *  0 (default) for the common single-stack series. */
  stackIndex?: number;
  /** SUV body-weight factor for the active series, when modality is
   *  PT and the SUV factor is computable from the DICOM header.
   *  When set, the layout post-processes annotation labels so ROI
   *  stats display SUVmean / SUVmax in addition to raw pixel values
   *  (multiply pixel × ``suvFactorBw`` after the chain's RescaleSlope).
   *  ``null`` keeps labels at raw pixel values. */
  suvFactorBw?: number | null;
  /** PET LUT name for the fusion overlay. Drives the RGB transfer
   *  function on the fusion volume actor. Spec §3.2 lists the four
   *  LUTs that nuclear medicine readers expect. */
  petColormap?: PetColormap;
  /** Study id for the active series. When set, the per-viewport camera
   *  overlay can save a captured PNG as a Document attached to this
   *  study via ``POST /api/studies/{studyId}/screenshots``. */
  studyId?: string | null;
  /** Optional callback fired after a screenshot is successfully saved
   *  to the study (parent can refresh its Documents tab). */
  onScreenshotSaved?: (info: { documentId: string; viewport: string }) => void;
  /** Non-cornerstone markers (bbox.lesion, fiducial, text-overlay) the
   *  layout should render on top of the axial viewport via the SVG
   *  ``MarkerOverlay``. Cornerstone-managed markers (Length,
   *  RectangleROI, …) are drawn by Cornerstone itself and MUST be
   *  filtered out by the caller (use ``markerIsManagedByCornerstone``
   *  from ``MarkerOverlay``). v3.5.1 fixed the gap where AI-generated
   *  bbox.lesion had no visual on the canvas. */
  overlayMarkers?: MarkerOverlayItem[];
  /** When set, the matching marker is rendered with a thicker,
   *  pulsing outline; for Cornerstone-managed markers, the layout
   *  flips the annotation's ``isSelected`` flag so Cornerstone applies
   *  its own selected-stroke style. Auto-clear is the caller's
   *  responsibility — typically a setTimeout in the page. */
  focusedMarkerId?: string | null;
  /** Optional click handler for the SVG overlay (the non-cornerstone
   *  side). Cornerstone-managed annotations have their own click
   *  affordance, so this only fires from the overlay. */
  onOverlayMarkerClick?: (markerId: string) => void;
  /** Invoked when the operator pins the Lens probe (click on the
   *  canvas while ``activeTool === "measure-lens"`` or "Pin" button on
   *  the rail panel). The parent persists this as a
   *  ``measurement.ellipse`` marker with ``computed.source =
   *  "lens-probe"`` and rounds-trips to ``/roi-stats`` for the server-
   *  confirmed values; the layout's job stops at emitting the snapshot.
   */
  onLensPin?: (params: {
    axis: Axis;
    centerIjk: [number, number, number];
    radiusMm: number;
    isPet: boolean;
    suvFactor: number | null;
    stats: { count: number; mean: number; std: number; min: number; max: number };
  }) => void;
}

/** PET colormap names exposed via the sidebar selector. Each value
 *  maps to an RGB transfer function applied on the fusion volume's
 *  vtk colour function (see ``applyPetColormap``). The names match
 *  spec §3.2: hot iron, PET rainbow, inverse grayscale, hot metal. */
export type PetColormap = "hot-iron" | "rainbow" | "inverse-gray" | "hot-metal";

// The Cornerstone volume builder lives in ``@/lib/cornerstoneVolume``
// (``buildLocalVolume``) so the MPR layout, the MIP/3D viewport and the
// alternate cornerstone route all produce an identical volume for the
// same ``volumeId`` (they share the Cornerstone cache).

/** Apply one of the four PET LUTs from spec §3.2 to a vtk colour
 *  transfer function. Inputs `t` are in [0, 1] (the normalised SUV
 *  position) — the actor's own scalar opacity already gates which
 *  values reach the colour function, so the LUT only specifies the
 *  RGB ramp. */
function applyPetColormap(
  ctf: {
    addRGBPoint: (x: number, r: number, g: number, b: number) => void;
    removeAllPoints: () => void;
  },
  name: PetColormap,
): void {
  ctf.removeAllPoints();
  switch (name) {
    case "hot-iron":
      // black → red → yellow → white (NM convention for FDG)
      ctf.addRGBPoint(0, 0, 0, 0);
      ctf.addRGBPoint(0.5, 0.4, 0, 0);
      ctf.addRGBPoint(0.7, 0.95, 0.35, 0);
      ctf.addRGBPoint(0.85, 1, 0.85, 0.1);
      ctf.addRGBPoint(1, 1, 1, 0.95);
      break;
    case "rainbow":
      // blue → green → yellow → red (PET rainbow, high dynamic)
      ctf.addRGBPoint(0, 0, 0, 0.5);
      ctf.addRGBPoint(0.25, 0, 0.6, 0.95);
      ctf.addRGBPoint(0.5, 0, 0.95, 0.2);
      ctf.addRGBPoint(0.75, 0.95, 0.95, 0);
      ctf.addRGBPoint(1, 0.95, 0, 0);
      break;
    case "inverse-gray":
      // white → black (radiologist reading convention for MIP)
      ctf.addRGBPoint(0, 1, 1, 1);
      ctf.addRGBPoint(1, 0, 0, 0);
      break;
    case "hot-metal":
      // hot-iron variant with extra mid-range contrast for small lesions
      ctf.addRGBPoint(0, 0, 0, 0);
      ctf.addRGBPoint(0.3, 0.3, 0, 0);
      ctf.addRGBPoint(0.55, 0.85, 0.2, 0);
      ctf.addRGBPoint(0.75, 1, 0.6, 0);
      ctf.addRGBPoint(0.9, 1, 0.95, 0.3);
      ctf.addRGBPoint(1, 1, 1, 1);
      break;
  }
}

/** Register the four PET LUTs with Cornerstone3D's colormap registry
 *  so they can be applied via ``vp.setProperties({colormap: {name}},
 *  volumeId)`` — the only path that goes through CS3D's
 *  ``_getApplicableVolumeActor`` and reliably finds the fusion
 *  volume actor regardless of how it was bound. The previous
 *  hand-rolled ``vp.getActor(volumeId)`` lookup returned undefined
 *  on volumes mounted via ``setVolumesForViewports`` because the
 *  internal actor UID differs from the volumeId in CS3D 4.x.
 *
 *  Idempotent: ``registerColormap`` overwrites by name, so calling
 *  this on every viewer mount is harmless. The names are
 *  ``bvp-pet-*`` to avoid collisions with CS3D's own presets.
 *
 *  ``RGBPoints`` is the flat ``[t, r, g, b, t, r, g, b, ...]`` shape
 *  CS3D's registry expects; the ramps below mirror exactly the
 *  ``applyPetColormap`` cases above so the on-canvas appearance is
 *  identical between the legacy initial-mount path and the
 *  setProperties path. */
function registerPetColormaps(): void {
  const csAny = cs as unknown as {
    utilities?: {
      colormap?: {
        registerColormap?: (m: {
          Name: string;
          ColorSpace: string;
          RGBPoints: number[];
        }) => void;
      };
    };
  };
  const reg = csAny.utilities?.colormap?.registerColormap;
  if (!reg) return;
  const ramps: Array<{ name: string; rgb: number[] }> = [
    {
      name: "bvp-pet-hot-iron",
      rgb: [0, 0, 0, 0, 0.5, 0.4, 0, 0, 0.7, 0.95, 0.35, 0, 0.85, 1, 0.85, 0.1, 1, 1, 1, 0.95],
    },
    {
      name: "bvp-pet-rainbow",
      rgb: [
        0, 0, 0, 0.5, 0.25, 0, 0.6, 0.95, 0.5, 0, 0.95, 0.2, 0.75, 0.95, 0.95, 0, 1, 0.95, 0, 0,
      ],
    },
    {
      name: "bvp-pet-inverse-gray",
      rgb: [0, 1, 1, 1, 1, 0, 0, 0],
    },
    {
      name: "bvp-pet-hot-metal",
      rgb: [
        0, 0, 0, 0, 0.3, 0.3, 0, 0, 0.55, 0.85, 0.2, 0, 0.75, 1, 0.6, 0, 0.9, 1, 0.95, 0.3, 1, 1, 1,
        1,
      ],
    },
  ];
  for (const { name, rgb } of ramps) {
    try {
      reg({ Name: name, ColorSpace: "RGB", RGBPoints: rgb });
    } catch {
      /* registry not writable in this build — fall back to the
         legacy in-place CTF path which we keep for compatibility */
    }
  }
}

/** Mapping from the public ``PetColormap`` token to the registered
 *  CS3D colormap name. Kept in one place so the picker labels and
 *  the registry stay in sync. */
const PET_COLORMAP_REGISTRY_NAME: Record<PetColormap, string> = {
  "hot-iron": "bvp-pet-hot-iron",
  rainbow: "bvp-pet-rainbow",
  "inverse-gray": "bvp-pet-inverse-gray",
  "hot-metal": "bvp-pet-hot-metal",
};

const CornerstoneMPRLayout = forwardRef<MPRLayoutHandle, ExtendedProps>(
  function CornerstoneMPRLayout(
    {
      volume,
      showAxial = true,
      showSagittal = false,
      showCoronal = false,
      show3D = false,
      showMip = false,
      layout,
      showOblique = false,
      onCrosshairChange,
      activeTool,
      onActiveToolChange,
      seriesDescription,
      seriesId,
      stackIndex = 0,
      volumeViewerRef,
      modality,
      suggestedVoi,
      customOpacityStops,
      customRange,
      customColorStops,
      cropBox,
      fusionVolume,
      fusionSeriesId,
      fusionExpected,
      fusionFailed,
      fusionModality,
      onMeasurementsChange,
      measurements: incomingMeasurements,
      markerFade,
      suvFactorBw,
      petColormap = "hot-iron",
      petSuvHide,
      studyId,
      onScreenshotSaved,
      overlayMarkers,
      focusedMarkerId,
      onOverlayMarkerClick,
      onLensPin,
    },
    ref,
  ) {
    const [nx, ny, nz] = volume.dimensions;
    const defaultWC = (volume.range[0] + volume.range[1]) / 2;
    const defaultWW = volume.range[1] - volume.range[0];
    const [wc, setWC] = useState(defaultWC);
    const [ww, setWW] = useState(defaultWW);
    const [invert, setInvert] = useState(false);
    // Per-modality VOI state. The spec is explicit: "Cambiare la
    // finestra PET in una viewport deve aggiornarla in tutte le
    // altre viewport che mostrano PET. Cambiare W/L su CT non deve
    // toccare la PET." We honour this by keeping the fusion volume
    // on its own (wc, ww) pair, plumbed through Cornerstone's
    // per-volume ``setProperties(voi, volumeId)`` API. The primary
    // ``(wc, ww)`` above remains the CT (or non-PT) window.
    const [fusionWC, setFusionWC] = useState(0);
    const [fusionWW, setFusionWW] = useState(1);
    // Fusion overlay alpha multiplier. Spec §2.2: opacity slider,
    // default 50% (we keep the existing 0.05 conservative default
    // because the cumulative-alpha cylinder problem the user hit
    // originally is solved by the OTF cutoff, not by clamping
    // global alpha; 0.05 is the empirically useful "halo" value).
    const [fusionAlpha, setFusionAlpha] = useState(0.6);
    // Fusion overlay visibility toggle. Standard PET-CT review
    // workflow: flip the PET layer off to see the underlying CT
    // alone, then flip it back on to see what the metabolic signal
    // adds over the structural finding. We zero the OTF of the
    // fusion actor instead of detaching the volume — that way
    // toggling back on doesn't trigger a fresh re-bind through
    // setVolumesForViewports (which would dispatch the
    // colormap/opacity callback again and briefly flicker).
    const [fusionVisible, setFusionVisible] = useState(true);
    const [crosshair, setCrosshair] = useState<[number, number, number]>([
      Math.floor(nx / 2),
      Math.floor(ny / 2),
      Math.floor(nz / 2),
    ]);

    const axialDivRef = useRef<HTMLDivElement | null>(null);
    const sagDivRef = useRef<HTMLDivElement | null>(null);
    const corDivRef = useRef<HTMLDivElement | null>(null);
    const engineRef = useRef<cs.RenderingEngine | null>(null);
    // One-shot ResizeObserver that fits the camera the moment this pane's
    // canvas first gets a stable non-zero box — the safety net for a grid
    // cell (e.g. the 2nd compare pane) that lays out after the rAF settle
    // loop gives up. Disconnected after the first fit / on unmount.
    const settleObserverRef = useRef<ResizeObserver | null>(null);
    // Tracks which volumeId we have already auto-windowed. The modality
    // auto-WL must re-apply whenever a pane's volume (re)loads or is swapped
    // — NOT just once. A one-shot boolean here left a re-run of setup() (deps:
    // volume / volumeId / modality) skipping the auto-WL, so the pane kept the
    // flat full-range default window (defaultWW = range[1]-range[0]) and a CT
    // rendered all black (e.g. the basale / tardiva phases of a contrast study
    // whose volume or modality settled in a second setup pass).
    const autoWindowedVolumeRef = useRef<string | null>(null);
    // ``true`` when the last setCrosshairWorld mapped a world point that fell
    // OUTSIDE this pane's voxel grid (clamped to the nearest valid slice). The
    // phases of a multiphase study share a FrameOfReference but cover different
    // z-extents, so a synced world point can land beyond a shorter phase; the
    // UI marks such a pane "out of coverage" instead of showing wrong anatomy.
    const coverageRef = useRef(false);
    // ``true`` once ``setup()`` has finished registering the tool
    // group. The activeTool effect bails until then and re-runs
    // when this flips, so the very first tool selection (e.g. the
    // user picking "Distance" before the volume is wired) actually
    // takes effect instead of being silently dropped.
    const [toolGroupReady, setToolGroupReady] = useState(false);

    // Modal API for in-app prompts (replaces the native ``window.prompt``
    // that ``ArrowAnnotateTool`` defaults to). Held in a ref so the
    // tool callbacks — registered once at setup time — see the latest
    // function without needing to re-register on every render.
    const modalApi = useModal();
    const promptRef = useRef(modalApi.prompt);
    useEffect(() => {
      promptRef.current = modalApi.prompt;
    }, [modalApi.prompt]);

    const tMarker = useTranslations("markerEdit");
    const markerLabelsRef = useRef({
      annotationTitle: tMarker("annotationTitle"),
      annotationLabel: tMarker("annotationLabel"),
      annotationPlaceholder: tMarker("annotationPlaceholder"),
      annotationEditTitle: tMarker("annotationEditTitle"),
      measurementTitle: tMarker("measurementTitle"),
      measurementLabelLabel: tMarker("measurementLabelLabel"),
    });
    useEffect(() => {
      markerLabelsRef.current = {
        annotationTitle: tMarker("annotationTitle"),
        annotationLabel: tMarker("annotationLabel"),
        annotationPlaceholder: tMarker("annotationPlaceholder"),
        annotationEditTitle: tMarker("annotationEditTitle"),
        measurementTitle: tMarker("measurementTitle"),
        measurementLabelLabel: tMarker("measurementLabelLabel"),
      };
    }, [tMarker]);

    const instanceKey = seriesId ?? "anon";
    const ids = useMemo(() => makeIds(instanceKey), [instanceKey]);
    const { engineId, toolGroupId, vpAxial, vpSag, vpCor } = ids;
    const viewportIdByAxis: Record<Axis, string> = useMemo(
      () => ({ axial: vpAxial, sagittal: vpSag, coronal: vpCor }),
      [vpAxial, vpSag, vpCor],
    );

    // Fold the sub-stack index into the cache key so each contrast of a
    // multi-stack series (mDIXON W/F/IP/OP) gets its own Cornerstone
    // volume; switching rebuilds the panes instead of re-showing the
    // stale stack. Stack 0 keeps the legacy key verbatim (no regression
    // for the common single-stack series).
    const volumeId = useMemo(
      () =>
        `${VOLUME_PRIMARY_PREFIX}${seriesId ?? "anon"}${stackIndex > 0 ? `:s${stackIndex}` : ""}${
          volume?.resolution === "preview" ? ":preview" : ""
        }`,
      [seriesId, stackIndex, volume?.resolution],
    );
    const fusionVolumeId = useMemo(
      () => (fusionVolume && fusionSeriesId ? `${VOLUME_FUSION_PREFIX}${fusionSeriesId}` : null),
      [fusionVolume, fusionSeriesId],
    );

    // Per-viewport voxel-value overlay text. Updated by mousemove
    // listeners we attach directly to the Cornerstone viewport divs;
    // setting React state on every mousemove is fine because the
    // overlay JSX is the only consumer.
    const [voxelHud, setVoxelHud] = useState<Record<Axis, string | null>>({
      axial: null,
      sagittal: null,
      coronal: null,
    });

    // Lens probe state. Active only when ``activeTool === "measure-lens"``.
    // Radius is shared across the three MPR panes (the same disc size
    // is what the operator wants regardless of which pane they hover).
    // Readout is per-axis because the cursor is in one pane at a time.
    const [lensRadiusMm, setLensRadiusMmState] = useState<number>(5);
    const lensRadiusRef = useRef(lensRadiusMm);
    useEffect(() => {
      lensRadiusRef.current = lensRadiusMm;
    }, [lensRadiusMm]);
    type LensReadout = {
      canvasCenter: { x: number; y: number };
      radiusCanvasPx: number;
      lines: string[];
    };
    const [lensReadout, setLensReadout] = useState<Record<Axis, LensReadout | null>>({
      axial: null,
      sagittal: null,
      coronal: null,
    });
    // Most-recent hover snapshot per axis. Used by the rail panel's
    // "Pin" button (imperative ``pinCurrentLens``) so the operator can
    // persist a readout without re-clicking on the canvas. We keep one
    // entry per axis (the operator may have hovered axial last but be
    // looking at sagittal); ``pinCurrentLens`` picks the most-recently
    // updated one.
    type LensSnapshot = {
      axis: Axis;
      centerIjk: [number, number, number];
      stats: { count: number; mean: number; std: number; min: number; max: number };
      timestamp: number;
    };
    const lensSnapshotRef = useRef<LensSnapshot | null>(null);
    // Legacy in-pane "i" toggle removed: the toolbar's ``activeTool``
    // is now the single source of truth (default = crosshair, opt-in
    // for everything else). Kept the comment as a breadcrumb in case
    // the toggle resurfaces — see ``activeTool`` handling below.
    // Persistent corner overlays — modality + slice + W/L + zoom +
    // voxel HUD. Default-on; the user can toggle individual corners
    // from the top-right gear (TODO Phase 2d). Match the standard
    // PACS layout: top-left identity, top-right slice/window,
    // bottom-left zoom + body-part, bottom-right cursor readout.
    const [overlaysOn, setOverlaysOn] = useState(true);
    // Per-pane maximize toggle. When set, only that pane is shown,
    // filling the whole grid. Click the same maximize button again
    // to revert. The selection is keyed by ``PaneKey`` (axial /
    // sagittal / coronal / 3d / mip / oblique). The 3 ortho axes are
    // always mounted so the engine binding stays stable across
    // toggles, so "hide non-maximized axis panes" boils down to
    // ``display: none`` on the wrappers (the engine still owns its
    // viewport, ResizeObserver brings the canvas back to size when
    // the pane reappears).
    const [maximizedPane, setMaximizedPane] = useState<PaneKey | null>(null);
    // Camera-icon menu open/closed per pane. When ``cameraMenuFor``
    // matches a pane, the popover with Download / Clipboard / Save-to-
    // study actions is rendered next to the camera button. ``null``
    // means no menu open (default). One menu at a time keeps the
    // overlay light and avoids two popovers fighting for the same
    // click-outside dismiss listener.
    const [cameraMenuFor, setCameraMenuFor] = useState<PaneKey | null>(null);
    // Per-pane busy flag while a save-to-study request is in flight,
    // so the user gets a spinner / disabled menu rather than a silent
    // double-click that uploads two copies. Cleared on success or
    // error.
    const [screenshotBusy, setScreenshotBusy] = useState<PaneKey | null>(null);
    // Per-pane status string ("Saved", "Copied", "Downloaded",
    // "Error: …") shown for ~2.5 s after an action so the user gets
    // feedback without leaving the viewport.
    const [screenshotStatus, setScreenshotStatus] = useState<{
      pane: PaneKey;
      msg: string;
    } | null>(null);
    // Live zoom + slice index per viewport, sampled from the camera
    // events. Resets on volume change.
    const [perVpInfo, setPerVpInfo] = useState<
      Record<Axis, { sliceIndex: number; sliceTotal: number; zoomPct: number }>
    >({
      axial: { sliceIndex: 0, sliceTotal: 0, zoomPct: 100 },
      sagittal: { sliceIndex: 0, sliceTotal: 0, zoomPct: 100 },
      coronal: { sliceIndex: 0, sliceTotal: 0, zoomPct: 100 },
    });

    // Per-pane anatomical edge letters + transform flags, recomputed from
    // each viewport camera on CAMERA_MODIFIED. Only meaningful when the
    // volume carries real DICOM geometry (``volume.direction``); on a
    // legacy identity-frame pack we hide the letters rather than show an
    // assumption (a confident-but-wrong L/R is a wrong-side hazard).
    const hasRealGeometry = Array.isArray(volume.direction);
    const [orientationByAxis, setOrientationByAxis] = useState<
      Record<Axis, { letters: EdgeLetters | null; flags: TransformFlags }>
    >({
      axial: { letters: null, flags: { flipped: false, rotated: false } },
      sagittal: { letters: null, flags: { flipped: false, rotated: false } },
      coronal: { letters: null, flags: { flipped: false, rotated: false } },
    });

    // Mount: init Cornerstone, register the volume, build viewports
    // for the visible axes, attach tools.
    // biome-ignore lint/correctness/useExhaustiveDependencies: viewer lifecycle effect — re-running on derived deps would tear down GPU resources.
    useEffect(() => {
      let cancelled = false;
      const setup = async () => {
        await ensureCornerstoneInit();
        // PET colormaps go in the CS3D registry once per session;
        // ``registerColormap`` overwrites by name so calling this
        // on every mount is harmless. The names are then resolvable
        // via ``vp.setProperties({colormap: {name}}, volumeId)``.
        registerPetColormaps();
        if (cancelled) return;

        // Gate: defer la creazione dell'engine quando la fusion è attesa
        // via URL ?fusion= ma il volume non è ancora landato. Se entrassimo
        // qui senza il gate il layout chiamerebbe ``setVolumesForViewports``
        // due volte: una con il solo primary (la fusion arriva ~1.5s dopo
        // via fetchVolume), poi una seconda volta con entrambi i volumi.
        // La seconda passata distrugge/ricrea il toolGroup, sporcando lo
        // stato dell'engine condiviso. (La MIP vive sullo stesso engine
        // via ``enableElement``: ora che la registrazione MPR e' additiva
        // — ``disableElement``/``enableElement`` solo dei propri viewport,
        // non piu' ``setViewports`` che li rimpiazzava tutti — la MIP non
        // viene piu' azzerata, ma il churn del toolGroup resta da evitare.)
        //
        // Speculare al gate identico in CornerstoneMipViewport (beta.81).
        // Qui è essenziale gattare PRIMA della creazione dell'engine,
        // altrimenti la MIP fa polling su ``cs.getRenderingEngine``,
        // lo trova mid-flight e si attacca, e la seconda passata MPR
        // glielo rimpiazza sotto i piedi comunque.
        //
        // Failure path: ``fusionFailed=true`` (settato dal page sul
        // ``.catch()`` o sul watchdog 30s di ``fetchVolume``) rilascia
        // il gate immediatamente; la condizione ``fusionReady`` a L613
        // poi degrada il volumeInputs a primary-only naturalmente,
        // identico al comportamento silent-failure pre-fix.
        const fusionPending =
          fusionExpected === true &&
          fusionFailed !== true &&
          (fusionVolume == null || fusionVolumeId == null || fusionModality == null);
        if (fusionPending) {
          // eslint-disable-next-line no-console
          console.debug("[viewer:fusion-setup]", {
            phase: "gate_pending",
            hasVolume: !!fusionVolume,
            hasId: !!fusionVolumeId,
            hasModality: !!fusionModality,
            fusionFailed: !!fusionFailed,
            ts: Date.now(),
          });
          return;
        }

        // Resolve the FrameOfReferenceUID to build each volume with.
        // Cornerstone only layers two volumes on one viewport when they
        // share a FoR. We use the volumes' REAL FoR (recovered from
        // ``X-Volume-*`` headers) so the FoR-mismatch safety check stays
        // meaningful instead of being defeated by a blanket synthetic id:
        //   - no fusion → the primary's real FoR (synthetic only for a
        //     legacy pack with no geometry).
        //   - fusion with MATCHING real FoRs → genuinely co-registered;
        //     share the real FoR.
        //   - fusion with DIFFERING / missing FoRs → not hardware
        //     co-registered; synthesise a shared id so the overlay the
        //     user explicitly requested still renders, and warn loudly
        //     that the alignment is unverified.
        const syntheticFoR = `bvp-for:${seriesId ?? "anon"}`;
        const primaryFoR = volume.frameOfReferenceUid;
        const fusionFoR = fusionVolume?.frameOfReferenceUid;
        let primaryRenderFoR: string;
        let fusionRenderFoR: string | undefined;
        if (fusionVolume && fusionVolumeId) {
          const coRegistered = !!primaryFoR && !!fusionFoR && primaryFoR === fusionFoR;
          if (coRegistered) {
            primaryRenderFoR = primaryFoR as string;
            fusionRenderFoR = fusionFoR;
          } else {
            primaryRenderFoR = syntheticFoR;
            fusionRenderFoR = syntheticFoR;
            if (primaryFoR && fusionFoR && primaryFoR !== fusionFoR) {
              // eslint-disable-next-line no-console
              console.warn(
                "[viewer:fusion] FrameOfReferenceUID mismatch — fusion overlay alignment is NOT verified",
                { primaryFoR, fusionFoR },
              );
            }
          }
        } else {
          primaryRenderFoR = primaryFoR ?? syntheticFoR;
        }
        if (!cs.cache.getVolume(volumeId)) {
          buildLocalVolume(volumeId, volume, primaryRenderFoR);
        }
        if (fusionVolume && fusionVolumeId && !cs.cache.getVolume(fusionVolumeId)) {
          buildLocalVolume(fusionVolumeId, fusionVolume, fusionRenderFoR);
        }

        let engine = cs.getRenderingEngine(engineId);
        if (!engine) engine = new cs.RenderingEngine(engineId);
        engineRef.current = engine;

        // Wire all three ortho axes onto their (always-mounted) divs.
        // The render tree below mounts axial / sagittal / coronal
        // wrappers unconditionally and toggles ``display: none`` for
        // axes the parent doesn't want — this keeps the engine ↔ DOM
        // binding stable across show* toggles. Without that, toggling
        // an axis off and back on left the engine pointing at a
        // detached DOM node and the new node un-bound (the canvas
        // never appeared, see "after Axial ON again" in the toggle
        // repro).
        const inputs: cs.Types.PublicViewportInput[] = [];
        const axisToElement: { axis: Axis; el: HTMLDivElement }[] = [];
        const allAxes: Array<[Axis, React.MutableRefObject<HTMLDivElement | null>]> = [
          ["axial", axialDivRef],
          ["sagittal", sagDivRef],
          ["coronal", corDivRef],
        ];
        for (const [axis, divRef] of allAxes) {
          const el = divRef.current;
          if (!el) continue;
          inputs.push({
            viewportId: viewportIdByAxis[axis],
            type: cs.Enums.ViewportType.ORTHOGRAPHIC,
            element: el,
            defaultOptions: {
              orientation: ORIENT[axis],
              background: [0, 0, 0],
            },
          });
          axisToElement.push({ axis, el });
        }
        if (inputs.length === 0) return;
        // Shared engine: register THIS pane's viewports ADDITIVELY.
        // ``setViewports`` would replace ALL viewports on the engine and
        // thus wipe every sibling pane. Disable our own three first to
        // clear any stale binding from a prior seriesId / re-run, then
        // enable. Sibling panes' viewports (different ids) are untouched.
        for (const input of inputs) {
          try {
            engine.disableElement(input.viewportId);
          } catch {
            /* not currently enabled */
          }
        }
        for (const input of inputs) engine.enableElement(input);

        // Hot colormap + soft-shoulder opacity for the fusion overlay
        // (PET-on-CT default). Background tissue stays fully
        // transparent and only upper-percentile uptake glows through
        // (Addendum A §5: ``smoothstep(threshold, threshold * 1.5)``).
        const fusionOverlay = (overlayVolumeId: string) => ({
          volumeId: overlayVolumeId,
          callback: ({
            volumeActor,
          }: {
            volumeActor: {
              getProperty: () => {
                getScalarOpacity: (i: number) => unknown;
                getRGBTransferFunction: (i: number) => unknown;
              };
            };
          }) => {
            const ofun = volumeActor.getProperty().getScalarOpacity(0) as {
              addPoint: (x: number, y: number) => void;
              removeAllPoints: () => void;
            };
            ofun.removeAllPoints();
            ofun.addPoint(0, 0);
            ofun.addPoint(0.55, 0);
            ofun.addPoint(0.6, 0.05);
            ofun.addPoint(0.7, 0.3);
            ofun.addPoint(0.8, 0.6);
            ofun.addPoint(1, 0.9);
            const ctf = volumeActor.getProperty().getRGBTransferFunction(0) as {
              addRGBPoint: (x: number, r: number, g: number, b: number) => void;
              removeAllPoints: () => void;
            };
            applyPetColormap(ctf, petColormap);
          },
        });

        // Decide ordering by modality (pure function, unit tested in
        // ``__tests__/fusionVolumeOrder.test.ts``). Functional (PT/NM)
        // is always the overlay, anatomical (CT/MR/...) the base —
        // independent of which one the user opened first. This kills
        // the "PET in foreground until reload" race because the order
        // no longer depends on async cache timing.
        const fusionReady = !!fusionVolumeId && !!cs.cache.getVolume(fusionVolumeId);
        const order = resolveFusionOrder(
          { volumeId, modality: modality ?? null },
          fusionReady && fusionVolumeId
            ? { volumeId: fusionVolumeId, modality: fusionModality ?? null }
            : null,
        );
        const volumeInputs: cs.Types.IVolumeInput[] = order.overlayVolumeId
          ? [{ volumeId: order.baseVolumeId }, fusionOverlay(order.overlayVolumeId)]
          : [{ volumeId: order.baseVolumeId }];
        // Telemetry: log the resolved ordering so a future regression
        // (volume swap mid-flight, modality misdetection) is greppable
        // in DevTools without re-instrumenting.
        // eslint-disable-next-line no-console
        console.debug("[viewer:fusion-setup]", {
          phase: "before_setVolumesForViewports",
          primary: {
            volumeId,
            modality: (modality ?? "").toUpperCase() || null,
            cached: !!cs.cache.getVolume(volumeId),
          },
          fusion: fusionVolumeId
            ? {
                volumeId: fusionVolumeId,
                modality: (fusionModality ?? "").toUpperCase() || null,
                cached: fusionReady,
              }
            : null,
          resolvedOrder: volumeInputs.map((vi) => vi.volumeId),
          swapped: order.swapped,
          ts: Date.now(),
        });
        await cs.setVolumesForViewports(
          engine,
          volumeInputs,
          inputs.map((i) => i.viewportId),
        );

        // Tool group: one per layout. Recreate so previous lifecycle
        // (different seriesId) doesn't leave stale viewport bindings.
        try {
          csTools.ToolGroupManager.destroyToolGroup(toolGroupId);
        } catch {
          /* not previously created */
        }
        const tg = csTools.ToolGroupManager.createToolGroup(toolGroupId);
        if (!tg) return;
        for (const i of inputs) tg.addViewport(i.viewportId, engineId);
        tg.addTool(csTools.CrosshairsTool.toolName, {
          getReferenceLineColor: (vpId: string) =>
            vpId === vpAxial
              ? "rgb(68, 170, 255)"
              : vpId === vpSag
                ? "rgb(255, 68, 68)"
                : "rgb(68, 255, 68)",
        });
        tg.addTool(csTools.WindowLevelTool.toolName);
        tg.addTool(csTools.PanTool.toolName);
        tg.addTool(csTools.ZoomTool.toolName);
        tg.addTool(csTools.StackScrollTool.toolName);
        // Measurement tools start passive; the ``activeTool`` prop
        // change-effect activates the matching one on Primary mouse
        // when the user picks a measure-* button in the toolbar.
        //
        // Each measurement tool gets a ``getTextLines`` override so
        // that ``annotation.data.label`` (set by the post-completion
        // prompt below) renders alongside the auto-computed value.
        // Without the override Cornerstone's defaults emit only the
        // numeric value — labels would be stored but never drawn.
        const round = (n: number) =>
          Math.abs(n) >= 100 ? n.toFixed(0) : Math.abs(n) >= 10 ? n.toFixed(1) : n.toFixed(2);
        const lengthLines = (
          data: {
            label?: string;
            cachedStats?: Record<string, { length?: number; unit?: string }>;
          },
          targetId: string,
        ) => {
          const stats = data.cachedStats?.[targetId];
          const lines: string[] = [];
          if (data.label) lines.push(data.label);
          if (stats?.length != null && Number.isFinite(stats.length)) {
            lines.push(`${round(stats.length)} ${stats.unit ?? "mm"}`);
          }
          return lines;
        };
        const angleLines = (
          data: {
            label?: string;
            cachedStats?: Record<string, { angle?: number; unit?: string }>;
          },
          targetId: string,
        ) => {
          const stats = data.cachedStats?.[targetId];
          const lines: string[] = [];
          if (data.label) lines.push(data.label);
          if (stats?.angle != null && Number.isFinite(stats.angle)) {
            lines.push(`${round(stats.angle)} ${stats.unit ?? "°"}`);
          }
          return lines;
        };
        const roiLines = (
          data: {
            label?: string;
            cachedStats?: Record<
              string,
              {
                area?: number;
                areaUnit?: string;
                mean?: number;
                stdDev?: number;
                max?: number;
                min?: number;
                modalityUnit?: string;
                // Custom field populated by the server-side roi-stats
                // listener (see ``ANNOTATION_COMPLETED`` handler below).
                // ``suvPeak`` is the PERCIST 1.0 1 cm³ kernel mean
                // around the ROI's argmax voxel — only meaningful on
                // PET, undefined elsewhere. ``suvSd`` is needed by the
                // PERCIST measurable-lesion floor (1.5 × mean + 2 × SD).
                bvSuvMean?: number;
                bvSuvSd?: number;
                bvSuvMax?: number;
                bvSuvPeak?: number;
                bvSuvPending?: boolean;
              }
            >;
          },
          targetId: string,
        ) => {
          const stats = data.cachedStats?.[targetId] ?? {};
          const lines: string[] = [];
          if (data.label) lines.push(data.label);
          if (stats.area != null && Number.isFinite(stats.area)) {
            lines.push(`${round(stats.area)} ${stats.areaUnit ?? "mm²"}`);
          }
          if (stats.mean != null && Number.isFinite(stats.mean)) {
            const unit = stats.modalityUnit ?? "";
            const sd =
              stats.stdDev != null && Number.isFinite(stats.stdDev)
                ? ` ± ${round(stats.stdDev)}`
                : "";
            lines.push(`Mean ${round(stats.mean)}${sd} ${unit}`.trim());
          }
          if (
            stats.max != null &&
            Number.isFinite(stats.max) &&
            stats.min != null &&
            Number.isFinite(stats.min)
          ) {
            lines.push(`Max ${round(stats.max)} · Min ${round(stats.min)}`);
          }
          // PET enrichment (server-side roi-stats). Surfaced as a
          // separate line so it doesn't clobber the native CS3D
          // mean/max display. ``bvSuvPending`` shows a spinner-dot
          // while the request is in flight.
          if (stats.bvSuvPending) {
            lines.push("SUVpeak: …");
          } else if (stats.bvSuvPeak != null && Number.isFinite(stats.bvSuvPeak)) {
            const peak = round(stats.bvSuvPeak);
            const mean =
              stats.bvSuvMean != null && Number.isFinite(stats.bvSuvMean)
                ? round(stats.bvSuvMean)
                : null;
            const sd =
              stats.bvSuvSd != null && Number.isFinite(stats.bvSuvSd) ? round(stats.bvSuvSd) : null;
            const max =
              stats.bvSuvMax != null && Number.isFinite(stats.bvSuvMax)
                ? round(stats.bvSuvMax)
                : null;
            const parts: string[] = [];
            // ``mean ± sd`` keeps the PERCIST helper inputs visible
            // on the overlay so the user can read them directly from
            // the canvas without opening the side panel.
            if (mean !== null) {
              parts.push(sd !== null ? `mean ${mean} ± ${sd}` : `mean ${mean}`);
            }
            if (max !== null) parts.push(`max ${max}`);
            parts.push(`peak ${peak}`);
            lines.push(`SUV ✓ ${parts.join(" · ")}`);
          }
          return lines;
        };
        const probeLines = (
          data: {
            label?: string;
            cachedStats?: Record<string, { value?: number; modalityUnit?: string }>;
          },
          targetId: string,
        ) => {
          const stats = data.cachedStats?.[targetId];
          const lines: string[] = [];
          if (data.label) lines.push(data.label);
          if (stats?.value != null && Number.isFinite(stats.value)) {
            lines.push(`${round(stats.value)} ${stats.modalityUnit ?? ""}`.trim());
          }
          return lines;
        };
        tg.addTool(csTools.LengthTool.toolName, { getTextLines: lengthLines });
        tg.addTool(csTools.AngleTool.toolName, { getTextLines: angleLines });
        tg.addTool(csTools.EllipticalROITool.toolName, { getTextLines: roiLines });
        tg.addTool(csTools.RectangleROITool.toolName, { getTextLines: roiLines });
        tg.addTool(csTools.CircleROITool.toolName, { getTextLines: roiLines });
        // ArrowAnnotateTool defaults to ``window.prompt(...)`` for both
        // the initial label and subsequent edits. Inject our in-app
        // modal here so the user gets the themed centered dialog
        // instead of the browser bar at the top of the window.
        const arrowPrompt = (done: (label?: string) => void, defaultValue?: string) => {
          promptRef
            .current({
              title: markerLabelsRef.current.annotationTitle,
              label: markerLabelsRef.current.annotationLabel,
              defaultValue: defaultValue ?? "",
              placeholder: markerLabelsRef.current.annotationPlaceholder,
            })
            .then((text) => {
              // ``null`` (cancel) is forwarded as ``undefined`` so the
              // tool drops the in-progress annotation cleanly.
              done(text == null ? undefined : text);
            })
            .catch(() => done(undefined));
        };
        tg.addTool(csTools.ArrowAnnotateTool.toolName, {
          getTextCallback: (done: (label?: string) => void) => arrowPrompt(done),
          changeTextCallback: (
            data: { data?: { text?: string } } | undefined,
            _evtData: unknown,
            done: (data: unknown, evtData: unknown, label?: string) => void,
          ) =>
            promptRef
              .current({
                title: markerLabelsRef.current.annotationEditTitle,
                label: markerLabelsRef.current.annotationLabel,
                defaultValue: data?.data?.text ?? "",
                placeholder: markerLabelsRef.current.annotationPlaceholder,
              })
              .then((text) => done(data, _evtData, text == null ? undefined : text))
              .catch(() => done(data, _evtData, undefined)),
        });
        tg.addTool(csTools.ProbeTool.toolName);
        tg.addTool(csTools.PlanarFreehandROITool.toolName);
        tg.addTool(csTools.BidirectionalTool.toolName);
        // Default primary = crosshair (matches OHIF + the example
        // the user pointed at). W/L drag isn't bound to mouse here
        // — the sidebar slider remains the canonical W/L control,
        // and a tool-switcher toolbar can be wired in Phase 2c when
        // we want primary-WL back.
        tg.setToolActive(csTools.CrosshairsTool.toolName, {
          bindings: [{ mouseButton: csTools.Enums.MouseBindings.Primary }],
        });
        tg.setToolPassive(csTools.WindowLevelTool.toolName);
        tg.setToolActive(csTools.PanTool.toolName, {
          bindings: [{ mouseButton: csTools.Enums.MouseBindings.Auxiliary }],
        });
        tg.setToolActive(csTools.ZoomTool.toolName, {
          bindings: [{ mouseButton: csTools.Enums.MouseBindings.Secondary }],
        });
        tg.setToolActive(csTools.StackScrollTool.toolName, {
          bindings: [{ mouseButton: csTools.Enums.MouseBindings.Wheel }],
        });

        // Auto-WL on each volume (re)load: the sliders' default (range[0]
        // .. range[1]) is a flat panchromatic window that crushes
        // contrast — a PT slab looks like a uniform grey blob, a CT
        // looks washed out / all black. Modality-aware compute lands on a
        // useful window without the user having to fiddle. Keyed on
        // volumeId so a volume swap re-windows the new volume instead of
        // inheriting the previous one's window — or, worse, the flat
        // full-range default that a skipped auto-WL leaves behind.
        if (autoWindowedVolumeRef.current !== volumeId) {
          const m = (modality || "").toUpperCase();
          let autoWC = wc;
          let autoWW = ww;
          let autoInvert = invert;
          if (m === "PT") {
            // 50-99.5 percentile + invert (high SUV = dark, the
            // standard radiology nuclear convention).
            const r = ptAutoWL(volume.scalars);
            autoWC = r.wc;
            autoWW = r.ww;
            autoInvert = true;
          } else if (m === "CT") {
            // Soft-tissue HU window (W=400, L=40). The single most
            // useful default for oncologic / abdominal CT, which is
            // what the PET-CT fusion case sits in front of.
            autoWC = 40;
            autoWW = 400;
          } else if (suggestedVoi) {
            // Tier-1 (see windowing.ts): the radiologist's own WC/WW
            // recovered from the acquisition DICOM tags. Preferred for
            // MR / CR / DX / MG / OT so the viewer opens on the same
            // window the manual "DICOM" sidebar button applies, instead
            // of silently skipping the documented top-priority tier and
            // jumping to the histogram. (CT/PT keep their tuned
            // modality defaults above.)
            autoWC = suggestedVoi.wc;
            autoWW = suggestedVoi.ww;
          } else {
            // Generic robust 1-99 percentile of nonzero voxels —
            // matches ``computeAutoWL`` legacy behaviour.
            const r = robustAutoWL(volume.scalars);
            autoWC = r.wc;
            autoWW = r.ww;
          }
          setWC(autoWC);
          setWW(autoWW);
          setInvert(autoInvert);
          applyVoiToAll(engine, [vpAxial, vpSag, vpCor], autoWC, autoWW, autoInvert, volumeId);
        } else {
          applyVoiToAll(engine, [vpAxial, vpSag, vpCor], wc, ww, invert, volumeId);
        }
        // Fusion volume gets its own VOI (PT-style 50-99.5
        // percentile auto-window when first loaded). Independent of
        // primary so radiologists can dial the SUV scale without
        // touching the underlying CT contrast.
        if (fusionVolume && fusionVolumeId) {
          const r = ptAutoWL(fusionVolume.scalars);
          setFusionWC(r.wc);
          setFusionWW(r.ww);
          applyVoiToAll(engine, [vpAxial, vpSag, vpCor], r.wc, r.ww, false, fusionVolumeId);
        }
        autoWindowedVolumeRef.current = volumeId;

        engine.renderViewports([vpAxial, vpSag, vpCor]);
        // Tell the activeTool effect the tool group is now wired
        // up. Without this, picking a measure tool BEFORE this
        // async setup finishes used to silently no-op (the effect
        // bailed at ``getToolGroup`` returning undefined and
        // wasn't re-triggered when setup completed).
        setToolGroupReady(true);

        // Authoritative initial frame. ``setVolumesForViewports`` above
        // already fit the camera (via ``addActors({resetCamera:true})``),
        // but at whatever canvas size existed when this async setup
        // resolved — and the pane is frequently still settling at that
        // point (the effect awaits Cornerstone init + GPU texture upload,
        // which can resolve before the browser performs the first real
        // flex/grid layout of the canvas cell, or before a sibling like
        // the identity banner expands). CS3D's ResizeObserver path then
        // calls ``engine.resize(true)`` with ``keepCamera`` defaulting
        // true, which — for our orthographic viewports (no displayArea) —
        // RESTORES that stale camera (``resetCameraForResize()`` then
        // ``setCamera(prevCamera)``), locking the bad framing. The result
        // was an image scrolled/zoomed out of view (a tall, left-weighted
        // mammogram showed only its black background) until the user hit
        // the per-pane reset by hand.
        //
        // Fix: once the visible pane's box has been stable for two frames
        // (so we never fit mid-reflow), issue an explicit ``resetCamera``
        // at the settled size. This runs once per volume mount; the setup
        // effect does NOT re-run on layout toggles (deps below), so it
        // never clobbers a user's zoom/pan when they switch 1×1 ↔ 2×2.
        const visiblePanes = () => [
          { id: vpAxial, el: showAxial ? axialDivRef.current : null },
          { id: vpSag, el: showSagittal ? sagDivRef.current : null },
          { id: vpCor, el: showCoronal ? corDivRef.current : null },
        ];
        let prevFitW = -1;
        let prevFitH = -1;
        let fitTries = 0;
        let fitted = false;
        const probeEl = () => axialDivRef.current ?? sagDivRef.current ?? corDivRef.current;
        const tryFit = (): boolean => {
          const eng = engineRef.current;
          const probe = probeEl();
          const w = probe?.clientWidth ?? 0;
          const h = probe?.clientHeight ?? 0;
          if (!eng || w <= 0 || h <= 0) return false;
          fitViewportsToCanvas(eng, visiblePanes());
          return true;
        };
        const settleAndFit = () => {
          if (cancelled || fitted) return;
          const probe = probeEl();
          const w = probe?.clientWidth ?? 0;
          const h = probe?.clientHeight ?? 0;
          // While the cell is still 0×0 (e.g. the 2nd grid column hasn't laid
          // out yet) keep waiting WITHOUT spending the settle budget — a
          // late-settling pane would otherwise hit the ceiling at 0×0 and
          // never get fitted (the empty-right-pane bug).
          if (w <= 0 || h <= 0) {
            requestAnimationFrame(settleAndFit);
            return;
          }
          const stable = w === prevFitW && h === prevFitH;
          // ~30 frames (≈500 ms) ceiling once the box is non-zero, so a
          // never-stabilising layout still gets one fit.
          if (stable || fitTries >= 30) {
            fitted = tryFit();
            return;
          }
          prevFitW = w;
          prevFitH = h;
          fitTries += 1;
          requestAnimationFrame(settleAndFit);
        };
        requestAnimationFrame(settleAndFit);

        // Safety net for a cell that only gets its box AFTER the rAF settle
        // gave up (slow grid reflow, a sibling banner expanding): fit once
        // when a stable non-zero size is first observed, then disconnect so
        // we never clobber the user's later zoom/pan.
        const probe0 = probeEl();
        if (probe0 && typeof ResizeObserver !== "undefined") {
          let roW = -1;
          let roH = -1;
          const ro = new ResizeObserver(() => {
            if (cancelled || fitted) {
              settleObserverRef.current?.disconnect();
              return;
            }
            const probe = probeEl();
            const w = probe?.clientWidth ?? 0;
            const h = probe?.clientHeight ?? 0;
            if (w > 0 && h > 0 && w === roW && h === roH) {
              fitted = tryFit();
              if (fitted) settleObserverRef.current?.disconnect();
            }
            roW = w;
            roH = h;
          });
          ro.observe(probe0);
          settleObserverRef.current?.disconnect();
          settleObserverRef.current = ro;
        }
      };

      // Reset readiness while the new tool group is being wired so
      // the activation effect re-fires after ``setToolGroupReady(true)``
      // at the end of setup. Without this, a volume swap leaves the
      // state stuck at ``true`` from the previous mount and the
      // activation effect never re-runs against the freshly-built
      // tool group.
      setToolGroupReady(false);
      void setup();
      return () => {
        cancelled = true;
        settleObserverRef.current?.disconnect();
        settleObserverRef.current = null;
      };
    }, [
      volume,
      seriesId,
      volumeId,
      modality,
      // ``fusionModality`` was missing from the deps: it lands in a
      // separate fetch (study series list) which often resolves AFTER
      // the first setup run, leaving the resolver to swap on a null
      // modality and putting the PT volume at pos[0] with no colormap
      // (the "only PET, all red" symptom). Re-run when it lands.
      fusionModality,
      fusionVolume,
      fusionVolumeId,
      // ``fusionFailed`` libera il gate ``fusionPending`` quando il
      // fetch ha rigettato (404 / network / watchdog 30s). Senza il
      // dep, la transizione false→true del flag non triggererebbe
      // un re-run del setup e MPR/MIP resterebbero appesi nonostante
      // il fail-flag. ``fusionExpected`` non serve nei deps: è
      // derivato sincronicamente da ``fusionParam`` URL ed è stabile
      // per la durata della route (URL change → component remount).
      fusionFailed,
    ]);

    // Map the legacy ``activeTool`` string to a Cornerstone tool
    // name. The set is the same the toolbar already exposes; values
    // outside the map fall back to the user's chosen primary tool
    // (Crosshair or W/L). This is the single switch that lets the
    // viewer page toolbar reach the new layout without rewiring.
    const measureToolByActive: Record<string, string | undefined> = {
      "measure-dist": csTools.LengthTool.toolName,
      "measure-angle": csTools.AngleTool.toolName,
      "measure-ellipse": csTools.EllipticalROITool.toolName,
      "measure-rect": csTools.RectangleROITool.toolName,
      // ``measure-sphere`` draws a 2D circle on the active MPR slice;
      // the backend ``/roi-stats`` endpoint interprets it as the
      // equatorial cross-section of a 3D sphere when we POST with
      // ``kind=sphere``. The cornerstone tool itself has no sphere
      // mode — extruding into 3D is a server-side concern.
      "measure-sphere": csTools.CircleROITool.toolName,
      "measure-arrow": csTools.ArrowAnnotateTool.toolName,
      "measure-text": csTools.ArrowAnnotateTool.toolName,
      "measure-probe": csTools.ProbeTool.toolName,
      "measure-area": csTools.PlanarFreehandROITool.toolName,
      "measure-freehand": csTools.PlanarFreehandROITool.toolName,
      "measure-bidirectional": csTools.BidirectionalTool.toolName,
    };

    // Tool binding effect: rebind Primary mouse based on
    // ``activeTool`` (toolbar button) + ``primaryTool`` (the
    // crosshair-vs-WL toggle, used only when activeTool === "wl").
    // Every measurement tool that isn't the active one is forced
    // back to passive so a stale binding doesn't keep capturing
    // clicks.
    // biome-ignore lint/correctness/useExhaustiveDependencies: viewer lifecycle effect — re-running on derived deps would tear down GPU resources.
    useEffect(() => {
      if (!toolGroupReady) return;
      const tg = csTools.ToolGroupManager.getToolGroup(toolGroupId);
      if (!tg) return;
      const ALL_MEASURE_TOOLS = [
        csTools.LengthTool.toolName,
        csTools.AngleTool.toolName,
        csTools.EllipticalROITool.toolName,
        csTools.RectangleROITool.toolName,
        csTools.CircleROITool.toolName,
        csTools.ArrowAnnotateTool.toolName,
        csTools.ProbeTool.toolName,
        csTools.PlanarFreehandROITool.toolName,
        csTools.BidirectionalTool.toolName,
      ];
      const measureTool = measureToolByActive[activeTool ?? ""] ?? null;
      // Cornerstone's ``setToolActive`` MERGES bindings with whatever
      // the tool already had (see @cornerstonejs/tools ToolGroup.js
      // setToolActive: ``[...prevBindings, ...newBindings]``). That
      // means once Pan is bound to Primary, no later
      // ``setToolActive(Pan, { Auxiliary })`` will release it, and a
      // subsequent measure tool can't capture Primary clicks because
      // Pan still claims them. ``setToolDisabled`` clears bindings
      // but ALSO stops the tool from rendering — that's fine for
      // Pan / W/L (no visible overlay) but it would wipe the
      // CrosshairsTool's reference-line annotations and leave the
      // viewport without crosshairs until the user re-picks a nav
      // tool. ``setToolEnabled`` clears bindings while keeping the
      // tool's drawings visible, so it's the right reset for the
      // crosshair specifically. Measurement tools stay on
      // ``setToolPassive`` so already-drawn annotations keep
      // rendering; their stale Primary binding is harmless because
      // Passive tools don't receive primary-click events.
      // Pan / W/L can be hard-reset (Disabled): they have no visible
      // overlay to lose. The CrosshairsTool, on the other hand, is
      // both a renderer (reference lines) AND an interactive widget
      // (drag-the-line gestures rotate / translate). ``Disabled`` /
      // ``Enabled`` would either erase or freeze the lines; only
      // ``Passive`` preserves rendering AND keeps the line handles
      // draggable. Leftover Primary bindings on a Passive tool are
      // harmless because Passive doesn't dispatch on Primary clicks
      // — only Active does — so the new tool's Primary capture wins.
      tg.setToolDisabled(csTools.PanTool.toolName);
      tg.setToolPassive(csTools.CrosshairsTool.toolName);
      tg.setToolDisabled(csTools.WindowLevelTool.toolName);
      // Pan is ALWAYS available two ways, whatever the active tool:
      //   - Auxiliary (middle button) — radiology-workstation convention;
      //   - Shift + left-drag — works on a trackpad with no middle button and
      //     while a measure/crosshair tool owns the plain Primary click. This is
      //     the dependable "I just want to pan" gesture (the tool-palette Pan
      //     button can be missed in the compact grid toolbar).
      tg.setToolActive(csTools.PanTool.toolName, {
        bindings: [
          { mouseButton: csTools.Enums.MouseBindings.Auxiliary },
          {
            mouseButton: csTools.Enums.MouseBindings.Primary,
            modifierKey: csTools.Enums.KeyboardBindings.Shift,
          },
        ],
      });
      if (measureTool) {
        for (const t of ALL_MEASURE_TOOLS) {
          if (t !== measureTool) tg.setToolPassive(t);
        }
        tg.setToolActive(measureTool, {
          bindings: [{ mouseButton: csTools.Enums.MouseBindings.Primary }],
        });
      } else if (activeTool === "pan") {
        // Dedicated pan mode: Primary drags too (in addition to the
        // Auxiliary baseline above). Indispensable when zoomed in —
        // the middle-button-only binding is unusable on trackpads.
        for (const t of ALL_MEASURE_TOOLS) tg.setToolPassive(t);
        tg.setToolActive(csTools.PanTool.toolName, {
          bindings: [{ mouseButton: csTools.Enums.MouseBindings.Primary }],
        });
      } else if (activeTool === "wl") {
        // W/L mode: Primary drag adjusts window/level. Opt-in only.
        for (const t of ALL_MEASURE_TOOLS) tg.setToolPassive(t);
        tg.setToolActive(csTools.WindowLevelTool.toolName, {
          bindings: [{ mouseButton: csTools.Enums.MouseBindings.Primary }],
        });
      } else {
        // Default mode (no toolbar selection): Primary mouse drives
        // the crosshair — clicks set the new focal point, drags grab
        // the reference-line handles. The legacy in-pane "i" toggle
        // (``primaryTool`` state) is no longer consulted here; the
        // toolbar buttons are now the single source of truth.
        for (const t of ALL_MEASURE_TOOLS) tg.setToolPassive(t);
        tg.setToolActive(csTools.CrosshairsTool.toolName, {
          bindings: [{ mouseButton: csTools.Enums.MouseBindings.Primary }],
        });
      }
    }, [activeTool, toolGroupReady]);

    // Crosshair → React state sync. Without this, when the user
    // moves the Cornerstone CrosshairsTool the React ``crosshair``
    // state stays at its last programmatic value, the sidebar slice
    // slider freezes, and ``onCrosshairChange`` never fires for the
    // parent. We listen to ``CAMERA_MODIFIED`` directly on each
    // viewport's DOM element (not ``cs.eventTarget``) — Cornerstone
    // dispatches camera events on both, but the element-level path
    // is the canonical one for per-viewport listeners.
    //
    // HUD source = BASE volume, not always primary
    // -------------------------------------------
    // ``StackScrollTool`` scrolls the first volume in ``volumeInputs``
    // (= the BASE returned by ``resolveFusionOrder``). When a PT is
    // opened as primary with a CT fusion, the resolver swaps and the
    // BASE becomes the CT — so wheel scroll moves the CT (1 mm
    // slices) while the page-level ``volume`` prop still describes
    // the PT (3–4 mm slices). Before the fix, the slice-index HUD
    // and the per-viewport ``perVpInfo`` were computed against the
    // PT, so 3–4 wheel ticks were needed before the HUD index
    // visibly changed — the user reports this as "slices don't
    // scroll, only if CT is there". The fix reads imageData +
    // dimensions from the volume actually being scrolled (BASE).
    useEffect(() => {
      const handler = (evtRaw: Event | unknown) => {
        // Resolve engine + imageData lazily on each event — this
        // effect re-runs whenever ``volumeId`` changes but the
        // volume itself is loaded asynchronously, so capturing the
        // cached volume at effect-setup time would race the load
        // and silently disable the listener.
        const engine = engineRef.current;
        if (!engine) return;
        // Pick the volume Cornerstone is actually scrolling. When
        // the resolver swapped (PT primary + CT fusion), the BASE is
        // the fusion volume; the HUD must follow it, not the primary.
        // When fusion isn't loaded yet, ``resolveFusionOrder`` returns
        // the primary as base — same behaviour as before the fix.
        const order = resolveFusionOrder(
          { volumeId, modality: modality ?? null },
          fusionVolumeId && cs.cache.getVolume(fusionVolumeId)
            ? { volumeId: fusionVolumeId, modality: fusionModality ?? null }
            : null,
        );
        const hudVolumeId = order.baseVolumeId;
        const cachedVol = cs.cache.getVolume(hudVolumeId);
        type CachedVolumeShape = {
          imageData?: { worldToIndex: unknown };
          dimensions?: number[];
        };
        const cachedShape = cachedVol as unknown as CachedVolumeShape;
        const imageData = cachedShape?.imageData;
        if (!imageData) return;
        const hudDims =
          (cachedShape?.dimensions as [number, number, number] | undefined) ?? volume.dimensions;
        const evt = evtRaw as {
          detail?: { viewportId?: string };
        };
        const vpId = evt?.detail?.viewportId;
        if (!vpId || (vpId !== vpAxial && vpId !== vpSag && vpId !== vpCor)) return;
        const vp = engine.getViewport(vpId) as cs.Types.IVolumeViewport | undefined;
        if (!vp) return;
        const cam = vp.getCamera();
        // Orientation markers: recompute this pane's edge letters +
        // transform flags from the live camera. Done before the
        // crosshair/index work below (which can early-return) so the
        // letters stay correct on the very first camera event too.
        if (hasRealGeometry) {
          const oAxis: Axis | null =
            vpId === vpAxial
              ? "axial"
              : vpId === vpSag
                ? "sagittal"
                : vpId === vpCor
                  ? "coronal"
                  : null;
          if (oAxis) {
            const letters = cameraEdgeLetters(cam as unknown as OrientationCamera);
            const flags = cameraTransformFlags(cam as unknown as OrientationCamera);
            setOrientationByAxis((prev) => {
              const cur = prev[oAxis];
              const same =
                cur.flags.flipped === flags.flipped &&
                cur.flags.rotated === flags.rotated &&
                cur.letters?.top === letters?.top &&
                cur.letters?.bottom === letters?.bottom &&
                cur.letters?.left === letters?.left &&
                cur.letters?.right === letters?.right;
              return same ? prev : { ...prev, [oAxis]: { letters, flags } };
            });
          }
        }
        if (!cam.focalPoint) return;
        let idx: cs.Types.Point3;
        try {
          idx = cs.utilities.transformWorldToIndex(
            imageData as Parameters<typeof cs.utilities.transformWorldToIndex>[0],
            cam.focalPoint as cs.Types.Point3,
          ) as cs.Types.Point3;
        } catch {
          return;
        }
        // Each ortho viewport is authoritative ONLY for its own slice
        // axis component of the crosshair. The other two components of
        // ``cam.focalPoint`` are the viewport's in-plane pan position
        // — they do NOT represent the crosshair's indices on the other
        // axes, because ``updateCrosshair`` deliberately preserves
        // each viewport's in-plane focal across crosshair moves (so a
        // user's pan isn't reset on every slice change). Reading them
        // back as crosshair indices created a feedback loop: moving
        // the Z slider triggered a sagittal ``setCamera`` (snapping
        // focal[0] to the new voxel-aligned world X), the resulting
        // ``CAMERA_MODIFIED`` on sagittal then read its stale focal[2]
        // and overwrote ``mprCrosshair[2]`` with the OLD Z, snapping
        // the slider back. Wheel scroll never hit this because
        // ``StackScrollTool`` only moves the scrolled viewport.
        const sliceAxisOfVp: Record<string, 0 | 1 | 2> = {
          [vpAxial]: 2,
          [vpSag]: 0,
          [vpCor]: 1,
        };
        const sliceAxisIdx = sliceAxisOfVp[vpId];
        const sliceIdxRounded = Math.round(idx[sliceAxisIdx]);
        // Greppable telemetry — opt-in via ``localStorage.debug``
        // contains "viewer:scroll" so it doesn't spam every camera
        // event in normal use. ``order.swapped`` makes the
        // PT-primary + CT-fusion case obvious without re-deriving
        // the decision.
        try {
          const dbg =
            typeof window !== "undefined" &&
            (window.localStorage.getItem("debug") ?? "").includes("viewer:scroll");
          if (dbg) {
            // eslint-disable-next-line no-console
            console.debug("[viewer:scroll]", {
              vpId,
              hudVolumeId,
              primary: volumeId,
              swapped: order.swapped,
              sliceAxisIdx,
              sliceIdxRounded,
              hudTotal: hudDims[sliceAxisIdx],
              focal: cam.focalPoint,
              ts: Date.now(),
            });
          }
        } catch {
          /* localStorage may throw in private mode; debug is best-effort. */
        }
        // Dedup: only fire onCrosshairChange + setCrosshair when
        // the voxel index actually changed. Avoids a setCamera ↔
        // listener loop (camera may modify within sub-voxel
        // tolerance during animation).
        setCrosshair((prev) => {
          if (prev[sliceAxisIdx] === sliceIdxRounded) {
            return prev;
          }
          const next: [number, number, number] = [prev[0], prev[1], prev[2]];
          next[sliceAxisIdx] = sliceIdxRounded;
          // Also notify the parent about the user-driven move.
          // Wrapping in queueMicrotask defers the callback past the
          // current render commit so the parent's setMprCrosshair
          // doesn't fire mid-render.
          queueMicrotask(() => onCrosshairChange?.(next));
          return next;
        });
        // Per-viewport slice index + zoom for the corner overlays.
        const axisOf: Record<string, Axis> = {
          [vpAxial]: "axial",
          [vpSag]: "sagittal",
          [vpCor]: "coronal",
        };
        const axis = axisOf[vpId];
        if (axis) {
          const total = hudDims[sliceAxisIdx];
          const sliceIndex = Math.max(0, Math.min(total - 1, sliceIdxRounded));
          // Cornerstone exposes parallel scale via the camera —
          // ``parallelScale`` is the half-height of the viewport in
          // world units. We compare to the initial scale (volume's
          // physical size on that axis) to derive a percent zoom.
          const ps = (cam as { parallelScale?: number }).parallelScale ?? 0;
          const baseHalf = (volume.spacing[1] * volume.dimensions[1]) / 2;
          const zoomPct = ps > 0 && baseHalf > 0 ? Math.round((baseHalf / ps) * 100) : 100;
          setPerVpInfo((prev) => {
            const cur = prev[axis];
            if (
              cur.sliceIndex === sliceIndex &&
              cur.sliceTotal === total &&
              cur.zoomPct === zoomPct
            ) {
              return prev;
            }
            return {
              ...prev,
              [axis]: { sliceIndex, sliceTotal: total, zoomPct },
            };
          });
        }
      };
      // Element-level subscription on each visible viewport div.
      // Cornerstone dispatches CAMERA_MODIFIED on the element first,
      // then bubbles to ``cs.eventTarget``; element-level listeners
      // are guaranteed to fire even when the global queue is busy.
      const els: HTMLDivElement[] = [];
      const tryWire = (ref: React.RefObject<HTMLDivElement | null>) => {
        const el = ref.current;
        if (!el) return;
        el.addEventListener(cs.Enums.Events.CAMERA_MODIFIED, handler);
        els.push(el);
      };
      tryWire(axialDivRef);
      tryWire(sagDivRef);
      tryWire(corDivRef);
      // Belt + suspenders: also subscribe to the global event target
      // — covers the case where Cornerstone's element event timing
      // differs across versions.
      cs.eventTarget.addEventListener(cs.Enums.Events.CAMERA_MODIFIED, handler);
      return () => {
        for (const el of els) {
          el.removeEventListener(cs.Enums.Events.CAMERA_MODIFIED, handler);
        }
        cs.eventTarget.removeEventListener(cs.Enums.Events.CAMERA_MODIFIED, handler);
      };
    }, [
      volumeId,
      fusionVolumeId,
      modality,
      fusionModality,
      onCrosshairChange,
      vpAxial,
      vpSag,
      vpCor,
      volume.dimensions,
      volume.spacing,
      hasRealGeometry,
    ]);

    // Annotation event sync (Cornerstone → legacy persistence).
    // Each annotation now carries ``worldPoints`` so the parent can
    // round-trip it back via the ``measurements`` prop on a future
    // mount (see the reverse-sync effect below). Stats labels are
    // SUV-enriched when the active series has a known factor.
    // biome-ignore lint/correctness/useExhaustiveDependencies: viewer lifecycle effect — re-running on derived deps would tear down GPU resources.
    useEffect(() => {
      // Cornerstone-tool-name → legacy short name mapping. The
      // legacy persistence layer maps short names to ``MarkerKind``;
      // emitting the Cornerstone class name (``Length``,
      // ``EllipticalROI``, ...) defaults all of them to
      // ``measurement.distance``, which produces the bogus
      // "everything is measurement.distance" symptom in the
      // markers panel. Map explicitly here.
      const csToLegacy: Record<string, string> = {
        Length: "dist",
        Bidirectional: "dist",
        Angle: "angle",
        EllipticalROI: "ellipse",
        RectangleROI: "bbox",
        // CircleROI tool draws a 2D circle, which the backend treats
        // as the equator of a 3D sphere — surface it under the
        // ``sphere`` legacy kind so MarkerListPanel + report-composer
        // can present "Sphere ROI (PERCIST)" instead of generic
        // "measurement.distance".
        CircleROI: "sphere",
        ArrowAnnotate: "arrow",
        Probe: "probe",
        PlanarFreehandROI: "freehand",
      };
      const onChange = () => {
        const cachedVol = cs.cache.getVolume(volumeId);
        const imageData = (
          cachedVol as unknown as {
            imageData?: {
              worldToIndex: (w: cs.Types.Point3) => cs.Types.Point3;
            };
          }
        )?.imageData;
        const groups = csTools.annotation.state.getAllAnnotations() as Array<{
          metadata: { toolName: string; FrameOfReferenceUID?: string };
          annotationUID: string;
          data?: {
            handles?: { points?: Array<[number, number, number]> };
            cachedStats?: Record<
              string,
              {
                length?: number;
                width?: number;
                mean?: number;
                max?: number;
                area?: number;
                bvSuvMean?: number;
                bvSuvSd?: number;
                bvSuvMax?: number;
                bvSuvPeak?: number;
              }
            >;
            label?: string;
          };
        }>;
        const out = groups.map((a, i) => {
          const stats = a.data?.cachedStats ? Object.values(a.data.cachedStats)[0] : undefined;
          // BidirectionalTool (RECIST) reports both axes in patient mm.
          const { longAxisMm, shortAxisMm } = extractBidirectionalMm(stats);
          let value: string;
          if (shortAxisMm !== undefined && longAxisMm !== undefined) {
            // Bidirectional: surface "long × short mm" so the canvas label
            // and markers panel read as a RECIST measurement.
            value = `${longAxisMm.toFixed(1)} × ${shortAxisMm.toFixed(1)} mm`;
          } else if (stats?.length !== undefined) {
            value = `${stats.length.toFixed(1)} mm`;
          } else if (
            stats?.mean !== undefined &&
            suvFactorBw !== undefined &&
            suvFactorBw !== null
          ) {
            // SUV-enriched ROI label: pixel mean is in the volume's
            // raw scale (Bq/mL after RescaleSlope for PT BQML); SUV
            // = pixel × suvFactorBw. We surface both raw + SUV so
            // the user knows what they're looking at.
            const suvMean = stats.mean * suvFactorBw;
            const suvMax = stats.max !== undefined ? stats.max * suvFactorBw : undefined;
            value = `SUVmean ${suvMean.toFixed(2)}${
              suvMax !== undefined ? ` / SUVmax ${suvMax.toFixed(2)}` : ""
            }${stats.area !== undefined ? ` · ${stats.area.toFixed(0)} mm²` : ""}`;
          } else if (stats?.mean !== undefined) {
            value = `mean ${stats.mean.toFixed(1)}${
              stats.max !== undefined ? ` / max ${stats.max.toFixed(1)}` : ""
            }${stats.area !== undefined ? ` · ${stats.area.toFixed(0)} mm²` : ""}`;
          } else if (stats?.area !== undefined) {
            value = `${stats.area.toFixed(0)} mm²`;
          } else {
            value = a.data?.label ?? "";
          }
          const worldPoints = (a.data?.handles?.points ?? []).map(
            ([x, y, z]) => [x, y, z] as [number, number, number],
          );
          // Convert world coords → voxel indices so the legacy
          // persistence layer can store ``geometry.points`` as
          // ``[i, j, k]`` triples (its existing schema). Without
          // this the markers persist with empty 2D points and
          // ``MarkerListPanel`` shows them in the "across slices"
          // bucket with no jump-to.
          const voxelPoints: Array<[number, number, number]> = [];
          if (imageData && worldPoints.length > 0) {
            for (const wp of worldPoints) {
              try {
                const idx = imageData.worldToIndex(wp) as cs.Types.Point3;
                voxelPoints.push([Math.round(idx[0]), Math.round(idx[1]), Math.round(idx[2])]);
              } catch {
                /* skip */
              }
            }
          }
          // Centroid Z = anchor slice for the legacy slice-fade
          // renderer + the "row in slice N" grouping in the panel.
          let sliceIndex: number | undefined;
          if (voxelPoints.length > 0) {
            let sumZ = 0;
            for (const vp of voxelPoints) sumZ += vp[2];
            sliceIndex = Math.round(sumZ / voxelPoints.length);
          }
          const legacyTool = csToLegacy[a.metadata.toolName] ?? a.metadata.toolName.toLowerCase();
          // ``points`` in the legacy (canvas) shape are 2D ``{x,y}``
          // pairs anchored to a slice. Emit a 2D projection of each
          // voxel so jump-to + the slice-fade renderer have
          // something to work with; the canonical 3D source of
          // truth stays in ``worldPoints`` for round-trip.
          const points2d = voxelPoints.map(([x, y]) => ({ x, y }));
          // Server-side SUV stats are stashed onto cachedStats by the
          // ``/roi-stats`` fetch effect (PERCIST 1.0 bw factor). They
          // only exist on PT series with a usable suv_factor_bw; on
          // CT / MR / unusable PT they stay undefined and the
          // PERCIST helper falls back to the manual paste inputs.
          const suv =
            stats?.bvSuvMean !== undefined ||
            stats?.bvSuvSd !== undefined ||
            stats?.bvSuvMax !== undefined ||
            stats?.bvSuvPeak !== undefined
              ? {
                  mean: stats.bvSuvMean,
                  sd: stats.bvSuvSd,
                  max: stats.bvSuvMax,
                  peak: stats.bvSuvPeak,
                }
              : undefined;
          return {
            id: i,
            tool: legacyTool,
            csToolName: a.metadata.toolName,
            value,
            // Optional user-supplied label, surfaced separately from
            // ``value`` (the auto-computed measurement string). The
            // viewer page persists this as the marker's ``body`` and
            // the layout's ``getTextLines`` overrides render it on
            // the canvas alongside the value.
            label: a.data?.label ?? undefined,
            points: points2d,
            worldPoints,
            frameOfReferenceUID: a.metadata.FrameOfReferenceUID,
            sliceIndex,
            markerId: a.annotationUID,
            suv,
            longAxisMm,
            shortAxisMm,
          };
        });
        onMeasurementsChange?.(out);
      };
      const evt = cs.eventTarget;
      const events = csTools.Enums.Events;
      evt.addEventListener(events.ANNOTATION_ADDED, onChange);
      evt.addEventListener(events.ANNOTATION_MODIFIED, onChange);
      evt.addEventListener(events.ANNOTATION_REMOVED, onChange);
      evt.addEventListener(events.ANNOTATION_COMPLETED, onChange);
      return () => {
        evt.removeEventListener(events.ANNOTATION_ADDED, onChange);
        evt.removeEventListener(events.ANNOTATION_MODIFIED, onChange);
        evt.removeEventListener(events.ANNOTATION_REMOVED, onChange);
        evt.removeEventListener(events.ANNOTATION_COMPLETED, onChange);
      };
    }, [onMeasurementsChange, suvFactorBw]);

    // Prompt for an optional label on every measurement the user
    // completes. ArrowAnnotateTool already has its own
    // ``getTextCallback`` (set up in ``addTool`` above) so we skip
    // it here to avoid double-prompting; same for the crosshair
    // reference-line annotation, which the user never "completes"
    // explicitly. The label is stored on ``annotation.data.label``;
    // the per-tool ``getTextLines`` override renders it next to the
    // numeric value, the ``onChange`` handler above surfaces it to
    // React, and the viewer page persists it as the marker's
    // ``body`` so it round-trips across reloads.
    // biome-ignore lint/correctness/useExhaustiveDependencies: viewer lifecycle effect — re-running on derived deps would tear down GPU resources.
    useEffect(() => {
      const SKIP = new Set<string>([
        csTools.ArrowAnnotateTool.toolName,
        csTools.CrosshairsTool.toolName,
      ]);
      const onCompleted = (evtRaw: Event) => {
        const evt = evtRaw as CustomEvent<{
          annotation?: {
            metadata?: { toolName?: string };
            data?: { label?: string };
            annotationUID?: string;
          };
        }>;
        const annotation = evt.detail?.annotation;
        if (!annotation) return;
        const toolName = annotation.metadata?.toolName;
        if (!toolName || SKIP.has(toolName)) return;
        // Don't re-prompt if the tool (or a previous import) already
        // populated a label on the annotation.
        if (annotation.data?.label) return;
        // Fire the prompt asynchronously so the tool's own
        // post-completion bookkeeping (cachedStats, render, ...)
        // gets to finish before React state updates from the modal.
        Promise.resolve()
          .then(() =>
            promptRef.current({
              title: markerLabelsRef.current.measurementTitle,
              label: markerLabelsRef.current.measurementLabelLabel,
              defaultValue: "",
              placeholder: tMarker("labelPlaceholder"),
            }),
          )
          .then((text) => {
            const trimmed = (text ?? "").trim();
            if (!trimmed || !annotation.data) return;
            annotation.data.label = trimmed;
            const el = axialDivRef.current ?? sagDivRef.current ?? corDivRef.current ?? null;
            const utils = (
              csTools as unknown as {
                utilities?: {
                  setAnnotationLabel?: (a: unknown, el: HTMLElement, label: string) => void;
                };
              }
            ).utilities;
            if (el && utils?.setAnnotationLabel) {
              try {
                utils.setAnnotationLabel(annotation, el, trimmed);
                return;
              } catch {
                /* fall through to engine.render */
              }
            }
            engineRef.current?.renderViewports([vpAxial, vpSag, vpCor]);
          })
          .catch(() => {
            /* user cancelled — leave the annotation unlabelled */
          });
      };
      const events = csTools.Enums.Events;
      cs.eventTarget.addEventListener(events.ANNOTATION_COMPLETED, onCompleted);
      return () => {
        cs.eventTarget.removeEventListener(events.ANNOTATION_COMPLETED, onCompleted);
      };
    }, []);

    // PET ROI enrichment: when a Rectangle / Elliptical ROI completes
    // on a PET series, call the server-side ``/series/{id}/roi-stats``
    // endpoint to get the PERCIST SUVpeak (1 cm³ kernel) plus
    // audit-traceable mean/max. Result is stored on the annotation's
    // ``cachedStats`` under ``bvSuvPeak / bvSuvMean / bvSuvMax`` so the
    // ``roiLines`` text-line callback above can surface it on the
    // overlay. We update both on COMPLETED (creation) and MODIFIED
    // (edit handles) so the SUVpeak follows the ROI as the user
    // adjusts it.
    useEffect(() => {
      if (!seriesId || suvFactorBw == null) return;
      const ROI_TOOLS = new Set<string>([
        csTools.RectangleROITool.toolName,
        csTools.EllipticalROITool.toolName,
        csTools.CircleROITool.toolName,
      ]);
      // Per-annotation debounce so dragging an edit handle doesn't
      // fire 30 backend calls per second; we coalesce to 200 ms after
      // the last MODIFIED event before issuing the request.
      const timers = new Map<string, ReturnType<typeof setTimeout>>();
      const inflight = new Map<string, AbortController>();

      const fetchPeak = async (annotationUID: string) => {
        const cachedVol = cs.cache.getVolume(volumeId);
        const imageData = (
          cachedVol as unknown as {
            imageData?: {
              worldToIndex: (w: cs.Types.Point3) => cs.Types.Point3;
            };
          }
        )?.imageData;
        if (!imageData) return;
        const ann = (
          csTools.annotation.state.getAnnotation as (uid: string) =>
            | {
                metadata?: { toolName?: string };
                data?: {
                  handles?: { points?: Array<[number, number, number]> };
                  cachedStats?: Record<string, Record<string, unknown>>;
                };
              }
            | undefined
        )(annotationUID);
        if (!ann) return;
        const toolName = ann.metadata?.toolName ?? "";
        if (!ROI_TOOLS.has(toolName)) return;
        const points = ann.data?.handles?.points ?? [];
        if (points.length < 2) return;

        // Build the body of the ``/roi-stats`` request from the
        // annotation's geometry. Rectangle / ellipse use the bbox of
        // the two opposite corner handles; circle uses the first
        // handle as the center and the Euclidean distance to the
        // second handle (in world mm) as the radius — the cornerstone
        // ``CircleROITool`` lays its handles out exactly that way.
        // The backend treats ``kind=sphere`` as a 3D spherical mask
        // anchored on ``center_ijk``.
        let body: Record<string, unknown>;
        if (toolName === csTools.CircleROITool.toolName) {
          const [centerWorld, edgeWorld] = points as [
            [number, number, number],
            [number, number, number],
          ];
          const dx = edgeWorld[0] - centerWorld[0];
          const dy = edgeWorld[1] - centerWorld[1];
          const dz = edgeWorld[2] - centerWorld[2];
          const radiusMm = Math.sqrt(dx * dx + dy * dy + dz * dz);
          if (!Number.isFinite(radiusMm) || radiusMm <= 0) return;
          const ijkCenter = imageData.worldToIndex(centerWorld as cs.Types.Point3);
          body = {
            kind: "sphere",
            center_ijk: [
              Math.round(ijkCenter[0]),
              Math.round(ijkCenter[1]),
              Math.round(ijkCenter[2]),
            ],
            radius_mm: radiusMm,
            suv_variant: "bw",
          };
        } else {
          const ijkPoints = points.map((p) => imageData.worldToIndex(p as cs.Types.Point3));
          const min: [number, number, number] = [
            Math.floor(Math.min(...ijkPoints.map((p) => p[0]))),
            Math.floor(Math.min(...ijkPoints.map((p) => p[1]))),
            Math.floor(Math.min(...ijkPoints.map((p) => p[2]))),
          ];
          const max: [number, number, number] = [
            Math.ceil(Math.max(...ijkPoints.map((p) => p[0]))),
            Math.ceil(Math.max(...ijkPoints.map((p) => p[1]))),
            Math.ceil(Math.max(...ijkPoints.map((p) => p[2]))),
          ];
          body = {
            kind: toolName === csTools.EllipticalROITool.toolName ? "ellipse" : "rectangle",
            min_ijk: min,
            max_ijk: max,
            suv_variant: "bw",
          };
        }

        // Mark the ROI as pending so the overlay shows "SUVpeak: …".
        const stats = ann.data?.cachedStats ?? {};
        const targets = Object.keys(stats);
        for (const t of targets) {
          (stats[t] as Record<string, unknown>).bvSuvPending = true;
        }
        engineRef.current?.renderViewports([vpAxial, vpSag, vpCor]);

        // Cancel a previous in-flight request for the same annotation.
        const prev = inflight.get(annotationUID);
        if (prev) prev.abort();
        const ac = new AbortController();
        inflight.set(annotationUID, ac);
        try {
          // Use the shared ``request`` wrapper so the Bearer token
          // is injected automatically; a raw fetch returns 401.
          const json = await request<{
            suv_mean?: number | null;
            suv_sd?: number | null;
            suv_max?: number | null;
            suv_peak?: number | null;
          }>(`/api/series/${seriesId}/roi-stats`, {
            method: "POST",
            json: body,
            signal: ac.signal,
          });
          for (const t of targets) {
            const tgt = stats[t] as Record<string, unknown>;
            tgt.bvSuvPending = false;
            tgt.bvSuvMean = json.suv_mean ?? undefined;
            tgt.bvSuvSd = json.suv_sd ?? undefined;
            tgt.bvSuvMax = json.suv_max ?? undefined;
            tgt.bvSuvPeak = json.suv_peak ?? undefined;
          }
        } catch (err) {
          if ((err as { name?: string })?.name === "AbortError") return;
          for (const t of targets) {
            (stats[t] as Record<string, unknown>).bvSuvPending = false;
          }
        } finally {
          inflight.delete(annotationUID);
          engineRef.current?.renderViewports([vpAxial, vpSag, vpCor]);
        }
      };

      const schedule = (annotationUID: string) => {
        const prev = timers.get(annotationUID);
        if (prev) clearTimeout(prev);
        const t = setTimeout(() => {
          timers.delete(annotationUID);
          void fetchPeak(annotationUID);
        }, 200);
        timers.set(annotationUID, t);
      };

      const onEvt = (evtRaw: Event) => {
        const evt = evtRaw as CustomEvent<{
          annotation?: { annotationUID?: string; metadata?: { toolName?: string } };
        }>;
        const ann = evt.detail?.annotation;
        if (!ann?.annotationUID) return;
        if (!ROI_TOOLS.has(ann.metadata?.toolName ?? "")) return;
        schedule(ann.annotationUID);
      };
      const events = csTools.Enums.Events;
      cs.eventTarget.addEventListener(events.ANNOTATION_COMPLETED, onEvt);
      cs.eventTarget.addEventListener(events.ANNOTATION_MODIFIED, onEvt);
      return () => {
        cs.eventTarget.removeEventListener(events.ANNOTATION_COMPLETED, onEvt);
        cs.eventTarget.removeEventListener(events.ANNOTATION_MODIFIED, onEvt);
        for (const t of timers.values()) clearTimeout(t);
        for (const ac of inflight.values()) ac.abort();
      };
    }, [seriesId, volumeId, suvFactorBw]);

    // Reverse sync: ``measurements`` prop → Cornerstone state. Fires
    // once per (engine ready + measurements change) — we restore any
    // measurement that has ``worldPoints`` (Cornerstone-native) and
    // isn't already in the annotation state. Skips legacy
    // canvas-coord-only measurements (no ``worldPoints``) since we
    // can't reconstruct world positions without the original
    // viewport's zoom/pan/canvas size.
    useEffect(() => {
      const engine = engineRef.current;
      if (!engine || !incomingMeasurements?.length) return;
      const cachedVol = cs.cache.getVolume(volumeId);
      const frameOfRef =
        (cachedVol as unknown as { metadata?: { FrameOfReferenceUID?: string } })?.metadata
          ?.FrameOfReferenceUID || volumeId;
      // Take a snapshot of which UIDs are already in state so we
      // don't re-add Cornerstone annotations that already exist.
      const existing = new Set<string>(
        (
          csTools.annotation.state.getAllAnnotations() as Array<{
            annotationUID: string;
          }>
        ).map((a) => a.annotationUID),
      );
      for (const m of incomingMeasurements as Array<{
        markerId?: string;
        csToolName?: string;
        worldPoints?: Array<[number, number, number]>;
        value?: string;
        label?: string;
      }>) {
        if (!m.markerId || !m.csToolName || !m.worldPoints?.length) continue;
        if (existing.has(m.markerId)) continue;
        const annotation = {
          annotationUID: m.markerId,
          highlighted: false,
          isLocked: false,
          isVisible: true,
          invalidated: false,
          metadata: {
            toolName: m.csToolName,
            FrameOfReferenceUID: frameOfRef,
            referencedImageId: undefined,
          },
          data: {
            handles: { points: m.worldPoints, activeHandleIndex: null },
            // Prefer the dedicated ``label`` field (user-supplied
            // free text); fall back to ``value`` only for legacy
            // payloads written before the split, so old persisted
            // ArrowAnnotate / measurement.text rows still surface
            // their stored caption.
            label: m.label ?? m.value ?? "",
            cachedStats: {},
          },
        } as unknown as Parameters<typeof csTools.annotation.state.addAnnotation>[0];
        try {
          csTools.annotation.state.addAnnotation(annotation, frameOfRef);
        } catch {
          /* annotation rejected (toolName not registered) — skip */
        }
      }
      engine.renderViewports([vpAxial, vpSag, vpCor]);
    }, [incomingMeasurements, volumeId]);

    // Marker fade across slices: hide annotations whose anchor world
    // point is more than ``markerFade.range`` slices away from the
    // axial viewport's current focal point. Cornerstone has no
    // native "ramp opacity" API, so this is a binary
    // visible/hidden switch — matching the legacy behaviour when
    // ``markerFade.opacity`` was 0 (out of range).
    // biome-ignore lint/correctness/useExhaustiveDependencies: viewer lifecycle effect — re-running on derived deps would tear down GPU resources.
    useEffect(() => {
      const engine = engineRef.current;
      if (!engine) return;
      if (!markerFade?.enabled) return;
      const apply = () => {
        const ax = engine.getViewport(vpAxial) as cs.Types.IVolumeViewport | undefined;
        if (!ax) return;
        const cam = ax.getCamera();
        if (!cam.focalPoint) return;
        const z = cam.focalPoint[2];
        // Convert ``range`` (slices) to world distance using the
        // primary volume's Z spacing.
        const sz = volume.spacing[2];
        const tol = markerFade.range * sz;
        const all = csTools.annotation.state.getAllAnnotations() as Array<{
          annotationUID: string;
          data?: { handles?: { points?: Array<[number, number, number]> } };
        }>;
        for (const a of all) {
          const pts = a.data?.handles?.points ?? [];
          if (pts.length === 0) continue;
          // Centroid Z; for non-axial-anchored measurements this
          // collapses to the single point's Z which is what we want.
          let sumZ = 0;
          for (const p of pts) sumZ += p[2];
          const centroidZ = sumZ / pts.length;
          const dist = Math.abs(centroidZ - z);
          const visible = dist <= tol;
          csTools.annotation.visibility.setAnnotationVisibility(a.annotationUID, visible);
        }
        for (const id of [vpAxial, vpSag, vpCor]) {
          engine.getViewport(id)?.render();
        }
      };
      apply();
      const onCamera = () => apply();
      const evt = cs.eventTarget;
      evt.addEventListener(cs.Enums.Events.CAMERA_MODIFIED, onCamera);
      return () => {
        evt.removeEventListener(cs.Enums.Events.CAMERA_MODIFIED, onCamera);
      };
    }, [markerFade?.enabled, markerFade?.range, volume.spacing]);

    // Voxel HUD: attach mousemove listeners to each viewport div. We
    // sample the primary volume and, when a fusion is mounted, the
    // fusion as well — so on a CT-primary + PT-fusion view the user
    // reads HU and SUV at the same cursor. Active SUV variant is
    // applied via ``suvFactorBw`` (which carries whichever variant
    // the sidebar selector is on, not just BW).
    // biome-ignore lint/correctness/useExhaustiveDependencies: viewer lifecycle effect — re-running on derived deps would tear down GPU resources.
    useEffect(() => {
      const engine = engineRef.current;
      if (!engine) return;
      // Cornerstone3D 4.x dropped ``volume.getScalarData()`` from the
      // public surface — sampling now goes through ``voxelManager``.
      // Fall back to the legacy method only as a safety net for any
      // volume kind that still exposes it.
      type SamplingVolume = {
        imageData?: {
          worldToIndex: (w: cs.Types.Point3) => cs.Types.Point3;
          getDimensions: () => [number, number, number];
        };
        voxelManager?: {
          getAtIJK?: (i: number, j: number, k: number) => number;
        };
        getScalarData?: () => ArrayLike<number>;
        dimensions?: [number, number, number];
      };
      const sampler = (vol: SamplingVolume | null | undefined) => {
        if (!vol) return null;
        const imageData = vol.imageData;
        if (!imageData) return null;
        const dims = imageData.getDimensions();
        const getAtIJK = vol.voxelManager?.getAtIJK?.bind(vol.voxelManager);
        if (getAtIJK) {
          return { imageData, dims, sample: getAtIJK };
        }
        const scalars = vol.getScalarData?.();
        if (!scalars) return null;
        const stride0 = dims[0];
        const stride1 = dims[0] * dims[1];
        return {
          imageData,
          dims,
          sample: (i: number, j: number, k: number) =>
            scalars[i + j * stride0 + k * stride1] as number,
        };
      };
      const primary = sampler(cs.cache.getVolume(volumeId) as unknown as SamplingVolume | null);
      if (!primary) return;
      const { imageData: primaryImageData, dims: primaryDims, sample: samplePrimary } = primary;
      const isPrimaryPet = (modality ?? "").toUpperCase() === "PT";

      const fusion = fusionVolumeId
        ? sampler(cs.cache.getVolume(fusionVolumeId) as unknown as SamplingVolume | null)
        : null;
      const fusionImageData = fusion?.imageData;
      const fusionDims = fusion?.dims;
      const sampleFusion = fusion?.sample;

      const labelFor = (
        v: number,
        i: number,
        j: number,
        k: number,
        suvCapable: boolean,
      ): string => {
        const showSuv = suvCapable && suvFactorBw && suvFactorBw > 0;
        if (showSuv) {
          const suv = v * suvFactorBw;
          return `SUV ${suv.toFixed(2)}  (raw ${v.toFixed(1)} · ${i},${j},${k})`;
        }
        if (suvCapable) return `${v.toFixed(1)} Bq/mL  (${i},${j},${k})`;
        return `${v.toFixed(0)} HU  (${i},${j},${k})`;
      };

      const sample = (vpId: string, axis: Axis, evt: MouseEvent) => {
        const vp = engine.getViewport(vpId) as
          | (cs.Types.IVolumeViewport & {
              canvasToWorld: (canvasPos: cs.Types.Point2) => cs.Types.Point3;
            })
          | undefined;
        if (!vp) return;
        const target = evt.currentTarget as HTMLElement | null;
        if (!target) return;
        const rect = target.getBoundingClientRect();
        const cx = evt.clientX - rect.left;
        const cy = evt.clientY - rect.top;
        let world: cs.Types.Point3;
        try {
          world = vp.canvasToWorld([cx, cy]);
        } catch {
          return;
        }
        // Primary sample.
        let primaryText: string | null = null;
        try {
          const idx = primaryImageData.worldToIndex(world);
          const i = Math.round(idx[0]);
          const j = Math.round(idx[1]);
          const k = Math.round(idx[2]);
          if (
            i >= 0 &&
            j >= 0 &&
            k >= 0 &&
            i < primaryDims[0] &&
            j < primaryDims[1] &&
            k < primaryDims[2]
          ) {
            const v = samplePrimary(i, j, k);
            if (typeof v === "number" && Number.isFinite(v)) {
              primaryText = labelFor(v, i, j, k, isPrimaryPet);
            }
          }
        } catch {
          /* outside volume */
        }
        // Fusion sample (only when modality differs and a fusion is
        // mounted). When the fusion is PT we apply suvFactorBw to it.
        let fusionText: string | null = null;
        if (fusionImageData && sampleFusion && fusionDims) {
          try {
            const idx = fusionImageData.worldToIndex(world);
            const i = Math.round(idx[0]);
            const j = Math.round(idx[1]);
            const k = Math.round(idx[2]);
            if (
              i >= 0 &&
              j >= 0 &&
              k >= 0 &&
              i < fusionDims[0] &&
              j < fusionDims[1] &&
              k < fusionDims[2]
            ) {
              const v = sampleFusion(i, j, k);
              if (typeof v === "number" && Number.isFinite(v)) {
                // The fusion is treated as PT when the primary is not.
                fusionText = labelFor(v, i, j, k, !isPrimaryPet);
              }
            }
          } catch {
            /* outside fusion volume */
          }
        }
        const text = [primaryText, fusionText].filter(Boolean).join(" · ");
        if (!text) {
          setVoxelHud((p) => (p[axis] === null ? p : { ...p, [axis]: null }));
          return;
        }
        setVoxelHud((p) => (p[axis] === text ? p : { ...p, [axis]: text }));
      };

      const wire = (ref: React.RefObject<HTMLDivElement | null>, vpId: string, axis: Axis) => {
        const el = ref.current;
        if (!el) return null;
        const handler = (e: MouseEvent) => sample(vpId, axis, e);
        const leave = () => setVoxelHud((p) => ({ ...p, [axis]: null }));
        el.addEventListener("mousemove", handler);
        el.addEventListener("mouseleave", leave);
        return () => {
          el.removeEventListener("mousemove", handler);
          el.removeEventListener("mouseleave", leave);
        };
      };

      const offs = [
        wire(axialDivRef, vpAxial, "axial"),
        wire(sagDivRef, vpSag, "sagittal"),
        wire(corDivRef, vpCor, "coronal"),
      ];
      return () => {
        for (const off of offs) off?.();
      };
      // ``toolGroupReady`` is in the deps list as a "setup complete"
      // signal: the volume is registered in ``cs.cache`` inside the
      // async ``setup()`` function, so on the very first render the
      // ``cs.cache.getVolume(volumeId)`` lookup above returns null
      // and the effect bails. Re-running it after setup flips the
      // ready flag is what wires the mousemove listeners against
      // the now-populated cache. Without this, the bottom-right HUD
      // stays empty for the whole session.
    }, [
      volumeId,
      fusionVolumeId,
      modality,
      suvFactorBw,
      showAxial,
      showSagittal,
      showCoronal,
      toolGroupReady,
    ]);

    // Lens probe listener — engaged only while ``activeTool ===
    // "measure-lens"``. Mounts mousemove / wheel / mousedown handlers
    // on each viewport ``<div>`` ref and feeds live disc stats to
    // ``lensReadout`` for SVG overlay rendering. The disc is sampled
    // client-side from the loaded volume (microseconds per frame); the
    // server-side ``compute_roi_stats`` is only called on pin via the
    // parent's ``onLensPin`` callback (the parent then writes the
    // marker and patches it with the server-confirmed values).
    //
    // Reuses the same sampler factory the voxel HUD effect builds, but
    // we can't share the closure (different lifecycle dep list);
    // factor-out into ``volumeSampling.ts`` keeps both call sites in
    // sync.
    // biome-ignore lint/correctness/useExhaustiveDependencies: viewer lifecycle effect — sampling closure intentionally captures snapshot deps.
    useEffect(() => {
      if (activeTool !== "measure-lens") return;
      if (!toolGroupReady) return;
      const engine = engineRef.current;
      if (!engine) return;

      const primaryVol = cs.cache.getVolume(volumeId) as unknown as Parameters<
        typeof makeSampler
      >[0];
      const primary = makeSampler(primaryVol);
      if (!primary) return;
      const fusion = fusionVolumeId
        ? makeSampler(
            cs.cache.getVolume(fusionVolumeId) as unknown as Parameters<typeof makeSampler>[0],
          )
        : null;
      const isPrimaryPet = (modality ?? "").toUpperCase() === "PT";
      const isFusionPet = (fusionModality ?? "").toUpperCase() === "PT";

      const formatLines = (
        statsPrim: ReturnType<typeof sampleDisk>,
        statsFus: ReturnType<typeof sampleDisk>,
        radiusMm: number,
      ): string[] => {
        const out: string[] = [];
        out.push(`Lens ${radiusMm.toFixed(1)} mm`);
        if (statsPrim) {
          const baseLabel = isPrimaryPet ? "PET" : "HU";
          const suvCapable = isPrimaryPet && !!suvFactorBw && suvFactorBw > 0;
          out.push(
            `${baseLabel} mean ${statsPrim.mean.toFixed(suvCapable ? 1 : 1)}` +
              ` ± ${statsPrim.std.toFixed(1)} · n=${statsPrim.count}`,
          );
          if (suvCapable) {
            const f = suvFactorBw as number;
            out.push(
              `SUV mean ${(statsPrim.mean * f).toFixed(2)} · max ${(statsPrim.max * f).toFixed(2)}`,
            );
          }
        }
        if (statsFus) {
          // The fusion HUD line lights up only when the fusion is the
          // PT (which is the standard PET-CT case). Otherwise we omit
          // it: the operator is looking at the CT anatomically and the
          // fusion's HU readout under the lens isn't useful.
          if (isFusionPet && suvFactorBw && suvFactorBw > 0) {
            const f = suvFactorBw;
            out.push(
              `Fusion SUV mean ${(statsFus.mean * f).toFixed(2)} · max ${(statsFus.max * f).toFixed(2)}`,
            );
          }
        }
        return out;
      };

      const wire = (ref: React.RefObject<HTMLDivElement | null>, vpId: string, axis: Axis) => {
        const el = ref.current;
        if (!el) return null;
        const vp = engine.getViewport(vpId) as
          | (cs.Types.IVolumeViewport & {
              canvasToWorld: (canvasPos: cs.Types.Point2) => cs.Types.Point3;
              worldToCanvas: (world: cs.Types.Point3) => cs.Types.Point2;
            })
          | undefined;
        if (!vp) return null;

        const onMove = (e: MouseEvent) => {
          const rect = el.getBoundingClientRect();
          const cx = e.clientX - rect.left;
          const cy = e.clientY - rect.top;
          let world: cs.Types.Point3;
          try {
            world = vp.canvasToWorld([cx, cy]);
          } catch {
            return;
          }
          const r = lensRadiusRef.current;
          // Pixel radius for the SVG circle — convert ``r mm`` to
          // canvas pixels by mapping a point ``r mm`` east of the
          // centre and measuring the screen distance.
          let radiusCanvasPx = Math.max(1, r * 2);
          try {
            const eastWorld: cs.Types.Point3 = [world[0] + r, world[1], world[2]];
            const eastCanvas = vp.worldToCanvas(eastWorld);
            const dxPx = eastCanvas[0] - cx;
            const dyPx = eastCanvas[1] - cy;
            const measured = Math.hypot(dxPx, dyPx);
            if (Number.isFinite(measured) && measured > 0) radiusCanvasPx = measured;
          } catch {
            /* keep fallback */
          }
          const statsPrim = sampleDisk(primary, world, axis, r);
          const statsFus = fusion ? sampleDisk(fusion, world, axis, r) : null;
          const lines = formatLines(statsPrim, statsFus, r);
          setLensReadout((p) => ({
            ...p,
            [axis]: {
              canvasCenter: { x: cx, y: cy },
              radiusCanvasPx,
              lines,
            },
          }));
          if (statsPrim) {
            const ijk = worldToIjk(primary, world);
            if (ijk) {
              lensSnapshotRef.current = {
                axis,
                centerIjk: ijk,
                stats: statsPrim,
                timestamp: performance.now(),
              };
            }
          }
        };

        const onLeave = () => {
          setLensReadout((p) => (p[axis] === null ? p : { ...p, [axis]: null }));
        };

        const onWheel = (e: WheelEvent) => {
          if (!e.shiftKey) return; // plain wheel = slice scroll, untouched
          e.preventDefault();
          e.stopImmediatePropagation();
          const step = e.deltaY < 0 ? 1.1 : 1 / 1.1;
          setLensRadiusMmState((prev) => {
            const next = Math.min(50, Math.max(1, Math.round(prev * step * 2) / 2));
            return next;
          });
        };

        const onDown = (e: MouseEvent) => {
          if (e.button !== 0) return; // pin only on primary button
          const snap = lensSnapshotRef.current;
          if (!snap || snap.axis !== axis) return;
          e.preventDefault();
          e.stopPropagation();
          onLensPin?.({
            axis: snap.axis,
            centerIjk: snap.centerIjk,
            radiusMm: lensRadiusRef.current,
            isPet: isPrimaryPet,
            suvFactor: suvFactorBw ?? null,
            stats: snap.stats,
          });
        };

        el.addEventListener("mousemove", onMove);
        el.addEventListener("mouseleave", onLeave);
        el.addEventListener("wheel", onWheel, { capture: true, passive: false });
        // ``mousedown`` capture phase so we intercept before Cornerstone's
        // CrosshairsTool or any passive tool acts on the click.
        el.addEventListener("mousedown", onDown, { capture: true });
        return () => {
          el.removeEventListener("mousemove", onMove);
          el.removeEventListener("mouseleave", onLeave);
          el.removeEventListener("wheel", onWheel, { capture: true } as EventListenerOptions);
          el.removeEventListener("mousedown", onDown, { capture: true } as EventListenerOptions);
        };
      };

      const offs = [
        wire(axialDivRef, vpAxial, "axial"),
        wire(sagDivRef, vpSag, "sagittal"),
        wire(corDivRef, vpCor, "coronal"),
      ];
      return () => {
        for (const off of offs) off?.();
        // Clear all per-axis readouts and snapshots when leaving the
        // lens mode so the SVG circle disappears immediately.
        setLensReadout({ axial: null, sagittal: null, coronal: null });
        lensSnapshotRef.current = null;
      };
    }, [
      activeTool,
      toolGroupReady,
      volumeId,
      fusionVolumeId,
      modality,
      fusionModality,
      suvFactorBw,
      onLensPin,
    ]);

    // Keep the rendering canvases aspect-correct when the layout
    // changes (e.g. user toggles 1x1 ↔ 1x2 ↔ 2x2). Without an
    // explicit ``engine.resize()`` Cornerstone keeps the previous
    // canvas dimensions and the browser scales the bitmap to fit
    // the new CSS box, which stretches the image. Observe each
    // viewport DIV and resize on every change. ``resize(true)`` =
    // re-render immediately; the second arg is left default so the
    // camera (zoom/pan) is preserved.
    // biome-ignore lint/correctness/useExhaustiveDependencies: viewer lifecycle effect — re-running on derived deps would tear down GPU resources.
    useEffect(() => {
      if (typeof ResizeObserver === "undefined") return;
      const targets: HTMLElement[] = [];
      for (const ref of [axialDivRef, sagDivRef, corDivRef]) {
        if (ref.current) targets.push(ref.current);
      }
      if (targets.length === 0) return;
      let raf = 0;
      const ro = new ResizeObserver(() => {
        if (raf) return;
        raf = requestAnimationFrame(() => {
          raf = 0;
          const engine = engineRef.current;
          if (!engine) return;
          try {
            engine.resize(true);
          } catch {
            /* engine torn down between frames */
          }
        });
      });
      for (const el of targets) ro.observe(el);
      return () => {
        if (raf) cancelAnimationFrame(raf);
        ro.disconnect();
      };
    }, [showAxial, showSagittal, showCoronal]);

    // Pane teardown on unmount. The engine is a SINGLE shared singleton across
    // all panes (one WebGL context), so we do NOT call engine.destroy() (it
    // reaches into vtk's render context which React may already have released,
    // and dev StrictMode double-fires the cleanup → a null-context crash). What
    // we MUST do is release THIS pane's viewports via disableElement, else
    // viewports + canvases accumulate on the shared engine as panes mount.
    // biome-ignore lint/correctness/useExhaustiveDependencies: viewer lifecycle effect — re-running on derived deps would tear down GPU resources.
    useEffect(() => {
      return () => {
        const engine = engineRef.current;
        engineRef.current = null;
        if (engine) {
          for (const vpId of [vpAxial, vpSag, vpCor]) {
            try {
              engine.disableElement(vpId);
            } catch {
              /* viewport not enabled */
            }
          }
        }
        try {
          csTools.ToolGroupManager.destroyToolGroup(toolGroupId);
        } catch {
          /* not created */
        }
      };
    }, []);

    // Re-apply VOI when the sidebar sliders change. Primary and
    // fusion are kept on independent (wc, ww) pairs so the user
    // can dial CT contrast without disturbing PET SUV scaling.
    useEffect(() => {
      const engine = engineRef.current;
      if (!engine) return;
      applyVoiToAll(engine, [vpAxial, vpSag, vpCor], wc, ww, invert, volumeId);
    }, [wc, ww, invert, volumeId, vpAxial, vpSag, vpCor]);

    // Highlight the focused marker. For cornerstone-managed
    // annotations (Length, RectangleROI, …) the markerId is the
    // annotationUID — flip ``isSelected`` so Cornerstone applies its
    // own selected-stroke style and re-renders. For non-cornerstone
    // markers (bbox.lesion, fiducial, text-overlay) the SVG overlay
    // handles its own highlight, so the only thing we have to do
    // here is keep the cornerstone selection model in sync. On
    // ``focusedMarkerId=null`` we clear any prior selection so the
    // ring doesn't stick when the operator clicks elsewhere.
    useEffect(() => {
      try {
        const sel = (
          csTools.annotation as {
            selection?: {
              setAnnotationSelected?: (
                uid: string,
                selected: boolean,
                preserveSelected?: boolean,
              ) => void;
              deselectAnnotation?: (uid?: string) => void;
            };
          }
        ).selection;
        if (!sel?.setAnnotationSelected) return;
        if (sel.deselectAnnotation) {
          sel.deselectAnnotation();
        }
        if (!focusedMarkerId) return;
        // Guard: only select when the id matches a known annotation.
        // Cornerstone tolerates unknown ids, but the no-op log noise
        // is annoying in dev.
        const all = csTools.annotation.state.getAllAnnotations() as Array<{
          annotationUID?: string;
        }>;
        if (all.some((a) => a.annotationUID === focusedMarkerId)) {
          sel.setAnnotationSelected(focusedMarkerId, true, false);
          engineRef.current?.renderViewports([vpAxial, vpSag, vpCor]);
        }
      } catch {
        // Selection API surface drifted across CS Tools minor
        // versions; failure here is non-fatal — the SVG overlay still
        // pulses for non-cornerstone markers, and the crosshair has
        // already jumped to the marker's centroid.
      }
    }, [focusedMarkerId]);

    useEffect(() => {
      const engine = engineRef.current;
      if (!engine || !fusionVolumeId) return;
      applyVoiToAll(engine, [vpAxial, vpSag, vpCor], fusionWC, fusionWW, false, fusionVolumeId);
    }, [fusionWC, fusionWW, fusionVolumeId, vpAxial, vpSag, vpCor]);

    // Re-apply the PET LUT when the user picks a different colormap.
    // Goes through ``vp.setProperties({colormap: {name}}, volumeId)``
    // — the only path that consults CS3D's
    // ``_getApplicableVolumeActor`` and reliably finds the fusion
    // actor regardless of how it was bound. The previous
    // hand-rolled ``vp.getActor(volumeId)`` returned undefined on
    // volumes mounted via ``setVolumesForViewports`` (the actor's
    // internal UID differs from the volumeId in CS3D 4.x), which is
    // why the colormap picker silently no-op'ed.
    //
    // Falls back to the in-place CTF rewrite when CS3D's colormap
    // registry isn't writable (e.g. a stripped build); the ramps in
    // ``applyPetColormap`` and ``registerPetColormaps`` are kept in
    // sync so the on-canvas appearance is identical either way.
    useEffect(() => {
      const engine = engineRef.current;
      if (!engine || !fusionVolumeId) return;
      const colormapName = PET_COLORMAP_REGISTRY_NAME[petColormap];
      for (const vpId of [vpAxial, vpSag, vpCor]) {
        const vp = engine.getViewport(vpId) as
          | (cs.Types.IVolumeViewport & {
              setProperties?: (
                props: {
                  colormap?: { name: string };
                },
                volumeId?: string,
              ) => void;
              getActor: (id: string) => { actor?: unknown } | undefined;
            })
          | undefined;
        if (!vp) continue;
        let applied = false;
        if (typeof vp.setProperties === "function") {
          try {
            vp.setProperties({ colormap: { name: colormapName } }, fusionVolumeId);
            applied = true;
          } catch {
            /* fall through to legacy direct CTF path */
          }
        }
        if (!applied) {
          const actorEntry = vp.getActor(fusionVolumeId);
          const actor = (actorEntry as { actor?: unknown } | undefined)?.actor as
            | {
                getProperty: () => {
                  getRGBTransferFunction: (i: number) => unknown;
                };
              }
            | undefined;
          if (actor) {
            const ctf = actor.getProperty().getRGBTransferFunction(0) as {
              addRGBPoint: (x: number, r: number, g: number, b: number) => void;
              removeAllPoints: () => void;
            };
            applyPetColormap(ctf, petColormap);
          }
        }
        vp.render();
      }
    }, [petColormap, fusionVolumeId, vpAxial, vpSag, vpCor]);

    // Crop-box clipping planes on the MPR viewports. The 3D pane has
    // its own VolumeViewer-side effect; here we replicate the planes
    // on each axial / sagittal / coronal volume mapper so the cut
    // also shows up in the slice views (otherwise the user cropped
    // the 3D rendering but the MPR slices kept showing the whole
    // body, which is what made the feature look broken).
    //
    // Goes through ``actor.getMapper()`` (the canonical CS3D path)
    // rather than reaching directly into the actor: ``getMapper()``
    // is stable across CS3D versions, and the mapper inherits
    // ``addClippingPlane`` / ``removeAllClippingPlanes`` from
    // ``vtkAbstractMapper`` so the same six axis-aligned planes work
    // on volume + ortho slice mappers without special-casing.
    // biome-ignore lint/correctness/useExhaustiveDependencies: viewer lifecycle effect — re-running on derived deps would tear down GPU resources.
    useEffect(() => {
      const engine = engineRef.current;
      if (!engine) return;
      const planes: Array<{
        origin: [number, number, number];
        normal: [number, number, number];
      }> = [];
      if (cropBox) {
        const [nx, ny, nz] = volume.dimensions;
        const [sx, sy, sz] = volume.spacing;
        const clamp = (n: number) => Math.max(0, Math.min(1, n));
        const xlo = clamp(cropBox.lower[0]) * (nx - 1) * sx;
        const ylo = clamp(cropBox.lower[1]) * (ny - 1) * sy;
        const zlo = clamp(cropBox.lower[2]) * (nz - 1) * sz;
        const xhi = clamp(cropBox.upper[0]) * (nx - 1) * sx;
        const yhi = clamp(cropBox.upper[1]) * (ny - 1) * sy;
        const zhi = clamp(cropBox.upper[2]) * (nz - 1) * sz;
        planes.push(
          { origin: [xlo, 0, 0], normal: [1, 0, 0] },
          { origin: [xhi, 0, 0], normal: [-1, 0, 0] },
          { origin: [0, ylo, 0], normal: [0, 1, 0] },
          { origin: [0, yhi, 0], normal: [0, -1, 0] },
          { origin: [0, 0, zlo], normal: [0, 0, 1] },
          { origin: [0, 0, zhi], normal: [0, 0, -1] },
        );
      }
      const apply = (vpId: string) => {
        const vp = engine.getViewport(vpId) as
          | (cs.Types.IVolumeViewport & {
              getActors?: () => Array<{ uid?: string; actor?: unknown }>;
            })
          | undefined;
        if (!vp) return;
        const actors = vp.getActors?.() ?? [];
        for (const entry of actors) {
          const a = entry.actor as
            | {
                getMapper?: () => {
                  removeAllClippingPlanes?: () => void;
                  addClippingPlane?: (p: unknown) => void;
                };
              }
            | undefined;
          const mapper = a?.getMapper?.();
          if (!mapper?.removeAllClippingPlanes) continue;
          mapper.removeAllClippingPlanes();
          for (const p of planes) {
            const plane = vtkPlane.newInstance();
            plane.setOrigin(p.origin[0], p.origin[1], p.origin[2]);
            plane.setNormal(p.normal[0], p.normal[1], p.normal[2]);
            mapper.addClippingPlane?.(plane);
          }
        }
        vp.render();
      };
      apply(vpAxial);
      apply(vpSag);
      apply(vpCor);
    }, [
      cropBox,
      volume.dimensions,
      volume.spacing,
      vpAxial,
      vpSag,
      vpCor,
      // ``toolGroupReady`` flips true after the volume mount setup
      // completes; without it this effect runs once before
      // ``setVolumesForViewports`` has bound the volume actor and
      // ``vp.getActors()`` returns an empty list.
      toolGroupReady,
    ]);

    // biome-ignore lint/correctness/useExhaustiveDependencies: viewer lifecycle effect — re-running on derived deps would tear down GPU resources.
    const updateCrosshair = useCallback(
      (pos: [number, number, number]) => {
        // ``mprRef.current.setCrosshair`` is invoked from the parent
        // page in a few places that run during the React render
        // (notably the ``setMprCrosshair`` updater function used by
        // the keyboard ↑/↓ slice hotkeys). Calling our React
        // ``setCrosshair`` setter synchronously inside that
        // updater triggers the dreaded "Cannot update a component
        // while rendering a different component" warning. Pushing
        // the local state update to a microtask defers it past the
        // current commit; the Cornerstone-side camera move stays
        // synchronous because vtk/Cornerstone aren't React.
        queueMicrotask(() => {
          setCrosshair((prev) =>
            prev[0] === pos[0] && prev[1] === pos[1] && prev[2] === pos[2] ? prev : pos,
          );
        });
        onCrosshairChange?.(pos);
        const engine = engineRef.current;
        if (!engine) return;
        const cachedVol = cs.cache.getVolume(volumeId);
        const imageData = (cachedVol as unknown as { imageData?: { worldToIndex: unknown } })
          ?.imageData;
        if (!imageData) return;
        const world = cs.utilities.transformIndexToWorld(imageData, [pos[0], pos[1], pos[2]]) as
          | cs.Types.Point3
          | undefined;
        if (!world) return;
        // Each ortho viewport slices through one world axis; for any
        // crosshair update we only change the focal point's component
        // along that viewport's slicing axis. The remaining two
        // components are in-plane positions (vertical/horizontal pan
        // within the slice) — leaving them alone is what makes
        // arrow-on-axial keep sagittal/coronal in place while the
        // CrosshairsTool just redraws the reference line at the new
        // Z. Without this, every crosshair tick translated the
        // sagittal/coronal cameras as well, looking like a phantom
        // pan.
        const sliceAxisOf: Record<string, 0 | 1 | 2> = {
          [vpAxial]: 2,
          [vpSag]: 0,
          [vpCor]: 1,
        };
        for (const id of [vpAxial, vpSag, vpCor]) {
          const vp = engine.getViewport(id) as cs.Types.IVolumeViewport | undefined;
          if (!vp) continue;
          const cam = vp.getCamera();
          if (!cam.focalPoint || !cam.position) {
            vp.setCamera({ focalPoint: world });
            vp.render();
            continue;
          }
          const axisIdx = sliceAxisOf[id];
          const delta = world[axisIdx] - cam.focalPoint[axisIdx];
          if (Math.abs(delta) < 1e-6) {
            vp.render();
            continue;
          }
          const newFocal: cs.Types.Point3 = [
            cam.focalPoint[0],
            cam.focalPoint[1],
            cam.focalPoint[2],
          ];
          newFocal[axisIdx] = world[axisIdx];
          const newPos: cs.Types.Point3 = [cam.position[0], cam.position[1], cam.position[2]];
          newPos[axisIdx] = cam.position[axisIdx] + delta;
          vp.setCamera({ focalPoint: newFocal, position: newPos });
          vp.render();
        }
      },
      [onCrosshairChange, volumeId],
    );

    const handleWindowChange = useCallback((newWC: number, newWW: number) => {
      setWC(newWC);
      setWW(newWW);
    }, []);

    // biome-ignore lint/correctness/useExhaustiveDependencies: viewer lifecycle effect — re-running on derived deps would tear down GPU resources.
    useImperativeHandle(
      ref,
      (): MPRLayoutHandle => ({
        crosshair,
        setCrosshair: (pos) => updateCrosshair(pos),
        dims: volume.dimensions,
        wc,
        ww,
        invert,
        setWC,
        setWW,
        setInvert,
        fusionWC,
        fusionWW,
        fusionAlpha,
        setFusionWC,
        setFusionWW,
        setFusionAlpha,
        fusionVisible,
        setFusionVisible,
        setCrosshairFromSourceSeries: (sourceSeriesId, voxel) => {
          // PT-IJK → world → primary-IJK conversion. Used by the
          // hot-spots panel: the backend ranks centroids in the PT
          // volume's frame; the MPR primary may be a CT (PT-CT
          // fusion review). Without this round-trip, ``setCrosshair``
          // would treat PT-IJK as primary-IJK and the click would
          // jump to the wrong slice. Returns ``false`` when either
          // volume isn't cached so the caller can fall back.
          const sourceId =
            sourceSeriesId === seriesId
              ? volumeId
              : fusionVolumeId && fusionSeriesId === sourceSeriesId
                ? fusionVolumeId
                : `${VOLUME_PRIMARY_PREFIX}${sourceSeriesId}`;
          const sourceVol = cs.cache.getVolume(sourceId) as unknown as
            | {
                imageData?: {
                  indexToWorld?: (i: cs.Types.Point3) => cs.Types.Point3;
                };
              }
            | undefined;
          const primaryVol = cs.cache.getVolume(volumeId) as unknown as
            | {
                imageData?: {
                  worldToIndex?: (w: cs.Types.Point3) => cs.Types.Point3;
                };
              }
            | undefined;
          const sourceImg = sourceVol?.imageData;
          const primaryImg = primaryVol?.imageData;
          if (!sourceImg?.indexToWorld || !primaryImg?.worldToIndex) {
            return false;
          }
          // Same volume: no conversion, just clamp + jump.
          if (sourceId === volumeId) {
            updateCrosshair([
              Math.max(0, Math.round(voxel[0])),
              Math.max(0, Math.round(voxel[1])),
              Math.max(0, Math.round(voxel[2])),
            ]);
            return true;
          }
          try {
            const world = sourceImg.indexToWorld([voxel[0], voxel[1], voxel[2]]);
            const idx = primaryImg.worldToIndex(world);
            updateCrosshair([
              Math.max(0, Math.round(idx[0])),
              Math.max(0, Math.round(idx[1])),
              Math.max(0, Math.round(idx[2])),
            ]);
            return true;
          } catch {
            return false;
          }
        },
        getCrosshairWorld: (ijk) => {
          const src = ijk ?? crosshair;
          const vol = cs.cache.getVolume(volumeId) as unknown as
            | { imageData?: { indexToWorld?: (i: cs.Types.Point3) => cs.Types.Point3 } }
            | undefined;
          const img = vol?.imageData;
          if (!img?.indexToWorld) return null;
          try {
            const w = img.indexToWorld([src[0], src[1], src[2]]);
            return [w[0], w[1], w[2]];
          } catch {
            return null;
          }
        },
        setCrosshairWorld: (world) => {
          const vol = cs.cache.getVolume(volumeId) as unknown as
            | { imageData?: { worldToIndex?: (w: cs.Types.Point3) => cs.Types.Point3 } }
            | undefined;
          const img = vol?.imageData;
          if (!img?.worldToIndex) return false;
          try {
            const idx = img.worldToIndex([world[0], world[1], world[2]]);
            const [nx, ny, nz] = volume.dimensions;
            const ri = Math.round(idx[0]);
            const rj = Math.round(idx[1]);
            const rk = Math.round(idx[2]);
            // Clamp to BOTH ends of this pane's grid. Without the upper clamp an
            // out-of-extent world point (a shorter phase of a multiphase study)
            // produced an out-of-bounds index → the camera flew into empty space
            // → BLACK pane. Snap to the nearest valid voxel and flag coverage so
            // the UI can mark the pane instead of showing wrong anatomy silently.
            const outOfCoverage =
              ri < 0 || ri > nx - 1 || rj < 0 || rj > ny - 1 || rk < 0 || rk > nz - 1;
            coverageRef.current = outOfCoverage;
            const clamped: [number, number, number] = [
              Math.max(0, Math.min(nx - 1, ri)),
              Math.max(0, Math.min(ny - 1, rj)),
              Math.max(0, Math.min(nz - 1, rk)),
            ];
            updateCrosshair(clamped);
            // Authoritative slice move + repaint via the viewport's own
            // ``jumpToWorld``. ``updateCrosshair``'s hand-rolled setCamera moved
            // the camera focal point correctly (instrumentation confirmed the
            // focal tracked the synced world Z on every pane) but did NOT repaint
            // the displayed slice in the multiphase panes — they froze on the
            // wrong anatomy. ``jumpToWorld`` is the cornerstone-sanctioned method
            // (the same path the working native StackScroll uses) and repaints
            // reliably. Jump to the CLAMPED world (nearest valid slice) so an
            // out-of-coverage phase snaps instead of flying into empty space.
            const engine = engineRef.current;
            if (engine) {
              const clampedWorld = cs.utilities.transformIndexToWorld(
                img as Parameters<typeof cs.utilities.transformIndexToWorld>[0],
                clamped,
              ) as cs.Types.Point3 | undefined;
              const axialVp = engine.getViewport(vpAxial) as
                | (cs.Types.IVolumeViewport & { jumpToWorld?: (w: cs.Types.Point3) => boolean })
                | undefined;
              if (clampedWorld && axialVp?.jumpToWorld) axialVp.jumpToWorld(clampedWorld);
              // Force the shared engine to repaint EVERY viewport's on-screen
              // canvas. Verified failure mode: on the shared rendering engine a
              // per-viewport render after a synced camera move only blits the
              // ONE pane it was called on — the sibling panes keep a stale frame
              // (HUD/crosshair read the new slice 153 while the canvas still
              // shows slice 0). A full engine render flags+copies all tiles.
              engine.render();
            }
            // false = caller learns the point isn't covered here (still rendered
            // at the nearest slice, never black).
            return !outOfCoverage;
          } catch {
            return false;
          }
        },
        getProbeState: () => {
          const ijk = crosshair;
          let lps: [number, number, number] | null = null;
          const v = cs.cache.getVolume(volumeId) as unknown as
            | {
                imageData?: {
                  indexToWorld?: (i: cs.Types.Point3) => cs.Types.Point3;
                  getOrigin?: () => number[];
                  getSpacing?: () => number[];
                  getDimensions?: () => number[];
                };
              }
            | undefined;
          try {
            const w = v?.imageData?.indexToWorld?.([ijk[0], ijk[1], ijk[2]]);
            if (w) lps = [w[0], w[1], w[2]];
          } catch {
            /* leave null */
          }
          // The ACTUAL displayed slice: the axial viewport's camera focal point
          // in world space. crosshairLps is the crosshair STATE; cameraFocalLps
          // is what the pane is really rendering. If they diverge, the display
          // is not following the crosshair (the multiphase-sync bug).
          let cameraFocalLps: [number, number, number] | null = null;
          try {
            const vp = engineRef.current?.getViewport(vpAxial) as
              | cs.Types.IVolumeViewport
              | undefined;
            const fp = vp?.getCamera?.()?.focalPoint;
            if (fp) cameraFocalLps = [fp[0], fp[1], fp[2]];
          } catch {
            /* leave null */
          }
          let volOrigin: number[] | null = null;
          let volSpacing: number[] | null = null;
          let volDims: number[] | null = null;
          try {
            volOrigin = v?.imageData?.getOrigin?.() ?? null;
            volSpacing = v?.imageData?.getSpacing?.() ?? null;
            volDims = v?.imageData?.getDimensions?.() ?? null;
          } catch {
            /* leave null */
          }
          const canvasEl = axialDivRef.current?.querySelector("canvas");
          return {
            crosshairIjk: [ijk[0], ijk[1], ijk[2]] as [number, number, number],
            crosshairLps: lps,
            sliceIndex: ijk[2],
            canvas: canvasEl ? { width: canvasEl.width, height: canvasEl.height } : null,
            outOfCoverage: coverageRef.current,
            cameraFocalLps,
            volOrigin,
            volSpacing,
            volDims,
            volumeIdUsed: volumeId,
          };
        },
        resetWL: () => {
          setWC(defaultWC);
          setWW(defaultWW);
        },
        resetFusionWL: () => {
          if (!fusionVolume || !fusionVolumeId) return;
          const r = ptAutoWL(fusionVolume.scalars);
          setFusionWC(r.wc);
          setFusionWW(r.ww);
          const engine = engineRef.current;
          if (engine) {
            applyVoiToAll(engine, [vpAxial, vpSag, vpCor], r.wc, r.ww, false, fusionVolumeId);
          }
        },
        // Rotation is handled implicitly by the crosshair tool's
        // T-handles (true oblique reformat); a hard 90° rotate
        // button on a Cornerstone VolumeViewport doesn't have a
        // public API in the current version. We render the button
        // as a no-op for now to keep the handle interface stable;
        // the crosshair gives us a strictly more capable gesture.
        rotateAll: () => {
          /* see comment above */
        },
        flipHAll: () => {
          flipAll(engineRef.current, [vpAxial, vpSag, vpCor], "horizontal");
        },
        flipVAll: () => {
          flipAll(engineRef.current, [vpAxial, vpSag, vpCor], "vertical");
        },
        removeAnnotation: (uid) => {
          try {
            csTools.annotation.state.removeAnnotation(uid);
          } catch {
            /* annotation may not exist anymore */
          }
          engineRef.current?.renderViewports([vpAxial, vpSag, vpCor]);
        },
        // Delete whatever annotation the operator has SELECTED (clicked). Reads
        // the Cornerstone selection model so a Del / button / shortcut can wipe
        // the active ROI without the caller having to know its UID. Returns the
        // removed UIDs so the parent can drop them from its own bookkeeping.
        // Skips navigation overlays (crosshairs) defensively.
        deleteSelected: () => {
          const sel = (
            csTools.annotation as {
              selection?: { getAnnotationsSelected?: () => string[] };
            }
          ).selection;
          const uids = sel?.getAnnotationsSelected?.() ?? [];
          if (!uids.length) return [];
          const byUid = new Map(
            (
              csTools.annotation.state.getAllAnnotations() as Array<{
                annotationUID?: string;
                metadata?: { toolName?: string };
              }>
            ).map((a) => [a.annotationUID, a]),
          );
          const removed: string[] = [];
          for (const uid of uids) {
            const a = byUid.get(uid);
            if (a?.metadata?.toolName === csTools.CrosshairsTool.toolName) continue;
            try {
              csTools.annotation.state.removeAnnotation(uid);
              removed.push(uid);
            } catch {
              /* already gone */
            }
          }
          if (removed.length) engineRef.current?.renderViewports([vpAxial, vpSag, vpCor]);
          return removed;
        },
        updateAnnotationLabel: (uid, label) => {
          const all = csTools.annotation.state.getAllAnnotations() as Array<{
            annotationUID?: string;
            data?: { label?: string };
          }>;
          const target = all.find((a) => a.annotationUID === uid);
          if (!target?.data) return;
          target.data.label = label;
          // Try the official helper first — it both sets the label
          // and dispatches the LabelChange event so any open inline
          // text-box (ArrowAnnotateTool) refreshes. Fall back to
          // ``engine.render()`` if the helper isn't available in
          // this Cornerstone build.
          const el = axialDivRef.current ?? sagDivRef.current ?? corDivRef.current ?? null;
          const utils = (
            csTools as unknown as {
              utilities?: {
                setAnnotationLabel?: (a: unknown, el: HTMLElement, label: string) => void;
              };
            }
          ).utilities;
          if (el && utils?.setAnnotationLabel) {
            try {
              utils.setAnnotationLabel(target, el, label);
              return;
            } catch {
              /* fall through */
            }
          }
          engineRef.current?.renderViewports([vpAxial, vpSag, vpCor]);
        },
        lensRadiusMm,
        setLensRadiusMm: (v) => {
          const clamped = Math.min(50, Math.max(1, Math.round(v * 2) / 2));
          setLensRadiusMmState(clamped);
        },
        pinCurrentLens: () => {
          const snap = lensSnapshotRef.current;
          if (!snap || !onLensPin) return false;
          onLensPin({
            axis: snap.axis,
            centerIjk: snap.centerIjk,
            radiusMm: lensRadiusRef.current,
            isPet: (modality ?? "").toUpperCase() === "PT",
            suvFactor: suvFactorBw ?? null,
            stats: snap.stats,
          });
          return true;
        },
        clearAnnotations: () => {
          // ``getAllAnnotations`` returns a flat list across all
          // FrameOfReferenceUIDs, which is exactly what we want when
          // the user hits "Clear all" — measurements drawn in the
          // primary and the fusion frames both get wiped.
          //
          // Crosshair reference lines are stored as annotations too
          // (toolName === ``CrosshairsTool.toolName``) — wiping them
          // would erase the default navigation overlay and force the
          // user to re-pick the tool to get them back. Filter them
          // out, plus any other built-in navigation tool that might
          // show up in the annotation state for the same reason.
          const PROTECTED_TOOLS = new Set([csTools.CrosshairsTool.toolName]);
          const all = csTools.annotation.state.getAllAnnotations() as Array<{
            annotationUID?: string;
            metadata?: { toolName?: string };
          }>;
          for (const a of all) {
            if (!a.annotationUID) continue;
            if (a.metadata?.toolName && PROTECTED_TOOLS.has(a.metadata.toolName)) {
              continue;
            }
            try {
              csTools.annotation.state.removeAnnotation(a.annotationUID);
            } catch {
              /* ignore individual failures */
            }
          }
          engineRef.current?.renderViewports([vpAxial, vpSag, vpCor]);
        },
        cancelDraw: () => {
          // Abort a half-drawn annotation on each of this pane's viewport
          // elements so the operator can interrupt a measurement (Esc / tool
          // switch) without leaving a stray partial ROI behind.
          for (const ref of [axialDivRef, sagDivRef, corDivRef]) {
            const el = ref.current;
            if (!el) continue;
            try {
              csTools.cancelActiveManipulations(el);
            } catch {
              /* nothing in progress */
            }
          }
          engineRef.current?.renderViewports([vpAxial, vpSag, vpCor]);
        },
      }),
      [
        crosshair,
        wc,
        ww,
        invert,
        defaultWC,
        defaultWW,
        updateCrosshair,
        volume.dimensions,
        fusionWC,
        fusionWW,
        fusionAlpha,
        fusionVolume,
        fusionVolumeId,
        vpAxial,
        vpSag,
        vpCor,
        lensRadiusMm,
        modality,
        onLensPin,
        suvFactorBw,
      ],
    );

    // Fusion visibility effect: zero / restore the OVERLAY volume's
    // scalar opacity so the user can toggle PET off, see the underlying
    // CT alone, and toggle PET back on.
    //
    // Triggers ONLY on a real fusionVisible flip. On first mount and
    // on async fills of volumeId/modality/fusionModality/petColormap
    // we skip: the setup callback already installed the soft-shoulder
    // OFUN via fusionOverlay.callback and the colormap effect (above)
    // has set the colormap NAME. Re-applying opacity here at mount
    // time leaks across the colormap setProperties call and ends up
    // toning down the wrong actor on initial render — symptom: PET
    // visible but CT base goes semi-transparent and the MIP shows
    // a saturated white frame until the user toggles MIP off+on.
    //
    // Goes through ``vp.setProperties({colormap: {opacity}}, volumeId)``
    // rather than reaching into the actor's OTF directly: in CS3D 4.x
    // ``vp.getActor(volumeId)`` returns undefined for volumes mounted
    // via ``setVolumesForViewports``.
    const prevFusionVisibleRef = useRef<boolean | null>(null);
    // biome-ignore lint/correctness/useExhaustiveDependencies: viewer lifecycle effect — re-running on derived deps would tear down GPU resources.
    useEffect(() => {
      const prev = prevFusionVisibleRef.current;
      prevFusionVisibleRef.current = fusionVisible;
      if (prev === null || prev === fusionVisible) {
        // First mount, or a non-fusionVisible dep changed: do nothing.
        // The setup callback owns the initial OFUN; the colormap effect
        // owns the colormap NAME on async petColormap changes.
        return;
      }
      const engine = engineRef.current;
      if (!engine || !fusionVolumeId) return;
      const cachedFusion = cs.cache.getVolume(fusionVolumeId);
      if (!cachedFusion) return;
      const order = resolveFusionOrder(
        { volumeId, modality: modality ?? null },
        { volumeId: fusionVolumeId, modality: fusionModality ?? null },
      );
      const overlayId = order.overlayVolumeId;
      if (!overlayId) return;
      // Match the setup callback's soft-shoulder ramp (Addendum A §5)
      // when visible. When hidden we send an OFUN array all at
      // ``opacity: 0`` rather than the scalar ``0``: CS3D 4.x's
      // ``setProperties`` accepts both shapes in TypeScript but in
      // practice the scalar path silently no-ops on
      // setVolumesForViewports-mounted volumes — symptom: the user
      // toggles "PET off" and the PET layer keeps rendering on top
      // of the CT, with the wheel still scrolling only the CT
      // underneath. Using the array form goes through the same
      // OFUN-rebuild path as the visible branch and reliably zeros
      // the overlay.
      const opacity = fusionVisible
        ? [
            { value: 0, opacity: 0 },
            { value: 0.55, opacity: 0 },
            { value: 0.6, opacity: 0.05 },
            { value: 0.7, opacity: 0.3 },
            { value: 0.8, opacity: 0.6 },
            { value: 1, opacity: 0.9 },
          ]
        : [
            { value: 0, opacity: 0 },
            { value: 1, opacity: 0 },
          ];
      for (const vpId of [vpAxial, vpSag, vpCor]) {
        const vp = engine.getViewport(vpId) as
          | (cs.Types.IVolumeViewport & {
              setProperties?: (
                props: {
                  colormap?: {
                    opacity?: { value: number; opacity: number }[] | number;
                  };
                },
                volumeId?: string,
              ) => void;
            })
          | undefined;
        if (!vp?.setProperties) continue;
        try {
          vp.setProperties({ colormap: { opacity } }, overlayId);
        } catch {
          /* swallow: a stripped-down build without setProperties just
             keeps the previous frame; the toggle is best-effort. */
        }
        vp.render();
      }
      // After flipping the overlay OFUN, force one engine-wide
      // re-frame. The per-viewport ``vp.render()`` above is
      // sufficient for the colormap update itself, but on the
      // "PET back on" path CS3D occasionally keeps the previous
      // composite cached until the camera reports a change — the
      // user observes "fusion is invisible until I hit reset
      // zoom+pan on axial". A second ``engine.render()`` invalidates
      // the cached composite without resetting the camera, so the
      // user keeps their pan + zoom.
      try {
        engine.renderViewports([vpAxial, vpSag, vpCor]);
      } catch {
        /* engine.render is best-effort like vp.render above. */
      }
    }, [fusionVisible, fusionVolumeId, volumeId, modality, fusionModality]);

    // SUV threshold filter (spec §3.4). Builds an opacity transfer
    // function that hides voxels with SUV outside [lo, hi] and applies
    // it to whichever volume holds the PT (primary if modality is PT,
    // otherwise the fusion overlay). Conversion SUV → scalar uses
    // ``suvFactorBw`` (which carries the active variant from the
    // page, not necessarily BW despite the prop name).
    //
    // Why an opacity TF and not voiRange:
    //   - voiRange controls W/L (contrast); pixels above the upper
    //     bound saturate to white but stay visible — kidneys/bladder
    //     would still glow as bright spots. The user wants them
    //     *gone*, not clamped, so we zero opacity outside the range.
    //   - Display-only by construction: ROI stats, hot-spot detection
    //     and the stored scalars are untouched.
    //
    // Goes through the same ``setProperties({colormap:{opacity}}, vid)``
    // path as the fusion-visibility toggle above (CS3D 4.x's
    // ``getActor(volumeId)`` returns undefined for volumes mounted via
    // ``setVolumesForViewports``, so direct OTF rewrites silently
    // no-op).
    //
    // Idempotent-on-disable: the effect only writes to the actor when
    // the user has actually engaged the threshold at least once. With
    // the threshold OFF we restore a sensible default (smoothstep on
    // fusion, opacity=1 on primary-PT) only after a previous "on"
    // run; otherwise we leave the actor's mount-time default
    // untouched, so first-load rendering matches the historical
    // behaviour exactly when the user never touches the slider.
    const hideOfunActiveRef = useRef(false);
    useEffect(() => {
      const engine = engineRef.current;
      if (!engine) return;
      const primaryIsPt = (modality ?? "").toUpperCase() === "PT";
      const fusionIsPt = (fusionModality ?? "").toUpperCase() === "PT";
      const ptVolumeId = primaryIsPt ? volumeId : fusionIsPt ? fusionVolumeId : null;
      if (!ptVolumeId) return;
      const ptScalarMax = primaryIsPt ? volume.range[1] : (fusionVolume?.range?.[1] ?? 1);
      // Build the OFUN points. CS3D's ``colormap.opacity`` accepts a
      // ``{value, opacity}[]`` array in the volume's raw scalar space
      // (Bq/mL for PET); vtk PiecewiseFunction interpolates between
      // points and CLAMPS at the endpoints, so we explicitly bracket
      // the volume's full scalar range to keep behaviour deterministic
      // outside the chosen window.
      type OpacityPt = { value: number; opacity: number };
      const buildHideOfun = (lo: number, hi: number | null): OpacityPt[] => {
        const f = suvFactorBw ?? null;
        if (!f || f <= 0) return [];
        const loBq = lo / f;
        const hiBq = hi != null ? hi / f : ptScalarMax;
        const eps = Math.max(1e-6, (hiBq - loBq) * 0.01);
        const upper = Math.max(hiBq + 2 * eps, ptScalarMax);
        const points: OpacityPt[] = [
          { value: 0, opacity: 0 },
          { value: Math.max(0, loBq - eps), opacity: 0 },
          { value: loBq, opacity: 0.85 },
        ];
        if (hi != null) {
          points.push(
            { value: hiBq, opacity: 0.85 },
            { value: hiBq + eps, opacity: 0 },
            { value: upper, opacity: 0 },
          );
        } else {
          points.push({ value: upper, opacity: 0.85 });
        }
        return points;
      };
      const FUSION_DEFAULT: OpacityPt[] = [
        { value: 0, opacity: 0 },
        { value: 0.55, opacity: 0 },
        { value: 0.6, opacity: 0.05 },
        { value: 0.7, opacity: 0.3 },
        { value: 0.8, opacity: 0.6 },
        { value: 1, opacity: 0.9 },
      ];
      let opacity: OpacityPt[] | number;
      if (petSuvHide && suvFactorBw && suvFactorBw > 0) {
        const ofun = buildHideOfun(petSuvHide.lo, petSuvHide.hi);
        if (ofun.length === 0) return;
        opacity = ofun;
        hideOfunActiveRef.current = true;
      } else {
        if (!hideOfunActiveRef.current) return;
        // Threshold was previously engaged and is now off — restore
        // the per-mode default. Primary-PT goes back to "show
        // everything" (constant opacity 1); fusion-PT goes back to
        // the soft-shoulder smoothstep that the setup callback
        // installs on first mount.
        opacity = primaryIsPt ? 1 : FUSION_DEFAULT;
        hideOfunActiveRef.current = false;
      }
      for (const vpId of [vpAxial, vpSag, vpCor]) {
        const vp = engine.getViewport(vpId) as
          | (cs.Types.IVolumeViewport & {
              setProperties?: (
                props: { colormap?: { opacity?: OpacityPt[] | number } },
                volumeId?: string,
              ) => void;
            })
          | undefined;
        if (!vp?.setProperties) continue;
        try {
          vp.setProperties({ colormap: { opacity } }, ptVolumeId);
        } catch {
          /* swallow: stripped CS3D builds without setProperties just
             keep the previous OFUN; the threshold is best-effort. */
        }
        vp.render();
      }
    }, [
      petSuvHide,
      suvFactorBw,
      modality,
      fusionModality,
      volumeId,
      fusionVolumeId,
      volume.range,
      fusionVolume,
      vpAxial,
      vpSag,
      vpCor,
    ]);

    const flipViewport = (axis: Axis, dir: "horizontal" | "vertical") => {
      const engine = engineRef.current;
      if (!engine) return;
      const vp = engine.getViewport(viewportIdByAxis[axis]) as
        | (cs.Types.IVolumeViewport & {
            flip: (d: { flipHorizontal?: boolean; flipVertical?: boolean }) => void;
          })
        | undefined;
      if (!vp) return;
      vp.flip(dir === "horizontal" ? { flipHorizontal: true } : { flipVertical: true });
      vp.render();
    };

    /** Zoom in/out by a multiplicative factor (1.25 in, 0.8 out).
     *  Cornerstone's parallel-projection camera uses
     *  ``parallelScale`` (half the world-space height of the
     *  viewport): smaller value = more zoom. We divide by the
     *  factor so callers can use intuitive ``> 1 = in`` semantics. */
    const zoomViewport = (axis: Axis, factor: number) => {
      const engine = engineRef.current;
      if (!engine) return;
      const vp = engine.getViewport(viewportIdByAxis[axis]) as cs.Types.IVolumeViewport | undefined;
      if (!vp) return;
      const cam = vp.getCamera();
      const ps = (cam as { parallelScale?: number }).parallelScale ?? 0;
      if (ps <= 0) return;
      vp.setCamera({ ...cam, parallelScale: ps / factor } as Parameters<typeof vp.setCamera>[0]);
      vp.render();
    };

    const resetViewportZoom = (axis: Axis) => {
      const engine = engineRef.current;
      if (!engine) return;
      const vp = engine.getViewport(viewportIdByAxis[axis]) as cs.Types.IVolumeViewport | undefined;
      if (!vp) return;
      vp.resetCamera();
      vp.render();
    };

    /** Rotate one viewport 90° clockwise around its viewing axis.
     *  Cornerstone's VolumeViewport doesn't expose a public rotate
     *  API, so we rotate the camera's ``viewUp`` vector around the
     *  ``viewPlaneNormal`` (= camera direction). 90° CW = swapping
     *  components and negating one. */
    const rotateViewport = (axis: Axis) => {
      const engine = engineRef.current;
      if (!engine) return;
      const vp = engine.getViewport(viewportIdByAxis[axis]) as cs.Types.IVolumeViewport | undefined;
      if (!vp) return;
      const cam = vp.getCamera();
      if (!cam.viewPlaneNormal || !cam.viewUp) return;
      // Rodrigues rotation: rotate viewUp by 90° around viewPlaneNormal.
      // For 90°: cosθ=0, sinθ=1 → v_rot = (n × v) + (n · v) * n.
      const n = cam.viewPlaneNormal;
      const v = cam.viewUp;
      const dot = n[0] * v[0] + n[1] * v[1] + n[2] * v[2];
      const cross: cs.Types.Point3 = [
        n[1] * v[2] - n[2] * v[1],
        n[2] * v[0] - n[0] * v[2],
        n[0] * v[1] - n[1] * v[0],
      ];
      const newViewUp: cs.Types.Point3 = [
        cross[0] + n[0] * dot,
        cross[1] + n[1] * dot,
        cross[2] + n[2] * dot,
      ];
      vp.setCamera({ viewUp: newViewUp });
      vp.render();
    };

    /** Capture the current viewport as a PNG Blob. Reads the canvas
     *  Cornerstone composes (raster + annotation SVG layer rendered
     *  onto the same canvas), so the resulting PNG carries everything
     *  the user sees — overlays, ROIs, crosshairs included. Returns
     *  ``null`` when the viewport / canvas isn't available yet. */
    const captureViewportPng = async (axis: Axis): Promise<Blob | null> => {
      const engine = engineRef.current;
      if (!engine) return null;
      const vp = engine.getViewport(viewportIdByAxis[axis]) as
        | (cs.Types.IVolumeViewport & {
            getCanvas?: () => HTMLCanvasElement;
          })
        | undefined;
      const canvas = vp?.getCanvas?.();
      if (!canvas) return null;
      // Force a fresh render before grabbing the bitmap so any
      // pending tool annotation paint lands on the canvas.
      try {
        vp?.render();
      } catch {
        /* render is best-effort */
      }
      return await new Promise<Blob | null>((resolve) => {
        try {
          canvas.toBlob((blob) => resolve(blob ?? null), "image/png");
        } catch {
          resolve(null);
        }
      });
    };

    const flashScreenshotStatus = (pane: PaneKey, msg: string) => {
      setScreenshotStatus({ pane, msg });
      window.setTimeout(() => {
        setScreenshotStatus((cur) => (cur && cur.pane === pane && cur.msg === msg ? null : cur));
      }, 2500);
    };

    const screenshotFilename = (axis: Axis) => {
      const ts = new Date().toISOString().replace(/[:.]/g, "-").slice(0, 19);
      const sid = (seriesId ?? "series").slice(0, 8);
      return `bv_${sid}_${axis}_${ts}.png`;
    };

    const downloadScreenshot = async (axis: Axis) => {
      setCameraMenuFor(null);
      const blob = await captureViewportPng(axis);
      if (!blob) {
        flashScreenshotStatus(axis, "Capture failed");
        return;
      }
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = screenshotFilename(axis);
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      // Defer revoking so Safari has time to fetch the blob.
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
      flashScreenshotStatus(axis, "Downloaded");
    };

    const copyScreenshotToClipboard = async (axis: Axis) => {
      setCameraMenuFor(null);
      const blob = await captureViewportPng(axis);
      if (!blob) {
        flashScreenshotStatus(axis, "Capture failed");
        return;
      }
      try {
        // Clipboard image write is HTTPS-only; on http://localhost it
        // works for dev. Older browsers without ClipboardItem fall
        // through to the catch and we report the limitation.
        const ClipboardItemCtor = (
          window as unknown as {
            ClipboardItem?: new (items: Record<string, Blob>) => unknown;
          }
        ).ClipboardItem;
        if (!ClipboardItemCtor || !navigator.clipboard?.write) {
          throw new Error("Clipboard API unavailable");
        }
        await navigator.clipboard.write([
          new ClipboardItemCtor({ "image/png": blob }) as ClipboardItem,
        ]);
        flashScreenshotStatus(axis, "Copied");
      } catch (err) {
        const reason = err instanceof Error ? err.message : "unknown error";
        flashScreenshotStatus(axis, `Clipboard: ${reason}`);
      }
    };

    const saveScreenshotToStudy = async (axis: Axis) => {
      setCameraMenuFor(null);
      if (!studyId) {
        flashScreenshotStatus(axis, "No study to attach to");
        return;
      }
      const blob = await captureViewportPng(axis);
      if (!blob) {
        flashScreenshotStatus(axis, "Capture failed");
        return;
      }
      setScreenshotBusy(axis);
      try {
        const fd = new FormData();
        fd.append("file", blob, screenshotFilename(axis));
        fd.append("viewport", axis);
        if (seriesId) fd.append("series_id", seriesId);
        // Use the shared ``request`` wrapper so the Bearer token from
        // localStorage gets injected. Multipart: pass the FormData
        // via ``body`` (do NOT set content-type — the browser sets
        // the multipart boundary automatically).
        const json = await request<{ document_id: string }>(`/api/studies/${studyId}/screenshots`, {
          method: "POST",
          body: fd,
        });
        flashScreenshotStatus(axis, "Saved to study");
        onScreenshotSaved?.({ documentId: json.document_id, viewport: axis });
      } catch (err) {
        const reason = err instanceof Error ? err.message : "unknown error";
        flashScreenshotStatus(axis, `Save failed: ${reason}`);
      } finally {
        setScreenshotBusy(null);
      }
    };

    // Axis-pane sub-renderer for the 2D viewports. Always mounted so
    // the Cornerstone engine ↔ DOM binding stays stable; visibility
    // is controlled via ``display: none`` on the wrapper. Hidden
    // grid items don't occupy a track, so the visible panes naturally
    // distribute over the grid (no need to recompute templates by
    // visibility alone).
    const renderAxisPane = (axis: Axis, visible: boolean) => {
      const ref = axis === "axial" ? axialDivRef : axis === "sagittal" ? sagDivRef : corDivRef;
      const color =
        axis === "axial"
          ? "rgb(68, 170, 255)"
          : axis === "sagittal"
            ? "rgb(255, 68, 68)"
            : "rgb(68, 255, 68)";
      return (
        <div
          key={axis}
          style={{
            position: "relative",
            overflow: "hidden",
            background: "#000",
            display: visible ? "block" : "none",
          }}
          onContextMenu={(e) => e.preventDefault()}
        >
          <div
            ref={ref}
            style={{
              position: "absolute",
              inset: 0,
            }}
          />
          {overlaysOn && hasRealGeometry && (
            <OrientationMarkers
              letters={orientationByAxis[axis].letters}
              flags={orientationByAxis[axis].flags}
              inverted={invert}
            />
          )}
          {axis === "axial" && overlayMarkers && overlayMarkers.length > 0 && (
            <MarkerOverlay
              containerRef={ref}
              viewportId={vpAxial}
              engine={engineRef.current}
              primarySeriesId={seriesId ?? "anon"}
              primaryImageData={
                (cs.cache.getVolume(volumeId) as { imageData?: unknown } | null)
                  ?.imageData as Parameters<typeof MarkerOverlay>[0]["primaryImageData"]
              }
              fusionSeriesId={fusionSeriesId ?? null}
              fusionImageData={
                fusionVolumeId
                  ? ((cs.cache.getVolume(fusionVolumeId) as { imageData?: unknown } | null)
                      ?.imageData as Parameters<typeof MarkerOverlay>[0]["fusionImageData"])
                  : null
              }
              markers={overlayMarkers}
              axis="axial"
              focusedMarkerId={focusedMarkerId ?? null}
              onMarkerClick={onOverlayMarkerClick}
            />
          )}
          {/* Top-left consolidated identity + slice info. Putting
              everything in one corner keeps the top-right free for
              the toolbar buttons (no collisions). The bottom-right
              voxel HUD and bottom-left zoom remain untouched. */}
          {overlaysOn && (
            <div
              style={{
                position: "absolute",
                top: 4,
                left: 6,
                fontSize: "0.7rem",
                color,
                fontFamily: "ui-monospace, monospace",
                letterSpacing: "0.04em",
                pointerEvents: "none",
                lineHeight: 1.35,
                textShadow: "0 1px 2px rgba(0,0,0,0.7)",
                maxWidth: "60%",
              }}
            >
              <div>
                <strong>{axis.toUpperCase()}</strong>
                {modality ? ` · ${modality}` : ""}
              </div>
              {seriesDescription && (
                <div
                  style={{
                    color: "#cbd5e1",
                    fontWeight: 400,
                    overflow: "hidden",
                    textOverflow: "ellipsis",
                    whiteSpace: "nowrap",
                  }}
                >
                  {seriesDescription}
                </div>
              )}
              <div style={{ color: "#e6ecf3" }}>
                Slice {perVpInfo[axis].sliceIndex + 1}
                {perVpInfo[axis].sliceTotal > 0 ? ` / ${perVpInfo[axis].sliceTotal}` : ""}
              </div>
              <div style={{ color: "#94a3b8" }}>
                W {Math.round(ww)} · L {Math.round(wc)}
              </div>
            </div>
          )}
          {/* Bottom-left: zoom percent — separate from the identity
              block so the user's eye finds it quickly during
              zoom/pan gestures. */}
          {overlaysOn && (
            <div
              style={{
                position: "absolute",
                bottom: 4,
                left: 6,
                fontSize: "0.7rem",
                color: "#cbd5e1",
                fontFamily: "ui-monospace, monospace",
                pointerEvents: "none",
                textShadow: "0 1px 2px rgba(0,0,0,0.7)",
              }}
            >
              Zoom {perVpInfo[axis].zoomPct}%
            </div>
          )}
          {/* Top-right: per-viewport rotate / flip overlay buttons.
              Same affordances as the legacy MPRViewport corner. */}
          <div
            style={{
              position: "absolute",
              top: 2,
              right: 4,
              display: "flex",
              gap: 2,
              zIndex: 2,
            }}
          >
            <button
              type="button"
              onClick={() => zoomViewport(axis, 1.25)}
              style={MINI_BTN}
              title="Zoom in"
            >
              +
            </button>
            <button
              type="button"
              onClick={() => zoomViewport(axis, 0.8)}
              style={MINI_BTN}
              title="Zoom out"
            >
              −
            </button>
            <button
              type="button"
              onClick={() => resetViewportZoom(axis)}
              style={MINI_BTN}
              title="Reset zoom + pan"
            >
              ⟲
            </button>
            {/* Pan toggle. Same effect as picking "Pan" in the
                sidebar Tools list, surfaced here so the operator
                doesn't have to leave the viewport to switch
                between crosshair-drag and pan-drag. Active state
                mirrors ``activeTool`` from the parent so the icon
                stays in sync with the sidebar. Falls back to the
                hand emoji for portability across fonts. */}
            {onActiveToolChange && (
              <button
                type="button"
                onClick={() => onActiveToolChange(activeTool === "pan" ? null : "pan")}
                style={{
                  ...MINI_BTN,
                  background: activeTool === "pan" ? "rgba(233,107,31,0.55)" : MINI_BTN.background,
                }}
                title={
                  activeTool === "pan"
                    ? "Pan ON — primary-drag translates. Click to disable."
                    : "Pan: primary-drag translates the image (great when zoomed in)"
                }
              >
                ✋
              </button>
            )}
            <button
              type="button"
              onClick={() => rotateViewport(axis)}
              style={MINI_BTN}
              title="Rotate 90°"
            >
              ↻
            </button>
            <button
              type="button"
              onClick={() => flipViewport(axis, "horizontal")}
              style={MINI_BTN}
              title="Flip horizontal"
            >
              ⇔
            </button>
            <button
              type="button"
              onClick={() => flipViewport(axis, "vertical")}
              style={MINI_BTN}
              title="Flip vertical"
            >
              ⇕
            </button>
            <button
              type="button"
              onClick={() => setMaximizedPane((p) => (p === axis ? null : axis))}
              style={{
                ...MINI_BTN,
                background: maximizedPane === axis ? "rgba(233,107,31,0.55)" : MINI_BTN.background,
              }}
              title={maximizedPane === axis ? "Restore layout" : "Maximize this pane"}
            >
              {maximizedPane === axis ? "⤡" : "⤢"}
            </button>
            <button
              type="button"
              onClick={() => setCameraMenuFor((cur) => (cur === axis ? null : axis))}
              disabled={screenshotBusy === axis}
              style={{
                ...MINI_BTN,
                background: cameraMenuFor === axis ? "rgba(233,107,31,0.55)" : MINI_BTN.background,
                opacity: screenshotBusy === axis ? 0.5 : 1,
              }}
              title="Capture viewport (download / clipboard / save to study)"
            >
              {screenshotBusy === axis ? "⏳" : "📷"}
            </button>
            <button
              type="button"
              onClick={() => setOverlaysOn((o) => !o)}
              style={{
                ...MINI_BTN,
                background: overlaysOn ? "rgba(233,107,31,0.55)" : MINI_BTN.background,
              }}
              title="Toggle text overlays"
            >
              i
            </button>
          </div>
          {/* Camera popover: anchored under the toolbar row, opens on
              click of the 📷 button. Three actions: Download (always
              available), Copy to clipboard (HTTPS / localhost only),
              Save to study (requires ``studyId`` prop). */}
          {cameraMenuFor === axis && (
            <div
              style={{
                position: "absolute",
                top: 32,
                right: 4,
                background: "rgba(15,23,42,0.96)",
                color: "#e6ecf3",
                border: "1px solid rgba(255,255,255,0.15)",
                borderRadius: 4,
                fontSize: "0.72rem",
                padding: "4px 0",
                minWidth: 180,
                zIndex: 5,
                boxShadow: "0 6px 18px rgba(0,0,0,0.45)",
              }}
              onMouseLeave={() => setCameraMenuFor(null)}
            >
              <button
                type="button"
                onClick={() => void downloadScreenshot(axis)}
                style={CAM_MENU_BTN}
              >
                ⬇ Download PNG
              </button>
              <button
                type="button"
                onClick={() => void copyScreenshotToClipboard(axis)}
                style={CAM_MENU_BTN}
              >
                ⎘ Copy to clipboard
              </button>
              <button
                type="button"
                onClick={() => void saveScreenshotToStudy(axis)}
                disabled={!studyId || screenshotBusy === axis}
                style={{
                  ...CAM_MENU_BTN,
                  opacity: studyId ? 1 : 0.4,
                  cursor: studyId ? "pointer" : "not-allowed",
                }}
                title={
                  studyId
                    ? "Attach the captured PNG to the current study as a Document"
                    : "Open from a study to enable this action"
                }
              >
                ☁ Save to study
              </button>
            </div>
          )}
          {/* Transient status banner (Saved / Copied / Error: …)
              rendered top-center so the user gets feedback without
              moving the eye to the toolbar row. Auto-dismisses
              after ~2.5 s via ``flashScreenshotStatus``. */}
          {screenshotStatus?.pane === axis && (
            <div
              style={{
                position: "absolute",
                top: 30,
                left: "50%",
                transform: "translateX(-50%)",
                background: "rgba(15,23,42,0.92)",
                color: "#e6ecf3",
                border: "1px solid rgba(255,255,255,0.15)",
                borderRadius: 3,
                padding: "3px 9px",
                fontSize: "0.7rem",
                pointerEvents: "none",
                zIndex: 4,
              }}
            >
              {screenshotStatus.msg}
            </div>
          )}
          {/* Lens probe overlay — only mounted when the tool is active
              AND we have a hover readout for this axis. The SVG disc
              tracks the cursor; the chip prints stats next to it. The
              ``cursor: crosshair`` is on the underlying viewport <div>
              via the activeTool css class. */}
          {(() => {
            const lr = activeTool === "measure-lens" ? lensReadout[axis] : null;
            if (!lr) return null;
            return (
              <>
                <svg
                  role="presentation"
                  aria-hidden="true"
                  style={{
                    position: "absolute",
                    inset: 0,
                    pointerEvents: "none",
                    zIndex: 5,
                  }}
                >
                  <title>Lens probe overlay</title>
                  <circle
                    cx={lr.canvasCenter.x}
                    cy={lr.canvasCenter.y}
                    r={lr.radiusCanvasPx}
                    fill="none"
                    stroke="rgba(233,107,31,0.9)"
                    strokeWidth={1.5}
                  />
                  <circle
                    cx={lr.canvasCenter.x}
                    cy={lr.canvasCenter.y}
                    r={2}
                    fill="rgba(233,107,31,0.9)"
                  />
                </svg>
                <div
                  style={{
                    position: "absolute",
                    left: Math.min(Math.max(lr.canvasCenter.x + 14, 4), 600),
                    top: Math.max(lr.canvasCenter.y - 24, 4),
                    background: "rgba(0,0,0,0.7)",
                    color: "#fff",
                    fontFamily: "ui-monospace, monospace",
                    fontSize: "0.7rem",
                    padding: "3px 7px",
                    borderRadius: 3,
                    pointerEvents: "none",
                    whiteSpace: "nowrap",
                    zIndex: 6,
                    lineHeight: 1.35,
                    border: "1px solid rgba(233,107,31,0.55)",
                  }}
                >
                  {lr.lines.map((line) => (
                    <div key={line}>{line}</div>
                  ))}
                </div>
              </>
            );
          })()}
          {/* Bottom-right: voxel value HUD (sampled live on mousemove). */}
          {voxelHud[axis] && (
            <div
              style={{
                position: "absolute",
                right: 6,
                bottom: 6,
                background: "rgba(0,0,0,0.55)",
                color: "#fff",
                fontFamily: "ui-monospace, monospace",
                fontSize: "0.7rem",
                padding: "2px 6px",
                borderRadius: 3,
                pointerEvents: "none",
              }}
            >
              {voxelHud[axis]}
            </div>
          )}
        </div>
      );
    };

    // Visibility resolver: when ``maximizedPane`` is set only that
    // pane is shown; otherwise the parent's show* flags decide. Used
    // for both the rendered ``display: none`` mask on always-mounted
    // axis wrappers and to skip mounting the optional panes (3d /
    // mip / oblique) when not needed.
    const isPaneVisible = (key: PaneKey): boolean => {
      if (maximizedPane !== null) return maximizedPane === key;
      switch (key) {
        case "axial":
          return showAxial;
        case "sagittal":
          return showSagittal;
        case "coronal":
          return showCoronal;
        case "3d":
          return show3D;
        case "mip":
          return Boolean(showMip && seriesId);
        case "oblique":
          return showOblique;
      }
    };

    // Render the maximize toggle button placed in the top-right
    // toolbar of the optional panes (3d / mip / oblique). Identical
    // affordance as the one inside ``renderAxisPane``, factored out so
    // the JSX below stays compact.
    const maximizeBtn = (key: PaneKey) => (
      <button
        type="button"
        onClick={() => setMaximizedPane((p) => (p === key ? null : key))}
        style={{
          position: "absolute",
          top: 2,
          right: 4,
          ...MINI_BTN,
          background: maximizedPane === key ? "rgba(233,107,31,0.55)" : MINI_BTN.background,
          zIndex: 4,
        }}
        title={maximizedPane === key ? "Restore layout" : "Maximize this pane"}
      >
        {maximizedPane === key ? "⤡" : "⤢"}
      </button>
    );

    const panels: React.ReactNode[] = [
      // The 3 ortho axes are always rendered so the engine ↔ DOM
      // binding stays stable. Visibility is a CSS concern.
      renderAxisPane("axial", isPaneVisible("axial")),
      renderAxisPane("sagittal", isPaneVisible("sagittal")),
      renderAxisPane("coronal", isPaneVisible("coronal")),
    ];
    if (isPaneVisible("oblique"))
      panels.push(
        <div key="oblique" style={{ position: "relative", overflow: "hidden" }}>
          <ObliqueMPRViewport
            volume={volume}
            windowCenter={wc}
            windowWidth={ww}
            invert={invert}
            onWindowChange={handleWindowChange}
            seriesDescription={seriesDescription}
          />
          {maximizeBtn("oblique")}
        </div>,
      );
    if (isPaneVisible("3d"))
      panels.push(
        <div key="3d" style={{ position: "relative", overflow: "hidden" }}>
          <VolumeViewer
            ref={volumeViewerRef as React.Ref<VolumeViewerHandle>}
            volume={volume}
            modality={modality ?? null}
            customOpacityStops={customOpacityStops}
            customRange={customRange ?? null}
            customColorStops={customColorStops ?? null}
            cropBox={cropBox ?? null}
          />
          {maximizeBtn("3d")}
        </div>,
      );
    if (isPaneVisible("mip") && seriesId)
      panels.push(
        <div key="mip" style={{ position: "relative", overflow: "hidden" }}>
          <CornerstoneMipViewport
            seriesId={seriesId}
            volume={volume}
            modality={modality ?? null}
            fusionSeriesId={fusionSeriesId ?? null}
            fusionVolume={fusionVolume ?? null}
            fusionModality={fusionModality ?? null}
            fusionExpected={fusionExpected ?? false}
            fusionFailed={fusionFailed ?? false}
            suvFactorBw={suvFactorBw ?? null}
          />
          {maximizeBtn("mip")}
        </div>,
      );

    // Visible-pane count drives the auto grid (only when no explicit
    // ``layout`` prop). Hidden axis wrappers (display:none) don't
    // occupy a track in CSS Grid so they don't need to be subtracted
    // again, but we count them here for the auto fallback so a
    // 1-axis selection lands on a 1×1 grid not 2×1.
    const count =
      (isPaneVisible("axial") ? 1 : 0) +
      (isPaneVisible("sagittal") ? 1 : 0) +
      (isPaneVisible("coronal") ? 1 : 0) +
      (isPaneVisible("3d") ? 1 : 0) +
      (isPaneVisible("mip") && seriesId ? 1 : 0) +
      (isPaneVisible("oblique") ? 1 : 0);
    if (count === 0) {
      return (
        <div
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            color: "#666",
            background: "#0a0d14",
            width: "100%",
            height: "100%",
          }}
        >
          Select a viewport to begin
        </div>
      );
    }
    // Grid sizing. ``maximizedPane`` always wins (1×1). Otherwise an
    // explicit ``layout`` prop drives the dims, falling back to a
    // count-based auto-flow when no protocol is selected. Crucially:
    // we now derive cols/rows from the *visible* count, not from
    // ``panels.length`` — the panels array always contains the 3
    // ortho axes (some hidden), so using its length would force a
    // 2×2 grid even when the user wants a 1×1.
    const [cols, rows]: [number, number] =
      maximizedPane !== null
        ? [1, 1]
        : layout
          ? LAYOUT_DIMS[layout]
          : [count <= 1 ? 1 : 2, count <= 2 ? 1 : 2];

    return (
      <div
        style={{
          display: "grid",
          gridTemplateColumns: `repeat(${cols}, 1fr)`,
          gridTemplateRows: `repeat(${rows}, 1fr)`,
          gap: 4,
          width: "100%",
          height: "100%",
          background: "#000",
        }}
      >
        {panels}
      </div>
    );
  },
);

export default CornerstoneMPRLayout;

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------

function applyVoiToAll(
  engine: cs.RenderingEngine,
  vpIds: string[],
  wc: number,
  ww: number,
  invert: boolean,
  targetVolumeId?: string,
) {
  for (const id of vpIds) {
    const vp = engine.getViewport(id) as cs.Types.IVolumeViewport | undefined;
    if (!vp) continue;
    // ``setProperties(props, volumeId)`` scopes the VOI / invert to
    // a specific volume actor; without the second arg Cornerstone
    // applies it to the first actor (the primary). Passing the
    // fusion volumeId lets us window PET independently from CT.
    vp.setProperties(
      {
        voiRange: { lower: wc - ww / 2, upper: wc + ww / 2 },
        invert,
      },
      targetVolumeId,
    );
    vp.render();
  }
}

/**
 * Re-fit the given panes' cameras to their CURRENT (settled) canvas size.
 *
 * ``setVolumesForViewports`` fits the camera once, at whatever size the
 * canvas has when the async mount resolves — which is often before the
 * pane has its final layout box. CS3D's ``engine.resize(keepCamera=true)``
 * then restores that stale camera on the next reflow (``resetCameraForResize``
 * followed by ``setCamera(prevCamera)``, since our viewports set no
 * ``displayArea``), so the bad framing sticks. We first sync the canvas
 * backing store to the live CSS box (``engine.resize``) and then issue an
 * authoritative ``resetCamera`` on each pane that actually has a non-zero
 * box — skipping hidden (``display:none``) or not-yet-laid-out panes so a
 * collapsed pane can't park the camera on empty bounds.
 *
 * Returns ``true`` once at least one pane was fitted.
 */
function fitViewportsToCanvas(
  engine: cs.RenderingEngine,
  panes: Array<{ id: string; el: HTMLElement | null }>,
): boolean {
  try {
    // Sync canvas backing-store dimensions to the current CSS box.
    // ``keepCamera`` is irrelevant here — we override with an explicit
    // ``resetCamera`` immediately after.
    engine.resize(true);
  } catch {
    return false;
  }
  let fitted = false;
  for (const { id, el } of panes) {
    if (!el || el.clientWidth === 0 || el.clientHeight === 0) continue;
    const vp = engine.getViewport(id) as cs.Types.IVolumeViewport | undefined;
    if (!vp) continue;
    try {
      vp.resetCamera();
      fitted = true;
    } catch {
      /* viewport torn down between frames */
    }
  }
  // Shared engine: render only THIS pane's viewports, not every pane's.
  if (fitted) engine.renderViewports(panes.map((p) => p.id));
  return fitted;
}

function flipAll(
  engine: cs.RenderingEngine | null,
  vpIds: string[],
  axis: "horizontal" | "vertical",
) {
  if (!engine) return;
  for (const id of vpIds) {
    const vp = engine.getViewport(id) as
      | (cs.Types.IVolumeViewport & {
          flip: (dir: { flipHorizontal?: boolean; flipVertical?: boolean }) => void;
        })
      | undefined;
    if (!vp) continue;
    if (axis === "horizontal") {
      vp.flip({ flipHorizontal: true });
    } else {
      vp.flip({ flipVertical: true });
    }
    vp.render();
  }
}

const MINI_BTN: React.CSSProperties = {
  background: "rgba(0,0,0,0.55)",
  color: "#fff",
  border: "1px solid rgba(255,255,255,0.25)",
  borderRadius: 3,
  fontSize: "0.78rem",
  width: 22,
  height: 22,
  padding: 0,
  cursor: "pointer",
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
};

const CAM_MENU_BTN: React.CSSProperties = {
  display: "block",
  width: "100%",
  textAlign: "left",
  background: "transparent",
  color: "inherit",
  border: 0,
  padding: "6px 12px",
  fontSize: "0.72rem",
  fontFamily: "inherit",
  cursor: "pointer",
};

/** PT auto-WL: 50-99.5 percentile of nonzero voxels. */
function ptAutoWL(scalars: Float32Array): { wc: number; ww: number } {
  return percentileWL(scalars, 0.5, 0.995);
}

/** Generic auto-WL: robust 1-99 percentile clipping over nonzero
 *  voxels. Matches ``computeAutoWL`` in ``lib/windowing.ts`` for
 *  non-PT modalities; gives MR / NM / OT a sane starting window
 *  without modality-specific tuning. */
function robustAutoWL(scalars: Float32Array): { wc: number; ww: number } {
  return percentileWL(scalars, 0.01, 0.99);
}

function percentileWL(scalars: Float32Array, loP: number, hiP: number): { wc: number; ww: number } {
  const N = scalars.length;
  if (N === 0) return { wc: 0, ww: 1 };
  const SAMPLE = Math.min(50000, N);
  const stride = Math.max(1, Math.floor(N / SAMPLE));
  const samples: number[] = [];
  for (let i = 0; i < N; i += stride) {
    const v = scalars[i];
    if (Number.isFinite(v) && v !== 0) samples.push(v);
  }
  if (samples.length < 100) return { wc: 0, ww: 1 };
  samples.sort((a, b) => a - b);
  const lo = samples[Math.floor(samples.length * loP)];
  const hi = samples[Math.floor(samples.length * hiP)];
  const ww = Math.max(1, hi - lo);
  return { wc: (lo + hi) / 2, ww };
}
