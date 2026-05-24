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

export default function Vr3DColorEditor({
  value,
  onChange,
}: {
  value: Array<{ pos: number; color: [number, number, number] }> | null;
  onChange: (v: Array<{ pos: number; color: [number, number, number] }> | null) => void;
}) {
  const stops =
    value ??
    ([
      { pos: 0, color: [0, 0, 0] },
      { pos: 0.5, color: [0.6, 0.4, 0.3] },
      { pos: 1, color: [1, 0.95, 0.85] },
    ] as Array<{ pos: number; color: [number, number, number] }>);
  const updateAt = (i: number, next: { pos: number; color: [number, number, number] }) => {
    const arr = stops.map((s, idx) => (idx === i ? next : s));
    onChange(arr.sort((a, b) => a.pos - b.pos));
  };
  const removeAt = (i: number) => {
    if (stops.length <= 2) return; // keep at least two stops for a valid TF
    const arr = stops.filter((_, idx) => idx !== i);
    onChange(arr);
  };
  const addStop = () => {
    // Insert a midpoint stop between the last two — easy to discover,
    // never out of range. Color = average of neighbours so the visual
    // gradient stays smooth on insertion.
    const a = stops[stops.length - 2];
    const b = stops[stops.length - 1];
    const mid: { pos: number; color: [number, number, number] } = {
      pos: (a.pos + b.pos) / 2,
      color: [
        (a.color[0] + b.color[0]) / 2,
        (a.color[1] + b.color[1]) / 2,
        (a.color[2] + b.color[2]) / 2,
      ],
    };
    onChange([...stops, mid].sort((x, y) => x.pos - y.pos));
  };
  const toHex = (c: [number, number, number]) =>
    `#${c
      .map((v) =>
        Math.round(Math.max(0, Math.min(1, v)) * 255)
          .toString(16)
          .padStart(2, "0"),
      )
      .join("")}`;
  const fromHex = (h: string): [number, number, number] => [
    Number.parseInt(h.slice(1, 3), 16) / 255,
    Number.parseInt(h.slice(3, 5), 16) / 255,
    Number.parseInt(h.slice(5, 7), 16) / 255,
  ];
  return (
    <>
      <h2 style={{ marginTop: "0.5rem" }}>Color transfer function</h2>
      <p className="meta" style={{ marginTop: "-0.4rem", fontSize: "0.7rem" }}>
        {value
          ? `${stops.length} stops · drag positions, pick colours`
          : "Preset colours · click Edit to override"}
      </p>
      <div style={{ display: "flex", flexDirection: "column", gap: "0.25rem" }}>
        {stops.map((s, i) => (
          <div
            // Stops have no intrinsic id; the editor identifies them by
            // position in the array (``updateAt(i, ...)``). Index key
            // matches the handler addressing.
            // biome-ignore lint/suspicious/noArrayIndexKey: stops are addressed by ordinal in updateAt(i, ...).
            key={i}
            style={{
              display: "flex",
              alignItems: "center",
              gap: "0.3rem",
              fontSize: "0.7rem",
            }}
          >
            <span className="meta" style={{ width: 28 }}>
              #{i}
            </span>
            <input
              type="range"
              min={0}
              max={1}
              step={0.01}
              value={s.pos}
              onChange={(e) => updateAt(i, { ...s, pos: Number(e.target.value) })}
              style={{ flex: 1, minWidth: 0 }}
              disabled={!value}
            />
            <span className="meta" style={{ width: 32 }}>
              {s.pos.toFixed(2)}
            </span>
            <input
              type="color"
              value={toHex(s.color)}
              onChange={(e) => updateAt(i, { ...s, color: fromHex(e.target.value) })}
              disabled={!value}
              style={{
                width: 28,
                height: 22,
                padding: 0,
                border: "1px solid #444",
              }}
            />
            <button
              type="button"
              className="ghost"
              onClick={() => removeAt(i)}
              disabled={!value || stops.length <= 2}
              style={{ fontSize: "0.7rem", padding: "0 0.3rem", color: "#f66" }}
              title="Remove stop"
            >
              ×
            </button>
          </div>
        ))}
      </div>
      <div style={{ display: "flex", gap: "0.3rem", marginTop: "0.3rem" }}>
        {value === null ? (
          <button
            type="button"
            className="viewer-btn"
            style={{ fontSize: "0.7rem" }}
            onClick={() => onChange(stops)}
          >
            Edit
          </button>
        ) : (
          <>
            <button
              type="button"
              className="viewer-btn"
              style={{ fontSize: "0.7rem" }}
              onClick={addStop}
            >
              + stop
            </button>
            <button
              type="button"
              className="viewer-btn"
              style={{ fontSize: "0.7rem" }}
              onClick={() => onChange(null)}
              title="Restore the preset's RGB ramp"
            >
              Reset
            </button>
          </>
        )}
      </div>
    </>
  );
}

/**
 * Axis-aligned crop box. Six sliders (X / Y / Z low+high) drive the
 * vtk clipping planes mounted on the volume mapper. Components are in
 * normalised [0, 1] volume space — VolumeViewer multiplies by the
 * volume's spacing × dimensions to convert to world coordinates.
 *
 * ``null`` = no crop; "Crop" button enables an open box (full volume)
 * the user can then narrow with the sliders. Reset clears the override.
 */
