"use client";

import { forwardRef, useEffect, useImperativeHandle, useMemo, useRef, useState } from "react";

import "@kitware/vtk.js/Rendering/Profiles/Volume";

import vtkDataArray from "@kitware/vtk.js/Common/Core/DataArray";
import vtkImageData from "@kitware/vtk.js/Common/DataModel/ImageData";
import vtkPiecewiseFunction from "@kitware/vtk.js/Common/DataModel/PiecewiseFunction";
import vtkPlane from "@kitware/vtk.js/Common/DataModel/Plane";
import vtkColorTransferFunction from "@kitware/vtk.js/Rendering/Core/ColorTransferFunction";
import vtkVolume from "@kitware/vtk.js/Rendering/Core/Volume";
import vtkVolumeMapper from "@kitware/vtk.js/Rendering/Core/VolumeMapper";
import vtkGenericRenderWindow from "@kitware/vtk.js/Rendering/Misc/GenericRenderWindow";

export interface VolumeData {
  dimensions: [number, number, number];
  spacing: [number, number, number];
  scalars: Float32Array;
  range: [number, number];
  /** Real DICOM patient-space geometry from the volume.raw X-Volume-*
   *  headers. ``origin`` = LPS position of voxel (0,0,0); ``direction``
   *  = 9-float [rowCos, colCos, sliceCos] (Cornerstone3D order);
   *  ``frameOfReferenceUid`` = DICOM FoR. Optional: legacy packs omit
   *  them and the viewer builds an identity frame. */
  origin?: [number, number, number];
  direction?: [number, number, number, number, number, number, number, number, number];
  frameOfReferenceUid?: string;
  /** Which resolution this payload carries. ``preview`` = the transient
   *  1/8 low-res first paint (no geometry); ``full`` = the authoritative
   *  diagnostic volume. The MPR layout folds this into the Cornerstone
   *  ``volumeId`` so preview→full is a clean cache-entry swap. Absent =
   *  ``full`` (legacy callers). */
  resolution?: "preview" | "full";
}

export type ColorPreset =
  | "bone"
  | "grayscale"
  | "cool-warm"
  | "hot"
  | "ice"
  | "spectral"
  | "jet"
  | "viridis"
  | "magma"
  | "ct-soft-tissue"
  | "ct-lung"
  | "ct-bone"
  | "ct-angio"
  | "ct-liver"
  | "ct-liver-vessels"
  | "ct-kidney"
  | "ct-pancreas"
  | "ct-spleen"
  | "ct-cardiac"
  | "mri-t1"
  | "mri-t2"
  | "mri-edge"
  | "pet-suv-hot"
  | "pet-suv-rainbow"
  | "skin"
  | "tissue"
  | "muscle";
export type BlendMode = "composite" | "mip" | "minip" | "average";

export interface SegMask {
  data: Uint8Array;
  color: [number, number, number];
}

export type FusionColormap = "hot" | "pet" | "jet" | "green" | "magenta" | "cyan";

export interface FusionVolume {
  dimensions: [number, number, number];
  spacing: [number, number, number];
  scalars: Float32Array;
  range: [number, number];
  colormap: FusionColormap;
  opacity: number; // 0..1, blend strength
}

export interface VolumeViewerHandle {
  setColorPreset: (p: ColorPreset) => void;
  setBlendMode: (m: BlendMode) => void;
  setOpacityShift: (v: number) => void;
  setSampleDistance: (v: number) => void;
  setShade: (v: boolean) => void;
  setCinematic: (v: boolean) => void;
  /** Shift the OTF's low-end cutoff. Range [-0.5, 0.5]; 0 = preset
   *  default, +0.3 = render only the top 35% of the displayRange,
   *  -0.2 = include more of the histogram. Updates in real-time. */
  setThreshold: (v: number) => void;
  setSegmentationMask: (mask: SegMask | null) => void;
  setCameraPreset: (preset: string) => void;
  setBackground: (r: number, g: number, b: number) => void;
  setCustomColorMap: (stops: Array<{ pos: number; color: [number, number, number] }>) => void;
  setFusionVolume: (fusion: FusionVolume | null) => void;
  setFusionOpacity: (v: number) => void;
  setFusionColormap: (c: FusionColormap) => void;
  /** Uptake-isolation threshold for the fusion overlay, in [0, 0.95]
   *  as a fraction of the displayed SUV/percentile range: alpha=0
   *  below it. Higher = only the hottest focal lesions; lower = more
   *  diffuse uptake. 0.65 = previous fixed default. */
  setFusionThreshold: (v: number) => void;
  resetCamera: () => void;
  colorPreset: ColorPreset;
  blendMode: BlendMode;
  opacityShift: number;
  sampleDistance: number;
  shade: boolean;
  cinematic: boolean;
  threshold: number;
}

interface Props {
  volume: VolumeData;
  className?: string;
  /**
   * DICOM modality of the source series. When provided, presets and
   * the robust-range computation can apply modality-specific defaults
   * (HU thresholds for CT, SUV-aware ranges for PT, percentile-based
   * for everything else). Optional — falls back to percentile range.
   */
  modality?: string | null;
  /**
   * Optional list of explicit opacity stops emitted by the custom
   * Transfer Function editor. When set, overrides the preset's
   * opacity curve; the color TF still comes from the active preset.
   */
  customOpacityStops?: Array<{ t: number; alpha: number }> | null;
  /**
   * Optional [lo, hi] HU / scalar override that replaces the
   * preset-resolved ``displayRange``. Lets the user dial in a tight
   * organ-specific window (e.g. 60–180 HU for portal-venous liver
   * vessels) without picking a different preset. ``null`` /
   * undefined falls back to the preset default.
   */
  customRange?: [number, number] | null;
  /**
   * Optional list of color stops emitted by the custom Color
   * Transfer Function editor. Each ``pos`` is in [0, 1] and gets
   * remapped onto the active ``displayRange``. When set, overrides
   * the preset's ``color`` ramp; the opacity TF still comes from
   * the preset (or ``customOpacityStops`` if present).
   */
  customColorStops?: Array<{ pos: number; color: [number, number, number] }> | null;
  /**
   * Optional axis-aligned crop box in scalar voxel space (i, j, k
   * triples for the lower / upper corners). Each component is
   * normalised to [0, 1] of the full volume bounds. When set, the
   * volume actor's mapper is given six clipping planes that cut
   * off everything outside the box. ``null`` / undefined renders
   * the full volume. Useful for exposing internal anatomy by
   * cropping out skin / bone / surrounding fat.
   */
  cropBox?: { lower: [number, number, number]; upper: [number, number, number] } | null;
  /**
   * Render mode. ``"volume"`` (default) does composite volume rendering
   * with shading/cinematic + transfer functions; ``"mip"`` configures
   * the volume mapper for Maximum Intensity Projection (per-ray max,
   * no shading, simple linear grayscale-ish ramp). Used by the
   * dedicated MIP viewport in the hanging-protocol grid.
   */
  mode?: "volume" | "mip";
}

/**
 * Per-preset definition used by the new transfer-function pipeline.
 *
 * - ``color``: list of ``[t, r, g, b]`` stops with ``t`` in [0,1]
 *   relative to the *display range* (lo..hi).
 * - ``opacity``: list of ``[t, alpha]`` stops with ``t`` in [0,1].
 *   When omitted, a sensible default ramp is applied.
 * - ``rangeMode``: how to derive [lo, hi] from the volume.
 *     * ``"percentile"``: 1st-99th percentile of non-zero voxels
 *       (robust default — kills outlier-driven contrast crush).
 *     * ``"hu"``: clamp to the HU window (e.g. [-1024, 3071]) and
 *       let the explicit ``rangeHint`` carve out a meaningful slab
 *       (lung/bone/soft).
 *     * ``"suv"``: percentile-based but heavily skewed to the bright
 *       tail — PET background is mostly noise, the diagnostic value
 *       is in the upper percentile.
 *     * ``"full"``: raw min..max (legacy behaviour for diagnostic
 *       inspection).
 * - ``rangeHint``: when ``rangeMode === "hu"`` or ``"suv"``, the
 *   absolute window the preset wants to highlight.
 */
