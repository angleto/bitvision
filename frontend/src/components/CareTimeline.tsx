"use client";

// Care timeline — the canonical view of a patient's clinical journey
// grouped into semantic phases. Replaces the legacy
// ClinicalEventsTimeline. Visual style mirrors
// ``timeline_fascicolo_canary_patient.svg``: a vertical dashed rail
// with phase chips on the left and event dots on the right.
//
// State managed by this component:
//   - timeline JSON (from /api/patients/{id}/care-timeline)
//   - health snapshot (from /care-timeline/health, panel above)
//   - expand/collapse set (URL-synced via ?expanded=slug1,slug2)
//   - filter chip bar (event kind + phase kind)
//   - date range, free-text search, "show unassigned" toggle
//   - edit-mode overlay (delegates to CarePhaseEditor)
//
// The component does NOT own the wire types or the REST surface —
// those live in api_records.ts so the MCP and admin tools can re-use them.

import { useLocale, useTranslations } from "next-intl";
import { useRouter, useSearchParams } from "next/navigation";
import { type CSSProperties, useCallback, useEffect, useMemo, useState } from "react";

import CarePhaseEditor from "@/components/CarePhaseEditor";
import EventDrawer from "@/components/EventDrawer";
import PhaseChip from "@/components/PhaseChip";
import PlanEventDialog from "@/components/PlanEventDialog";
import TimelineEventDot from "@/components/TimelineEventDot";
import TimelineRail from "@/components/timeline/TimelineRail";
import { eventToRailItem, eventsToRailItems } from "@/components/timeline/eventAdapter";
import type { TimelineEventItem } from "@/components/timeline/types";
import { API_BASE_URL, ApiError, authedDownload, systemApi } from "@/lib/api";
import {
  type CarePhaseDetail,
  type CareTimeline,
  type EventStatus,
  type TimelineEvent,
  type TimelineHealth,
  carePhasesApi,
} from "@/lib/api_records";
import { subscribeCarePhases } from "@/lib/care_phase_realtime";

interface Props {
  patientId: string;
  view?: "grouped" | "flat";
  /** Render mode forwarded to the editor overlay. The chrome stays
   *  identical; only mutations flip on. */
  isOwner?: boolean;
}

type SortDir = "desc" | "asc";

interface Filters {
  eventKind: string | null;
  phaseKind: string | null;
  // Multi-value: an empty array means "show every status" (the default
  // visible set; cancelled is included unless the user actively
  // narrows). We use ``null`` for "no preference, use default" and a
  // ``Set`` for the explicit selection.
  statuses: ReadonlySet<EventStatus> | null;
  dateFrom: string | null;
  dateTo: string | null;
  q: string;
  showUnassigned: boolean;
  sort: SortDir;
}

const URL_KEYS = {
  expanded: "expanded",
  view: "view",
  eventKind: "ek",
  phaseKind: "pk",
  // Comma-separated EventStatus values, e.g. ``?st=planned,confirmed``.
  // Round-trip with the multi-select chip in FilterBar.
  status: "st",
  dateFrom: "from",
  dateTo: "to",
  q: "q",
  unassigned: "u",
  sort: "s",
} as const;

const STATUS_FILTER_VALUES: readonly EventStatus[] = [
  "planned",
  "confirmed",
  "completed",
  "cancelled",
  "missed",
  "rescheduled",
] as const;

function parseStatusParam(raw: string | null): ReadonlySet<EventStatus> | null {
  if (raw === null) return null;
  if (raw === "") return new Set<EventStatus>();
  const out = new Set<EventStatus>();
  for (const tok of raw.split(",")) {
    const v = tok.trim() as EventStatus;
    if ((STATUS_FILTER_VALUES as readonly string[]).includes(v)) out.add(v);
  }
  return out;
}

const PHASE_KIND_LABEL: Record<string, string> = {
  imaging: "Imaging",
  surgery: "Chirurgia",
  followup: "Follow-up",
  surveillance: "Sorveglianza",
  visit: "Visita",
  reassessment: "Rivalutazione",
  planned: "Pianificati",
  other: "Altro",
};

const EVENT_KINDS = [
  "imaging_study",
  "surgical_procedure",
  "outpatient_visit",
  "inpatient_admission",
  "lab_batch",
  "consultation_event",
  "other",
] as const;

