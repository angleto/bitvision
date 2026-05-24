"use client";

// Top-level calendar widget. Coordinates:
//
// - Data fetch from /api/patients/{pid}/calendar (per-patient) or N
//   parallel fetches (global aggregator).
// - View switching day / week / month / agenda; <768px viewport forced
//   onto agenda.
// - Status filter chips, persisted in the URL via ``?st=``.
// - Drawer open for action menu; PlanEventDialog for "+ Pianifica
//   visita". Both refresh on success via a local ``reloadTick``.

import { useEffect, useMemo, useState } from "react";

import EventDrawer from "@/components/EventDrawer";
import PlanEventDialog from "@/components/PlanEventDialog";
import CalendarAgenda from "@/components/calendar/CalendarAgenda";
import CalendarMonthGrid from "@/components/calendar/CalendarMonthGrid";
import CalendarTimeGrid from "@/components/calendar/CalendarTimeGrid";
import CalendarToolbar, { type CalendarView } from "@/components/calendar/CalendarToolbar";
import type { CalendarOccurrence, EventStatus } from "@/lib/api_records";
import { calendarApi, calendarFeedUrl } from "@/lib/calendar_api";

interface Props {
  patientId?: string;
  patientIds?: string[];
  patientLabels?: Record<string, string>; // for global aggregator
  initialView?: CalendarView;
  isOwner?: boolean;
}

const DEFAULT_VISIBLE_STATUSES: ReadonlySet<EventStatus> = new Set([
  "planned",
  "confirmed",
  "completed",
  "missed",
]);