interface PresetDef {
  color: [number, number, number, number][];
  opacity: [number, number][];
  rangeMode: "percentile" | "hu" | "suv" | "full";
  rangeHint?: [number, number];
}

// Default opacity ramp shared by general-purpose presets. Suppresses the
// background entirely (alpha=0 below 20% of the display range) so the
// "filled cylinder" artefact disappears: voxels in the noise floor are
// truly transparent, not "ramping from 0.0 to 0.15" as the old generic
// curve produced. The aggressive low-end cut-off is the single biggest
// quality fix in this refactor.
const DEFAULT_OPACITY: [number, number][] = [
  [0.0, 0.0],
  [0.2, 0.0],
  [0.5, 0.25],
  [0.8, 0.7],
  [1.0, 0.95],
];

const PRESET_DEFS: Record<ColorPreset, PresetDef> = {
  bone: {
    color: [
      [0, 0, 0, 0],
      [0.25, 0.55, 0.25, 0.15],
      [0.5, 0.88, 0.6, 0.3],
      [0.75, 1, 0.9, 0.7],
      [1, 1, 1, 0.95],
    ],
    opacity: DEFAULT_OPACITY,
    rangeMode: "percentile",
  },
  grayscale: {
    color: [
      [0, 0, 0, 0],
      [1, 1, 1, 1],
    ],
    opacity: DEFAULT_OPACITY,
    rangeMode: "percentile",
  },
  "cool-warm": {
    color: [
      [0, 0.2, 0.4, 0.8],
      [0.5, 0.9, 0.9, 0.9],
      [1, 0.8, 0.2, 0.1],
    ],
    opacity: DEFAULT_OPACITY,
    rangeMode: "percentile",
  },
  hot: {
    color: [
      [0, 0, 0, 0],
      [0.33, 0.7, 0, 0],
      [0.66, 1, 0.7, 0],
      [1, 1, 1, 0.5],
    ],
    opacity: DEFAULT_OPACITY,
    rangeMode: "percentile",
  },
  ice: {
    color: [
      [0, 0, 0, 0.1],
      [0.5, 0.2, 0.5, 0.9],
      [1, 0.9, 0.95, 1],
    ],
    opacity: DEFAULT_OPACITY,
    rangeMode: "percentile",
  },
  spectral: {
    color: [
      [0, 0.2, 0, 0.3],
      [0.25, 0, 0.4, 0.8],
      [0.5, 0, 0.8, 0.2],
      [0.75, 0.9, 0.9, 0],
      [1, 0.9, 0.1, 0],
    ],
    opacity: DEFAULT_OPACITY,
    rangeMode: "percentile",
  },
  jet: {
    color: [
      [0, 0, 0, 0.5],
      [0.25, 0, 0.5, 1],
      [0.5, 0, 1, 0],
      [0.75, 1, 1, 0],
      [1, 1, 0, 0],
    ],
    opacity: DEFAULT_OPACITY,
    rangeMode: "percentile",
  },
  viridis: {
    color: [
      [0, 0.27, 0, 0.33],
      [0.25, 0.28, 0.36, 0.55],
      [0.5, 0.13, 0.57, 0.55],
      [0.75, 0.46, 0.79, 0.25],
      [1, 0.99, 0.91, 0.14],
    ],
    opacity: DEFAULT_OPACITY,
    rangeMode: "percentile",
  },
  magma: {
    color: [
      [0, 0, 0, 0.02],
      [0.25, 0.27, 0.1, 0.47],
      [0.5, 0.72, 0.15, 0.43],
      [0.75, 0.99, 0.49, 0.37],
      [1, 0.99, 0.98, 0.74],
    ],
    opacity: DEFAULT_OPACITY,
    rangeMode: "percentile",
  },
  "ct-soft-tissue": {
    color: [
      [0, 0, 0, 0],
      [0.15, 0.4, 0.2, 0.15],
      [0.4, 0.85, 0.55, 0.4],
      [0.6, 1, 0.85, 0.7],
      [1, 1, 1, 0.9],
    ],
    // -100 → 300 HU: skin / fat / soft organ window. Air and bone are
    // suppressed; soft tissue ramps in the middle.
    opacity: [
      [0.0, 0.0],
      [0.15, 0.0],
      [0.4, 0.4],
      [0.7, 0.85],
      [1.0, 1.0],
    ],
    rangeMode: "hu",
    rangeHint: [-100, 300],
  },
  "ct-lung": {
    color: [
      [0, 0, 0, 0],
      [0.1, 0, 0.05, 0.15],
      [0.3, 0.1, 0.3, 0.5],
      [0.7, 0.6, 0.7, 0.8],
      [1, 1, 1, 1],
    ],
    // -1000 → -300 HU: airspace + lung parenchyma. Anything denser
    // than -300 (vessels / soft tissue) goes opaque to give context.
    opacity: [
      [0.0, 0.0],
      [0.05, 0.0],
      [0.4, 0.05],
      [0.85, 0.3],
      [1.0, 0.6],
    ],
    rangeMode: "hu",
    rangeHint: [-1000, -300],
  },
  "ct-bone": {
    color: [
      [0, 0, 0, 0],
      [0.3, 0.2, 0.1, 0.05],
      [0.6, 0.7, 0.6, 0.5],
      [0.85, 0.95, 0.9, 0.85],
      [1, 1, 1, 1],
    ],
    // 200 → 1500 HU: cortical bone window. Below 200 HU stays
    // completely transparent so soft tissue doesn't fog the view.
    opacity: [
      [0.0, 0.0],
      [0.05, 0.0],
      [0.3, 0.55],
      [0.7, 0.85],
      [1.0, 1.0],
    ],
    rangeMode: "hu",
    rangeHint: [200, 1500],
  },
  "ct-angio": {
    color: [
      [0, 0, 0, 0],
      [0.2, 0.4, 0.05, 0.05],
      [0.5, 0.95, 0.4, 0.3],
      [0.8, 1, 0.85, 0.5],
      [1, 1, 1, 0.95],
    ],
    // 100 → 600 HU: contrast-filled vessels + soft surrounds. Tuned
    // so iodine shows as bright orange, vessel walls dimmer.
    opacity: [
      [0.0, 0.0],
      [0.1, 0.0],
      [0.4, 0.5],
      [0.75, 0.85],
      [1.0, 1.0],
    ],
    rangeMode: "hu",
    rangeHint: [100, 600],
  },
  // ----------------------------------------------------------------
  // Organ-specific 3D presets. The general pattern: a narrow HU
  // ``rangeHint`` that brackets the target tissue, an opacity ramp
  // with a hard cutoff at the low end (so air / fat / surrounding
  // organs don't fog the view via accumulated low-alpha rays), and
  // a tissue-coloured RGB ramp. They land best on contrast-enhanced
  // CT — non-contrast scans will still render but with less
  // separation between parenchyma and vessels.
  "ct-liver": {
    // Liver parenchyma in portal-venous CT sits around 100–110 HU,
    // hepatic / portal vessels reach 140–180 HU. Range 30 → 200 HU
    // captures both while suppressing fat (≤-50) and bone (≥250).
    // RGB: warm brown for parenchyma, brighter orange for vessels.
    color: [
      [0, 0, 0, 0],
      [0.15, 0.3, 0.15, 0.08],
      [0.4, 0.7, 0.4, 0.3],
      [0.65, 0.92, 0.6, 0.4],
      [0.85, 1, 0.82, 0.55],
      [1, 1, 0.95, 0.78],
    ],
    opacity: [
      [0.0, 0.0],
      [0.1, 0.0],
      [0.35, 0.45],
      [0.65, 0.75],
      [1.0, 0.95],
    ],
    rangeMode: "hu",
    rangeHint: [30, 200],
  },
  "ct-liver-vessels": {
    // Vessel-emphasising variant: 60 → 300 HU bracket centred on the
    // contrast-loaded portal / hepatic veins. Parenchyma at the low
    // end stays semi-transparent so the vascular tree pops without
    // losing context. Useful for portal-venous reformats and HCC
    // staging where vessel relationships matter more than parenchyma.
    color: [
      [0, 0, 0, 0],
      [0.15, 0.45, 0.18, 0.1],
      [0.4, 0.92, 0.45, 0.2],
      [0.7, 1, 0.75, 0.4],
      [1, 1, 0.95, 0.7],
    ],
    opacity: [
      [0.0, 0.0],
      [0.1, 0.0],
      [0.25, 0.15],
      [0.55, 0.7],
      [0.85, 0.9],
      [1.0, 1.0],
    ],
    rangeMode: "hu",
    rangeHint: [60, 300],
  },
  "ct-kidney": {
    // Renal cortex 100–140 HU in CMP / nephrographic, medulla
    // 60–80 HU. Range 20 → 250 HU spans non-contrast parenchyma
    // through excretory-phase pelvic contrast (>200 HU) without
    // letting bone bleed in. Tan / amber tissue colours.
    color: [
      [0, 0, 0, 0],
      [0.15, 0.35, 0.2, 0.1],
      [0.4, 0.78, 0.55, 0.32],
      [0.7, 0.95, 0.8, 0.5],
      [1, 1, 0.95, 0.75],
    ],
    opacity: [
      [0.0, 0.0],
      [0.08, 0.0],
      [0.3, 0.4],
      [0.65, 0.8],
      [1.0, 1.0],
    ],
    rangeMode: "hu",
    rangeHint: [20, 250],
  },
  "ct-pancreas": {
    // Pancreas parenchyma 30–50 HU baseline, 80–120 HU during the
    // pancreatic phase. Narrow range 20 → 150 HU keeps the focus on
    // the gland and surrounding fat planes; vessels (splenic /
    // portal confluence) are clipped at the high end so they don't
    // dominate. Pinkish tissue tone.
    color: [
      [0, 0, 0, 0],
      [0.2, 0.4, 0.2, 0.18],
      [0.5, 0.85, 0.55, 0.5],
      [0.8, 0.98, 0.78, 0.7],
      [1, 1, 0.95, 0.88],
    ],
    opacity: [
      [0.0, 0.0],
      [0.12, 0.0],
      [0.4, 0.55],
      [0.75, 0.85],
      [1.0, 1.0],
    ],
    rangeMode: "hu",
    rangeHint: [20, 150],
  },
  "ct-spleen": {
    // Splenic parenchyma is heterogeneous early-arterial (zebra
    // pattern) but homogeneous in portal-venous (~110 HU). Range
    // 30 → 200 HU mirrors the liver bracket; reddish RGB.
    color: [
      [0, 0, 0, 0],
      [0.15, 0.35, 0.1, 0.08],
      [0.45, 0.78, 0.28, 0.22],
      [0.75, 0.95, 0.55, 0.4],
      [1, 1, 0.85, 0.7],
    ],
    opacity: [
      [0.0, 0.0],
      [0.1, 0.0],
      [0.35, 0.5],
      [0.7, 0.8],
      [1.0, 0.95],
    ],
    rangeMode: "hu",
    rangeHint: [30, 200],
  },
  "ct-cardiac": {
    // Myocardium ~40–60 HU, blood-pool with contrast 250–400 HU,
    // calcifications >500 HU. Range 30 → 400 HU keeps the heart
    // and great vessels in view; bone (sternum, ribs, spine) sits
    // above the cap so it doesn't render. Red / muscle ramp.
    color: [
      [0, 0, 0, 0],
      [0.15, 0.4, 0.1, 0.1],
      [0.4, 0.85, 0.3, 0.25],
      [0.65, 1, 0.55, 0.3],
      [0.85, 1, 0.85, 0.5],
      [1, 1, 1, 0.85],
    ],
    opacity: [
      [0.0, 0.0],
      [0.08, 0.0],
      [0.3, 0.45],
      [0.6, 0.8],
      [0.9, 0.95],
      [1.0, 1.0],
    ],
    rangeMode: "hu",
    rangeHint: [30, 400],
  },
  "mri-t1": {
    color: [
      [0, 0, 0, 0],
      [0.3, 0.2, 0.15, 0.1],
      [0.5, 0.55, 0.35, 0.25],
      [0.7, 0.8, 0.65, 0.5],
      [1, 1, 0.95, 0.85],
    ],
    opacity: DEFAULT_OPACITY,
    rangeMode: "percentile",
  },
  "mri-t2": {
    color: [
      [0, 0, 0, 0.05],
      [0.2, 0.05, 0.15, 0.35],
      [0.5, 0.2, 0.45, 0.7],
      [0.8, 0.6, 0.8, 0.95],
      [1, 1, 1, 1],
    ],
    opacity: DEFAULT_OPACITY,
    rangeMode: "percentile",
  },
  "mri-edge": {
    // High-contrast grayscale tuned to surface-render MRI volumes.
    // The opacity ramp is steep at the high end so cortical surfaces
    // pop while parenchyma fades.
    color: [
      [0, 0.05, 0.05, 0.1],
      [0.4, 0.3, 0.32, 0.4],
      [0.7, 0.7, 0.72, 0.8],
      [1, 1, 1, 1],
    ],
    opacity: [
      [0.0, 0.0],
      [0.3, 0.0],
      [0.6, 0.2],
      [0.85, 0.7],
      [1.0, 0.95],
    ],
    rangeMode: "percentile",
  },
  "pet-suv-hot": {
    // Black → red → orange → yellow → white. Standard "PET hot" map.
    // The opacity uses a near-step cutoff at t=0.65: below that the
    // alpha is exactly 0 (zero accumulation along ray), above it the
    // alpha jumps fast. This is the only way to suppress the
    // "patient silhouette cylinder": with a smooth low-alpha ramp,
    // even 0.05 alpha across 200 voxels of normal tissue along a ray
    // accumulates to opaque. We need a hard cutoff.
    color: [
      [0, 0, 0, 0],
      [0.5, 0.4, 0, 0],
      [0.7, 0.95, 0.35, 0],
      [0.85, 1, 0.85, 0.1],
      [1, 1, 1, 0.95],
    ],
    opacity: [
      [0.0, 0.0],
      [0.65, 0.0],
      [0.66, 0.4],
      [0.85, 0.85],
      [1.0, 1.0],
    ],
    rangeMode: "suv",
  },
  "pet-suv-rainbow": {
    // Diagnostic rainbow (blue → green → yellow → red). Same
    // hard cutoff as ``pet-suv-hot`` so normal tissue doesn't fog
    // the lesions.
    color: [
      [0, 0, 0, 0],
      [0.5, 0, 0, 0.55],
      [0.7, 0, 0.65, 0.45],
      [0.82, 0.95, 0.95, 0.05],
      [0.92, 0.95, 0.45, 0],
      [1, 0.95, 0, 0],
    ],
    opacity: [
      [0.0, 0.0],
      [0.65, 0.0],
      [0.66, 0.4],
      [0.85, 0.85],
      [1.0, 1.0],
    ],
    rangeMode: "suv",
  },
  skin: {
    color: [
      [0, 0, 0, 0],
      [0.2, 0.4, 0.2, 0.1],
      [0.45, 0.85, 0.55, 0.35],
      [0.65, 0.95, 0.75, 0.55],
      [1, 1, 0.9, 0.8],
    ],
    opacity: DEFAULT_OPACITY,
    rangeMode: "percentile",
  },
  tissue: {
    color: [
      [0, 0, 0, 0],
      [0.15, 0.3, 0.05, 0.05],
      [0.35, 0.6, 0.15, 0.1],
      [0.55, 0.85, 0.45, 0.3],
      [0.75, 0.95, 0.7, 0.55],
      [1, 1, 0.9, 0.8],
    ],
    opacity: DEFAULT_OPACITY,
    rangeMode: "percentile",
  },
  muscle: {
    color: [
      [0, 0, 0, 0],
      [0.25, 0.4, 0.1, 0.08],
      [0.5, 0.65, 0.2, 0.15],
      [0.75, 0.85, 0.4, 0.3],
      [1, 0.95, 0.6, 0.5],
    ],
    opacity: DEFAULT_OPACITY,
    rangeMode: "percentile",
  },
};

