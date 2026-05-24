"use client";

// Colored phase chip. Reproduces the visual of
// ``timeline_fascicolo_canary_patient.svg``: a coloured rectangle on
// the left of the timeline carrying the phase name, kind icon and event
// count. The chip header navigates to the phase detail page;
// the chevron toggles in-place expansion of the contained dots.

import { useTranslations } from "next-intl";

import type { CarePhase, CarePhaseKind } from "@/lib/api_records";

const KIND_ICON: Record<string, string> = {
  imaging: "◫",
  surgery: "✚",
  followup: "↻",
  surveillance: "◉",
  visit: "♡",
  reassessment: "★",
  other: "◇",
};

interface Props {
  phase: CarePhase;
  expanded: boolean;
  onToggle: () => void;
  onNavigate: () => void;
  /** Render as a button instead of a link wrapper (used by the editor
   *  overlay so dnd-kit can intercept the press without nav). */
  noNavigate?: boolean;
}

/**
 * Pure-presentational phase chip. WCAG-friendly: colour is conveyed
 * also by the kind icon and count. Click on the chip body navigates;
 * click on the chevron toggles expand without navigation.
 */
export default function PhaseChip({
  phase,
  expanded,
  onToggle,
  onNavigate,
  noNavigate = false,
}: Props) {
  const t = useTranslations("phaseChip");
  const icon = KIND_ICON[phase.kind] ?? KIND_ICON.other;
  const studiesPart = phase.counts.n_studies
    ? t("studiesSuffix", { n: phase.counts.n_studies })
    : "";
  // Soft tint background, full-saturation accent border. Text colour
  // stays --bv-fg so dark/light themes remain readable; the colour
  // hex shows up as the left bar + icon stripe.
  return (
    <div
      className="phase-chip"
      data-phase-slug={phase.slug}
      style={{
        position: "relative",
        display: "flex",
        alignItems: "stretch",
        borderRadius: 8,
        background: `color-mix(in srgb, ${phase.color_hex} 8%, var(--bv-card-bg))`,
        border: `1px solid color-mix(in srgb, ${phase.color_hex} 35%, var(--bv-card-border))`,
        overflow: "hidden",
        minHeight: 64,
      }}
    >
      <div
        style={{
          width: 6,
          background: phase.color_hex,
          flexShrink: 0,
        }}
        aria-hidden
      />
      <button
        type="button"
        onClick={noNavigate ? onToggle : onNavigate}
        aria-label={t("openPhase", { name: phase.name })}
        style={{
          flex: 1,
          display: "flex",
          alignItems: "center",
          gap: "0.6rem",
          padding: "0.5rem 0.7rem",
          background: "transparent",
          border: "none",
          textAlign: "left",
          cursor: "pointer",
          color: "var(--bv-fg)",
          minWidth: 0,
        }}
      >
        <span
          aria-hidden
          style={{
            display: "inline-flex",
            alignItems: "center",
            justifyContent: "center",
            width: 28,
            height: 28,
            borderRadius: "50%",
            background: phase.color_hex,
            color: "#fff",
            fontSize: "0.95rem",
            fontWeight: 600,
            flexShrink: 0,
          }}
        >
          {icon}
        </span>
        <span style={{ display: "flex", flexDirection: "column", minWidth: 0 }}>
          <span
            style={{
              fontSize: "0.85rem",
              fontWeight: 600,
              overflow: "hidden",
              textOverflow: "ellipsis",
              whiteSpace: "nowrap",
            }}
          >
            {phase.name}
          </span>
          <span
            style={{
              fontSize: "0.72rem",
              color: "var(--bv-fg-soft)",
            }}
          >
            {t("eventsAndStudies", {
              events: phase.counts.n_events,
              studiesPart,
            })}
          </span>
        </span>
      </button>
      <button
        type="button"
        onClick={(e) => {
          e.stopPropagation();
          onToggle();
        }}
        aria-expanded={expanded}
        aria-label={expanded ? t("collapse") : t("expand")}
        title={expanded ? t("collapseShort") : t("expandShort")}
        className="ghost"
        style={{
          width: 32,
          padding: 0,
          background: "transparent",
          border: "none",
          color: "var(--bv-fg-soft)",
          cursor: "pointer",
          fontSize: "0.9rem",
          flexShrink: 0,
        }}
      >
        {expanded ? "▴" : "▾"}
      </button>
    </div>
  );
}

export function phaseKindIcon(kind: CarePhaseKind | string): string {
  return KIND_ICON[kind] ?? KIND_ICON.other;
}
