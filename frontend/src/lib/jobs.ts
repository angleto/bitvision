// Long-running Jobs client (DESIGN.md §11).
//
// Mirrors the backend Jobs API: status snapshot, active list,
// cancellation, plus localStorage helpers so a refresh between the
// POST that enqueues and the first GET that fetches the list still
// surfaces the active job. Polling hooks live in ./useJob.ts.

import { API_BASE_URL, ApiError, getStoredToken } from "./api";

export type JobStatus = "queued" | "running" | "succeeded" | "failed" | "cancelled";

export const JOB_TERMINAL_STATUSES: ReadonlySet<JobStatus> = new Set<JobStatus>([
  "succeeded",
  "failed",
  "cancelled",
]);

export interface JobOut {
  id: string;
  kind: string;
  owner_subject_id: string;
  status: JobStatus;
  progress_total: number | null;
  progress_done: number;
  stage: string | null;
  result_uri: string | null;
  /** Freshly-signed presigned GET URL, recomputed by the backend on
   * every read. May be null while the job is not yet succeeded, or
   * for jobs whose result_uri is not an s3:// pointer. */
  result_download_url: string | null;
  /** Optional structured result payload — workers (e.g. bulk_upload)
   * stash a JSON-serialisable summary here on succeeded so the UI
   * can render counts + skipped reasons without an extra fetch.
   * Shape is per-kind; consumers narrow with a type guard. */
  result: Record<string, unknown> | null;
  /** Doctor-friendly label written by the enqueue endpoint
   *  (``"TC torace 2024-12-01"``, ``"Fascicolo: Mamma"``, …). The
   *  JobsTray uses this so two parallel exports of the same kind are
   *  visually distinguishable. NULL on legacy rows or kinds that
   *  haven't opted in yet. */
  display_label: string | null;
  error: { code?: string; message?: string; [k: string]: unknown } | null;
  /** Resource ids the job is scoped to (mirrors the dedup hash
   *  inputs). Lets per-resource UIs match a server-side Job to a
   *  card without a localStorage hint — needed for cross-device
   *  recovery. NULL on legacy rows from before backend migration
   *  0092. */
  scope_ids: string[] | null;
  created_at: string;
  updated_at: string;
  started_at: string | null;
  finished_at: string | null;
  expires_at: string;
}

export interface JobListOut {
  items: JobOut[];
}

async function jobsRequest<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  const token = getStoredToken();
  if (token) headers.set("authorization", `Bearer ${token}`);
  const resp = await fetch(`${API_BASE_URL}${path}`, {
    credentials: "include",
    ...init,
    headers,
    cache: "no-store",
  });
  if (!resp.ok) {
    let detail: unknown;
    try {
      detail = await resp.json();
    } catch {
      detail = await resp.text();
    }
    throw new ApiError(resp.status, detail);
  }
  if (resp.status === 204) return undefined as T;
  return (await resp.json()) as T;
}

export const jobsApi = {
  /** GET /api/jobs/{id} — refreshes the presigned download URL on
   * every call. */
  get: (jobId: string): Promise<JobOut> =>
    jobsRequest<JobOut>(`/api/jobs/${encodeURIComponent(jobId)}`),

  /** GET /api/jobs?active=true — caller's active jobs. ``scope_id``
   *  narrows to a specific resource (study/document/...). */
  listActive: (kind?: string, scope_id?: string): Promise<JobListOut> => {
    const qs = new URLSearchParams({ active: "true" });
    if (kind) qs.set("kind", kind);
    if (scope_id) qs.set("scope_id", scope_id);
    return jobsRequest<JobListOut>(`/api/jobs?${qs.toString()}`);
  },

  /**
   * GET /api/jobs?active=false — recent jobs regardless of state.
   * Used by the "esportazioni recenti" surface so a user who closed
   * the dialog before the export finished can still find and
   * download the resulting ZIP. The backend already returns rows
   * ordered by ``created_at desc`` and bounded by ``expires_at``;
   * the cleanup cron drops rows past their TTL so this list is
   * naturally bounded to the last few days of work.
   */
  listRecent: (kind?: string, limit = 20, scope_id?: string): Promise<JobListOut> => {
    const qs = new URLSearchParams({ active: "false", limit: String(limit) });
    if (kind) qs.set("kind", kind);
    if (scope_id) qs.set("scope_id", scope_id);
    return jobsRequest<JobListOut>(`/api/jobs?${qs.toString()}`);
  },

  /** DELETE /api/jobs/{id} — request cancellation. The DB row flips
   * to ``cancelled`` immediately; running workers honour at next
   * checkpoint. */
  cancel: (jobId: string): Promise<JobOut> =>
    jobsRequest<JobOut>(`/api/jobs/${encodeURIComponent(jobId)}`, {
      method: "DELETE",
    }),
};

// ---- localStorage persistence ---------------------------------------
//
// We keep the last-N enqueued job ids per user in localStorage so a
// page refresh between enqueue and the first list-fetch still shows
// the work. The server-side list is the source of truth; this is a
// fallback for the "I just clicked, then F5" race.

