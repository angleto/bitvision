"use client";

// Per-pane contrast-phase chip for the multiphase viewer: shows the
// classified phase, its source (auto vs human-confirmed) and confidence,
// and lets the radiologist confirm a low-confidence candidate or correct
// the phase outright. Named ContrastPhaseTab (NOT PhaseChip) to stay
// disjoint from the care-timeline phase concept.

import type { SeriesPhase } from "@/lib/api";
import { useTranslations } from "next-intl";

// Mirrors backend db.models.dicom.ACQUISITION_PHASES (display order).
const ACQUISITION_PHASES = [
  "unenhanced",
  "arterial",
  "portal_venous",
  "delayed",
  "hepatobiliary",
  "corticomedullary",
  "nephrographic",
  "excretory",
  "dynamic",
  "other",
] as const;

export interface ContrastPhaseTabProps {
  phase: SeriesPhase;
  /** Persist a human override (null clears + re-enables auto). */
  onSet: (acquisitionPhase: string | null) => void;
  busy?: boolean;
}

export default function ContrastPhaseTab({ phase, onSet, busy }: ContrastPhaseTabProps) {
  const t = useTranslations("contrast");
  const label = phase.acquisition_phase ? t(`phase.${phase.acquisition_phase}`) : t("unclassified");
  const confirm = phase.needs_confirmation;
  const accent = confirm
    ? "var(--bv-warning, #b45309)"
    : phase.phase_source === "human"
      ? "var(--bv-success, #047857)"
      : "#475569";

  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: 6,
        minWidth: 0,
        flexWrap: "wrap",
      }}
    >
      <span
        className="badge"
        style={{ color: accent, borderColor: accent, whiteSpace: "nowrap" }}
        title={
          phase.phase_confidence != null
            ? `${Math.round(phase.phase_confidence * 100)}%`
            : undefined
        }
      >
        {label}
        {phase.phase_source === "auto" && phase.phase_confidence != null && (
          <span style={{ opacity: 0.7 }}> · {Math.round(phase.phase_confidence * 100)}%</span>
        )}
        {phase.phase_source === "human" && (
          <span style={{ opacity: 0.7 }}> · {t("sourceHuman")}</span>
        )}
      </span>

      {confirm && (
        <button
          type="button"
          className="ghost"
          disabled={busy}
          title={t("confirmHint")}
          onClick={() => onSet(phase.acquisition_phase)}
          style={{ fontSize: "0.72rem", padding: "1px 6px" }}
        >
          {t("confirm")}
        </button>
      )}

      <select
        aria-label={t("title")}
        disabled={busy}
        value={phase.acquisition_phase ?? ""}
        onChange={(e) => onSet(e.target.value === "" ? null : e.target.value)}
        style={{
          minWidth: 0,
          background: "#0b0e13",
          color: "#e6ecf3",
          border: "1px solid #1a1f2b",
          borderRadius: 6,
          padding: "1px 4px",
          fontSize: "0.72rem",
        }}
      >
        <option value="">{t("unclassified")}</option>
        {ACQUISITION_PHASES.map((p) => (
          <option key={p} value={p}>
            {t(`phase.${p}`)}
          </option>
        ))}
      </select>
    </div>
  );
}