const BLEND_MAP: Record<BlendMode, number> = {
  composite: 0,
  mip: 1,
  minip: 2,
  average: 3,
};

// Fusion colormaps: each entry is a list of [t, r, g, b] stops in 0..1.
// These are standard choices in PET/CT workstations — "hot" / "pet" emphasize
// high-uptake regions, the simple single-hue maps are useful for CT/MR overlays.
const FUSION_COLORMAPS: Record<FusionColormap, [number, number, number, number][]> = {
  hot: [
    [0, 0, 0, 0],
    [0.33, 0.7, 0, 0],
    [0.66, 1, 0.7, 0],
    [1, 1, 1, 0.6],
  ],
  pet: [
    [0, 0, 0, 0.05],
    [0.2, 0.25, 0, 0.5],
    [0.5, 1, 0.25, 0],
    [0.75, 1, 0.85, 0],
    [1, 1, 1, 0.9],
  ],
  jet: [
    [0, 0, 0, 0.5],
    [0.25, 0, 0.5, 1],
    [0.5, 0, 1, 0],
    [0.75, 1, 1, 0],
    [1, 1, 0, 0],
  ],
  green: [
    [0, 0, 0, 0],
    [1, 0.1, 1, 0.2],
  ],
  magenta: [
    [0, 0, 0, 0],
    [1, 1, 0.1, 0.8],
  ],
  cyan: [
    [0, 0, 0, 0],
    [1, 0.1, 0.9, 1],
  ],
};

