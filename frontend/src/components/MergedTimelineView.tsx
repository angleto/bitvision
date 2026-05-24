"use client";

// MergedTimelineView — events + tasks interleaved on a single
// TimelineRail. Activated when the user toggles "Vista unificata" from
// the CareTimeline or TaskTimeline header (URL key ``merge=1``).
//
// State managed here:
//   - timeline JSON + tasks list (via useTimelineData)
//   - status filters for events AND tasks (separate URL keys so the
//     two domains don't fight over the same chip)
//   - free-text search across both surfaces
//
// Click handlers route through the same drawers used by the
// dedicated views (EventDrawer for events, TaskDrawer for tasks).
// Selecting a row from the merged view feels identical to selecting
// it from its native view — no surprise behaviour.

import { useLocale, useTranslations } from "next-intl";
import { useRouter, useSearchParams } from "next/navigation";
import { type CSSProperties, useCallback, useMemo, useState } from "react";

import EventDrawer from "@/components/EventDrawer";
import TaskDrawer from "@/components/TaskDrawer";
import TimelineEventDot from "@/components/TimelineEventDot";
import TimelineTaskDot from "@/components/TimelineTaskDot";
import TimelineRail from "@/components/timeline/TimelineRail";
import { eventToRailItem } from "@/components/timeline/eventAdapter";
import { taskToRailItem } from "@/components/timeline/taskAdapter";
import type { TimelineRailItem } from "@/components/timeline/types";
import { useTimelineData } from "@/hooks/useTimelineData";
import type { EventStatus, PatientTask, TaskStatus, TimelineEvent } from "@/lib/api_records";

interface Props {
  patientId: string;
  isOwner?: boolean;
}

type SortDir = "desc" | "asc";

interface MergedFilters {
  eventStatuses: ReadonlySet<EventStatus> | null;
  taskStatuses: ReadonlySet<TaskStatus> | null;
  q: string;
  dateFrom: string | null;
  dateTo: string | null;
  sort: SortDir;
}

const URL_KEYS = {
  eventStatus: "est",
  taskStatus: "tst",
  q: "q",
  dateFrom: "from",
  dateTo: "to",
  sort: "s",
  merge: "merge",
} as const;

const EVENT_STATUS_VALUES: readonly EventStatus[] = [
  "planned",
  "confirmed",
  "completed",
  "cancelled",
  "missed",
  "rescheduled",
] as const;
const TASK_STATUS_VALUES: readonly TaskStatus[] = [
  "pending",
  "in_progress",
  "snoozed",
  "done",
  "dropped",
] as const;

function parseStatusParam<S extends string>(
  raw: string | null,
  allowed: readonly S[],
): ReadonlySet<S> | null {
  if (raw === null) return null;
  if (raw === "") return new Set<S>();
  const out = new Set<S>();
  for (const tok of raw.split(",")) {
    const v = tok.trim() as S;
    if ((allowed as readonly string[]).includes(v)) out.add(v);
  }
  return out;
}

