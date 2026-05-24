"use client";

// Compact chip rendering for a single occurrence on the calendar grid.
// Used by TimeGridView (day / week), MonthView and AgendaView.
//
// A11y: the cell is keyboard-focusable, exposes a descriptive aria-label
// combining time + title + status (so a screen-reader user can tell the
// state without inspecting visual badges), and reacts to Enter / Space
// to open the EventDrawer.

import { useTranslations } from "next-intl";
import type { CSSProperties } from "react";

import type { CalendarOccurrence } from "@/lib/api_records";
import { eventStatusStyle } from "@/lib/event_status_style";

interface Props {
  occurrence: CalendarOccurrence;
  onActivate: (occ: CalendarOccurrence) => void;
  // When true, show the patient name as a prefix. Used in the global
  // /calendar aggregator view.
  patientLabel?: string;
  // Compact: month-view cells are tight on space; we elide the time
  // and shrink the title.
  compact?: boolean;
}

function fmtTime(iso: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

export default function EventCell({
  occurrence,
  onActivate,
  patientLabel,
  compact = false,
}: Props) {
  const t = useTranslations("eventStatus");
  const sStyle = eventStatusStyle(occurrence.event_status);
  const time = fmtTime(occurrence.occurrence_dt_start);
  const ariaLabel = [
    time || "",
    patientLabel ?? "",
    occurrence.title,
    t(`${occurrence.event_status}_aria`),
  ]
    .filter(Boolean)
    .join(" — ");
  const cellStyle: CSSProperties = {
    display: "flex",
    flexDirection: "column",
    gap: "1px",
    padding: compact ? "1px 4px" : "3px 6px",
    borderRadius: 4,
    fontSize: compact ? "0.7rem" : "0.78rem",
    cursor: "pointer",
    overflow: "hidden",
    textOverflow: "ellipsis",
    whiteSpace: "nowrap",
    minWidth: 0,
    outline: "none",
    ...sStyle.containerStyle,
  };
  return (
    <button
      type="button"
      role="gridcell"
      aria-label={ariaLabel}
      onClick={() => onActivate(occurrence)}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onActivate(occurrence);
        }
      }}
      style={cellStyle}
      data-event-status={occurrence.event_status}
      data-event-id={occurrence.event_id}
    >
      <span
        style={{
          display: "flex",
          alignItems: "center",
          gap: 4,
          color: "var(--bv-fg)",
          textDecoration: sStyle.titleStrike ? "line-through" : "none",
          overflow: "hidden",
          textOverflow: "ellipsis",
          whiteSpace: "nowrap",
        }}
      >
        {sStyle.glyph && (
          <span aria-hidden style={{ color: sStyle.badgeFg, fontSize: "0.85em" }}>
            {sStyle.glyph}
          </span>
        )}
        {time && (
          <span
            style={{
              color: "var(--bv-fg-soft)",
              fontVariantNumeric: "tabular-nums",
              fontSize: "0.85em",
            }}
          >
            {time}
          </span>
        )}
        <span
          style={{
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: "nowrap",
            fontWeight: 500,
          }}
        >
          {patientLabel ? (
            <span style={{ color: "var(--bv-fg-soft)" }}>{patientLabel}: </span>
          ) : null}
          {occurrence.title}
        </span>
      </span>
    </button>
  );
}
