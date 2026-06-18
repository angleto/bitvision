// Type contract shared between ``CornerstoneMPRLayout`` and the
// surrounding viewer page. Lives in its own file so the legacy
// hand-rolled MPRLayout component can be removed without taking the
// type surface with it (the multi-series viewer, the imperative
// handle in the page, and the Cornerstone layout itself all import
// from here).

import type { LayoutId } from "@/lib/hangingProtocols";

import type { VolumeData, VolumeViewerHandle } from "./VolumeViewer";

export interface MPRLayoutProps {
  volume: VolumeData;
  showAxial?: boolean;
  showSagittal?: boolean;
  showCoronal?: boolean;
  show3D?: boolean;
  showMip?: boolean;
  layout?: LayoutId;
  showOblique?: boolean;
  onCrosshairChange?: (pos: [number, number, number]) => void;
  activeTool?: string;
  /** Optional callback the layout invokes when an in-viewport
   *  affordance (the pan-toggle hand icon, eventually other quick
   *  toggles) needs to flip the parent's ``activeTool`` state.
   *  Without this the in-overlay button would have nowhere to push
   *  its state change. */
  onActiveToolChange?: (tool: string | null) => void;
  seriesDescription?: string;
  seriesId?: string;
  volumeViewerRef?: React.RefObject<VolumeViewerHandle | null>;
  measurements?: Array<{
    id: number;
    tool: string;
    value: string;
    label?: string;
    points: Array<{ x: number; y: number }>;
    sliceIndex?: number;
    markerId?: string;
  }>;
  onMeasurementsChange?: (
    m: Array<{
      id: number;
      tool: string;
      value: string;
      label?: string;
      points: Array<{ x: number; y: number }>;
      sliceIndex?: number;
      markerId?: string;
      /** Cornerstone3D tool name (e.g. ``CircleROITool``). The page
       *  uses this to pick the most recent sphere ROI as the PERCIST
       *  liver reference — the legacy ``tool`` string is too coarse
       *  (e.g. ``"sphere"`` is shared with any future sphere tools). */
      csToolName?: string;
      /** Server-side SUV statistics from ``/roi-stats`` (PERCIST 1.0
       *  bw factor). Populated only on PT series with a computable
       *  SUV factor and only for circle/ellipse/rect ROIs that have
       *  been completed at least once. */
      suv?: {
        mean?: number;
        sd?: number;
        max?: number;
        peak?: number;
      };
      /** Patient/world (LPS) coordinates of the annotation handles,
       *  carried alongside the voxel ``points`` so a marker survives a
       *  re-pack and is comparable across series/modalities. */
      worldPoints?: Array<[number, number, number]>;
      /** FrameOfReferenceUID of the volume the annotation was drawn on —
       *  the spatial anchor that makes ``worldPoints`` meaningful across
       *  series. */
      frameOfReferenceUID?: string;
    }>,
  ) => void;
  markerFade?: { enabled: boolean; range: number; opacity: number };
  modality?: string | null;
  /** DICOM modality of the fusion overlay. Drives the MIP viewport's
   *  active-volume picker: when the fusion is PT and the primary
   *  isn't, the MIP renders the fusion (PET-CT review convention). */
  fusionModality?: string | null;
  customOpacityStops?: Array<{ t: number; alpha: number }> | null;
  /** HU / scalar range override piped to ``VolumeViewer``; ``null``
   *  falls back to the preset's ``displayRange``. Driven by the
   *  3D W/L sliders. */
  customRange?: [number, number] | null;
  /** Custom color transfer-function stops piped to ``VolumeViewer``.
   *  ``null`` keeps the preset's ramp; otherwise the array of
   *  {pos, [r,g,b]} replaces it. */
  customColorStops?: Array<{ pos: number; color: [number, number, number] }> | null;
  /** Axis-aligned crop box in normalised volume space. ``null`` =
   *  full volume; otherwise clip planes are applied to the 3D
   *  viewport's mapper. */
  cropBox?: { lower: [number, number, number]; upper: [number, number, number] } | null;
  /** SUV threshold filter (spec §3.4). When set, the layout zeroes
   *  the PT volume's opacity transfer function outside [suvLo, suvHi]
   *  so voxels with SUV below ``suvLo`` (background, sub-PERCIST) and
   *  above ``suvHi`` (physiological hot spots: kidneys, bladder) are
   *  rendered transparent. ``hi: null`` means "no upper bound" (clamp
   *  to volume max). The layout converts SUV → scalar using
   *  ``suvFactorBw`` (which already carries the active variant), so a
   *  factor must be available; the page hides the control otherwise.
   *  ``null`` = filter off (default); the layout restores the per-mode
   *  default OFUN (smoothstep on fusion overlay, near-opaque on
   *  primary PT). */
  petSuvHide?: { lo: number; hi: number | null } | null;
}

