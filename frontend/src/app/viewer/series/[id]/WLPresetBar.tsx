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

export default function WLPresetBar({
  series,
  volume,
  onApply,
  onReset,
}: {
  series: Series | null;
  volume: VolumeData;
  onApply: (wc: number, ww: number) => void;
  onReset: () => void;
}) {
  const dicom = suggestedFromDicom(series);
  const presets = modalityDefaults(series?.modality ?? "", series?.body_part_examined ?? undefined);
  return (
    <div style={{ display: "flex", gap: "0.3rem", flexWrap: "wrap" }}>
      <button
        type="button"
        className="viewer-btn"
        style={WL_BTN_STYLE}
        title="Auto from histogram (1%-99% clip)"
        onClick={() => {
          const { wc, ww } = computeAutoWL(volume.scalars, series?.modality ?? undefined);
          onApply(wc, ww);
        }}
      >
        Auto
      </button>
      {dicom && (
        <button
          type="button"
          className="viewer-btn"
          style={WL_BTN_STYLE}
          title={`From DICOM tags · WC ${Math.round(dicom.wc)} / WW ${Math.round(dicom.ww)}`}
          onClick={() => onApply(dicom.wc, dicom.ww)}
        >
          DICOM
        </button>
      )}
      {presets.map(({ label, wc, ww }) => (
        <button
          key={label}
          type="button"
          className="viewer-btn"
          style={WL_BTN_STYLE}
          onClick={() => onApply(wc, ww)}
        >
          {label}
        </button>
      ))}
      <button type="button" className="viewer-btn" style={WL_BTN_STYLE} onClick={onReset}>
        Reset
      </button>
    </div>
  );
}

// ---------------------------------------------------------------------------
// 3D control widgets — kept local to the viewer page since they read /
// write only the page-scoped vr* state. Splitting them out into separate
// files would force the parent to thread a dozen setters through props
// for negligible reuse benefit.
// ---------------------------------------------------------------------------

/**
 * HU / scalar window for the 3D rendering. Two slider knobs (low / high)
 * plus a "W/L" pair (window width + center) that move both knobs in
 * tandem. Operates on a [lo, hi] tuple in raw scalar units (HU for CT,
 * SUV for PT, etc.) and bubbles the override up to ``VolumeViewer.customRange``.
 *
 * ``null`` value = no override (use the preset's resolved range). The
 * Reset button restores ``null`` so the preset takes back over.
 */
