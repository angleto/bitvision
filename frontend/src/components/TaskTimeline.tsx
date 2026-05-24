"use client";

// TaskTimeline — operational checklist view for a patient. Sibling of
// CareTimeline at the routing level (events vs tasks), shares the
// same TimelineRail rendering primitive so the visual language stays
// uniform across the three views (clinical / tasks / merged).
//
// State managed here:
//   - tasks JSON (from /api/patients/{id}/tasks via tasksApi.list)
//   - filter chip bar (status + category + priority)
//   - due-date range + free-text search
//   - selected task → opens TaskDrawer for in-place edits + FSM moves
//
// Toggle "Vista unificata" navigates to the same patient page with
// ``?merge=1``, which the parent FascicoloViewToggle / page-level
// router resolves to MergedTimelineView.

import { useLocale, useTranslations } from "next-intl";
import { useRouter, useSearchParams } from "next/navigation";
import { type CSSProperties, useCallback, useMemo, useState } from "react";

import CreateTaskDialog from "@/components/CreateTaskDialog";
import TaskDrawer from "@/components/TaskDrawer";
import TimelineTaskDot from "@/components/TimelineTaskDot";
import TimelineRail from "@/components/timeline/TimelineRail";
import { tasksToRailItems } from "@/components/timeline/taskAdapter";
import { useTimelineData } from "@/hooks/useTimelineData";
import type { PatientTask, TaskCategory, TaskPriority, TaskStatus } from "@/lib/api_records";

interface Props {
  patientId: string;
  isOwner?: boolean;
}

type SortDir = "desc" | "asc";

interface Filters {
  statuses: ReadonlySet<TaskStatus> | null;
  category: TaskCategory | null;
  priority: TaskPriority | null;
  dueFrom: string | null;
  dueTo: string | null;
  q: string;
  sort: SortDir;
}

const URL_KEYS = {
  status: "st",
  category: "cat",
  priority: "pri",
  dueFrom: "from",
  dueTo: "to",
  q: "q",
  sort: "s",
  merge: "merge",
} as const;

const STATUS_FILTER_VALUES: readonly TaskStatus[] = [
  "pending",
  "in_progress",
  "snoozed",
  "done",
  "dropped",
] as const;

const CATEGORIES: readonly TaskCategory[] = [
  "admin",
  "pharmacy",
  "appointment_prep",
  "transport",
  "communication",
  "personal",
  "other",
] as const;

const PRIORITIES: readonly TaskPriority[] = ["low", "normal", "high", "urgent"] as const;

function parseStatusParam(raw: string | null): ReadonlySet<TaskStatus> | null {
  if (raw === null) return null;
  if (raw === "") return new Set<TaskStatus>();
  const out = new Set<TaskStatus>();
  for (const tok of raw.split(",")) {
    const v = tok.trim() as TaskStatus;
    if ((STATUS_FILTER_VALUES as readonly string[]).includes(v)) out.add(v);
  }
  return out;
}