export interface MPRLayoutHandle {
  crosshair: [number, number, number];
  setCrosshair: (pos: [number, number, number]) => void;
  dims: [number, number, number];
  wc: number;
  ww: number;
  invert: boolean;
  setWC: (v: number) => void;
  setWW: (v: number) => void;
  setInvert: (v: boolean) => void;
  resetWL: () => void;
  rotateAll: () => void;
  flipHAll: () => void;
  flipVAll: () => void;
  fusionWC?: number;
  fusionWW?: number;
  fusionAlpha?: number;
  setFusionWC?: (v: number) => void;
  setFusionWW?: (v: number) => void;
  setFusionAlpha?: (v: number) => void;
  /** Toggle the fusion overlay on/off. ``true`` (default) renders
   *  the PET overlay over the CT base with the soft-shoulder
   *  opacity ramp; ``false`` zeroes the OTF so only the CT shows.
   *  Standard PET-CT review workflow: flip PET off to confirm the
   *  underlying anatomical finding, flip back on to gauge the
   *  metabolic contribution. */
  fusionVisible?: boolean;
  setFusionVisible?: (v: boolean) => void;
  /** Re-centre the crosshair on a voxel originally addressed in the
   *  IJK frame of ``sourceSeriesId``. The layout looks up the
   *  cached volume for that series, converts IJK → world via its
   *  imageData, then world → IJK on the primary volume so the
   *  crosshair lands on the same anatomical point even when the
   *  primary and the source volume have different geometries
   *  (typical PT-CT fusion: different spacing + different bounds).
   *  Returns ``true`` when the conversion succeeded. */
  setCrosshairFromSourceSeries?: (
    sourceSeriesId: string,
    voxel: [number, number, number],
  ) => boolean;
  /** Current crosshair position as a world-space (LPS) point, or null if
   *  the volume is not cached. Used to synchronise two studies whose frames
   *  differ: read the world point here, map it through a registration
   *  transform, then ``setCrosshairWorld`` on the other pane. */
  getCrosshairWorld?: (ijk?: [number, number, number]) => [number, number, number] | null;
  /** Move the crosshair to a world-space (LPS) point (converted to the
   *  primary volume's IJK). Returns ``true`` on success. */
  setCrosshairWorld?: (world: [number, number, number]) => boolean;
  /** Re-apply the percentile-based auto-WL to the fusion volume. The
   *  layout runs this on mount; exposing it here lets the SUV "Auto"
   *  button restore the fusion's window without forcing the user to
   *  go to the dedicated fusion W/L preset bar. */
  resetFusionWL?: () => void;
  /** Remove a single Cornerstone annotation by its UID and re-render.
   *  The viewer page uses this when the user deletes a measurement
   *  from the side panel — without it, the marker disappears from the
   *  list but the SVG overlay stays glued to the image. */
  removeAnnotation: (annotationUID: string) => void;
  /** Drop every Cornerstone annotation across all FrameOfReferenceUIDs.
   *  Wired to the toolbar's "Clear all" button so React state and
   *  Cornerstone state stay in lockstep. */
  clearAnnotations: () => void;
  /** Set ``data.label`` on a single Cornerstone annotation and trigger
   *  a re-render. Wired to the side-panel's inline label edit so the
   *  on-canvas overlay reflects the new text immediately, without
   *  forcing the user to redraw the measurement. */
  updateAnnotationLabel: (annotationUID: string, label: string) => void;
  /** Current Lens probe radius in mm. Used by the right-rail panel to
   *  display the value, kept in sync with Shift+Wheel updates on the
   *  canvas. ``null`` when the lens has never been activated. */
  lensRadiusMm?: number | null;
  /** Programmatically set the Lens probe radius (in mm). Used by the
   *  right-rail panel's numeric input. The layout clamps the value to
   *  the [1, 50] mm range that matches the wheel-step UX. */
  setLensRadiusMm?: (v: number) => void;
  /** Fire the same "pin" event a click on the lens canvas would emit.
   *  Wired to the rail panel's "Pin" button so the operator can persist
   *  the current readout without leaving the keyboard. ``false`` return
   *  means the lens has no current hover position to pin. */
  pinCurrentLens?: () => boolean;
}
