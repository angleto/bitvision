// Extracted from viewer/series/[id]/page.tsx on 2026-05-21.

"use client";

"use client";

import { useTranslations } from "next-intl";
import dynamic from "next/dynamic";
import Link from "next/link";
import { useParams, useSearchParams } from "next/navigation";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import BrowserSupportGate from "@/components/BrowserSupportGate";
import FiducialPanel, { useFiducials } from "@/components/FiducialMarkers3D";
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
import type {
  BlendMode,
  ColorPreset,
  VolumeData,
  VolumeViewerHandle,
} from "@/components/VolumeViewer";
import {
  ApiError,
  type AppSetting,
  type DisplayMetadata,
  type Marker,
  type MarkerKind,
  type Series,
  fetchVolume,
  getStoredToken,
  markersApi,
  request,
  settingsApi,
  studiesApi,
} from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import { purgeCornerstoneCache } from "@/lib/cornerstoneSetup";
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
import { type ViewportStateBlob, useViewportState } from "@/lib/viewportState";
import { computeAutoWL, modalityDefaults, suggestedFromDicom } from "@/lib/windowing";
import { WL_BTN_STYLE } from "./viewerStyles";

export default function Vr3DRangeControl({
  fullRange,
  value,
  onChange,
}: {
  fullRange: [number, number];
  value: [number, number] | null;
  onChange: (v: [number, number] | null) => void;
}) {
  const [fmin, fmax] = fullRange;
  // Resolve the active [lo, hi]: the override when set, otherwise the
  // full volume range (the slider's natural neutral position).
  const lo = value ? value[0] : fmin;
  const hi = value ? value[1] : fmax;
  const w = hi - lo;
  const c = (hi + lo) / 2;
  const set = (next: [number, number]) => {
    // Reject inverted / zero-width ranges — vtk.js silently renders
    // a black volume in that case which is worse than ignoring the
    // gesture.
    if (next[1] - next[0] <= 0) return;
    onChange([Math.max(fmin, Math.min(fmax, next[0])), Math.max(fmin, Math.min(fmax, next[1]))]);
  };
  // Step: 1 HU is too coarse for SUV, so derive from the full span.
  const step = Math.max(0.01, (fmax - fmin) / 2000);
  return (
    <>
      <h2 style={{ marginTop: "0.5rem" }}>HU / scalar range</h2>
      <p className="meta" style={{ marginTop: "-0.4rem", fontSize: "0.7rem" }}>
        {value
          ? `Override: ${lo.toFixed(0)} → ${hi.toFixed(0)} (W ${Math.round(w)} · L ${Math.round(c)})`
          : `Preset default · drag to override (full ${fmin.toFixed(0)} → ${fmax.toFixed(0)})`}
      </p>
      <div style={{ display: "flex", flexDirection: "column", gap: "0.25rem" }}>
        <label className="meta" style={{ fontSize: "0.65rem" }}>
          Min
          <input
            type="range"
            min={fmin}
            max={fmax}
            step={step}
            value={lo}
            onChange={(e) => set([Number(e.target.value), hi])}
            style={{ width: "100%" }}
          />
        </label>
        <label className="meta" style={{ fontSize: "0.65rem" }}>
          Max
          <input
            type="range"
            min={fmin}
            max={fmax}
            step={step}
            value={hi}
            onChange={(e) => set([lo, Number(e.target.value)])}
            style={{ width: "100%" }}
          />
        </label>
        <label className="meta" style={{ fontSize: "0.65rem" }}>
          Window (W)
          <input
            type="range"
            min={step}
            max={fmax - fmin}
            step={step}
            value={w}
            onChange={(e) => {
              const nw = Number(e.target.value);
              set([c - nw / 2, c + nw / 2]);
            }}
            style={{ width: "100%" }}
          />
        </label>
        <label className="meta" style={{ fontSize: "0.65rem" }}>
          Level (L)
          <input
            type="range"
            min={fmin}
            max={fmax}
            step={step}
            value={c}
            onChange={(e) => {
              const nc = Number(e.target.value);
              set([nc - w / 2, nc + w / 2]);
            }}
            style={{ width: "100%" }}
          />
        </label>
      </div>
      <button
        type="button"
        className="viewer-btn"
        style={{ fontSize: "0.7rem", marginTop: "0.3rem" }}
        onClick={() => onChange(null)}
        disabled={value === null}
        title="Restore the preset's default HU range"
      >
        Reset to preset
      </button>
    </>
  );
}

/**
 * Color transfer-function editor. Each stop has a normalised position
 * in [0, 1] and an RGB picker. Stops are added/removed by buttons; the
 * list is sorted client-side so the parent always receives a monotonic
 * sequence (vtk.js otherwise drops out-of-order points and the volume
 * goes black). ``null`` = use the preset's RGB ramp; an array overrides.
 */
