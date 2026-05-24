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

export default function Vr3DCropBox({
  value,
  onChange,
}: {
  value: { lower: [number, number, number]; upper: [number, number, number] } | null;
  onChange: (
    v: { lower: [number, number, number]; upper: [number, number, number] } | null,
  ) => void;
}) {
  const enabled = value !== null;
  const lower = value?.lower ?? [0, 0, 0];
  const upper = value?.upper ?? [1, 1, 1];
  const setComponent = (side: "lower" | "upper", axis: 0 | 1 | 2, v: number) => {
    const lo = [...lower] as [number, number, number];
    const up = [...upper] as [number, number, number];
    if (side === "lower") lo[axis] = v;
    else up[axis] = v;
    // Keep lo < up by a small epsilon so we never produce an empty
    // crop volume (which renders pure black).
    const eps = 0.01;
    if (up[axis] - lo[axis] < eps) {
      if (side === "lower") lo[axis] = up[axis] - eps;
      else up[axis] = lo[axis] + eps;
    }
    onChange({ lower: lo, upper: up });
  };
  return (
    <>
      <h2 style={{ marginTop: "0.5rem" }}>Crop box</h2>
      <p className="meta" style={{ marginTop: "-0.4rem", fontSize: "0.7rem" }}>
        {enabled ? "Drag faces to expose internal anatomy" : "Off · click Crop to enable"}
      </p>
      {!enabled ? (
        <button
          type="button"
          className="viewer-btn"
          style={{ fontSize: "0.7rem" }}
          onClick={() => onChange({ lower: [0, 0, 0], upper: [1, 1, 1] })}
        >
          Crop
        </button>
      ) : (
        <>
          <div style={{ display: "flex", flexDirection: "column", gap: "0.2rem" }}>
            {(["X", "Y", "Z"] as const).map((axisLabel, axis) => (
              <div key={axisLabel} style={{ display: "flex", flexDirection: "column" }}>
                <span className="meta" style={{ fontSize: "0.65rem" }}>
                  {axisLabel}: {lower[axis as 0 | 1 | 2].toFixed(2)} →{" "}
                  {upper[axis as 0 | 1 | 2].toFixed(2)}
                </span>
                <input
                  type="range"
                  min={0}
                  max={1}
                  step={0.01}
                  value={lower[axis as 0 | 1 | 2]}
                  onChange={(e) => setComponent("lower", axis as 0 | 1 | 2, Number(e.target.value))}
                  style={{ width: "100%" }}
                />
                <input
                  type="range"
                  min={0}
                  max={1}
                  step={0.01}
                  value={upper[axis as 0 | 1 | 2]}
                  onChange={(e) => setComponent("upper", axis as 0 | 1 | 2, Number(e.target.value))}
                  style={{ width: "100%" }}
                />
              </div>
            ))}
          </div>
          <div style={{ display: "flex", gap: "0.3rem", marginTop: "0.3rem" }}>
            <button
              type="button"
              className="viewer-btn"
              style={{ fontSize: "0.7rem" }}
              onClick={() => onChange({ lower: [0, 0, 0], upper: [1, 1, 1] })}
              title="Reopen the full volume but keep cropping enabled"
            >
              Open
            </button>
            <button
              type="button"
              className="viewer-btn"
              style={{ fontSize: "0.7rem" }}
              onClick={() => onChange(null)}
              title="Disable cropping entirely (no clipping planes)"
            >
              Off
            </button>
          </div>
        </>
      )}
    </>
  );
}
