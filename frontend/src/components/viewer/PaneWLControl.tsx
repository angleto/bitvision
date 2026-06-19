"use client";

// Per-pane window/level control for the multiphase contrast grid: a clinical
// W/L preset dropdown (region-aware, from ``modalityDefaults``) plus Auto /
// Reset / Invert. It owns NO viewport handle — every action is delegated to
// the parent via callbacks that resolve the live pane handle(s) at click time
// (and honour the "Link W/L" toggle). Lifting a handle into a prop made it go
// stale ("preset selected but nothing changes"), so the control is now purely
// declarative.

import { computeAutoWL, modalityDefaults } from "@/lib/windowing";
import { useTranslations } from "next-intl";

const SELECT_STYLE: React.CSSProperties = {
  background: "#1a1f2b",
  color: "#e6ecf3",
  border: "1px solid #2a2f3b",
  borderRadius: 3,
  fontSize: "0.7rem",
  padding: "1px 4px",
  maxWidth: 150,
};
const BTN_STYLE: React.CSSProperties = { fontSize: "0.66rem", padding: "0.12rem 0.36rem" };

export interface PaneWLControlProps {
  modality?: string | null;
  bodyPart?: string | null;
  /** Voxel scalars for the "Auto" histogram window. */
  scalars?: Float32Array | null;
  /** Apply a window (preset / Auto). The parent routes it to this pane, or to
   *  every pane when "Link W/L" is on. */
  onApplyWL: (wc: number, ww: number) => void;
  onReset: () => void;
  onInvert: () => void;
}

export default function PaneWLControl({
  modality,
  bodyPart,
  scalars,
  onApplyWL,
  onReset,
  onInvert,
}: PaneWLControlProps) {
  const t = useTranslations("contrast");
  const presets = modalityDefaults(modality ?? "", bodyPart ?? undefined);

  return (
    <div style={{ display: "inline-flex", alignItems: "center", gap: 4, flexWrap: "wrap" }}>
      {presets.length > 0 && (
        <select
          value=""
          onChange={(e) => {
            const p = presets[Number(e.target.value)];
            if (p) onApplyWL(p.wc, p.ww);
          }}
          style={SELECT_STYLE}
          title={t("wlPresetTitle")}
        >
          <option value="">{t("wlPreset")}</option>
          {presets.map((p, i) => (
            <option key={p.label} value={i}>
              {p.label} ({p.wc}/{p.ww})
            </option>
          ))}
        </select>
      )}
      <button
        type="button"
        className="viewer-btn"
        style={BTN_STYLE}
        title={t("wlAutoTitle")}
        disabled={!scalars}
        onClick={() => {
          if (!scalars) return;
          const { wc, ww } = computeAutoWL(scalars, modality ?? undefined);
          onApplyWL(wc, ww);
        }}
      >
        {t("wlAuto")}
      </button>
      <button type="button" className="viewer-btn" style={BTN_STYLE} onClick={onReset}>
        {t("wlReset")}
      </button>
      <button
        type="button"
        className="viewer-btn"
        style={BTN_STYLE}
        title={t("wlInvert")}
        onClick={onInvert}
      >
        {t("wlInvert")}
      </button>
    </div>
  );
}