export default function MergedTimelineView({ patientId, isOwner = false }: Props) {
  const router = useRouter();
  const searchParams = useSearchParams();
  const t = useTranslations("timeline");
  const tEvStatus = useTranslations("eventStatus");
  const tTaskStatus = useTranslations("taskStatus");
  const locale = useLocale();

  const filters = useMemo<MergedFilters>(
    () => ({
      eventStatuses: parseStatusParam(searchParams.get(URL_KEYS.eventStatus), EVENT_STATUS_VALUES),
      taskStatuses: parseStatusParam(searchParams.get(URL_KEYS.taskStatus), TASK_STATUS_VALUES),
      q: searchParams.get(URL_KEYS.q) ?? "",
      dateFrom: searchParams.get(URL_KEYS.dateFrom),
      dateTo: searchParams.get(URL_KEYS.dateTo),
      sort: searchParams.get(URL_KEYS.sort) === "asc" ? "asc" : "desc",
    }),
    [searchParams],
  );

  const visibleEventStatuses = useMemo<ReadonlySet<EventStatus>>(() => {
    if (filters.eventStatuses === null) {
      return new Set<EventStatus>(["planned", "confirmed", "completed", "missed"]);
    }
    if (filters.eventStatuses.size === 0) return new Set<EventStatus>(EVENT_STATUS_VALUES);
    return filters.eventStatuses;
  }, [filters.eventStatuses]);

  const visibleTaskStatuses = useMemo<ReadonlySet<TaskStatus>>(() => {
    if (filters.taskStatuses === null) {
      return new Set<TaskStatus>(["pending", "in_progress", "snoozed"]);
    }
    if (filters.taskStatuses.size === 0) return new Set<TaskStatus>(TASK_STATUS_VALUES);
    return filters.taskStatuses;
  }, [filters.taskStatuses]);

  const taskFilters = useMemo(
    () => ({ statuses: Array.from(visibleTaskStatuses) }),
    [visibleTaskStatuses],
  );

  const { timeline, tasks, loading, error, refresh } = useTimelineData({
    patientId,
    locale,
    includeEvents: true,
    includeTasks: true,
    taskFilters,
  });

  const setUrlParam = useCallback(
    (key: string, value: string | null) => {
      const next = new URLSearchParams(searchParams.toString());
      if (value === null || value === "") next.delete(key);
      else next.set(key, value);
      router.replace(`?${next.toString()}`, { scroll: false });
    },
    [router, searchParams],
  );

  const exitMerge = useCallback(() => {
    const next = new URLSearchParams(searchParams.toString());
    next.delete(URL_KEYS.merge);
    router.push(`?${next.toString()}`, { scroll: false });
  }, [router, searchParams]);

  // ----- Build the merged item list ---------------------------------
  const items = useMemo<TimelineRailItem<TimelineEvent | PatientTask>[]>(() => {
    const out: TimelineRailItem<TimelineEvent | PatientTask>[] = [];
    if (timeline) {
      const q = filters.q.trim().toLowerCase();
      const inDateRange = (iso: string | null): boolean => {
        if (!iso) return true;
        if (filters.dateFrom && iso < filters.dateFrom) return false;
        if (filters.dateTo && iso > filters.dateTo) return false;
        return true;
      };
      // Events from all phases + unassigned, filtered by status +
      // search + date range. Colour comes from the originating phase.
      for (const phase of timeline.phases) {
        for (const ev of phase.events) {
          if (!visibleEventStatuses.has(ev.event_status ?? "completed")) continue;
          if (!inDateRange(ev.event_date)) continue;
          if (q && !`${ev.title} ${ev.body_part ?? ""}`.toLowerCase().includes(q)) continue;
          out.push(eventToRailItem(ev, phase.color_hex));
        }
      }
      for (const ev of timeline.unassigned_events) {
        if (!visibleEventStatuses.has(ev.event_status ?? "completed")) continue;
        if (!inDateRange(ev.event_date)) continue;
        if (q && !`${ev.title} ${ev.body_part ?? ""}`.toLowerCase().includes(q)) continue;
        out.push(eventToRailItem(ev, "var(--bv-fg-muted, #888)"));
      }
    }
    if (tasks) {
      const q = filters.q.trim().toLowerCase();
      const taskAccent = "var(--bv-accent, #4f46e5)";
      for (const task of tasks) {
        if (!visibleTaskStatuses.has(task.status)) continue;
        const dueKey = task.due_at?.slice(0, 10);
        if (filters.dateFrom && dueKey && dueKey < filters.dateFrom) continue;
        if (filters.dateTo && dueKey && dueKey > filters.dateTo) continue;
        if (
          q &&
          !`${task.title} ${task.description ?? ""} ${(task.labels ?? []).join(" ")}`
            .toLowerCase()
            .includes(q)
        )
          continue;
        out.push(taskToRailItem(task, taskAccent));
      }
    }
    return out;
  }, [
    timeline,
    tasks,
    visibleEventStatuses,
    visibleTaskStatuses,
    filters.q,
    filters.dateFrom,
    filters.dateTo,
  ]);

  const [selectedEventId, setSelectedEventId] = useState<string | null>(null);
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null);

  if (loading && !timeline && !tasks) return <p>{t("loading")}</p>;
  if (error) {
    return (
      <p role="alert" style={{ color: "var(--bv-danger, #c00)" }}>
        {error}
      </p>
    );
  }

  return (
    <div className="merged-timeline-view" data-patient={patientId}>
      <header
        style={{
          display: "flex",
          flexWrap: "wrap",
          alignItems: "center",
          gap: "0.6rem 1rem",
          marginBottom: "0.85rem",
        }}
      >
        <h2 style={{ margin: 0, fontSize: "1rem" }}>{t("mergedHeading")}</h2>
        <span style={{ flex: 1 }} />
        <button
          type="button"
          className="ghost"
          onClick={exitMerge}
          title={t("exitMergedTitle")}
          style={smallButtonStyle}
        >
          {t("exitMergedLabel")}
        </button>
      </header>

      <div
        style={{
          display: "flex",
          flexWrap: "wrap",
          alignItems: "center",
          gap: "0.4rem 0.6rem",
          marginBottom: "0.85rem",
        }}
      >
        <span style={{ fontSize: "0.7rem", color: "var(--bv-fg-soft)" }}>
          {t("eventStatusLegend")}:
        </span>
        {EVENT_STATUS_VALUES.map((s) => {
          const active =
            filters.eventStatuses === null
              ? s === "planned" || s === "confirmed" || s === "completed" || s === "missed"
              : filters.eventStatuses.has(s);
          return (
            <Chip
              key={`e-${s}`}
              active={active}
              onClick={() =>
                toggleStatus(URL_KEYS.eventStatus, filters.eventStatuses, s, setUrlParam)
              }
              label={tEvStatus(s)}
            />
          );
        })}
        <span style={{ width: 1, height: 18, background: "var(--bv-card-border)" }} />
        <span style={{ fontSize: "0.7rem", color: "var(--bv-fg-soft)" }}>
          {t("taskStatusLegend")}:
        </span>
        {TASK_STATUS_VALUES.map((s) => {
          const active =
            filters.taskStatuses === null
              ? s === "pending" || s === "in_progress" || s === "snoozed"
              : filters.taskStatuses.has(s);
          return (
            <Chip
              key={`t-${s}`}
              active={active}
              onClick={() =>
                toggleStatus(URL_KEYS.taskStatus, filters.taskStatuses, s, setUrlParam)
              }
              label={tTaskStatus(s)}
            />
          );
        })}
        <input
          type="date"
          value={filters.dateFrom ?? ""}
          onChange={(e) => setUrlParam(URL_KEYS.dateFrom, e.target.value || null)}
          style={smallInputStyle}
          title={t("dateFromTitle")}
        />
        <input
          type="date"
          value={filters.dateTo ?? ""}
          onChange={(e) => setUrlParam(URL_KEYS.dateTo, e.target.value || null)}
          style={smallInputStyle}
          title={t("dateToTitle")}
        />
        <input
          type="search"
          placeholder={t("searchPlaceholder")}
          value={filters.q}
          onChange={(e) => setUrlParam(URL_KEYS.q, e.target.value || null)}
          style={{ ...smallInputStyle, minWidth: 140 }}
        />
        <button
          type="button"
          className="ghost"
          onClick={() => setUrlParam(URL_KEYS.sort, filters.sort === "desc" ? "asc" : null)}
          aria-pressed={filters.sort === "asc"}
          style={smallButtonStyle}
        >
          <span aria-hidden>{filters.sort === "desc" ? "↓" : "↑"}</span>
          <span style={{ marginLeft: 6 }}>
            {filters.sort === "desc" ? t("sortDescLabel") : t("sortAscLabel")}
          </span>
        </button>
      </div>

      {items.length === 0 ? (
        <p
          style={{
            padding: "1.2rem",
            textAlign: "center",
            border: "1px dashed var(--bv-card-border)",
            borderRadius: 8,
            color: "var(--bv-fg-soft)",
          }}
        >
          {t("emptyMerged")}
        </p>
      ) : (
        <TimelineRail
          items={items}
          locale={locale}
          sort={filters.sort}
          undatedLabel={t("undatedBucket")}
          renderItem={(item) => {
            if (item.source === "event") {
              const ev = item.payload as TimelineEvent;
              return (
                <TimelineEventDot
                  event={ev}
                  color={item.color}
                  variant="horizontal"
                  onActivate={(e) => setSelectedEventId(e.id)}
                />
              );
            }
            const task = item.payload as PatientTask;
            return (
              <TimelineTaskDot
                task={task}
                color={item.color}
                variant="horizontal"
                onActivate={(t) => setSelectedTaskId(t.id)}
              />
            );
          }}
        />
      )}

      {selectedEventId && (
        <EventDrawer
          eventId={selectedEventId}
          isOwner={isOwner}
          onClose={() => setSelectedEventId(null)}
          onChanged={() => {
            setSelectedEventId(null);
            refresh();
          }}
        />
      )}
      {selectedTaskId && (
        <TaskDrawer
          taskId={selectedTaskId}
          isOwner={isOwner}
          onClose={() => setSelectedTaskId(null)}
          onChanged={() => {
            setSelectedTaskId(null);
            refresh();
          }}
        />
      )}
    </div>
  );
}

