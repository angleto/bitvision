"use client";

// Per-pane window/level control for the multiphase contrast grid: a clinical
// W/L preset dropdown (region-aware, from ``modalityDefaults``) plus Auto /
// Reset / Invert. Pure glue over ``lib/windowing`` and the MPRLayoutHandle —
// the same building blocks the single-series and multi viewers already use.

import type { MPRLayoutHandle } from "@/components/MPRLayoutTypes";
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
  handle: MPRLayoutHandle | null;
  modality?: string | null;
  bodyPart?: string | null;
  /** Voxel scalars for the "Auto" histogram window. */
  scalars?: Float32Array | null;
  /** When provided (linked-W/L mode), preset/Auto choices route here so the
   *  page can broadcast them to every pane. Absent = apply to this pane only. */
  onApply?: (wc: number, ww: number) => void;
}

export default function PaneWLControl({
  handle,
  modality,
  bodyPart,
  scalars,
  onApply,
}: PaneWLControlProps) {
  const t = useTranslations("contrast");
  const presets = modalityDefaults(modality ?? "", bodyPart ?? undefined);
  const apply = (wc: number, ww: number) => {
    if (onApply) onApply(wc, ww);
    else {
      handle?.setWC(wc);
      handle?.setWW(ww);
    }
  };

  return (
    <div style={{ display: "inline-flex", alignItems: "center", gap: 4, flexWrap: "wrap" }}>
      {presets.length > 0 && (
        <select
          value=""
          onChange={(e) => {
            const p = presets[Number(e.target.value)];
            if (p) apply(p.wc, p.ww);
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
          apply(wc, ww);
        }}
      >
        {t("wlAuto")}
      </button>
      <button
        type="button"
        className="viewer-btn"
        style={BTN_STYLE}
        onClick={() => handle?.resetWL()}
      >
        {t("wlReset")}
      </button>
      <button
        type="button"
        className="viewer-btn"
        style={BTN_STYLE}
        title={t("wlInvert")}
        onClick={() => handle?.setInvert(!handle.invert)}
      >
        {t("wlInvert")}
      </button>
    </div>
  );
}
