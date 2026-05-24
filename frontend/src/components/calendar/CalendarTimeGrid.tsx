"use client";

// Time-grid view for day (1 column) and week (7 columns). Renders an
// hourly ruler on the left and one column per day. Each occurrence is
// placed at its start hour and given a height proportional to its
// duration (clamped to 1 hour minimum so very short events stay
// readable).
//
// All-day events (no occurrence_dt_start, only event_date) and events
// straddling midnight are rendered in a single row at the top of each
// day column ("All day" band) — keeps the hourly grid uncluttered.
//
// A11y: the outer container is ``role="grid"`` with day headers as
// ``role="columnheader"`` and event cells as ``role="gridcell"``
// (via EventCell).

import type { CSSProperties } from "react";
import { useMemo } from "react";

import EventCell from "@/components/calendar/EventCell";
import type { CalendarOccurrence } from "@/lib/api_records";

interface Props {
  days: number; // 1 = day view, 7 = week view
  anchorDate: Date; // start day for the visible range
  occurrences: CalendarOccurrence[];
  onSelect: (occ: CalendarOccurrence) => void;
  patientLabelFor?: (occ: CalendarOccurrence) => string | undefined;
  // First and last hour shown on the grid. Defaults to 7..21.
  startHour?: number;
  endHour?: number;
}

const ROW_HEIGHT = 36;
const DAY_HEADER_HEIGHT = 36;

export default function CalendarTimeGrid({
  days,
  anchorDate,
  occurrences,
  onSelect,
  patientLabelFor,
  startHour = 7,
  endHour = 21,
}: Props) {
  // Compute the array of day-starts spanning the visible range.
  const dayStarts = useMemo(() => {
    const out: Date[] = [];
    const start = new Date(anchorDate);
    start.setHours(0, 0, 0, 0);
    for (let i = 0; i < days; i++) {
      const d = new Date(start);
      d.setDate(d.getDate() + i);
      out.push(d);
    }
    return out;
  }, [anchorDate, days]);

  // Bucket occurrences per day index (-1 for all-day rows that have
  // event_date only or no start at all).
  const buckets = useMemo(() => {
    const map = new Map<number, { allDay: CalendarOccurrence[]; timed: CalendarOccurrence[] }>();
    for (let i = 0; i < days; i++) map.set(i, { allDay: [], timed: [] });
    for (const occ of occurrences) {
      const dayIdx = occ.occurrence_dt_start ? dayIndexOf(occ.occurrence_dt_start, dayStarts) : -1;
      if (dayIdx < 0) continue;
      const bucket = map.get(dayIdx);
      if (!bucket) continue;
      if (!occ.occurrence_dt_start) bucket.allDay.push(occ);
      else bucket.timed.push(occ);
    }
    return map;
  }, [occurrences, dayStarts, days]);

  const hourLabels = useMemo(() => {
    const out: number[] = [];
    for (let h = startHour; h <= endHour; h++) out.push(h);
    return out;
  }, [startHour, endHour]);

  const gridStyle: CSSProperties = {
    display: "grid",
    gridTemplateColumns: `48px repeat(${days}, minmax(0, 1fr))`,
    gridAutoRows: `${ROW_HEIGHT}px`,
    border: "1px solid var(--bv-card-border, #e5e7eb)",
    borderRadius: 6,
    overflow: "hidden",
    background: "var(--bv-card-bg, #fff)",
  };

  return (
    <div role="grid" style={gridStyle}>
      {/* corner cell */}
      <div
        role="columnheader"
        style={{
          gridColumn: 1,
          gridRow: 1,
          height: DAY_HEADER_HEIGHT,
          borderBottom: "1px solid var(--bv-card-border, #e5e7eb)",
        }}
        aria-hidden
      />
      {/* day headers */}
      {dayStarts.map((d, i) => (
        <div
          key={i}
          role="columnheader"
          aria-label={d.toDateString()}
          style={{
            gridColumn: i + 2,
            gridRow: 1,
            height: DAY_HEADER_HEIGHT,
            display: "flex",
            flexDirection: "column",
            alignItems: "center",
            justifyContent: "center",
            fontSize: "0.78rem",
            fontWeight: 600,
            borderBottom: "1px solid var(--bv-card-border, #e5e7eb)",
            borderLeft: i > 0 ? "1px solid var(--bv-card-border, #e5e7eb)" : "none",
            color: isToday(d) ? "var(--bv-accent, #4f46e5)" : "var(--bv-fg)",
            background: isToday(d) ? "var(--bv-accent-soft, rgba(79,70,229,0.06))" : "transparent",
          }}
        >
          <span style={{ textTransform: "uppercase", fontSize: "0.7rem", opacity: 0.8 }}>
            {new Intl.DateTimeFormat(undefined, { weekday: "short" }).format(d)}
          </span>
          <span style={{ fontVariantNumeric: "tabular-nums" }}>{d.getDate()}</span>
        </div>
      ))}
      {/* all-day band (one row right under the headers) */}
      <div
        style={{
          gridColumn: 1,
          gridRow: 2,
          fontSize: "0.65rem",
          color: "var(--bv-fg-soft)",
          padding: "4px",
          borderBottom: "1px dashed var(--bv-card-border, #e5e7eb)",
          textAlign: "right",
        }}
      >
        all-day
      </div>
      {dayStarts.map((_d, i) => {
        const bucket = buckets.get(i);
        return (
          <div
            key={`all-${i}`}
            style={{
              gridColumn: i + 2,
              gridRow: 2,
              display: "flex",
              flexDirection: "column",
              gap: 2,
              padding: 2,
              borderBottom: "1px dashed var(--bv-card-border, #e5e7eb)",
              borderLeft: i > 0 ? "1px solid var(--bv-card-border, #e5e7eb)" : "none",
              minHeight: ROW_HEIGHT,
            }}
          >
            {bucket?.allDay.map((occ) => (
              <EventCell
                key={occ.event_id}
                occurrence={occ}
                onActivate={onSelect}
                patientLabel={patientLabelFor?.(occ)}
                compact
              />
            ))}
          </div>
        );
      })}
      {/* hourly ruler */}
      {hourLabels.map((h, idx) => (
        <div
          key={h}
          style={{
            gridColumn: 1,
            gridRow: idx + 3,
            fontSize: "0.7rem",
            color: "var(--bv-fg-soft)",
            padding: "2px 4px",
            textAlign: "right",
            fontVariantNumeric: "tabular-nums",
            borderTop: "1px solid var(--bv-card-border, #f3f4f6)",
          }}
        >
          {String(h).padStart(2, "0")}:00
        </div>
      ))}
      {/* day columns with timed events */}
      {dayStarts.map((dayStart, dayIdx) => {
        const bucket = buckets.get(dayIdx);
        return (
          <div
            key={`col-${dayIdx}`}
            role="row"
            style={{
              gridColumn: dayIdx + 2,
              gridRow: `3 / span ${hourLabels.length}`,
              position: "relative",
              borderLeft: dayIdx > 0 ? "1px solid var(--bv-card-border, #e5e7eb)" : "none",
              backgroundImage: `repeating-linear-gradient(to bottom, transparent 0, transparent ${ROW_HEIGHT - 1}px, var(--bv-card-border, #f3f4f6) ${ROW_HEIGHT - 1}px, var(--bv-card-border, #f3f4f6) ${ROW_HEIGHT}px)`,
            }}
          >
            {bucket?.timed.map((occ) => {
              const placement = computePlacement(occ, dayStart, startHour, endHour);
              if (!placement) return null;
              return (
                <div
                  key={occ.event_id}
                  style={{
                    position: "absolute",
                    top: placement.topPx,
                    left: 2,
                    right: 2,
                    height: placement.heightPx,
                    overflow: "hidden",
                  }}
                >
                  <EventCell
                    occurrence={occ}
                    onActivate={onSelect}
                    patientLabel={patientLabelFor?.(occ)}
                  />
                </div>
              );
            })}
          </div>
        );
      })}
    </div>
  );
}