const VolumeViewer = forwardRef<VolumeViewerHandle, Props>(function VolumeViewer(
  {
    volume,
    className,
    modality,
    customOpacityStops,
    customRange,
    customColorStops,
    cropBox,
    mode = "volume",
  },
  ref,
) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const vtkRefs = useRef<{
    grw: ReturnType<typeof vtkGenericRenderWindow.newInstance>;
    actor: ReturnType<typeof vtkVolume.newInstance>;
    mapper: ReturnType<typeof vtkVolumeMapper.newInstance>;
    ctf: ReturnType<typeof vtkColorTransferFunction.newInstance>;
    otf: ReturnType<typeof vtkPiecewiseFunction.newInstance>;
    segActor: ReturnType<typeof vtkVolume.newInstance> | null;
    segMapper: ReturnType<typeof vtkVolumeMapper.newInstance> | null;
    segImageData: ReturnType<typeof vtkImageData.newInstance> | null;
    fusionActor: ReturnType<typeof vtkVolume.newInstance> | null;
    fusionMapper: ReturnType<typeof vtkVolumeMapper.newInstance> | null;
    fusionImageData: ReturnType<typeof vtkImageData.newInstance> | null;
    fusionCtf: ReturnType<typeof vtkColorTransferFunction.newInstance> | null;
    fusionOtf: ReturnType<typeof vtkPiecewiseFunction.newInstance> | null;
    fusionRange: [number, number] | null;
    fusionColormap: FusionColormap;
    fusionOpacity: number;
    fusionThreshold: number;
  } | null>(null);

  // Default the preset to a modality-appropriate one when modality is
  // known. Bone for CT, MR-edge for MR, PET-Hot for PT — the user can
  // still override via the dropdown but we land on something useful
  // before they touch any control. ``mode === "mip"`` overrides this
  // to grayscale/spectral so the maximum-intensity projection reads
  // like a clinical MIP rather than a coloured volume.
  const initialPreset: ColorPreset = (() => {
    if (mode === "mip") {
      const m = (modality || "").toUpperCase();
      if (m === "PT") return "pet-suv-hot";
      return "grayscale";
    }
    const m = (modality || "").toUpperCase();
    if (m === "CT") return "ct-bone";
    if (m === "MR") return "mri-edge";
    if (m === "PT") return "pet-suv-hot";
    return "bone";
  })();
  const [colorPreset, setColorPreset] = useState<ColorPreset>(initialPreset);
  // For PET in volume mode, default to MIP blend: composite ray
  // accumulation through metabolically active tissue inevitably
  // produces a "patient silhouette cylinder" (the body is filled
  // edge-to-edge with low-alpha voxels that integrate to opaque).
  // MIP picks the max along each ray and is the clinical standard
  // for PET 3D — radiologists read PET as MIP, not as composite.
  const initialBlendMode: BlendMode = (() => {
    if (mode === "mip") return "mip";
    if ((modality || "").toUpperCase() === "PT") return "mip";
    return "composite";
  })();
  const [blendMode, setBlendMode] = useState<BlendMode>(initialBlendMode);
  // ``threshold`` shifts the lower cut of the active opacity stops.
  // 0 leaves the preset alone; positive values push the alpha=0
  // region up the histogram (less of the volume rendered); negative
  // pulls it down (more rendered). Works on top of the preset's
  // ``opacity`` curve so you can tune any preset without editing it.
  const [threshold, setThreshold] = useState(0);
  const [opacityShift, setOpacityShift] = useState(0);
  const [sampleDistance, setSampleDistance] = useState<number>(autoSampleDistance(volume.spacing));
  // Shading is meaningful for composite volume rendering, not for MIP
  // (each ray is a max-pick, not a Beer-Lambert integration). Same
  // for cinematic gradient-opacity tricks.
  const [shade, setShade] = useState(mode !== "mip");
  const [cinematic, setCinematic] = useState(mode !== "mip");

  // Resolved [lo, hi] used by the active preset. Recomputed when the
  // preset, modality, or volume change; kept stable across opacityShift
  // / sampleDistance / lighting tweaks so an opacity slider doesn't
  // re-walk 50k samples per frame.
  const presetDisplayRange = useMemo(
    () => computeDisplayRange(volume.scalars, volume.range, colorPreset, modality),
    [volume.scalars, volume.range, colorPreset, modality],
  );
  // ``customRange`` (when supplied via the parent slider) takes
  // precedence over the preset-resolved range. This is how the
  // sidebar's HU / W/L 3D sliders narrow the rendered window
  // without forcing the user to pick a different preset. We sanity-
  // check the order so a misconfigured drag doesn't pass an
  // inverted range to vtk.js.
  const displayRange = useMemo<[number, number]>(() => {
    if (customRange && Number.isFinite(customRange[0]) && Number.isFinite(customRange[1])) {
      const [a, b] = customRange;
      if (a < b) return [a, b];
      if (a > b) return [b, a];
    }
    return presetDisplayRange;
  }, [customRange, presetDisplayRange]);

  useImperativeHandle(ref, () => ({
    colorPreset,
    blendMode,
    opacityShift,
    sampleDistance,
    shade,
    cinematic,
    threshold,
    setColorPreset,
    setBlendMode,
    setOpacityShift,
    setSampleDistance,
    setShade,
    setCinematic,
    setThreshold,
    setFusionVolume: (fusion: FusionVolume | null) => {
      const v = vtkRefs.current;
      if (!v) return;
      const renderer = v.grw.getRenderer();

      if (v.fusionActor) {
        renderer.removeVolume(v.fusionActor);
        v.fusionActor.delete();
        v.fusionMapper?.delete();
        v.fusionImageData?.delete();
        v.fusionCtf?.delete();
        v.fusionOtf?.delete();
        v.fusionActor = null;
        v.fusionMapper = null;
        v.fusionImageData = null;
        v.fusionCtf = null;
        v.fusionOtf = null;
        v.fusionRange = null;
      }

      if (fusion) {
        const fusionData = vtkImageData.newInstance();
        fusionData.setDimensions(fusion.dimensions);
        fusionData.setSpacing(fusion.spacing);
        // Same origin as primary. We trust the scans are co-registered
        // at acquisition time (PET/CT combo scanner, or MR series sharing
        // a frame of reference). Real per-pixel registration is out of
        // scope for this MVP.
        fusionData.setOrigin([0, 0, 0]);
        fusionData.getPointData().setScalars(
          vtkDataArray.newInstance({
            name: "fusion",
            values: fusion.scalars,
            numberOfComponents: 1,
          }),
        );

        const fusionMapper = vtkVolumeMapper.newInstance();
        fusionMapper.setInputData(fusionData);
        fusionMapper.setSampleDistance(1.0);
        // Always composite for fusion — MIP of a PET overlay on top of a
        // composite CT looks wrong; stick to composite regardless of the
        // primary volume's blend mode.
        fusionMapper.setBlendMode(0);

        const fusionCtf = vtkColorTransferFunction.newInstance();
        const fusionOtf = vtkPiecewiseFunction.newInstance();

        const fusionActor = vtkVolume.newInstance();
        fusionActor.setMapper(fusionMapper);
        const fp = fusionActor.getProperty();
        fp.setRGBTransferFunction(0, fusionCtf);
        fp.setScalarOpacity(0, fusionOtf);
        fp.setInterpolationTypeToLinear();
        // Fusion layer is pure emissive color — no shading, no gradient
        // opacity. Shading on a PET overlay produces misleading
        // pseudo-anatomy from uptake intensity.
        fp.setShade(false);
        fp.setAmbient(1);
        fp.setDiffuse(0);
        fp.setSpecular(0);

        // Robust range for the fusion volume. The wire ``fusion.range``
        // is the volume's full min..max — for PET overlays a single hot
        // voxel pulls hi up by 10× and crushes the rest of the histogram
        // into the bottom 10% of the OTF, where even the hard-cutoff
        // step lets noise through and silhouette accumulates. Compute
        // a percentile-based range from the fusion scalars (50th-99.5th
        // for SUV-skewed data, mirroring the primary preset's ``suv``
        // mode) so the fusion overlay reads as floating hot spots
        // rather than a glowing torso.
        const robustFusionRange = (() => {
          const N = fusion.scalars.length;
          if (N === 0) return fusion.range;
          const SAMPLE = Math.min(50000, N);
          const stride = Math.max(1, Math.floor(N / SAMPLE));
          const samples: number[] = [];
          for (let i = 0; i < N; i += stride) {
            const x = fusion.scalars[i];
            if (Number.isFinite(x) && x !== 0) samples.push(x);
          }
          if (samples.length < 100) return fusion.range;
          samples.sort((a, b) => a - b);
          const pick = (p: number) =>
            samples[Math.max(0, Math.min(samples.length - 1, Math.floor(p * samples.length)))];
          // Aggressive low-end clip so background tissue is below ``lo``
          // and clamps to alpha=0 in the OTF.
          return [pick(0.7), pick(0.995)] as [number, number];
        })();

        renderer.addVolume(fusionActor);
        v.fusionActor = fusionActor;
        v.fusionMapper = fusionMapper;
        v.fusionImageData = fusionData;
        v.fusionCtf = fusionCtf;
        v.fusionOtf = fusionOtf;
        v.fusionRange = robustFusionRange;
        v.fusionColormap = fusion.colormap;
        v.fusionOpacity = fusion.opacity;
        applyFusionTransfer(
          fusionCtf,
          fusionOtf,
          fusion.colormap,
          robustFusionRange,
          fusion.opacity,
          v.fusionThreshold,
        );
      }

      v.grw.getRenderWindow().render();
    },
    setFusionOpacity: (value: number) => {
      const v = vtkRefs.current;
      if (!v || !v.fusionCtf || !v.fusionOtf || !v.fusionRange) return;
      v.fusionOpacity = value;
      applyFusionTransfer(
        v.fusionCtf,
        v.fusionOtf,
        v.fusionColormap,
        v.fusionRange,
        value,
        v.fusionThreshold,
      );
      v.grw.getRenderWindow().render();
    },
    setFusionColormap: (colormap: FusionColormap) => {
      const v = vtkRefs.current;
      if (!v || !v.fusionCtf || !v.fusionOtf || !v.fusionRange) return;
      v.fusionColormap = colormap;
      applyFusionTransfer(
        v.fusionCtf,
        v.fusionOtf,
        colormap,
        v.fusionRange,
        v.fusionOpacity,
        v.fusionThreshold,
      );
      v.grw.getRenderWindow().render();
    },
    setFusionThreshold: (value: number) => {
      const v = vtkRefs.current;
      if (!v || !v.fusionCtf || !v.fusionOtf || !v.fusionRange) return;
      v.fusionThreshold = value;
      applyFusionTransfer(
        v.fusionCtf,
        v.fusionOtf,
        v.fusionColormap,
        v.fusionRange,
        v.fusionOpacity,
        value,
      );
      v.grw.getRenderWindow().render();
    },
    setSegmentationMask: (mask: SegMask | null) => {
      const v = vtkRefs.current;
      if (!v) return;
      const renderer = v.grw.getRenderer();

      if (v.segActor) {
        renderer.removeVolume(v.segActor);
        v.segActor.delete();
        v.segMapper?.delete();
        v.segImageData?.delete();
        v.segActor = null;
        v.segMapper = null;
        v.segImageData = null;
      }

      if (mask) {
        const segData = vtkImageData.newInstance();
        segData.setDimensions(volume.dimensions);
        segData.setSpacing(volume.spacing);
        segData.setOrigin([0, 0, 0]);
        const floatMask = new Float32Array(mask.data.length);
        for (let i = 0; i < mask.data.length; i++) floatMask[i] = mask.data[i];
        segData
          .getPointData()
          .setScalars(
            vtkDataArray.newInstance({ name: "mask", values: floatMask, numberOfComponents: 1 }),
          );

        const segMapper = vtkVolumeMapper.newInstance();
        segMapper.setInputData(segData);
        segMapper.setSampleDistance(1.0);

        const segCtf = vtkColorTransferFunction.newInstance();
        segCtf.addRGBPoint(0, 0, 0, 0);
        segCtf.addRGBPoint(0.5, mask.color[0], mask.color[1], mask.color[2]);
        segCtf.addRGBPoint(1, mask.color[0], mask.color[1], mask.color[2]);

        const segOtf = vtkPiecewiseFunction.newInstance();
        segOtf.addPoint(0, 0);
        segOtf.addPoint(0.5, 0.4);
        segOtf.addPoint(1, 0.4);

        const segActor = vtkVolume.newInstance();
        segActor.setMapper(segMapper);
        segActor.getProperty().setRGBTransferFunction(0, segCtf);
        segActor.getProperty().setScalarOpacity(0, segOtf);
        segActor.getProperty().setInterpolationTypeToNearest();

        renderer.addVolume(segActor);
        v.segActor = segActor;
        v.segMapper = segMapper;
        v.segImageData = segData;
      }

      v.grw.getRenderWindow().render();
    },
    setCameraPreset: (preset: string) => {
      const v = vtkRefs.current;
      if (!v) return;
      const cam = v.grw.getRenderer().getActiveCamera();
      const presets: Record<
        string,
        { pos: [number, number, number]; up: [number, number, number] }
      > = {
        anterior: { pos: [0, -1, 0], up: [0, 0, 1] },
        posterior: { pos: [0, 1, 0], up: [0, 0, 1] },
        left: { pos: [-1, 0, 0], up: [0, 0, 1] },
        right: { pos: [1, 0, 0], up: [0, 0, 1] },
        superior: { pos: [0, 0, 1], up: [0, -1, 0] },
        inferior: { pos: [0, 0, -1], up: [0, 1, 0] },
      };
      const p = presets[preset];
      if (!p) return;
      const bounds = v.grw.getRenderer().computeVisiblePropBounds();
      const cx = (bounds[0] + bounds[1]) / 2;
      const cy = (bounds[2] + bounds[3]) / 2;
      const cz = (bounds[4] + bounds[5]) / 2;
      const dist =
        Math.max(bounds[1] - bounds[0], bounds[3] - bounds[2], bounds[5] - bounds[4]) * 2;
      cam.setPosition(cx + p.pos[0] * dist, cy + p.pos[1] * dist, cz + p.pos[2] * dist);
      cam.setFocalPoint(cx, cy, cz);
      cam.setViewUp(...p.up);
      v.grw.getRenderer().resetCameraClippingRange();
      v.grw.getRenderWindow().render();
    },
    setBackground: (r: number, g: number, b: number) => {
      const v = vtkRefs.current;
      if (!v) return;
      v.grw.getRenderer().setBackground(r, g, b);
      v.grw.getRenderWindow().render();
    },
    setCustomColorMap: (stops: Array<{ pos: number; color: [number, number, number] }>) => {
      const v = vtkRefs.current;
      if (!v) return;
      const [lo, hi] = displayRange;
      const span = hi - lo || 1;
      v.ctf.removeAllPoints();
      for (const s of stops) {
        v.ctf.addRGBPoint(lo + s.pos * span, s.color[0], s.color[1], s.color[2]);
      }
      v.grw.getRenderWindow().render();
    },
    resetCamera: () => {
      const v = vtkRefs.current;
      if (!v) return;
      v.grw.getRenderer().resetCamera();
      v.grw.getRenderWindow().render();
    },
  }));

  // biome-ignore lint/correctness/useExhaustiveDependencies: viewer lifecycle effect — re-running on derived deps would tear down GPU resources.
  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;

    const grw = vtkGenericRenderWindow.newInstance({ background: [0.04, 0.05, 0.08] });
    grw.setContainer(container);
    grw.resize();

    const imageData = vtkImageData.newInstance();
    imageData.setDimensions(volume.dimensions);
    imageData.setSpacing(volume.spacing);
    imageData.setOrigin([0, 0, 0]);
    imageData.getPointData().setScalars(
      vtkDataArray.newInstance({
        name: "scalars",
        values: volume.scalars,
        numberOfComponents: 1,
      }),
    );

    const mapper = vtkVolumeMapper.newInstance();
    mapper.setInputData(imageData);
    mapper.setSampleDistance(sampleDistance);
    mapper.setBlendMode(BLEND_MAP[blendMode]);
    // Jittering breaks up the wood-grain artefacts you get when the
    // ray's first sample lands on a regular grid: each ray's start
    // position is randomised within one sample-distance, the result
    // is a frame-to-frame noisy but structurally smoother volume
    // (the noise averages out to a softer surface). Especially
    // visible on coarse CT bone where regularly-spaced steps
    // produced perceptible stripes.
    // ``setUseJittering`` exists in vtk.js >=27 but isn't in the
    // current type declarations; bracket-access keeps the call
    // forward-compatible without a type cast that would silence
    // unrelated typos.
    const mapperAny = mapper as unknown as { setUseJittering?: (v: boolean) => void };
    mapperAny.setUseJittering?.(true);

    const ctf = vtkColorTransferFunction.newInstance();
    const otf = vtkPiecewiseFunction.newInstance();

    const actor = vtkVolume.newInstance();
    actor.setMapper(mapper);
    const prop = actor.getProperty();
    prop.setRGBTransferFunction(0, ctf);
    prop.setScalarOpacity(0, otf);
    prop.setInterpolationTypeToLinear();
    prop.setShade(shade);
    prop.setAmbient(0.2);
    prop.setDiffuse(0.7);
    prop.setSpecular(0.3);
    prop.setSpecularPower(20);
    // Compute lighting normals from the opacity gradient when shading
    // is on. This makes thin tissue (vessel walls, skin) light
    // correctly even when the underlying scalar field is mostly
    // homogeneous — surface boundaries come from where opacity
    // changes, not where scalar values change. Falls back to scalar
    // gradient automatically when the data doesn't have one.
    const propAny = prop as unknown as {
      setComputeNormalFromOpacity?: (v: boolean) => void;
    };
    if (mode !== "mip") {
      propAny.setComputeNormalFromOpacity?.(true);
    }

    grw.getRenderer().addVolume(actor);
    vtkRefs.current = {
      grw,
      actor,
      mapper,
      ctf,
      otf,
      segActor: null,
      segMapper: null,
      segImageData: null,
      fusionActor: null,
      fusionMapper: null,
      fusionImageData: null,
      fusionCtf: null,
      fusionOtf: null,
      fusionRange: null,
      fusionColormap: "hot",
      fusionOpacity: 0.6,
      fusionThreshold: 0.65,
    };

    applyShadeParams(prop, shade, cinematic);
    applyPreset(
      ctf,
      otf,
      colorPreset,
      displayRange,
      opacityShift,
      customOpacityStops,
      threshold,
      customColorStops,
    );

    // Tighten the camera + light setup. The default vtk.js scene has a
    // single headlight; adding a fill light from the side and a rim
    // light from behind makes anatomy read as 3D instead of "flat
    // glowing blob". The renderer's TwoSidedLighting flag also helps
    // the back faces of thin walls (lung mesh, skin) light up cleanly.
    const renderer = grw.getRenderer();
    renderer.setTwoSidedLighting(true);
    renderer.resetCamera();
    renderer.getActiveCamera().elevation(15);
    renderer.resetCameraClippingRange();
    grw.getRenderWindow().render();

    const onResize = () => grw.resize();
    window.addEventListener("resize", onResize);

    return () => {
      window.removeEventListener("resize", onResize);
      const v = vtkRefs.current;
      if (v) {
        if (v.fusionActor) {
          v.fusionActor.delete();
          v.fusionMapper?.delete();
          v.fusionImageData?.delete();
          v.fusionCtf?.delete();
          v.fusionOtf?.delete();
        }
        if (v.segActor) {
          v.segActor.delete();
          v.segMapper?.delete();
          v.segImageData?.delete();
        }
      }
      vtkRefs.current = null;
      actor.delete();
      mapper.delete();
      imageData.delete();
      ctf.delete();
      otf.delete();
      grw.delete();
    };
    // only rebuild on volume change, not on settings
  }, [volume]);

  useEffect(() => {
    const v = vtkRefs.current;
    if (!v) return;
    applyPreset(
      v.ctf,
      v.otf,
      colorPreset,
      displayRange,
      opacityShift,
      customOpacityStops,
      threshold,
      customColorStops,
    );
    v.grw.getRenderWindow().render();
  }, [colorPreset, opacityShift, displayRange, customOpacityStops, customColorStops, threshold]);

  useEffect(() => {
    const v = vtkRefs.current;
    if (!v) return;
    v.mapper.setBlendMode(BLEND_MAP[blendMode]);
    // MIP / MinIP / Average pick a single value per ray (max / min /
    // mean) and ignore shading + gradient opacity. Leaving those on
    // is harmless mathematically, but vtk.js can keep a stale
    // composite shader cached, so the user sees no apparent change
    // when toggling blend mode. Force the volume property to its
    // non-composite state on every switch — the shader gets
    // re-evaluated and the new mode actually takes effect. ``shade``
    // and ``cinematic`` are restored when going back to composite.
    const isComposite = blendMode === "composite";
    applyShadeParams(
      v.actor.getProperty(),
      isComposite ? shade : false,
      isComposite ? cinematic : false,
    );
    // Belt-and-suspenders ``modified()`` on the mapper + property so
    // vtk.js re-runs the shader build path instead of reusing the
    // last-baked composite kernel. ``modified()`` is on the parent
    // ``vtkObject`` interface but isn't exposed on the typed API
    // surface; bracket-access keeps the cast tight.
    (v.mapper as unknown as { modified?: () => void }).modified?.();
    (v.actor.getProperty() as unknown as { modified?: () => void }).modified?.();
    v.grw.getRenderWindow().render();
  }, [blendMode, shade, cinematic]);

  useEffect(() => {
    const v = vtkRefs.current;
    if (!v) return;
    v.mapper.setSampleDistance(sampleDistance);
    v.grw.getRenderWindow().render();
  }, [sampleDistance]);

  // Apply / clear axis-aligned clipping planes from ``cropBox``.
  // The box is expressed in normalised volume space ([0, 1] per axis);
  // we convert to world coordinates using the volume's spacing +
  // dimensions, then mount up to six planes on the mapper. ``cropBox``
  // null wipes them. vtk.js caps the count at six which fits an axis-
  // aligned box exactly (one plane per face).
  useEffect(() => {
    const v = vtkRefs.current;
    if (!v) return;
    v.mapper.removeAllClippingPlanes();
    if (cropBox) {
      const [nx, ny, nz] = volume.dimensions;
      const [sx, sy, sz] = volume.spacing;
      const xlo = Math.max(0, Math.min(1, cropBox.lower[0])) * (nx - 1) * sx;
      const ylo = Math.max(0, Math.min(1, cropBox.lower[1])) * (ny - 1) * sy;
      const zlo = Math.max(0, Math.min(1, cropBox.lower[2])) * (nz - 1) * sz;
      const xhi = Math.max(0, Math.min(1, cropBox.upper[0])) * (nx - 1) * sx;
      const yhi = Math.max(0, Math.min(1, cropBox.upper[1])) * (ny - 1) * sy;
      const zhi = Math.max(0, Math.min(1, cropBox.upper[2])) * (nz - 1) * sz;
      // Each plane keeps everything in the half-space the normal
      // points INTO. Origin sits on the cut, normal points toward
      // the half we keep (= toward the centre of the kept box).
      const planes: Array<{
        origin: [number, number, number];
        normal: [number, number, number];
      }> = [
        { origin: [xlo, 0, 0], normal: [1, 0, 0] },
        { origin: [xhi, 0, 0], normal: [-1, 0, 0] },
        { origin: [0, ylo, 0], normal: [0, 1, 0] },
        { origin: [0, yhi, 0], normal: [0, -1, 0] },
        { origin: [0, 0, zlo], normal: [0, 0, 1] },
        { origin: [0, 0, zhi], normal: [0, 0, -1] },
      ];
      for (const p of planes) {
        const plane = vtkPlane.newInstance();
        plane.setOrigin(p.origin[0], p.origin[1], p.origin[2]);
        plane.setNormal(p.normal[0], p.normal[1], p.normal[2]);
        v.mapper.addClippingPlane(plane);
      }
    }
    v.grw.getRenderWindow().render();
  }, [cropBox, volume.dimensions, volume.spacing]);

  return (
    <div
      ref={containerRef}
      className={className}
      style={{ width: "100%", height: "100%", background: "#0a0d14" }}
    />
  );
});

