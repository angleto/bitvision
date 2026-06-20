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

// The handful of general body windows a radiologist reaches for on most CT
// reads. Everything else (organ-specific / multi-phase sub-presets) is folded
// behind an "Altri…" expander so the default bar isn't a 30-button wall.
const COMMON_PRESET_LABELS = new Set([
  "CT Lung",
  "CT Mediastinum",
  "CT Abdomen",
  "CT Soft Tissue",
  "CT Bone",
  "CT Brain",
  "CT Liver",
  "CT Angio",
]);
const REGION_WORDS = new Set([
  "Liver",
  "Kidney",
  "Lung",
  "Pancreas",
  "Adrenal",
  "Spleen",
  "Brain",
  "Bone",
  "Mediastinum",
  "Abdomen",
  "Angio",
]);

/** Region heading for the grouped "Altri…" section, derived from the label
 *  (strip the modality prefix, take the leading organ word). */
function presetRegion(label: string): string {
  const stripped = label.replace(/^(CT|MRI?|PT|US|CR|DX)\s+/i, "");
  const first = stripped.split(/\s+/)[0] ?? "Altri";
  return REGION_WORDS.has(first) ? first : "Altri";
}

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
  const [showMore, setShowMore] = useState(false);
  const dicom = suggestedFromDicom(series);
  const presets = modalityDefaults(series?.modality ?? "", series?.body_part_examined ?? undefined);

  const common = presets.filter((p) => COMMON_PRESET_LABELS.has(p.label));
  const rest = presets.filter((p) => !COMMON_PRESET_LABELS.has(p.label));
  // Only collapse a genuinely long list (CT). MR/PT etc. have few presets —
  // keep them flat so nothing is needlessly hidden behind a click.
  const collapse = presets.length > 10 && common.length >= 4;
  const primary = collapse ? common : presets;

  // Group the overflow presets by anatomical region, preserving first-seen order.
  const groups: Array<[string, typeof rest]> = [];
  if (collapse) {
    const byRegion = new Map<string, typeof rest>();
    for (const p of rest) {
      const r = presetRegion(p.label);
      const arr = byRegion.get(r) ?? [];
      arr.push(p);
      byRegion.set(r, arr);
    }
    groups.push(...byRegion.entries());
  }

  const presetBtn = (label: string, wc: number, ww: number, title?: string) => (
    <button
      key={label}
      type="button"
      className="viewer-btn"
      style={WL_BTN_STYLE}
      title={title}
      onClick={() => onApply(wc, ww)}
    >
      {label}
    </button>
  );

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "0.4rem" }}>
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
        {dicom &&
          presetBtn(
            "DICOM",
            dicom.wc,
            dicom.ww,
            `From DICOM tags · WC ${Math.round(dicom.wc)} / WW ${Math.round(dicom.ww)}`,
          )}
        {primary.map(({ label, wc, ww }) => presetBtn(label, wc, ww))}
        {collapse && (
          <button
            type="button"
            className="viewer-btn"
            style={WL_BTN_STYLE}
            aria-expanded={showMore}
            onClick={() => setShowMore((v) => !v)}
          >
            {showMore ? "Meno ▲" : `Altri… (${rest.length}) ▾`}
          </button>
        )}
        <button type="button" className="viewer-btn" style={WL_BTN_STYLE} onClick={onReset}>
          Reset
        </button>
      </div>
      {collapse && showMore && (
        <div style={{ display: "flex", flexDirection: "column", gap: "0.35rem" }}>
          {groups.map(([region, items]) => (
            <div key={region}>
              <div
                className="meta"
                style={{
                  fontSize: "0.62rem",
                  textTransform: "uppercase",
                  letterSpacing: "0.05em",
                  marginBottom: "0.2rem",
                  opacity: 0.7,
                }}
              >
                {region}
              </div>
              <div style={{ display: "flex", gap: "0.3rem", flexWrap: "wrap" }}>
                {items.map(({ label, wc, ww }) => presetBtn(label, wc, ww))}
              </div>
            </div>
          ))}
        </div>
      )}
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