const ACTIVE_JOBS_STORAGE_KEY = "bvp.activeJobs.v1";
const MAX_TRACKED_JOBS = 25;

interface TrackedJob {
  id: string;
  kind: string;
  enqueued_at: string;
  /**
   * Optional scope tag so per-resource UIs (a study card, a document
   * row) can rebind to "their" job after a tab reload / logout / fresh
   * session: the page indexes ``jobsStorage.list()`` by
   * ``(scope_kind, scope_id)`` and resumes polling. Plain server-side
   * lists don't expose scope, so this local hint is what closes the
   * loop without a backend round-trip.
   */
  scope_kind?: string;
  scope_id?: string;
}

function _read(): TrackedJob[] {
  if (typeof window === "undefined") return [];
  try {
    const raw = window.localStorage.getItem(ACTIVE_JOBS_STORAGE_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed
      .filter(
        (j): j is TrackedJob =>
          j != null &&
          typeof j === "object" &&
          typeof (j as TrackedJob).id === "string" &&
          typeof (j as TrackedJob).kind === "string",
      )
      .map((j) => ({
        id: j.id,
        kind: j.kind,
        enqueued_at: j.enqueued_at,
        scope_kind: typeof j.scope_kind === "string" ? j.scope_kind : undefined,
        scope_id: typeof j.scope_id === "string" ? j.scope_id : undefined,
      }));
  } catch {
    return [];
  }
}

function _write(jobs: TrackedJob[]): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(
      ACTIVE_JOBS_STORAGE_KEY,
      JSON.stringify(jobs.slice(0, MAX_TRACKED_JOBS)),
    );
  } catch {
    // Quota errors etc. — fail silently; the panel still works once
    // the server-side list arrives.
  }
}

export const jobsStorage = {
  list: (): TrackedJob[] => _read(),
  add: (job: {
    id: string;
    kind: string;
    scope_kind?: string;
    scope_id?: string;
  }): void => {
    const existing = _read();
    if (existing.some((j) => j.id === job.id)) return;
    _write([
      {
        id: job.id,
        kind: job.kind,
        enqueued_at: new Date().toISOString(),
        scope_kind: job.scope_kind,
        scope_id: job.scope_id,
      },
      ...existing,
    ]);
  },
  remove: (jobId: string): void => {
    _write(_read().filter((j) => j.id !== jobId));
  },
  /** Lookup the most-recently-enqueued tracked job for a given scope.
   *  Used by per-resource UIs (study card, document row) to rebind to
   *  an in-flight or recently-completed export after a reload. */
  findByScope: (scope_kind: string, scope_id: string): TrackedJob | null => {
    for (const j of _read()) {
      if (j.scope_kind === scope_kind && j.scope_id === scope_id) return j;
    }
    return null;
  },
  clear: (): void => _write([]),
};

// ---- Dismissed jobs ------------------------------------------------
//
// Server-side cleanup happens at the 48h TTL boundary; in the meantime
// the recent-jobs list returned by ``GET /api/jobs?active=false``
// keeps growing as the user runs more exports. The user reported the
// JobsTray badge climbing to "0/11 operazioni" with no way to clear
// it. This per-device dismissed set lets the FE hide rows the user
// has already consumed, without changing server-side semantics (a
// dismissed row is still downloadable until its expires_at — open it
// from the share / shares table to bring it back).

const DISMISSED_JOBS_STORAGE_KEY = "bvp.dismissedJobs.v1";

function _readDismissed(): Set<string> {
  if (typeof window === "undefined") return new Set();
  try {
    const raw = window.localStorage.getItem(DISMISSED_JOBS_STORAGE_KEY);
    if (!raw) return new Set();
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return new Set();
    return new Set(parsed.filter((s): s is string => typeof s === "string"));
  } catch {
    return new Set();
  }
}

function _writeDismissed(ids: Set<string>): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(DISMISSED_JOBS_STORAGE_KEY, JSON.stringify([...ids]));
    // Cross-tab sync hook so the JobsTray on every open tab updates
    // when the user dismisses on one tab.
    window.dispatchEvent(new CustomEvent("bv:dismissed-jobs-changed"));
  } catch {
    // quota / disabled storage — non-fatal
  }
}

export const dismissedJobs = {
  list: (): Set<string> => _readDismissed(),
  isDismissed: (jobId: string): boolean => _readDismissed().has(jobId),
  dismiss: (jobId: string): void => {
    const cur = _readDismissed();
    if (cur.has(jobId)) return;
    cur.add(jobId);
    _writeDismissed(cur);
  },
  dismissMany: (jobIds: string[]): void => {
    const cur = _readDismissed();
    let changed = false;
    for (const id of jobIds) {
      if (!cur.has(id)) {
        cur.add(id);
        changed = true;
      }
    }
    if (changed) _writeDismissed(cur);
  },
  /** Drop a dismissal — used when the user re-opens an artefact via
   *  the share dialog so the row reappears in the JobsTray. */
  undismiss: (jobId: string): void => {
    const cur = _readDismissed();
    if (!cur.delete(jobId)) return;
    _writeDismissed(cur);
  },
  clear: (): void => _writeDismissed(new Set()),
};