/**
 * Tune shading parameters for either the plain Phong path or the
 * "cinematic" approximation. True cinematic rendering uses Monte Carlo
 * light transport, which vtk.js doesn't ship. We approximate:
 *
 * - gradient opacity on → edges appear sharper, flat tissue fades out
 *   (mimics surface-aware light absorption)
 * - raised ambient → softer shadows, fewer jet-black cavities, closer to
 *   skylit ambient in cinematic renders
 * - stronger specular with lower power → broad, diffuse highlights as on
 *   sub-surface-scattering skin
 *
 * It's not true MC path tracing, but side-by-side with the plain Phong
 * mode the difference is clearly visible on CT skin/bone presets.
 */
type VolumeProperty = ReturnType<ReturnType<typeof vtkVolume.newInstance>["getProperty"]>;

function applyShadeParams(prop: VolumeProperty, shade: boolean, cinematic: boolean) {
  prop.setShade(shade);
  prop.setInterpolationTypeToLinear();
  if (cinematic) {
    // Cinematic-ish: gradient opacity emphasises tissue boundaries
    // (the volume becomes "surface-like" rather than the cloud you
    // get with a pure compositing pass), and the lighting recipe
    // raises ambient + adds broader specular so cavities don't go
    // pitch-black and surfaces look sub-surface-scattered. This is
    // not Monte-Carlo path tracing (vtk.js doesn't ship that) but
    // it's the closest we can do in real-time.
    prop.setUseGradientOpacity(0, true);
    prop.setGradientOpacityMinimumValue(0, 0);
    prop.setGradientOpacityMinimumOpacity(0, 0);
    prop.setGradientOpacityMaximumValue(0, 150);
    prop.setGradientOpacityMaximumOpacity(0, 1);
    prop.setAmbient(0.35);
    prop.setDiffuse(0.65);
    prop.setSpecular(0.55);
    prop.setSpecularPower(12);
    // Mid-range scalar opacity unit distance: keeps thin tissue
    // (skin, vessel walls) from going fully opaque, so the user can
    // see through to deeper structures without dropping the global
    // opacity. Smaller → more transparent.
    prop.setScalarOpacityUnitDistance(0, 1.5);
  } else {
    prop.setUseGradientOpacity(0, false);
    prop.setAmbient(0.25);
    prop.setDiffuse(0.7);
    prop.setSpecular(0.35);
    prop.setSpecularPower(18);
    prop.setScalarOpacityUnitDistance(0, 1.0);
  }
}