function dayIndexOf(iso: string, dayStarts: Date[]): number {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return -1;
  const ds = new Date(d);
  ds.setHours(0, 0, 0, 0);
  for (let i = 0; i < dayStarts.length; i++) {
    if (dayStarts[i].getTime() === ds.getTime()) return i;
  }
  return -1;
}

function isToday(d: Date): boolean {
  const t = new Date();
  return (
    t.getFullYear() === d.getFullYear() &&
    t.getMonth() === d.getMonth() &&
    t.getDate() === d.getDate()
  );
}

function computePlacement(
  occ: CalendarOccurrence,
  dayStart: Date,
  startHour: number,
  endHour: number,
): { topPx: number; heightPx: number } | null {
  if (!occ.occurrence_dt_start) return null;
  const start = new Date(occ.occurrence_dt_start);
  if (Number.isNaN(start.getTime())) return null;
  const end = occ.occurrence_dt_end
    ? new Date(occ.occurrence_dt_end)
    : new Date(start.getTime() + 60 * 60 * 1000); // default 1h
  const hoursFromStart = (start.getTime() - dayStart.getTime()) / 3_600_000 - startHour;
  const durHours = Math.max(0.5, (end.getTime() - start.getTime()) / 3_600_000);
  const visibleHours = endHour - startHour + 1;
  if (hoursFromStart >= visibleHours || hoursFromStart + durHours <= 0) return null;
  const clampedStart = Math.max(0, hoursFromStart);
  const clampedDur = Math.min(durHours, visibleHours - clampedStart);
  return {
    topPx: Math.round(clampedStart * ROW_HEIGHT),
    heightPx: Math.max(20, Math.round(clampedDur * ROW_HEIGHT) - 2),
  };
}
