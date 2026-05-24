"use client";

// React hooks for polling the Jobs API (DESIGN.md §11.8).
//
// useJob: track a single job from queued → running → terminal. Poll
// with exponential backoff so a quick job hits the result fast and a
// slow one does not hammer the API.
//
// useActiveJobs: list the caller's active jobs at low frequency for
// the "operazioni in corso" panel.
//
// Polling is intentionally hand-rolled (no react-query) so the
// surface stays small and the dependency graph clean.

import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError } from "./api";
import { JOB_TERMINAL_STATUSES, type JobOut, jobsApi, jobsStorage } from "./jobs";

const INITIAL_DELAY_MS = 1000;
const MAX_DELAY_MS = 15000;
const ACTIVE_LIST_INTERVAL_MS = 30000;

interface UseJobResult {
  job: JobOut | null;
  isLoading: boolean;
  error: ApiError | Error | null;
  refetch: () => void;
}

/**
 * Poll a single job by id with exponential backoff (1s → 2s → 4s …
 * capped at 15s). Stops polling when the job reaches a terminal
 * state. Pass ``jobId = null`` to disable.
 */
export function useJob(jobId: string | null): UseJobResult {
  const [job, setJob] = useState<JobOut | null>(null);
  const [isLoading, setIsLoading] = useState(jobId !== null);
  const [error, setError] = useState<ApiError | Error | null>(null);
  const [version, setVersion] = useState(0);

  const cancelledRef = useRef(false);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // biome-ignore lint/correctness/useExhaustiveDependencies: ``version`` is the explicit refetch trigger bumped by ``refetch()``; biome doesn't see the indirection.
  useEffect(() => {
    cancelledRef.current = false;
    if (timerRef.current !== null) {
      clearTimeout(timerRef.current);
      timerRef.current = null;
    }
    if (!jobId) {
      setJob(null);
      setIsLoading(false);
      setError(null);
      return;
    }

    let delay = INITIAL_DELAY_MS;

    async function tick(): Promise<void> {
      try {
        const fetched = await jobsApi.get(jobId as string);
        if (cancelledRef.current) return;
        setJob(fetched);
        setError(null);
        if (JOB_TERMINAL_STATUSES.has(fetched.status)) {
          setIsLoading(false);
          jobsStorage.remove(fetched.id);
          return;
        }
        delay = Math.min(delay * 2, MAX_DELAY_MS);
        timerRef.current = setTimeout(tick, delay);
      } catch (e) {
        if (cancelledRef.current) return;
        const err = e instanceof Error ? e : new Error(String(e));
        setError(err);
        // On 404 (job vanished, e.g. cleanup beat us) stop polling;
        // any other error retries with backoff so a transient blip
        // does not strand the UI.
        if (e instanceof ApiError && e.status === 404) {
          setIsLoading(false);
          jobsStorage.remove(jobId as string);
          return;
        }
        delay = Math.min(delay * 2, MAX_DELAY_MS);
        timerRef.current = setTimeout(tick, delay);
      }
    }

    setIsLoading(true);
    void tick();

    return () => {
      cancelledRef.current = true;
      if (timerRef.current !== null) {
        clearTimeout(timerRef.current);
        timerRef.current = null;
      }
    };
  }, [jobId, version]);

  const refetch = useCallback(() => setVersion((v) => v + 1), []);

  return { job, isLoading, error, refetch };
}

interface UseActiveJobsResult {
  jobs: JobOut[];
  isLoading: boolean;
  error: ApiError | Error | null;
  refetch: () => void;
}

/**
 * List the caller's active jobs (queued + running). Polls every
 * ``intervalMs`` (default 30s); call ``refetch()`` after a
 * user-initiated change for an immediate refresh.
 */