function applyFusionTransfer(
  ctf: ReturnType<typeof vtkColorTransferFunction.newInstance>,
  otf: ReturnType<typeof vtkPiecewiseFunction.newInstance>,
  colormap: FusionColormap,
  range: [number, number],
  opacity: number,
  threshold = 0.65,
) {
  const [lo, hi] = range;
  const span = hi - lo || 1;

  ctf.removeAllPoints();
  for (const [t, r, g, b] of FUSION_COLORMAPS[colormap]) {
    ctf.addRGBPoint(lo + t * span, r, g, b);
  }

  // Fusion overlays render *additively* over the base volume's
  // composite pass, so any lingering low-alpha contribution
  // accumulates along the ray and re-introduces the "patient
  // silhouette cylinder" the primary OTF goes to great lengths to
  // avoid. We mirror the primary preset strategy: a hard alpha=0 step
  // up to ``threshold`` (a fraction of the displayed SUV/percentile
  // range), then a short ramp to the blend ``opacity``. ``threshold``
  // is the user's uptake-isolation knob: raising it suppresses the
  // physiologic background (liver, blood pool, bladder) so only the
  // hottest focal lesions remain; lowering it reveals more diffuse
  // uptake. Default 0.65 reproduces the previous fixed behaviour.
  const thr = Math.max(0, Math.min(0.95, threshold));
  const mid = thr + (1 - thr) * 0.6;
  otf.removeAllPoints();
  otf.addPoint(lo, 0);
  otf.addPoint(lo + thr * span, 0);
  otf.addPoint(lo + Math.min(thr + 0.01, 0.999) * span, 0.4 * opacity);
  otf.addPoint(lo + mid * span, 0.85 * opacity);
  otf.addPoint(hi, opacity);
}

