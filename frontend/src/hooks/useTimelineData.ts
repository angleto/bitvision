"use client";

// Unified data hook for the three timeline views (clinical / tasks /
// merged). Fetches the care-timeline JSON and / or the patient-tasks
// list based on the ``includeEvents`` / ``includeTasks`` flags, and
// invalidates on demand via the returned ``refresh`` function.
//
// Why a hook (and not three independent useEffects in each component):
//
// * The merged view needs both fetches in flight at the same time, and
//   wants to expose a single ``loading`` state to the UI (so the
//   caller doesn't show two separate spinners that flicker out of
//   sync).
// * The realtime subscription that triggers refresh on agent edits is
//   per-patient, not per-source — keeping the subscription wiring in
//   one place avoids two subscribe/unsubscribe pairs for the merged
//   view.
// * Sprint A2's CareTimeline already inlined this logic; A3 lifts it
//   so TaskTimeline / MergedTimelineView don't re-implement the same
//   loading-state plumbing.

import { useCallback, useEffect, useState } from "react";

import {
  type CareTimeline,
  type PatientTask,
  type TimelineHealth,
  carePhasesApi,
  tasksApi,
} from "@/lib/api_records";
import { subscribeCarePhases } from "@/lib/care_phase_realtime";

export interface UseTimelineDataOptions {
  patientId: string;
  /** Locale forwarded to ``carePhasesApi.timeline`` for date labels
   *  inside the timeline JSON. */
  locale: string;
  includeEvents: boolean;
  includeTasks: boolean;
  /**
   * If ``true``, also fetches the timeline-health snapshot used by
   * the SaluteBar in CareTimeline. The merged view skips this because
   * its SaluteBar is rendered by the underlying CareTimeline-style
   * panel; TaskTimeline doesn't need it at all.
   */
  includeHealth?: boolean;
  /**
   * Optional filter for the tasks list. ``include_deleted`` defaults
   * to ``false`` so tombstones stay hidden.
   */
  taskFilters?: Parameters<typeof tasksApi.list>[1];
}

export interface UseTimelineDataResult {
  timeline: CareTimeline | null;
  tasks: PatientTask[] | null;
  health: TimelineHealth | null;
  loading: boolean;
  error: string | null;
  /** Force a re-fetch. Used by post-mutation handlers + by the agent
   *  realtime subscription. */
  refresh: () => void;
}

export function useTimelineData(options: UseTimelineDataOptions): UseTimelineDataResult {
  const {
    patientId,
    locale,
    includeEvents,
    includeTasks,
    includeHealth = false,
    taskFilters,
  } = options;
  const [timeline, setTimeline] = useState<CareTimeline | null>(null);
  const [tasks, setTasks] = useState<PatientTask[] | null>(null);
  const [health, setHealth] = useState<TimelineHealth | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [reloadTick, setReloadTick] = useState(0);

  const refresh = useCallback(() => setReloadTick((x) => x + 1), []);

  // The dependency on ``taskFilters`` is by-reference: the caller is
  // expected to memoise the filter object if it wants stable identity
  // (otherwise every render forces a refetch). We pin only the
  // declared inputs.
  // biome-ignore lint/correctness/useExhaustiveDependencies: reloadTick + locale + filter object are the explicit invalidation triggers.
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    const promises: Promise<unknown>[] = [];

    if (includeEvents) {
      promises.push(
        carePhasesApi
          .timeline(patientId, { lang: locale })
          .then((tl) => {
            if (!cancelled) setTimeline(tl);
          })
          .catch((e: unknown) => {
            if (!cancelled) setError(e instanceof Error ? e.message : "timeline load failed");
          }),
      );
    } else {
      setTimeline(null);
    }

    if (includeTasks) {
      promises.push(
        tasksApi
          .list(patientId, taskFilters ?? {})
          .then((rows) => {
            if (!cancelled) setTasks(rows);
          })
          .catch((e: unknown) => {
            if (!cancelled) setError(e instanceof Error ? e.message : "tasks load failed");
          }),
      );
    } else {
      setTasks(null);
    }

    if (includeHealth && includeEvents) {
      promises.push(
        carePhasesApi
          .health(patientId)
          .then((h) => {
            if (!cancelled) setHealth(h);
          })
          .catch(() => {
            if (!cancelled) setHealth(null);
          }),
      );
    } else {
      setHealth(null);
    }

    Promise.allSettled(promises).then(() => {
      if (!cancelled) setLoading(false);
    });

    return () => {
      cancelled = true;
    };
  }, [patientId, locale, includeEvents, includeTasks, includeHealth, taskFilters, reloadTick]);

  // Live updates from agents / peers. The subscription is scoped per
  // patient; both events and tasks invalidate on the same signal
  // (the backend bus emits one care-phases-touched message that
  // covers both surfaces). Wiring lives here so multiple consumers
  // of the hook on the same page share the same subscription cost.
  useEffect(() => {
    return subscribeCarePhases(patientId, () => refresh());
  }, [patientId, refresh]);

  return { timeline, tasks, health, loading, error, refresh };
}