export default function CareTimelineComponent({
  patientId,
  view: initialView = "grouped",
  isOwner = false,
}: Props) {
  const router = useRouter();
  const searchParams = useSearchParams();
  const t = useTranslations("fascicolo.v3.events");
  const tPanel = useTranslations("careTimelinePanel");
  const locale = useLocale();

  // -----------------------------------------------------------------
  // Data
  // -----------------------------------------------------------------
  const [timeline, setTimeline] = useState<CareTimeline | null>(null);
  const [health, setHealth] = useState<TimelineHealth | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [reloadTick, setReloadTick] = useState(0);

  const refresh = useCallback(() => setReloadTick((x) => x + 1), []);

  // biome-ignore lint/correctness/useExhaustiveDependencies: reloadTick + locale are the explicit invalidation triggers for this effect.
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    Promise.all([
      carePhasesApi.timeline(patientId, { lang: locale }),
      carePhasesApi.health(patientId).catch(() => null),
    ])
      .then(([tl, h]) => {
        if (cancelled) return;
        setTimeline(tl);
        setHealth(h);
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
  }, [patientId, reloadTick, locale]);

  // Live updates from agents / peers (no-op until backend bus lands).
  useEffect(() => {
    return subscribeCarePhases(patientId, () => refresh());
  }, [patientId, refresh]);

  // -----------------------------------------------------------------
  // URL-synced state
  // -----------------------------------------------------------------
  const view = ((): "grouped" | "flat" => {
    const v = searchParams.get(URL_KEYS.view);
    if (v === "flat" || v === "grouped") return v;
    return initialView;
  })();

  const initialExpanded = useMemo(() => {
    const raw = searchParams.get(URL_KEYS.expanded);
    if (raw === null) return null; // null = expand-all-by-default
    if (raw === "") return new Set<string>();
    return new Set(raw.split(",").filter(Boolean));
  }, [searchParams]);
  const [expandedOverride, setExpandedOverride] = useState<Set<string> | null>(initialExpanded);

  const filters = useMemo<Filters>(
    () => ({
      eventKind: searchParams.get(URL_KEYS.eventKind),
      phaseKind: searchParams.get(URL_KEYS.phaseKind),
      statuses: parseStatusParam(searchParams.get(URL_KEYS.status)),
      dateFrom: searchParams.get(URL_KEYS.dateFrom),
      dateTo: searchParams.get(URL_KEYS.dateTo),
      q: searchParams.get(URL_KEYS.q) ?? "",
      showUnassigned: (searchParams.get(URL_KEYS.unassigned) ?? "1") !== "0",
      sort: searchParams.get(URL_KEYS.sort) === "asc" ? "asc" : "desc",
    }),
    [searchParams],
  );

  // When ``filters.statuses`` is null we apply the default visible
  // set: everything except cancelled and rescheduled (those clutter
  // the historical view; user opts them in explicitly via the chips).
  // The eventVisible() predicate below uses this resolved set.
  const visibleStatuses = useMemo<ReadonlySet<EventStatus>>(() => {
    if (filters.statuses === null) {
      return new Set<EventStatus>(["planned", "confirmed", "completed", "missed"]);
    }
    if (filters.statuses.size === 0) {
      // Empty explicit set === "show all" (user toggled all chips off
      // then on again, or wants to inspect cancelled too).
      return new Set<EventStatus>(STATUS_FILTER_VALUES);
    }
    return filters.statuses;
  }, [filters.statuses]);

  const setUrlParam = useCallback(
    (key: string, value: string | null) => {
      const next = new URLSearchParams(searchParams.toString());
      if (value === null || value === "") next.delete(key);
      else next.set(key, value);
      const qs = next.toString();
      router.replace(`?${qs}`, { scroll: false });
    },
    [router, searchParams],
  );

  // -----------------------------------------------------------------
  // Derived: phases after filtering
  // -----------------------------------------------------------------
  const filteredPhases: CarePhaseDetail[] = useMemo(() => {
    if (!timeline) return [];
    const q = filters.q.trim().toLowerCase();
    const inDateRange = (iso: string | null): boolean => {
      if (!iso) return true;
      if (filters.dateFrom && iso < filters.dateFrom) return false;
      if (filters.dateTo && iso > filters.dateTo) return false;
      return true;
    };
    const phases = timeline.phases
      .filter((p) => (filters.phaseKind === null ? true : p.kind === filters.phaseKind))
      .map((p) => {
        const events = p.events
          .filter((ev) => {
            if (filters.eventKind && ev.kind !== filters.eventKind) return false;
            if (!visibleStatuses.has(ev.event_status ?? "completed")) return false;
            if (!inDateRange(ev.event_date)) return false;
            if (q && !`${ev.title} ${ev.body_part ?? ""}`.toLowerCase().includes(q)) return false;
            return true;
          })
          .slice()
          .sort(eventDateComparator(filters.sort));
        return { ...p, events };
      });
    return filters.sort === "desc" ? phases.reverse() : phases;
  }, [timeline, filters, visibleStatuses]);

  const filteredUnassigned = useMemo<TimelineEvent[]>(() => {
    if (!timeline) return [];
    const q = filters.q.trim().toLowerCase();
    return timeline.unassigned_events
      .filter((ev) => {
        if (filters.eventKind && ev.kind !== filters.eventKind) return false;
        if (!visibleStatuses.has(ev.event_status ?? "completed")) return false;
        if (filters.dateFrom && ev.event_date && ev.event_date < filters.dateFrom) return false;
        if (filters.dateTo && ev.event_date && ev.event_date > filters.dateTo) return false;
        if (q && !`${ev.title} ${ev.body_part ?? ""}`.toLowerCase().includes(q)) return false;
        return true;
      })
      .slice()
      .sort(eventDateComparator(filters.sort));
  }, [timeline, filters, visibleStatuses]);

  // Banner counts: upcoming (planned/confirmed in the future) and
  // overdue (planned/confirmed in the past — likely missed events
  // awaiting a status decision). The user is prompted to act on them.
  const statusBanner = useMemo(() => {
    if (!timeline) return null;
    const all: TimelineEvent[] = [
      ...timeline.phases.flatMap((p) => p.events),
      ...timeline.unassigned_events,
    ];
    const todayIso = new Date().toISOString().slice(0, 10);
    let upcoming = 0;
    let overdue = 0;
    for (const ev of all) {
      if (ev.event_status !== "planned" && ev.event_status !== "confirmed") continue;
      // Prefer planned_start_at for the cut-off (hour granularity);
      // fall back to event_date (DATE) when no timestamp is set.
      const cutoff = ev.planned_start_at ?? ev.event_date;
      if (cutoff === null) continue;
      const cutoffDate = cutoff.length > 10 ? cutoff.slice(0, 10) : cutoff;
      if (cutoffDate < todayIso) overdue += 1;
      else upcoming += 1;
    }
    return { upcoming, overdue };
  }, [timeline]);

  // expanded set: explicit override wins; otherwise expand all by default.
  const isExpanded = useCallback(
    (slug: string): boolean => {
      if (expandedOverride === null) return true;
      return expandedOverride.has(slug);
    },
    [expandedOverride],
  );

  const toggleExpanded = useCallback(
    (slug: string) => {
      const allSlugs = (timeline?.phases ?? []).map((p) => p.slug);
      const current =
        expandedOverride === null ? new Set<string>(allSlugs) : new Set(expandedOverride);
      if (current.has(slug)) current.delete(slug);
      else current.add(slug);
      setExpandedOverride(current);
      const allExpanded = current.size === allSlugs.length && allSlugs.every((s) => current.has(s));
      setUrlParam(URL_KEYS.expanded, allExpanded ? null : Array.from(current).join(","));
    },
    [expandedOverride, setUrlParam, timeline],
  );

  // -----------------------------------------------------------------
  // Mutations
  // -----------------------------------------------------------------
  const [editing, setEditing] = useState(false);
  const [planDialogOpen, setPlanDialogOpen] = useState(false);
  // ``selectedEventId``: when set, render the same EventDrawer the
  // calendar uses. Single source of truth for "view + act on an
  // event" — no duplicated dialog code.
  const [selectedEventId, setSelectedEventId] = useState<string | null>(null);
  const [proposing, setProposing] = useState(false);
  const [proposeError, setProposeError] = useState<string | null>(null);
  // ``null`` until the features endpoint replies. We default to "enabled"
  // optimistically so the button is not greyed for the brief moment
  // before the probe lands; on a 503 the catch-block downgrades to
  // ``llmAvailable=false`` so subsequent clicks are blocked client-side.
  const [llmAvailable, setLlmAvailable] = useState<boolean | null>(null);

  useEffect(() => {
    let cancelled = false;
    systemApi
      .features()
      .then((f) => {
        if (!cancelled) setLlmAvailable(f.llm_classifier);
      })
      .catch(() => {
        // Probe failure is non-fatal — fall back to optimistic enable
        // and rely on the 503 path for the eventual user feedback.
        if (!cancelled) setLlmAvailable(true);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  async function handlePropose() {
    if (proposing) return;
    setProposing(true);
    setProposeError(null);
    try {
      await carePhasesApi.propose(patientId, { lang: locale });
      // The proposal is async; refreshing fetches the updated health
      // snapshot (pending_proposals++) and lets the user open the
      // proposal review panel from there. Once the worker finishes the
      // realtime subscription will trigger another refresh.
      refresh();
    } catch (e) {
      if (e instanceof ApiError && e.status === 503) {
        setLlmAvailable(false);
        setProposeError(tPanel("proposeViaMcpHint"));
      } else {
        setProposeError(e instanceof Error ? e.message : "propose failed");
      }
    } finally {
      setProposing(false);
    }
  }

  // -----------------------------------------------------------------
  // Render
  // -----------------------------------------------------------------
  if (loading) {
    return <p>{t("loading")}</p>;
  }
  if (error) {
    return (
      <p role="alert" style={{ color: "var(--bv-danger, #c00)" }}>
        {error}
      </p>
    );
  }
  if (!timeline) return null;

  const hasPhases = timeline.phases.length > 0;
  const hasAnyEvent = hasPhases || timeline.unassigned_events.length > 0;

  return (
    <div className="care-timeline" data-patient={patientId}>
      <SaluteBar
        health={health}
        onPropose={handlePropose}
        proposing={proposing}
        proposeError={proposeError}
        isOwner={isOwner}
        llmAvailable={llmAvailable}
      />

      {statusBanner && (statusBanner.upcoming > 0 || statusBanner.overdue > 0) && (
        <StatusBanner
          upcoming={statusBanner.upcoming}
          overdue={statusBanner.overdue}
          onFilterUpcoming={() => setUrlParam(URL_KEYS.status, "planned,confirmed")}
        />
      )}

      <FilterBar
        filters={filters}
        view={view}
        timeline={timeline}
        onChange={setUrlParam}
        onToggleEdit={() => setEditing((v) => !v)}
        onOpenPlanDialog={() => setPlanDialogOpen(true)}
        editing={editing}
        isOwner={isOwner}
      />

      {!hasAnyEvent && (
        <EmptyState
          variant="no-events"
          isOwner={isOwner}
          proposing={proposing}
          onPropose={handlePropose}
          llmAvailable={llmAvailable}
        />
      )}

      {hasAnyEvent && !hasPhases && (
        <EmptyState
          variant="events-no-phases"
          isOwner={isOwner}
          proposing={proposing}
          onPropose={handlePropose}
          llmAvailable={llmAvailable}
        />
      )}

      {hasPhases && view === "grouped" && (
        <ol className="care-timeline-rail" style={railStyle}>
          {filteredPhases.map((phase) => (
            <li key={phase.id} style={{ position: "relative" }}>
              <PhaseRow
                phase={phase}
                expanded={isExpanded(phase.slug)}
                onToggle={() => toggleExpanded(phase.slug)}
                onNavigate={() => router.push(`/patients/${patientId}/care-phases/${phase.slug}`)}
                onActivateEvent={(ev) => setSelectedEventId(ev.id)}
                locale={locale}
                sort={filters.sort}
              />
            </li>
          ))}
          {filters.showUnassigned && filteredUnassigned.length > 0 && (
            <UnassignedRow
              events={filteredUnassigned}
              color="var(--bv-fg-muted, #888)"
              onActivateEvent={(ev) => setSelectedEventId(ev.id)}
              locale={locale}
              sort={filters.sort}
            />
          )}
        </ol>
      )}

      {hasPhases && view === "flat" && (
        <FlatList
          phases={filteredPhases}
          unassigned={filters.showUnassigned ? filteredUnassigned : []}
          sort={filters.sort}
          onActivateEvent={(ev) => setSelectedEventId(ev.id)}
          locale={locale}
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

      {editing && isOwner && (
        <CarePhaseEditor
          patientId={patientId}
          timeline={timeline}
          onClose={() => setEditing(false)}
          onChanged={refresh}
        />
      )}

      {planDialogOpen && (
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

// ---------------------------------------------------------------------
// Subcomponents
// ---------------------------------------------------------------------

const railStyle: CSSProperties = {
  listStyle: "none",
  margin: 0,
  padding: 0,
  display: "flex",
  flexDirection: "column",
  gap: "1rem",
  position: "relative",
  // The vertical dashed rail. Lives inside ::before so it sits behind
  // the chips/dots without claiming layout space.
  // eslint-disable-next-line @typescript-eslint/consistent-type-assertions
};

function PhaseRow({
  phase,
  expanded,
  onToggle,
  onNavigate,
  onActivateEvent,
  locale,
  sort,
}: {
  phase: CarePhaseDetail;
  expanded: boolean;
  onToggle: () => void;
  onNavigate: () => void;
  onActivateEvent: (ev: TimelineEvent) => void;
  locale: string;
  sort: SortDir;
}) {
  const t = useTranslations("careTimelinePanel");
  // The events for this phase, wrapped as rail items so the swimlane
  // can group by date. Same color across the whole row (matches the
  // legacy "phase chip → dot colour" mapping).
  const items: TimelineEventItem[] = useMemo(
    () => eventsToRailItems(phase.events, phase.color_hex),
    [phase.events, phase.color_hex],
  );
  return (
    <div
      style={{
        display: "grid",
        gridTemplateColumns: "minmax(140px, 200px) 1fr",
        gap: "1rem",
        alignItems: "start",
        position: "relative",
      }}
      className="care-timeline-row"
    >
      <div style={{ position: "relative" }}>
        <PhaseChip phase={phase} expanded={expanded} onToggle={onToggle} onNavigate={onNavigate} />
      </div>
      <div
        style={{
          paddingLeft: "1rem",
          borderLeft: "1px dashed var(--bv-card-border, #ccc)",
          minHeight: 32,
        }}
        aria-hidden={!expanded}
      >
        {expanded ? (
          phase.events.length === 0 ? (
            <p className="meta" style={{ fontSize: "0.78rem", margin: "0.25rem 0" }}>
              {t("emptyInPhase")}
            </p>
          ) : (
            <TimelineRail
              items={items}
              locale={locale}
              sort={sort}
              undatedLabel={t("undatedBucket")}
              renderItem={(item) => (
                <TimelineEventDot
                  event={item.payload}
                  color={item.color}
                  variant="horizontal"
                  onActivate={onActivateEvent}
                />
              )}
            />
          )
        ) : (
          <p className="meta" style={{ fontSize: "0.78rem", margin: "0.25rem 0" }}>
            {t("eventsCollapsed", { n: phase.events.length })}
          </p>
        )}
      </div>
    </div>
  );
}

function UnassignedRow({
  events,
  color,
  onActivateEvent,
  locale,
  sort,
}: {
  events: TimelineEvent[];
  color: string;
  onActivateEvent: (ev: TimelineEvent) => void;
  locale: string;
  sort: SortDir;
}) {
  const t = useTranslations("careTimelinePanel");
  const items = useMemo(() => eventsToRailItems(events, color), [events, color]);
  return (
    <li>
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "minmax(140px, 200px) 1fr",
          gap: "1rem",
          alignItems: "start",
        }}
      >
        <div
          style={{
            padding: "0.5rem 0.7rem",
            border: "1px dashed var(--bv-card-border, #ccc)",
            borderRadius: 8,
            fontSize: "0.82rem",
            color: "var(--bv-fg-soft)",
          }}
        >
          {t("unassignedHeading")}
          <div style={{ fontSize: "0.7rem" }}>{t("eventsCollapsed", { n: events.length })}</div>
        </div>
        <div
          style={{
            paddingLeft: "1rem",
            borderLeft: "1px dashed var(--bv-card-border, #ccc)",
          }}
        >
          <TimelineRail
            items={items}
            locale={locale}
            sort={sort}
            undatedLabel={t("undatedBucket")}
            renderItem={(item) => (
              <TimelineEventDot
                event={item.payload}
                color={item.color}
                variant="horizontal"
                onActivate={onActivateEvent}
              />
            )}
          />
        </div>
      </div>
    </li>
  );
}

function eventDateComparator(dir: SortDir): (a: TimelineEvent, b: TimelineEvent) => number {
  // Null dates sink to the bottom regardless of direction so they don't
  // pollute the chronological reading. Same shape as the backend SQL
  // (NULLS LAST) — keeps the UI consistent with `find_clinical_events`.
  return (a, b) => {
    const da = a.event_date;
    const db = b.event_date;
    if (da === null && db === null) return 0;
    if (da === null) return 1;
    if (db === null) return -1;
    return dir === "asc" ? da.localeCompare(db) : db.localeCompare(da);
  };
}

function FlatList({
  phases,
  unassigned,
  sort,
  onActivateEvent,
  locale,
}: {
  phases: CarePhaseDetail[];
  unassigned: TimelineEvent[];
  sort: SortDir;
  onActivateEvent: (ev: TimelineEvent) => void;
  locale: string;
}) {
  const t = useTranslations("careTimelinePanel");
  // Merge all events into a single rail-item list, preserving the
  // originating phase's colour. The rail's groupByDate then collapses
  // same-day items onto one swimlane row — the main visual win of the
  // flat view: a real chronological reading without the phase chrome.
  const items = useMemo<TimelineEventItem[]>(() => {
    const out: TimelineEventItem[] = [];
    for (const p of phases) {
      for (const ev of p.events) out.push(eventToRailItem(ev, p.color_hex));
    }
    for (const ev of unassigned) out.push(eventToRailItem(ev, "var(--bv-fg-muted, #888)"));
    return out;
  }, [phases, unassigned]);

  return (
    <TimelineRail
      items={items}
      locale={locale}
      sort={sort}
      undatedLabel={t("undatedBucket")}
      renderItem={(item) => (
        <TimelineEventDot
          event={item.payload}
          color={item.color}
          variant="horizontal"
          onActivate={onActivateEvent}
        />
      )}
    />
  );
}

function SaluteBar({
  health,
  onPropose,
  proposing,
  proposeError,
  isOwner,
  llmAvailable,
}: {
  health: TimelineHealth | null;
  onPropose: () => void;
  proposing: boolean;
  proposeError: string | null;
  isOwner: boolean;
  /** Probe of ``/api/system/features.llm_classifier``. ``null`` while
   * the request is in flight; ``false`` after a 503 from propose. */
  llmAvailable: boolean | null;
}) {
  const t = useTranslations("careTimelinePanel");
  if (!health) return null;
  const llmDisabled = llmAvailable === false;
  return (
    <section
      aria-label={t("saluteAriaLabel")}
      style={{
        display: "flex",
        flexWrap: "wrap",
        alignItems: "center",
        gap: "0.6rem 1.2rem",
        padding: "0.6rem 0.8rem",
        marginBottom: "0.85rem",
        background: "var(--bv-card-bg)",
        border: "1px solid var(--bv-card-border)",
        borderRadius: 8,
        fontSize: "0.82rem",
      }}
    >
      <Stat label={t("statPhases")} value={String(health.n_phases)} />
      <Stat
        label={t("statEventsAssigned")}
        value={`${health.n_events_assigned}/${health.n_events} (${Math.round(
          health.pct_assigned * 100,
        )}%)`}
      />
      <Stat
        label={t("statLastRun")}
        value={
          health.last_classifier_run ? new Date(health.last_classifier_run).toLocaleString() : "—"
        }
      />
      {health.pending_proposals > 0 && (
        <span
          style={{
            padding: "0.15rem 0.45rem",
            borderRadius: 999,
            background: "var(--bv-warning-soft, #fef3c7)",
            color: "var(--bv-warning, #92400e)",
            fontSize: "0.72rem",
          }}
        >
          {t("pendingProposals", { n: health.pending_proposals })}
        </span>
      )}
      <span style={{ flex: 1 }} />
      {isOwner && (
        <button
          type="button"
          onClick={onPropose}
          disabled={proposing || llmDisabled}
          aria-disabled={proposing || llmDisabled}
          className="ghost"
          title={llmDisabled ? t("proposeViaMcpShort") : t("reproposeTitle")}
        >
          {proposing ? t("reproposeBusy") : t("repropose")}
        </button>
      )}
      {proposeError && <span style={{ color: "var(--bv-danger, #c00)" }}>{proposeError}</span>}
    </section>
  );
}

function StatusBanner({
  upcoming,
  overdue,
  onFilterUpcoming,
}: {
  upcoming: number;
  overdue: number;
  onFilterUpcoming: () => void;
}) {
  const t = useTranslations("eventStatus");
  return (
    <section
      // Polite live region: when the timeline reloads and the banner
      // appears, screen readers announce it without interrupting the
      // user's current task.
      aria-live="polite"
      aria-label={t("bannerAriaLabel")}
      style={{
        display: "flex",
        flexWrap: "wrap",
        gap: "0.6rem 1rem",
        alignItems: "center",
        padding: "0.5rem 0.8rem",
        marginBottom: "0.85rem",
        background:
          overdue > 0
            ? "var(--bv-status-missed-bg, rgba(217,119,6,0.08))"
            : "var(--bv-status-planned-bg, rgba(91,141,239,0.06))",
        border: `1px solid ${
          overdue > 0
            ? "var(--bv-status-missed-border, #d97706)"
            : "var(--bv-status-planned-border, #5b8def)"
        }`,
        borderRadius: 8,
        fontSize: "0.82rem",
      }}
    >
      {upcoming > 0 && (
        <span>
          <strong style={{ marginRight: 4 }}>⏳ {upcoming}</strong>
          {t("bannerUpcoming", { n: upcoming })}
        </span>
      )}
      {overdue > 0 && (
        <span style={{ color: "var(--bv-status-missed-border, #9a5b04)" }}>
          <strong style={{ marginRight: 4 }}>△ {overdue}</strong>
          {t("bannerOverdue", { n: overdue })}
        </span>
      )}
      <span style={{ flex: 1 }} />
      <button
        type="button"
        className="ghost"
        onClick={onFilterUpcoming}
        style={{ fontSize: "0.75rem" }}
      >
        {t("bannerFilterCta")}
      </button>
    </section>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <span style={{ display: "inline-flex", flexDirection: "column" }}>
      <span style={{ fontSize: "0.7rem", color: "var(--bv-fg-soft)" }}>{label}</span>
      <span style={{ fontWeight: 600 }}>{value}</span>
    </span>
  );
}

function FilterBar({
  filters,
  view,
  timeline,
  onChange,
  onToggleEdit,
  onOpenPlanDialog,
  editing,
  isOwner,
}: {
  filters: Filters;
  view: "grouped" | "flat";
  timeline: CareTimeline;
  onChange: (key: string, value: string | null) => void;
  onToggleEdit: () => void;
  onOpenPlanDialog: () => void;
  editing: boolean;
  isOwner: boolean;
}) {
  const t = useTranslations("careTimelinePanel");
  const tStatus = useTranslations("eventStatus");
  const tCalendar = useTranslations("calendar");
  // Phase kinds present in this timeline (avoid showing chips for
  // kinds that have zero phases for this patient).
  const phaseKindsPresent = useMemo(() => {
    const set = new Set<string>();
    for (const p of timeline.phases) set.add(p.kind);
    return Array.from(set);
  }, [timeline]);

  // Status chips show only when the patient has more than one status
  // represented (else they're noise on a fully historical fascicolo).
  const statusesPresent = useMemo<EventStatus[]>(() => {
    const set = new Set<EventStatus>();
    for (const p of timeline.phases)
      for (const ev of p.events) set.add(ev.event_status ?? "completed");
    for (const ev of timeline.unassigned_events) set.add(ev.event_status ?? "completed");
    return STATUS_FILTER_VALUES.filter((s) => set.has(s));
  }, [timeline]);

  function toggleStatus(s: EventStatus): void {
    const current = filters.statuses;
    if (current === null) {
      // Default visible set → narrow to just this one (single-chip
      // shortcut, common case: "show me only the planned events").
      onChange(URL_KEYS.status, s);
      return;
    }
    const next = new Set<EventStatus>(current);
    if (next.has(s)) next.delete(s);
    else next.add(s);
    onChange(URL_KEYS.status, next.size === 0 ? null : Array.from(next).join(","));
  }

  function isStatusActive(s: EventStatus): boolean {
    if (filters.statuses === null) {
      // Reflects the default visible set (defined in CareTimelineComponent).
      return s === "planned" || s === "confirmed" || s === "completed" || s === "missed";
    }
    return filters.statuses.has(s);
  }

  return (
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
        active={filters.eventKind === null}
        onClick={() => onChange(URL_KEYS.eventKind, null)}
        label={t("allEvents")}
      />
      {EVENT_KINDS.map((k) => (
        <Chip
          key={k}
          active={filters.eventKind === k}
          onClick={() => onChange(URL_KEYS.eventKind, filters.eventKind === k ? null : k)}
          label={shortEventKind(k, t)}
        />
      ))}
      <span style={{ width: 1, height: 18, background: "var(--bv-card-border)" }} />
      {phaseKindsPresent.map((k) => (
        <Chip
          key={k}
          active={filters.phaseKind === k}
          onClick={() => onChange(URL_KEYS.phaseKind, filters.phaseKind === k ? null : k)}
          label={PHASE_KIND_LABEL[k] ?? k}
        />
      ))}
      {statusesPresent.length > 1 && (
        <>
          <span style={{ width: 1, height: 18, background: "var(--bv-card-border)" }} />
          {statusesPresent.map((s) => (
            <Chip
              key={s}
              active={isStatusActive(s)}
              onClick={() => toggleStatus(s)}
              label={tStatus(s)}
            />
          ))}
        </>
      )}
      <input
        type="date"
        value={filters.dateFrom ?? ""}
        onChange={(e) => onChange(URL_KEYS.dateFrom, e.target.value || null)}
        style={smallInputStyle}
        title={t("dateFromTitle")}
      />
      <input
        type="date"
        value={filters.dateTo ?? ""}
        onChange={(e) => onChange(URL_KEYS.dateTo, e.target.value || null)}
        style={smallInputStyle}
        title={t("dateToTitle")}
      />
      <input
        type="search"
        placeholder={t("searchPlaceholder")}
        value={filters.q}
        onChange={(e) => onChange(URL_KEYS.q, e.target.value || null)}
        style={{ ...smallInputStyle, minWidth: 140 }}
      />
      <label
        style={{
          display: "inline-flex",
          alignItems: "center",
          gap: "0.3rem",
          fontSize: "0.78rem",
        }}
      >
        <input
          type="checkbox"
          checked={filters.showUnassigned}
          onChange={(e) => onChange(URL_KEYS.unassigned, e.target.checked ? null : "0")}
        />
        {t("unassignedToggle")}
      </label>
      <Chip
        active={view === "grouped"}
        onClick={() => onChange(URL_KEYS.view, "grouped")}
        label={t("byPhase")}
      />
      <Chip
        active={view === "flat"}
        onClick={() => onChange(URL_KEYS.view, "flat")}
        label={t("viewFlat")}
      />
      <button
        type="button"
        className="ghost"
        onClick={() => onChange(URL_KEYS.sort, filters.sort === "desc" ? "asc" : null)}
        style={{ ...smallButtonStyle, display: "inline-flex", alignItems: "center", gap: "0.3rem" }}
        title={filters.sort === "desc" ? t("sortDescTitle") : t("sortAscTitle")}
        aria-label={filters.sort === "desc" ? t("sortDescTitle") : t("sortAscTitle")}
        aria-pressed={filters.sort === "asc"}
      >
        <span aria-hidden>{filters.sort === "desc" ? "↓" : "↑"}</span>
        <span>{filters.sort === "desc" ? t("sortDescLabel") : t("sortAscLabel")}</span>
      </button>
      <span style={{ flex: 1 }} />
      <MergedToggleButton />
      <ExportButtons patientId={timeline.patient_id} />
      {isOwner && (
        <button
          type="button"
          onClick={onOpenPlanDialog}
          style={{ fontSize: "0.78rem" }}
          title={tCalendar("planEvent")}
        >
          + {tCalendar("planEvent")}
        </button>
      )}
      {isOwner && (
        <button
          type="button"
          onClick={onToggleEdit}
          className={editing ? undefined : "ghost"}
          style={{ fontSize: "0.78rem" }}
          title={t("editToggleTitle")}
        >
          {editing ? t("closeEdit") : t("edit")}
        </button>
      )}
    </div>
  );
}

function MergedToggleButton() {
  // Tiny inline component: navigates to the same patient page with
  // ``?merge=1`` so the parent FascicoloViewToggle swaps in the
  // MergedTimelineView in place of this CareTimeline. The state
  // change is URL-only — no client-side handoff, no React context.
  const t = useTranslations("timeline");
  const router = useRouter();
  const searchParams = useSearchParams();
  return (
    <button
      type="button"
      className="ghost"
      onClick={() => {
        const next = new URLSearchParams(searchParams.toString());
        next.set("merge", "1");
        router.push(`?${next.toString()}`, { scroll: false });
      }}
      title={t("toggleMergedTitle")}
      style={{
        fontSize: "0.72rem",
        padding: "0.18rem 0.5rem",
        borderRadius: 6,
      }}
    >
      {t("toggleMergedLabel")}
    </button>
  );
}

function ExportButtons({ patientId }: { patientId: string }) {
  const t = useTranslations("careTimelinePanel");
  const locale = useLocale();
  // Anchor-with-href doesn't carry the localStorage Bearer token, so
  // plain ``<a href>`` against the API responds 401. Trigger the
  // download via ``authedDownload`` (fetch + blob) instead.
  function download(format: "svg" | "pdf" | "ics", ext: string) {
    const url = `${API_BASE_URL}/api/patients/${patientId}/care-timeline?format=${format}&lang=${locale}`;
    void authedDownload(url, `care-timeline-${patientId}.${ext}`);
  }
  return (
    <span style={{ display: "inline-flex", gap: "0.25rem" }}>
      <button
        type="button"
        onClick={() => download("svg", "svg")}
        className="ghost"
        style={smallButtonStyle}
        title={t("exportSvg")}
      >
        SVG
      </button>
      <button
        type="button"
        onClick={() => download("pdf", "pdf")}
        className="ghost"
        style={smallButtonStyle}
        title={t("exportPdf")}
      >
        PDF
      </button>
      <button
        type="button"
        onClick={() => download("ics", "ics")}
        className="ghost"
        style={smallButtonStyle}
        title={t("exportIcal")}
      >
        iCal
      </button>
      <button
        type="button"
        className="ghost"
        style={smallButtonStyle}
        onClick={() => {
          if (typeof window === "undefined") return;
          void navigator.clipboard?.writeText(window.location.href);
        }}
        title={t("copyLinkTitle")}
      >
        {t("copyLink")}
      </button>
    </span>
  );
}

function EmptyState({
  variant,
  isOwner,
  proposing,
  onPropose,
  llmAvailable,
}: {
  variant: "no-events" | "events-no-phases";
  isOwner: boolean;
  proposing: boolean;
  onPropose: () => void;
  llmAvailable: boolean | null;
}) {
  const t = useTranslations("careTimelinePanel");
  const llmDisabled = llmAvailable === false;
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
      {variant === "no-events" ? (
        <>
          <p style={{ marginTop: 0 }}>{t("emptyNoEvents")}</p>
          <p style={{ fontSize: "0.85rem" }}>{t("emptyTimeline")}</p>
        </>
      ) : (
        <>
          <p style={{ marginTop: 0 }}>{t("noPhasesYet")}</p>
          {isOwner && (
            <button
              type="button"
              onClick={onPropose}
              disabled={proposing || llmDisabled}
              aria-disabled={proposing || llmDisabled}
              title={llmDisabled ? t("proposeViaMcpShort") : undefined}
              style={{ marginTop: "0.5rem" }}
            >
              {proposing ? t("proposeBusy") : t("propose")}
            </button>
          )}
          {llmDisabled && (
            <p style={{ marginTop: "0.5rem", fontSize: "0.78rem" }}>{t("proposeViaMcpHint")}</p>
          )}
        </>
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

function shortEventKind(k: string, t: (key: string) => string): string {
  const map: Record<string, string> = {
    imaging_study: t("kindStudies"),
    surgical_procedure: t("kindSurgery"),
    outpatient_visit: t("kindVisits"),
    inpatient_admission: t("kindAdmissions"),
    lab_batch: t("kindLab"),
    consultation_event: t("kindConsultations"),
    other: t("kindOther"),
  };
  return map[k] ?? k;
}