function applyPreset(
  ctf: ReturnType<typeof vtkColorTransferFunction.newInstance>,
  otf: ReturnType<typeof vtkPiecewiseFunction.newInstance>,
  preset: ColorPreset,
  range: [number, number],
  opacityShift: number,
  customOpacityStops?: Array<{ t: number; alpha: number }> | null,
  threshold = 0,
  customColorStops?: Array<{ pos: number; color: [number, number, number] }> | null,
) {
  const def = PRESET_DEFS[preset];
  const [lo, hi] = range;
  const span = hi - lo || 1;

  ctf.removeAllPoints();
  if (customColorStops && customColorStops.length > 0) {
    // User-curated CTF: each stop's ``pos`` is normalised in [0, 1]
    // and gets remapped onto the active ``displayRange``. Sorted
    // before iteration so out-of-order drag gestures don't produce
    // a non-monotonic transfer function (vtk.js silently drops
    // out-of-order points, which would look like missing colours).
    const sorted = [...customColorStops].sort((a, b) => a.pos - b.pos);
    for (const s of sorted) {
      const t = Math.max(0, Math.min(1, s.pos));
      const [r, g, b] = s.color;
      ctf.addRGBPoint(lo + t * span, r, g, b);
    }
  } else {
    for (const [t, r, g, b] of def.color) {
      ctf.addRGBPoint(lo + t * span, r, g, b);
    }
  }

  otf.removeAllPoints();
  // Two orthogonal controls combine here:
  //   - ``threshold`` shifts every stop's t-position along the
  //     histogram (positive = transparent region grows). This is the
  //     anti-cylinder knob: push the alpha=0 cutoff up the histogram.
  //   - ``opacityShift`` is a *multiplier* on every stop's alpha,
  //     ranging effectively in [0, 2]. Encoded on the wire as
  //     -50..+50 for slider ergonomics; mapped to ``1 + shift/50`` so
  //     the midpoint (0) leaves the preset alpha untouched, and the
  //     extremes halve / double the visible opacity. Positive =
  //     bolder rendering, negative = ghostlier.
  // The two were folded into a single t-shift in an earlier pass;
  // splitting them gives the user real independent controls.
  const alphaScale = Math.max(0, 1 + opacityShift / 50);
  const stops = customOpacityStops
    ? customOpacityStops.map((s) => [s.t, s.alpha] as [number, number])
    : def.opacity;
  for (const [t, alpha] of stops) {
    const tShifted = Math.max(0, Math.min(1, t + threshold));
    const alphaScaled = Math.max(0, Math.min(1, alpha * alphaScale));
    otf.addPoint(lo + tShifted * span, alphaScaled);
  }
}