function toggleStatus<S extends string>(
  key: string,
  current: ReadonlySet<S> | null,
  value: S,
  setUrlParam: (key: string, value: string | null) => void,
): void {
  if (current === null) {
    setUrlParam(key, value);
    return;
  }
  const next = new Set<S>(current);
  if (next.has(value)) next.delete(value);
  else next.add(value);
  setUrlParam(key, next.size === 0 ? null : Array.from(next).join(","));
}

function Chip({
  active,
  label,
  onClick,
}: {
  active: boolean;
  label: string;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      className={active ? undefined : "ghost"}
      style={{
        padding: "0.15rem 0.55rem",
        borderRadius: 999,
        fontSize: "0.75rem",
        fontWeight: active ? 600 : 400,
      }}
    >
      {label}
    </button>
  );
}

const smallInputStyle: CSSProperties = {
  fontSize: "0.78rem",
  padding: "0.2rem 0.4rem",
  border: "1px solid var(--bv-card-border, #d0d5dd)",
  borderRadius: 6,
  background: "var(--bv-card-bg, transparent)",
  color: "var(--bv-fg)",
};

const smallButtonStyle: CSSProperties = {
  fontSize: "0.72rem",
  padding: "0.18rem 0.5rem",
  textDecoration: "none",
  borderRadius: 6,
  display: "inline-flex",
  alignItems: "center",
};
