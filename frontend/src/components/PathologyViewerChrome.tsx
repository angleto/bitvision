"use client";

/*
 * Clinical chrome overlaid on the whole-slide OpenSeadragon viewer:
 * a scale bar (from the slide's µm/pixel), a live magnification readout
 * with preset jumps (1× … 40×, clamped to the scanned power so the
 * pathologist can't over-zoom into interpolated mush), and a colour chip
 * noting tiles are server-managed sRGB (ICC baked in at tiling time).
 *
 * All arithmetic comes from the pure ``lib/pathology/scaleMath`` module
 * (unit-tested); this component is thin presentation fed the live
 * ``screenPxPerImagePx`` the page computes from the OSD viewport.
 */

import { useTranslations } from "next-intl";

import type { PathologySlide } from "@/lib/api";
import {
  availablePresets,
  baseMagnification,
  computeScaleBar,
  currentMagnification,
  formatMagnification,
} from "@/lib/pathology/scaleMath";

interface Props {
  slide: PathologySlide;
  /** CSS pixels per base-level image pixel at the current zoom (0 until
   *  the viewport is ready). */
  screenPxPerImagePx: number;
  /** Manual-calibration override for ``mpp_x`` (gross photos with no
   *  scanner mpp). Null ⇒ use the slide's own mpp. */
  calibrationMpp?: number | null;
  onSelectMagnification: (mag: number) => void;
}

export default function PathologyViewerChrome({
  slide,
  screenPxPerImagePx,
  calibrationMpp,
  onSelectMagnification,
}: Props) {
  const t = useTranslations("pathologyLibrary");
  const mppX = calibrationMpp ?? slide.mpp_x;
  const baseMag = baseMagnification(slide.magnification, mppX);
  const scaleBar = computeScaleBar({ mppX, screenPxPerImagePx, targetScreenPx: 120 });
  const liveMag = currentMagnification(baseMag, screenPxPerImagePx);
  const presets = availablePresets(baseMag);
  const uncalibrated = !mppX && slide.slide_class !== "wsi";

  const chipStyle: React.CSSProperties = {
    background: "rgba(12,10,9,0.72)",
    color: "#f5f5f4",
    padding: "0.2rem 0.5rem",
    borderRadius: 6,
    fontSize: "0.72rem",
    lineHeight: 1.2,
    pointerEvents: "none",
  };

  return (
    <>
      {/* Top-left: live magnification + presets */}
      <div
        style={{
          position: "absolute",
          top: 8,
          left: 8,
          display: "flex",
          gap: "0.4rem",
          alignItems: "center",
          flexWrap: "wrap",
          zIndex: 5,
        }}
      >
        <span style={chipStyle} aria-label={t("magnificationLabel")}>
          {formatMagnification(liveMag)}
        </span>
        <div style={{ display: "flex", gap: 2 }}>
          {presets.map((p) => (
            <button
              key={p}
              type="button"
              onClick={() => onSelectMagnification(p)}
              title={`${p}×`}
              style={{
                background: "rgba(12,10,9,0.72)",
                color: "#f5f5f4",
                border: "1px solid rgba(255,255,255,0.18)",
                borderRadius: 5,
                fontSize: "0.7rem",
                padding: "0.15rem 0.4rem",
                cursor: "pointer",
              }}
            >
              {p}×
            </button>
          ))}
        </div>
      </div>

      {/* Top-right offset (navigator sits TOP_RIGHT): colour chip */}
      <div style={{ position: "absolute", top: 8, right: 8, zIndex: 5 }}>
        <span style={chipStyle} title={t("colorManagedHint")}>
          {t("colorManaged")}
        </span>
      </div>

      {/* Bottom-left: scale bar (or an uncalibrated warning for gross photos) */}
      <div style={{ position: "absolute", bottom: 12, left: 12, zIndex: 5 }}>
        {scaleBar ? (
          <div style={{ display: "flex", flexDirection: "column", gap: 2, alignItems: "center" }}>
            <span style={{ ...chipStyle, background: "transparent", padding: 0 }}>
              {scaleBar.label}
            </span>
            <div
              style={{
                width: Math.round(scaleBar.screenPx),
                height: 4,
                background: "#f5f5f4",
                border: "1px solid rgba(0,0,0,0.6)",
                borderRadius: 1,
              }}
            />
          </div>
        ) : uncalibrated ? (
          <span style={{ ...chipStyle, background: "rgba(180,120,20,0.85)" }}>
            {t("uncalibrated")}
          </span>
        ) : null}
      </div>
    </>
  );
}