/**
 * Compute the [lo, hi] used to map preset stops onto data values.
 * The choice depends on the preset's ``rangeMode`` and the modality:
 *
 *  * ``"hu"`` → modality must be CT; we clamp to the preset's
 *    rangeHint (e.g. [200, 1500] for bone). The volume's actual range
 *    is ignored since HU values are absolute.
 *  * ``"suv"`` → percentile-based but skewed toward the bright tail,
 *    matching how PET workstations render: 0.5th to 99th percentile of
 *    non-zero voxels, clipped at ``[0, 99th]`` so the noise floor near
 *    zero is fully transparent.
 *  * ``"percentile"`` → robust 1st-99th percentile of non-zero voxels.
 *    The standard fallback for any volume; outlier voxels (a single
 *    fiducial marker, a hot streak from beam-hardening) no longer
 *    crush the contrast of every other voxel.
 *  * ``"full"`` → raw min..max from the wire (legacy).
 *
 * The percentile pass samples the volume rather than walking every
 * voxel — for a 512×512×400 volume that's ~100M reads, which would
 * stall the UI on switching presets. Sampling 50k random voxels gives
 * percentile estimates within ~1% of truth.
 */
function computeDisplayRange(
  scalars: Float32Array,
  fullRange: [number, number],
  preset: ColorPreset,
  modality: string | null | undefined,
): [number, number] {
  const def = PRESET_DEFS[preset];
  const mod = (modality || "").toUpperCase();

  if (def.rangeMode === "hu" && def.rangeHint && (mod === "CT" || !mod)) {
    return def.rangeHint;
  }
  if (def.rangeMode === "full") {
    return fullRange;
  }
  // Percentile-based for both ``percentile`` and ``suv``. Sample the
  // volume sparsely to keep the operation under a few ms.
  const N = scalars.length;
  if (N === 0) return fullRange;
  const SAMPLE = Math.min(50000, N);
  const stride = Math.max(1, Math.floor(N / SAMPLE));
  const samples: number[] = [];
  for (let i = 0; i < N; i += stride) {
    const v = scalars[i];
    if (Number.isFinite(v) && v !== 0) samples.push(v);
  }
  if (samples.length < 100) {
    // Sparse data (e.g. mostly-zero PET background); fall back to
    // the wire range so the TF still resolves something.
    return fullRange;
  }
  samples.sort((a, b) => a - b);
  const pick = (p: number) =>
    samples[Math.max(0, Math.min(samples.length - 1, Math.floor(p * samples.length)))];
  if (def.rangeMode === "suv") {
    // PT: lo at 70th percentile, hi at 99.5th. Combined with the
    // preset's near-step opacity at t=0.65, only voxels above
    // roughly the 90th percentile of nonzero data render — exactly
    // where lesions and high-uptake organs live. Normal tissue
    // (50-70th percentile of the histogram) falls below the
    // displayRange's lower bound and is forced to alpha=0 by vtk's
    // out-of-range clamp, so no silhouette cylinder.
    return [pick(0.7), pick(0.995)];
  }
  return [pick(0.01), pick(0.99)];
}

/**
 * Auto sample distance: VTK's volume mapper steps along view rays in
 * world units. With a 1 mm-isotropic CT and the legacy 0.7 default the
 * raycaster takes ~70% of a voxel per sample, which is fine for
 * smooth volumes but blurs sharp bone/iodine edges. We pick 0.4 ×
 * smallest spacing so high-resolution volumes get crisper edges and
 * coarse ones don't pay an unnecessary perf tax. Clamped to keep
 * pathological (sub-millimetre) volumes from grinding the GPU.
 */
function autoSampleDistance(spacing: [number, number, number]): number {
  const minSpacing = Math.min(...spacing);
  if (!Number.isFinite(minSpacing) || minSpacing <= 0) return 0.7;
  return Math.max(0.15, Math.min(1.0, 0.4 * minSpacing));
}

export default VolumeViewer;
