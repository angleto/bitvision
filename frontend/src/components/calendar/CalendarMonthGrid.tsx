"use client";

// Month view: 6-row x 7-col grid of date cells. Each cell shows up to
// three events; overflow rendered as "+N altri" link that opens the
// day-view focused on that date (handled by the parent through
// onSelectDate).

import { useMemo } from "react";

import EventCell from "@/components/calendar/EventCell";
import type { CalendarOccurrence } from "@/lib/api_records";

interface Props {
  anchorDate: Date; // any date in the month to render
  occurrences: CalendarOccurrence[];
  onSelect: (occ: CalendarOccurrence) => void;
  onSelectDate: (d: Date) => void;
  patientLabelFor?: (occ: CalendarOccurrence) => string | undefined;
}

const MAX_VISIBLE_PER_CELL = 3;

export default function CalendarMonthGrid({
  anchorDate,
  occurrences,
  onSelect,
  onSelectDate,
  patientLabelFor,
}: Props) {
  const days = useMemo(() => buildMonthGrid(anchorDate), [anchorDate]);
  const byDay = useMemo(() => groupByDay(occurrences), [occurrences]);
  const monthIdx = anchorDate.getMonth();

  return (
    <div
      role="grid"
      aria-label={new Intl.DateTimeFormat(undefined, { month: "long", year: "numeric" }).format(
        anchorDate,
      )}
      style={{
        display: "grid",
        gridTemplateColumns: "repeat(7, minmax(0, 1fr))",
        gridAutoRows: "minmax(96px, auto)",
        gap: 0,
        border: "1px solid var(--bv-card-border, #e5e7eb)",
        borderRadius: 6,
        overflow: "hidden",
        background: "var(--bv-card-bg, #fff)",
      }}
    >
      {/* weekday headers — Monday-first */}
      {WEEKDAYS_MONDAY_FIRST.map((dow, i) => (
        <div
          key={dow}
          role="columnheader"
          style={{
            padding: "4px 6px",
            fontSize: "0.7rem",
            textTransform: "uppercase",
            color: "var(--bv-fg-soft)",
            borderBottom: "1px solid var(--bv-card-border, #e5e7eb)",
            borderLeft: i > 0 ? "1px solid var(--bv-card-border, #e5e7eb)" : "none",
            background: "var(--bv-card-bg-soft, #f9fafb)",
          }}
        >
          {new Intl.DateTimeFormat(undefined, { weekday: "short" }).format(REF_WEEK[dow])}
        </div>
      ))}
      {days.map((d) => {
        const key = dateKey(d);
        const list = byDay.get(key) ?? [];
        const visible = list.slice(0, MAX_VISIBLE_PER_CELL);
        const overflow = list.length - visible.length;
        const inMonth = d.getMonth() === monthIdx;
        const today = isToday(d);
        return (
          <div
            key={key}
            role="gridcell"
            style={{
              display: "flex",
              flexDirection: "column",
              gap: 2,
              padding: "4px",
              borderTop: "1px solid var(--bv-card-border, #e5e7eb)",
              borderLeft: "1px solid var(--bv-card-border, #e5e7eb)",
              background: today
                ? "var(--bv-accent-soft, rgba(79,70,229,0.04))"
                : inMonth
                  ? "transparent"
                  : "var(--bv-card-bg-soft, #fafafa)",
              opacity: inMonth ? 1 : 0.55,
              minHeight: 96,
            }}
          >
            <button
              type="button"
              className="ghost"
              onClick={() => onSelectDate(d)}
              style={{
                fontSize: "0.78rem",
                fontWeight: today ? 700 : 500,
                color: today ? "var(--bv-accent, #4f46e5)" : "var(--bv-fg)",
                alignSelf: "flex-start",
                padding: "0 4px",
                borderRadius: 999,
              }}
              aria-label={d.toDateString()}
            >
              {d.getDate()}
            </button>
            {visible.map((occ) => (
              <EventCell
                key={occ.event_id}
                occurrence={occ}
                onActivate={onSelect}
                patientLabel={patientLabelFor?.(occ)}
                compact
              />
            ))}
            {overflow > 0 && (
              <button
                type="button"
                className="ghost"
                onClick={() => onSelectDate(d)}
                style={{ fontSize: "0.7rem", padding: "0 4px", alignSelf: "flex-start" }}
              >
                +{overflow}
              </button>
            )}
          </div>
        );
      })}
    </div>
  );
}

const WEEKDAYS_MONDAY_FIRST = [0, 1, 2, 3, 4, 5, 6] as const;
// Reference dates Mon..Sun for Intl.DateTimeFormat to derive locale-aware
// weekday names (any Monday will do; we pick a recent one).
const REF_WEEK: readonly Date[] = (() => {
  const out: Date[] = [];
  // 2024-01-01 was a Monday.
  for (let i = 0; i < 7; i++) {
    const d = new Date(Date.UTC(2024, 0, 1 + i));
    out.push(d);
  }
  return out;
})();

function buildMonthGrid(anchor: Date): Date[] {
  const first = new Date(anchor.getFullYear(), anchor.getMonth(), 1);
  const dow = (first.getDay() + 6) % 7; // 0 = Monday
  const start = new Date(first);
  start.setDate(first.getDate() - dow);
  start.setHours(0, 0, 0, 0);
  const out: Date[] = [];
  for (let i = 0; i < 42; i++) {
    const d = new Date(start);
    d.setDate(start.getDate() + i);
    out.push(d);
  }
  return out;
}

function dateKey(d: Date): string {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

function groupByDay(occs: CalendarOccurrence[]): Map<string, CalendarOccurrence[]> {
  const out = new Map<string, CalendarOccurrence[]>();
  for (const occ of occs) {
    const iso = occ.occurrence_dt_start;
    if (!iso) continue;
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) continue;
    const k = dateKey(d);
    const arr = out.get(k);
    if (arr) arr.push(occ);
    else out.set(k, [occ]);
  }
  for (const arr of out.values()) {
    arr.sort((a, b) => (a.occurrence_dt_start ?? "").localeCompare(b.occurrence_dt_start ?? ""));
  }
  return out;
}

function isToday(d: Date): boolean {
  const t = new Date();
  return (
    t.getFullYear() === d.getFullYear() &&
    t.getMonth() === d.getMonth() &&
    t.getDate() === d.getDate()
  );
}