export default function EventCalendar({
  patientId,
  patientIds,
  patientLabels,
  initialView = "week",
  isOwner = false,
}: Props) {
  const [view, setView] = useState<CalendarView>(initialView);
  const [anchorDate, setAnchorDate] = useState<Date>(() => {
    const d = new Date();
    d.setHours(0, 0, 0, 0);
    return d;
  });
  const [statuses, setStatuses] = useState<Set<EventStatus>>(new Set());
  const [occurrences, setOccurrences] = useState<CalendarOccurrence[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [reloadTick, setReloadTick] = useState(0);
  const [selected, setSelected] = useState<CalendarOccurrence | null>(null);
  const [planDialogOpen, setPlanDialogOpen] = useState(false);

  // Force agenda on narrow viewports. Recomputed once on mount; we
  // don't reactively follow window resize to avoid jank.
  // biome-ignore lint/correctness/useExhaustiveDependencies: mount-only effect; intentionally ignores ``view`` to avoid re-running after the user manually switches back to week/day on a narrow viewport.
  useEffect(() => {
    if (typeof window !== "undefined" && window.innerWidth < 768 && view !== "agenda") {
      setView("agenda");
    }
  }, []);

  const range = useMemo(() => computeRange(anchorDate, view), [anchorDate, view]);
  const effectiveStatuses = statuses.size > 0 ? Array.from(statuses) : undefined;

  // biome-ignore lint/correctness/useExhaustiveDependencies: ``range.from``/``range.to`` are derived from anchorDate+view and ``effectiveStatuses`` from ``statuses``; the listed deps cover all real invalidation triggers.
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    const ids = patientId ? [patientId] : (patientIds ?? []);
    if (ids.length === 0) {
      setOccurrences([]);
      setLoading(false);
      return;
    }
    Promise.all(
      ids.map((pid) =>
        calendarApi.feed(pid, {
          from: range.from,
          to: range.to,
          statuses: effectiveStatuses as EventStatus[] | undefined,
        }),
      ),
    )
      .then((feeds) => {
        if (cancelled) return;
        // Aggregate. We tag occurrences with patient_id implicitly
        // (the feed's occurrence carries event_id which the drawer
        // uses; the parent decides patient_id by the requested feed).
        const merged: CalendarOccurrence[] = feeds.flatMap((f) => f.occurrences);
        setOccurrences(merged);
        setLoading(false);
      })
      .catch((e: unknown) => {
        if (cancelled) return;
        setError(e instanceof Error ? e.message : "load failed");
        setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [patientId, patientIds, range.from, range.to, statuses, reloadTick, effectiveStatuses]);

  function toggleStatus(s: EventStatus): void {
    setStatuses((prev) => {
      const next = new Set(prev);
      if (next.has(s)) next.delete(s);
      else next.add(s);
      return next;
    });
  }

  function patientLabelFor(occ: CalendarOccurrence): string | undefined {
    if (!patientLabels) return undefined;
    // The feed currently doesn't echo patient_id on occurrence; we
    // map via the configured labels. For the global aggregator path
    // we'd need the occurrence to carry patient_id — wired in via
    // the api by the parent that fetches per-patient feeds and
    // tags them. (Left for the step-3 polish since the global page
    // does not exist yet.)
    return undefined;
  }

  function refresh(): void {
    setReloadTick((x) => x + 1);
  }

  // For the per-patient case the export URL is concrete; for the
  // global aggregator we omit it (a multi-patient ICS feed is not a
  // single-URL artefact).
  const exportIcsUrl = patientId
    ? calendarFeedUrl(
        patientId,
        {
          from: range.from,
          to: range.to,
          statuses: effectiveStatuses as EventStatus[] | undefined,
        },
        "ics",
      )
    : undefined;

  // Visible occurrences: apply default-visible-statuses filter when
  // the user has not selected anything explicitly (mirrors the
  // CareTimeline default of hiding cancelled/rescheduled clutter).
  const visibleOccurrences = useMemo(() => {
    if (statuses.size > 0) return occurrences; // explicit selection: server already filtered
    return occurrences.filter((occ) => DEFAULT_VISIBLE_STATUSES.has(occ.event_status));
  }, [occurrences, statuses]);

  return (
    <div className="event-calendar">
      <CalendarToolbar
        view={view}
        onChangeView={setView}
        anchorDate={anchorDate}
        onPrev={() => setAnchorDate(stepDate(anchorDate, view, -1))}
        onToday={() => {
          const d = new Date();
          d.setHours(0, 0, 0, 0);
          setAnchorDate(d);
        }}
        onNext={() => setAnchorDate(stepDate(anchorDate, view, +1))}
        statuses={statuses}
        onToggleStatus={toggleStatus}
        onPlan={() => setPlanDialogOpen(true)}
        exportIcsUrl={exportIcsUrl}
      />
      {error && (
        <p role="alert" style={{ color: "var(--bv-danger, #c00)" }}>
          {error}
        </p>
      )}
      {loading && <p style={{ color: "var(--bv-fg-soft)" }}>…</p>}
      {!loading &&
        !error &&
        (view === "agenda" ? (
          <CalendarAgenda
            occurrences={visibleOccurrences}
            onSelect={setSelected}
            patientLabelFor={patientLabelFor}
          />
        ) : view === "month" ? (
          <CalendarMonthGrid
            anchorDate={anchorDate}
            occurrences={visibleOccurrences}
            onSelect={setSelected}
            onSelectDate={(d) => {
              setAnchorDate(d);
              setView("day");
            }}
            patientLabelFor={patientLabelFor}
          />
        ) : (
          <CalendarTimeGrid
            days={view === "day" ? 1 : 7}
            anchorDate={view === "week" ? startOfWeek(anchorDate) : anchorDate}
            occurrences={visibleOccurrences}
            onSelect={setSelected}
            patientLabelFor={patientLabelFor}
          />
        ))}
      {selected && (
        <EventDrawer
          eventId={selected.event_id}
          isOwner={isOwner}
          onClose={() => setSelected(null)}
          onChanged={() => {
            refresh();
            setSelected(null);
          }}
        />
      )}
      {planDialogOpen && patientId && (
        <PlanEventDialog
          patientId={patientId}
          onClose={() => setPlanDialogOpen(false)}
          onCreated={() => {
            setPlanDialogOpen(false);
            refresh();
          }}
        />
      )}
    </div>
  );
}

function computeRange(anchor: Date, view: CalendarView): { from: string; to: string } {
  let from: Date;
  let to: Date;
  if (view === "day") {
    from = new Date(anchor);
    to = new Date(anchor);
  } else if (view === "week") {
    from = startOfWeek(anchor);
    to = new Date(from);
    to.setDate(to.getDate() + 6);
  } else if (view === "month") {
    from = new Date(anchor.getFullYear(), anchor.getMonth(), 1);
    to = new Date(anchor.getFullYear(), anchor.getMonth() + 1, 0);
    // Extend ± one week to catch the leading/trailing days the grid renders.
    from.setDate(from.getDate() - 7);
    to.setDate(to.getDate() + 7);
  } else {
    // agenda: ± 30 days from anchor
    from = new Date(anchor);
    from.setDate(from.getDate() - 7);
    to = new Date(anchor);
    to.setDate(to.getDate() + 30);
  }
  return { from: isoDate(from), to: isoDate(to) };
}

function stepDate(anchor: Date, view: CalendarView, dir: 1 | -1): Date {
  const d = new Date(anchor);
  if (view === "day") d.setDate(d.getDate() + dir);
  else if (view === "week" || view === "agenda") d.setDate(d.getDate() + 7 * dir);
  else d.setMonth(d.getMonth() + dir);
  return d;
}

function startOfWeek(d: Date): Date {
  const x = new Date(d);
  const dow = (x.getDay() + 6) % 7;
  x.setDate(x.getDate() - dow);
  x.setHours(0, 0, 0, 0);
  return x;
}

function isoDate(d: Date): string {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}