export function useActiveJobs(
  options: { kind?: string; intervalMs?: number } = {},
): UseActiveJobsResult {
  const { kind, intervalMs = ACTIVE_LIST_INTERVAL_MS } = options;
  const [jobs, setJobs] = useState<JobOut[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<ApiError | Error | null>(null);
  const [version, setVersion] = useState(0);

  const cancelledRef = useRef(false);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // biome-ignore lint/correctness/useExhaustiveDependencies: ``version`` is the explicit refetch trigger bumped by ``refetch()``; biome doesn't see the indirection.
  useEffect(() => {
    cancelledRef.current = false;

    async function tick(): Promise<void> {
      try {
        const list = await jobsApi.listActive(kind);
        if (cancelledRef.current) return;
        setJobs(list.items);
        setError(null);
        setIsLoading(false);
      } catch (e) {
        if (cancelledRef.current) return;
        setError(e instanceof Error ? e : new Error(String(e)));
        setIsLoading(false);
      }
      if (cancelledRef.current) return;
      timerRef.current = setTimeout(tick, intervalMs);
    }

    setIsLoading(true);
    void tick();

    return () => {
      cancelledRef.current = true;
      if (timerRef.current !== null) {
        clearTimeout(timerRef.current);
        timerRef.current = null;
      }
    };
  }, [kind, intervalMs, version]);

  const refetch = useCallback(() => setVersion((v) => v + 1), []);

  return { jobs, isLoading, error, refetch };
}

/**
 * List the caller's recently-completed jobs (succeeded / failed /
 * cancelled). Polls every ``intervalMs`` (default 60s); used by the
 * "esportazioni recenti" surface so a user who closed the export
 * dialog before completion can still find the ZIP.
 *
 * The backend bounds the list by ``expires_at`` (default 7 days for
 * exports) and the cleanup cron drops rows past their TTL, so this
 * list never grows unbounded.
 */
/**
 * Per-resource job binding for "request → poll → download" UIs that
 * sit on a study card or document row.
 *
 * Looks up the most-recently-enqueued tracked job for a given
 * ``(scope_kind, scope_id)`` in ``jobsStorage`` and resumes polling
 * via :func:`useJob`. Survives reload / logout / fresh tab because
 * the localStorage entry is created at enqueue time and removed at
 * terminal-state.
 *
 * Returns ``jobId = null`` when no tracked job exists for the scope
 * (idle state) so the caller can render a fresh "request export"
 * button. Set ``jobId`` via the returned ``bind`` setter immediately
 * after a successful enqueue so the UI flips into the "queued" state
 * without waiting for the next storage scan.
 */
export function useScopedJob(
  scope_kind: string,
  scope_id: string | null,
  /** Optional server-side fallback. When ``localStorage`` has no
   *  tracked job for this scope (fresh device / browser / private
   *  tab), the hook queries ``GET /api/jobs?kind=<jobKind>&scope_id=...``
   *  and rebinds to the most-recently-created match. Pass the same
   *  ``kind`` value used at enqueue time. */
  jobKind?: string,
): UseJobResult & {
  bind: (jobId: string | null) => void;
} {
  const [overrideId, setOverrideId] = useState<string | null>(null);
  const [storedId, setStoredId] = useState<string | null>(null);

  // biome-ignore lint/correctness/useExhaustiveDependencies: storedId initialiser only depends on scope; rescans are explicit via bind().
  useEffect(() => {
    let cancelled = false;
    if (!scope_id) {
      setStoredId(null);
      return () => {
        cancelled = true;
      };
    }
    const found = jobsStorage.findByScope(scope_kind, scope_id);
    if (found) {
      setStoredId(found.id);
      return () => {
        cancelled = true;
      };
    }
    // localStorage miss → check the server. This is the
    // cross-device path: a Job started on another browser is
    // discoverable here through ``scope_ids`` on the row (backend
    // migration 0092). We re-populate ``jobsStorage`` so the next
    // mount short-circuits without a network round-trip.
    if (!jobKind) {
      setStoredId(null);
      return () => {
        cancelled = true;
      };
    }
    setStoredId(null);
    void (async () => {
      try {
        // Active first: an in-flight job is the highest-priority
        // bind target.
        let list = await jobsApi.listActive(jobKind, scope_id);
        if (cancelled) return;
        let candidate: JobOut | undefined = list.items[0];
        if (!candidate) {
          // No active match → look for a recently-completed job that
          // still has a downloadable artifact.
          list = await jobsApi.listRecent(jobKind, 10, scope_id);
          if (cancelled) return;
          candidate = list.items.find((j) => j.status === "succeeded" && !!j.result_download_url);
        }
        if (!candidate) return;
        jobsStorage.add({
          id: candidate.id,
          kind: candidate.kind,
          scope_kind,
          scope_id,
        });
        setStoredId(candidate.id);
      } catch {
        // Server-fallback errors are non-fatal: the user can still
        // start a fresh export. Silent so a 401 during a token
        // refresh doesn't flash a scary toast on every card.
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [scope_kind, scope_id, jobKind]);

  const effectiveId = overrideId ?? storedId;
  const result = useJob(effectiveId);

  // Drop the localStorage entry when the bound job reaches a terminal
  // state — useJob itself does this for the "active" lifetime, but we
  // also clear our local override so the next render sees the fresh
  // idle state.
  useEffect(() => {
    if (result.job && JOB_TERMINAL_STATUSES.has(result.job.status)) {
      // Keep the localStorage entry for ``succeeded`` so the user can
      // still click "Save ZIP" after a reload until they consume it
      // or the 48h TTL elapses; remove on failure/cancel so a retry
      // starts fresh.
      if (result.job.status !== "succeeded") {
        jobsStorage.remove(result.job.id);
      }
    }
  }, [result.job]);

  return { ...result, bind: setOverrideId };
}

export function useRecentJobs(
  options: { kind?: string; intervalMs?: number; limit?: number } = {},
): UseActiveJobsResult {
  const { kind, intervalMs = 60_000, limit = 20 } = options;
  const [jobs, setJobs] = useState<JobOut[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<ApiError | Error | null>(null);
  const [version, setVersion] = useState(0);

  const cancelledRef = useRef(false);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // biome-ignore lint/correctness/useExhaustiveDependencies: ``version`` is the explicit refetch trigger bumped by ``refetch()``; biome doesn't see the indirection.
  useEffect(() => {
    cancelledRef.current = false;

    async function tick(): Promise<void> {
      try {
        const list = await jobsApi.listRecent(kind, limit);
        if (cancelledRef.current) return;
        setJobs(list.items);
        setError(null);
        setIsLoading(false);
      } catch (e) {
        if (cancelledRef.current) return;
        setError(e instanceof Error ? e : new Error(String(e)));
        setIsLoading(false);
      }
      if (cancelledRef.current) return;
      timerRef.current = setTimeout(tick, intervalMs);
    }

    setIsLoading(true);
    void tick();

    return () => {
      cancelledRef.current = true;
      if (timerRef.current !== null) {
        clearTimeout(timerRef.current);
        timerRef.current = null;
      }
    };
  }, [kind, intervalMs, limit, version]);

  const refetch = useCallback(() => setVersion((v) => v + 1), []);

  return { jobs, isLoading, error, refetch };
}
