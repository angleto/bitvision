// Shared visual style per ``event_status``. Extracted from
// TimelineEventDot so both the timeline dot and the calendar grid
// EventCell reuse the same rules (single source-of-truth for the
// look-and-feel of a planned/confirmed/cancelled/etc. event chip).
//
// WCAG-AA: every status carries an icon glyph + a localisable label
// in addition to the colour, so users with deuteranopia / monochrome
// printers can still distinguish states.

import type { CSSProperties } from "react";

import type { EventStatus } from "@/lib/api_records";

export interface StatusStyle {
  containerStyle: CSSProperties;
  badgeBg: string;
  badgeFg: string;
  glyph: string;
  titleStrike: boolean;
}

export function eventStatusStyle(status: EventStatus): StatusStyle {
  switch (status) {
    case "planned":
      return {
        containerStyle: {
          border: "1px dashed var(--bv-status-planned-border, #5b8def)",
          background: "var(--bv-status-planned-bg, rgba(91,141,239,0.06))",
          opacity: 0.92,
        },
        badgeBg: "var(--bv-status-planned-bg, rgba(91,141,239,0.12))",
        badgeFg: "var(--bv-status-planned-border, #2557d6)",
        glyph: "⏳",
        titleStrike: false,
      };
    case "confirmed":
      return {
        containerStyle: {
          border: "1px solid var(--bv-status-confirmed-border, #1e8e3e)",
          background: "var(--bv-status-confirmed-bg, rgba(30,142,62,0.07))",
        },
        badgeBg: "var(--bv-status-confirmed-bg, rgba(30,142,62,0.12))",
        badgeFg: "var(--bv-status-confirmed-border, #146b2d)",
        glyph: "✓",
        titleStrike: false,
      };
    case "cancelled":
      return {
        containerStyle: {
          border: "1px solid var(--bv-status-cancelled-border, #9ca3af)",
          background: "var(--bv-status-cancelled-bg, rgba(156,163,175,0.10))",
          opacity: 0.75,
        },
        badgeBg: "var(--bv-status-cancelled-bg, rgba(156,163,175,0.20))",
        badgeFg: "var(--bv-status-cancelled-border, #4b5563)",
        glyph: "⊘",
        titleStrike: true,
      };
    case "missed":
      return {
        containerStyle: {
          border: "1px solid var(--bv-status-missed-border, #d97706)",
          background: "var(--bv-status-missed-bg, rgba(217,119,6,0.08))",
        },
        badgeBg: "var(--bv-status-missed-bg, rgba(217,119,6,0.18))",
        badgeFg: "var(--bv-status-missed-border, #9a5b04)",
        glyph: "△",
        titleStrike: false,
      };
    case "rescheduled":
      return {
        containerStyle: {
          border: "1px dashed var(--bv-status-cancelled-border, #9ca3af)",
          background: "transparent",
          opacity: 0.7,
        },
        badgeBg: "transparent",
        badgeFg: "var(--bv-fg-soft)",
        glyph: "↻",
        titleStrike: true,
      };
    default:
      // ``completed`` — neutral. Mirrors the historical render so a
      // pre-0098 timeline looks unchanged.
      return {
        containerStyle: {
          border: "1px solid var(--bv-card-border, #e5e7eb)",
          background: "var(--bv-card-bg, #fff)",
        },
        badgeBg: "transparent",
        badgeFg: "var(--bv-fg-soft)",
        glyph: "",
        titleStrike: false,
      };
  }
}