export default function TaskTimeline({ patientId, isOwner = false }: Props) {
  const router = useRouter();
  const searchParams = useSearchParams();
  const t = useTranslations("taskTimeline");
  const tStatus = useTranslations("taskStatus");
  const tCategory = useTranslations("taskCategory");
  const tPriority = useTranslations("taskPriority");
  const tTimeline = useTranslations("timeline");
  const locale = useLocale();

  // ----- URL-synced state -------------------------------------------
  const filters = useMemo<Filters>(
    () => ({
      statuses: parseStatusParam(searchParams.get(URL_KEYS.status)),
      category: (searchParams.get(URL_KEYS.category) as TaskCategory | null) ?? null,
      priority: (searchParams.get(URL_KEYS.priority) as TaskPriority | null) ?? null,
      dueFrom: searchParams.get(URL_KEYS.dueFrom),
      dueTo: searchParams.get(URL_KEYS.dueTo),
      q: searchParams.get(URL_KEYS.q) ?? "",
      sort: searchParams.get(URL_KEYS.sort) === "asc" ? "asc" : "desc",
    }),
    [searchParams],
  );

  const setUrlParam = useCallback(
    (key: string, value: string | null) => {
      const next = new URLSearchParams(searchParams.toString());
      if (value === null || value === "") next.delete(key);
      else next.set(key, value);
      router.replace(`?${next.toString()}`, { scroll: false });
    },
    [router, searchParams],
  );

  // ----- Default visible set -----------------------------------------
  // ``filters.statuses === null`` means "use the default": show
  // pending / in_progress / snoozed (the actionable subset). The
  // user opts into done / dropped explicitly via the chips.
  const visibleStatuses = useMemo<ReadonlySet<TaskStatus>>(() => {
    if (filters.statuses === null) {
      return new Set<TaskStatus>(["pending", "in_progress", "snoozed"]);
    }
    if (filters.statuses.size === 0) return new Set<TaskStatus>(STATUS_FILTER_VALUES);
    return filters.statuses;
  }, [filters.statuses]);

  // ----- Data fetch --------------------------------------------------
  const taskFilters = useMemo(
    () => ({
      statuses: Array.from(visibleStatuses),
      category: filters.category ?? undefined,
      priority: filters.priority ?? undefined,
      due_from: filters.dueFrom ?? undefined,
      due_to: filters.dueTo ?? undefined,
    }),
    [visibleStatuses, filters.category, filters.priority, filters.dueFrom, filters.dueTo],
  );

  const { tasks, loading, error, refresh } = useTimelineData({
    patientId,
    locale,
    includeEvents: false,
    includeTasks: true,
    taskFilters,
  });

  // ----- Derived: client-side free-text filter -----------------------
  const filteredTasks = useMemo<PatientTask[]>(() => {
    if (!tasks) return [];
    const q = filters.q.trim().toLowerCase();
    if (!q) return tasks;
    return tasks.filter((t) =>
      `${t.title} ${t.description ?? ""} ${(t.labels ?? []).join(" ")}`.toLowerCase().includes(q),
    );
  }, [tasks, filters.q]);

  const items = useMemo(
    () =>
      tasksToRailItems(
        filteredTasks,
        "var(--bv-accent, #4f46e5)", // tasks share one accent colour
      ),
    [filteredTasks],
  );

  // ----- Drawer state ------------------------------------------------
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null);
  const [createOpen, setCreateOpen] = useState(false);

  // ----- Banner counts: overdue + due-today -------------------------
  const banner = useMemo(() => {
    if (!filteredTasks.length) return null;
    const now = new Date();
    const todayKey = now.toISOString().slice(0, 10);
    let overdue = 0;
    let today = 0;
    for (const task of filteredTasks) {
      if (task.status !== "pending" && task.status !== "in_progress") continue;
      if (!task.due_at) continue;
      const due = task.due_at.slice(0, 10);
      if (due < todayKey) overdue += 1;
      else if (due === todayKey) today += 1;
    }
    return { overdue, today };
  }, [filteredTasks]);

  // ----- Toggle merge view ------------------------------------------
  const goToMergedView = useCallback(() => {
    const next = new URLSearchParams(searchParams.toString());
    next.set(URL_KEYS.merge, "1");
    router.push(`?${next.toString()}`, { scroll: false });
  }, [router, searchParams]);

  // ----- Render ------------------------------------------------------
  if (loading && !tasks) return <p>{t("loading")}</p>;
  if (error) {
    return (
      <p role="alert" style={{ color: "var(--bv-danger, #c00)" }}>
        {error}
      </p>
    );
  }

  return (
    <div className="task-timeline" data-patient={patientId}>
      <header
        style={{
          display: "flex",
          flexWrap: "wrap",
          alignItems: "center",
          gap: "0.6rem 1rem",
          marginBottom: "0.85rem",
        }}
      >
        <h2 style={{ margin: 0, fontSize: "1rem" }}>{t("heading")}</h2>
        <span style={{ flex: 1 }} />
        {isOwner && (
          <button
            type="button"
            onClick={() => setCreateOpen(true)}
            title={t("createTitle")}
            style={smallButtonStyle}
          >
            <span aria-hidden style={{ marginRight: 4 }}>
              +
            </span>
            {t("createLabel")}
          </button>
        )}
        <button
          type="button"
          className="ghost"
          onClick={goToMergedView}
          aria-pressed={false}
          title={tTimeline("toggleMergedTitle")}
          style={smallButtonStyle}
        >
          {tTimeline("toggleMergedLabel")}
        </button>
      </header>

      {banner && (banner.overdue > 0 || banner.today > 0) && (
        <section
          aria-live="polite"
          style={{
            display: "flex",
            flexWrap: "wrap",
            gap: "0.6rem 1rem",
            alignItems: "center",
            padding: "0.5rem 0.8rem",
            marginBottom: "0.85rem",
            background:
              banner.overdue > 0
                ? "var(--bv-status-missed-bg, rgba(217,119,6,0.08))"
                : "var(--bv-status-planned-bg, rgba(91,141,239,0.06))",
            border: `1px solid ${
              banner.overdue > 0
                ? "var(--bv-status-missed-border, #d97706)"
                : "var(--bv-status-planned-border, #5b8def)"
            }`,
            borderRadius: 8,
            fontSize: "0.82rem",
          }}
        >
          {banner.today > 0 && (
            <span>
              <strong style={{ marginRight: 4 }}>☐ {banner.today}</strong>
              {t("bannerToday", { n: banner.today })}
            </span>
          )}
          {banner.overdue > 0 && (
            <span style={{ color: "var(--bv-status-missed-border, #9a5b04)" }}>
              <strong style={{ marginRight: 4 }}>△ {banner.overdue}</strong>
              {t("bannerOverdue", { n: banner.overdue })}
            </span>
          )}
        </section>
      )}

      <div
        style={{
          display: "flex",
          flexWrap: "wrap",
          alignItems: "center",
          gap: "0.4rem 0.6rem",
          marginBottom: "0.85rem",
        }}
      >
        <Chip
          active={filters.statuses === null}
          onClick={() => setUrlParam(URL_KEYS.status, null)}
          label={t("allActive")}
        />
        {STATUS_FILTER_VALUES.map((s) => {
          const active =
            filters.statuses === null
              ? s === "pending" || s === "in_progress" || s === "snoozed"
              : filters.statuses.has(s);
          return (
            <Chip
              key={s}
              active={active}
              onClick={() => {
                const current = filters.statuses;
                if (current === null) {
                  setUrlParam(URL_KEYS.status, s);
                  return;
                }
                const next = new Set<TaskStatus>(current);
                if (next.has(s)) next.delete(s);
                else next.add(s);
                setUrlParam(URL_KEYS.status, next.size === 0 ? null : Array.from(next).join(","));
              }}
              label={tStatus(s)}
            />
          );
        })}
        <span style={{ width: 1, height: 18, background: "var(--bv-card-border)" }} />
        {CATEGORIES.map((c) => (
          <Chip
            key={c}
            active={filters.category === c}
            onClick={() => setUrlParam(URL_KEYS.category, filters.category === c ? null : c)}
            label={tCategory(c)}
          />
        ))}
        <span style={{ width: 1, height: 18, background: "var(--bv-card-border)" }} />
        {PRIORITIES.map((p) => (
          <Chip
            key={p}
            active={filters.priority === p}
            onClick={() => setUrlParam(URL_KEYS.priority, filters.priority === p ? null : p)}
            label={tPriority(p)}
          />
        ))}
        <input
          type="date"
          value={filters.dueFrom ?? ""}
          onChange={(e) => setUrlParam(URL_KEYS.dueFrom, e.target.value || null)}
          style={smallInputStyle}
          title={t("dueFromTitle")}
        />
        <input
          type="date"
          value={filters.dueTo ?? ""}
          onChange={(e) => setUrlParam(URL_KEYS.dueTo, e.target.value || null)}
          style={smallInputStyle}
          title={t("dueToTitle")}
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
          style={smallButtonStyle}
          aria-pressed={filters.sort === "asc"}
          title={filters.sort === "desc" ? t("sortDescTitle") : t("sortAscTitle")}
        >
          <span aria-hidden>{filters.sort === "desc" ? "↓" : "↑"}</span>
          <span style={{ marginLeft: 6 }}>
            {filters.sort === "desc" ? t("sortDescLabel") : t("sortAscLabel")}
          </span>
        </button>
      </div>

      {filteredTasks.length === 0 ? (
        <EmptyState
          isOwner={isOwner}
          hint={t("emptyHint")}
          cta={t("emptyCta")}
          createLabel={t("createLabel")}
          onCreate={() => setCreateOpen(true)}
        />
      ) : (
        <TimelineRail
          items={items}
          locale={locale}
          sort={filters.sort}
          undatedLabel={t("undatedBucket")}
          renderItem={(item) => (
            <TimelineTaskDot
              task={item.payload}
              color={item.color}
              variant="horizontal"
              onActivate={(task) => setSelectedTaskId(task.id)}
            />
          )}
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

      {createOpen && (
        <CreateTaskDialog
          patientId={patientId}
          onClose={() => setCreateOpen(false)}
          onCreated={() => {
            setCreateOpen(false);
            refresh();
          }}
        />
      )}
    </div>
  );
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

function EmptyState({
  isOwner,
  hint,
  cta,
  createLabel,
  onCreate,
}: {
  isOwner: boolean;
  hint: string;
  cta: string;
  createLabel: string;
  onCreate: () => void;
}) {
  return (
    <div
      style={{
        padding: "1.2rem",
        textAlign: "center",
        border: "1px dashed var(--bv-card-border)",
        borderRadius: 8,
        color: "var(--bv-fg-soft)",
      }}
    >
      <p style={{ marginTop: 0 }}>{hint}</p>
      {isOwner && (
        <>
          <p style={{ fontSize: "0.85rem", marginBottom: "0.6rem" }}>{cta}</p>
          <button type="button" onClick={onCreate}>
            <span aria-hidden style={{ marginRight: 4 }}>
              +
            </span>
            {createLabel}
          </button>
        </>
      )}
    </div>
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
