"use client";

// Agenda view: a flat, day-grouped list of occurrences. Used as the
// mobile default (forced under 768px) and as a desktop alternative
// when the user wants a print-friendly linear view.

import { useTranslations } from "next-intl";
import { useMemo } from "react";

import EventCell from "@/components/calendar/EventCell";
import type { CalendarOccurrence } from "@/lib/api_records";

interface Props {
  occurrences: CalendarOccurrence[];
  onSelect: (occ: CalendarOccurrence) => void;
  patientLabelFor?: (occ: CalendarOccurrence) => string | undefined;
}

export default function CalendarAgenda({ occurrences, onSelect, patientLabelFor }: Props) {
  const t = useTranslations("calendar");
  const groups = useMemo(() => groupByDay(occurrences), [occurrences]);

  if (groups.length === 0) {
    return (
      <p
        style={{
          padding: "1rem",
          color: "var(--bv-fg-soft)",
          textAlign: "center",
          border: "1px dashed var(--bv-card-border, #e5e7eb)",
          borderRadius: 6,
        }}
      >
        {t("agendaEmpty")}
      </p>
    );
  }

  return (
    <ol
      style={{
        listStyle: "none",
        padding: 0,
        margin: 0,
        display: "flex",
        flexDirection: "column",
        gap: "0.6rem",
      }}
    >
      {groups.map((g) => (
        <li key={g.key}>
          <h3
            style={{
              fontSize: "0.78rem",
              textTransform: "uppercase",
              letterSpacing: "0.04em",
              color: "var(--bv-fg-soft)",
              margin: "0 0 0.25rem 0",
              borderBottom: "1px solid var(--bv-card-border, #e5e7eb)",
              paddingBottom: 2,
            }}
          >
            {new Intl.DateTimeFormat(undefined, {
              weekday: "long",
              day: "numeric",
              month: "long",
            }).format(g.date)}
          </h3>
          <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            {g.items.map((occ) => (
              <EventCell
                key={occ.event_id}
                occurrence={occ}
                onActivate={onSelect}
                patientLabel={patientLabelFor?.(occ)}
              />
            ))}
          </div>
        </li>
      ))}
    </ol>
  );
}

function groupByDay(occs: CalendarOccurrence[]): {
  key: string;
  date: Date;
  items: CalendarOccurrence[];
}[] {
  const map = new Map<string, { date: Date; items: CalendarOccurrence[] }>();
  for (const occ of occs) {
    const iso = occ.occurrence_dt_start;
    if (!iso) continue;
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) continue;
    const key = `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`;
    const entry = map.get(key);
    if (entry) entry.items.push(occ);
    else
      map.set(key, {
        date: new Date(d.getFullYear(), d.getMonth(), d.getDate()),
        items: [occ],
      });
  }
  const out = Array.from(map.entries()).map(([key, value]) => ({
    key,
    date: value.date,
    items: value.items.sort((a, b) =>
      (a.occurrence_dt_start ?? "").localeCompare(b.occurrence_dt_start ?? ""),
    ),
  }));
  out.sort((a, b) => a.date.getTime() - b.date.getTime());
  return out;
}
