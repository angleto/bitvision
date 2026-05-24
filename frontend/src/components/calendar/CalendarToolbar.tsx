"use client";

// Toolbar above any calendar view: prev / today / next, view switcher
// (day / week / month / agenda), status multi-select chips, and the
// "Pianifica visita" button that opens PlanEventDialog.

import { useTranslations } from "next-intl";
import type { CSSProperties } from "react";

import { authedDownload } from "@/lib/api";
import type { EventStatus } from "@/lib/api_records";

export type CalendarView = "day" | "week" | "month" | "agenda";

interface Props {
  view: CalendarView;
  onChangeView: (v: CalendarView) => void;
  // Centre of the visible range; the parent computes the actual
  // [from, to] window from this + view.
  anchorDate: Date;
  onPrev: () => void;
  onToday: () => void;
  onNext: () => void;
  // Multi-select status filter. Empty array means "show all".
  statuses: ReadonlySet<EventStatus>;
  onToggleStatus: (s: EventStatus) => void;
  onPlan: () => void;
  exportIcsUrl?: string;
}

const ALL_STATUSES: readonly EventStatus[] = [
  "planned",
  "confirmed",
  "completed",
  "cancelled",
  "missed",
  "rescheduled",
];

export default function CalendarToolbar({
  view,
  onChangeView,
  anchorDate,
  onPrev,
  onToday,
  onNext,
  statuses,
  onToggleStatus,
  onPlan,
  exportIcsUrl,
}: Props) {
  const t = useTranslations("calendar");
  const tStatus = useTranslations("eventStatus");

  return (
    <div
      style={{
        display: "flex",
        flexWrap: "wrap",
        alignItems: "center",
        gap: "0.4rem 0.6rem",
        padding: "0.4rem 0",
        marginBottom: "0.5rem",
        borderBottom: "1px solid var(--bv-card-border, #e5e7eb)",
      }}
    >
      <div style={{ display: "inline-flex", gap: 4 }}>
        <button
          type="button"
          className="ghost"
          onClick={onPrev}
          aria-label={t("prev")}
          style={iconBtn}
        >
          ‹
        </button>
        <button type="button" className="ghost" onClick={onToday} style={smallBtn}>
          {t("today")}
        </button>
        <button
          type="button"
          className="ghost"
          onClick={onNext}
          aria-label={t("next")}
          style={iconBtn}
        >
          ›
        </button>
      </div>
      <span
        aria-live="polite"
        style={{
          fontWeight: 600,
          fontSize: "0.9rem",
          padding: "0 0.4rem",
        }}
      >
        {formatRange(anchorDate, view, t("rangeMonthLabel"))}
      </span>
      <span style={{ flex: 1 }} />
      <div role="tablist" aria-label={t("viewSwitcher")} style={{ display: "inline-flex", gap: 4 }}>
        {(["day", "week", "month", "agenda"] as CalendarView[]).map((v) => (
          <button
            key={v}
            type="button"
            role="tab"
            aria-selected={view === v}
            onClick={() => onChangeView(v)}
            className={view === v ? undefined : "ghost"}
            style={smallBtn}
          >
            {t(`view_${v}`)}
          </button>
        ))}
      </div>
      <div style={{ display: "inline-flex", flexWrap: "wrap", gap: 4 }}>
        {ALL_STATUSES.map((s) => (
          <button
            key={s}
            type="button"
            className={statuses.has(s) || statuses.size === 0 ? undefined : "ghost"}
            aria-pressed={statuses.has(s)}
            onClick={() => onToggleStatus(s)}
            style={chip}
          >
            {tStatus(s)}
          </button>
        ))}
      </div>
      <span style={{ flex: 1 }} />
      <button type="button" onClick={onPlan} style={primaryBtn}>
        + {t("planEvent")}
      </button>
      {exportIcsUrl && (
        <button
          type="button"
          onClick={() => void authedDownload(exportIcsUrl, "calendar.ics")}
          className="ghost"
          style={{ ...smallBtn, textDecoration: "none" }}
          title={t("exportIcs")}
        >
          ICS
        </button>
      )}
    </div>
  );
}

function formatRange(anchor: Date, view: CalendarView, monthLabel: string): string {
  // ``monthLabel`` is a localised template like "{m} {y}"; we keep it
  // simple and rely on Intl for month names.
  const m = new Intl.DateTimeFormat(undefined, { month: "long" }).format(anchor);
  const y = anchor.getFullYear();
  if (view === "month") return monthLabel.replace("{m}", m).replace("{y}", String(y));
  if (view === "day") {
    return new Intl.DateTimeFormat(undefined, {
      weekday: "long",
      day: "numeric",
      month: "long",
      year: "numeric",
    }).format(anchor);
  }
  // week + agenda: show the week-of label
  const start = startOfWeek(anchor);
  const end = new Date(start);
  end.setDate(end.getDate() + 6);
  const sf = new Intl.DateTimeFormat(undefined, { day: "numeric", month: "short" });
  return `${sf.format(start)} → ${sf.format(end)} ${end.getFullYear()}`;
}

function startOfWeek(d: Date): Date {
  // Locale-aware would be ideal, but week-starts-on-Monday is the
  // pragmatic default for IT/EN bilingual app; if the user is on a
  // Sunday-start locale we can revisit.
  const x = new Date(d);
  const dow = (x.getDay() + 6) % 7; // 0=Monday
  x.setDate(x.getDate() - dow);
  x.setHours(0, 0, 0, 0);
  return x;
}

const iconBtn: CSSProperties = {
  width: 28,
  height: 28,
  borderRadius: 6,
  fontSize: "1rem",
  padding: 0,
};

const smallBtn: CSSProperties = {
  fontSize: "0.78rem",
  padding: "0.2rem 0.6rem",
  borderRadius: 6,
};

const chip: CSSProperties = {
  fontSize: "0.7rem",
  padding: "0.15rem 0.45rem",
  borderRadius: 999,
};

const primaryBtn: CSSProperties = {
  fontSize: "0.82rem",
  padding: "0.3rem 0.7rem",
  fontWeight: 600,
};
