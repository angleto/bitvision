// Auto-split from lib/api.ts on 2026-05-21.
//
// Shared primitives used by every domain endpoint module:
//   * URL helpers (API_BASE_URL, absoluteApiUrl)
//   * Auth-token compatibility shims (HttpOnly cookie path)
//   * ApiError + the message-formatter
//   * request() / authedDownload() / mintDownloadUrl()
//   * Paginated<T>, SearchParams, SearchFacets, StudyListParams
//   * qs() query-string builder + QSValue type
//
// Everything else lives in ./index.ts. Callers keep importing
// from "@/lib/api"; the barrel re-exports both.

// Typed client for the bitvision phoenix REST API. JWT (when present)
// is pulled from localStorage on every call; components that need auth
// state go through the AuthContext wrapper in ../auth/context.tsx.

export const API_BASE_URL = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

/**
 * Resolve a backend path to an ABSOLUTE URL (scheme + host + path).
 *
 * ``API_BASE_URL`` is inlined at *build* time from
 * ``NEXT_PUBLIC_API_BASE_URL``. When the frontend image is built
 * without that build-arg — or with an empty string — the value is
 * ``""`` (the ``??`` fallback only catches null/undefined, not ``""``)
 * and a naive ``API_BASE_URL + path`` produces a path-only string.
 * That is harmless for same-origin ``fetch()`` but broken for any URL
 * a human copies out of the app: an iCal subscription URL pasted into
 * Google / Apple Calendar MUST be absolute.
 *
 * Rule:
 *   - ``API_BASE_URL`` is an absolute http(s) URL → use it verbatim
 *     (honors split-domain deployments where the API lives on a
 *     different host than the web app).
 *   - otherwise (empty / relative) → resolve against the browser's
 *     current origin; the user is, by definition, on the public host.
 */
export function absoluteApiUrl(path: string): string {
  if (/^https?:\/\//i.test(API_BASE_URL)) return `${API_BASE_URL}${path}`;
  if (typeof window !== "undefined") return `${window.location.origin}${path}`;
  return `${API_BASE_URL}${path}`;
}

// Auth model
// ----------
// 2026-05-21: the SPA migrated from a JWT-in-localStorage model to a
// strict HttpOnly cookie model (``bvp_session`` set by the backend at
// /api/auth/login). The shims below are kept as a no-op surface so
// callers that previously read / wrote the legacy token compile
// without ceremony — every read returns null and every write is
// silently ignored, because the token now lives in a cookie the JS
// layer cannot touch (XSS-to-account-takeover defence).
//
// New requests rely on ``credentials: "include"`` (see ``request``
// below); the browser attaches the HttpOnly cookie automatically and
// the backend dep ``_creds_from_request`` resolves it.
export function getStoredToken(): string | null {
  return null;
}

export function setStoredToken(_token: string | null): void {
  // no-op — cookie is server-controlled.
}

export class ApiError extends Error {
  readonly status: number;
  readonly detail: unknown;
  constructor(status: number, detail: unknown) {
    super(_apiErrorMessage(status, detail));
    this.status = status;
    this.detail = detail;
  }
}

/**
 * The machine-readable kind of an error, if any. Our backend emits RFC 7807
 * problem bodies (see middleware/problem_details.py) whose ``type`` ends in a
 * slug — ``".../study_not_indexed"``, ``".../not_found"``, etc.; this returns
 * that slug. Falls back to a top-level ``code`` or a nested ``detail.code``
 * for any non-problem-details structured error. Returns null for plain-string
 * details, Pydantic validation arrays, and non-ApiError errors.
 */
export function errorCode(e: unknown): string | null {
  if (!(e instanceof ApiError)) return null;
  const d = e.detail as
    | { type?: unknown; code?: unknown; detail?: { code?: unknown } }
    | null
    | undefined;
  if (!d || typeof d !== "object") return null;
  if (typeof d.type === "string") {
    const slug = d.type.split("/").pop();
    if (slug) return slug;
  }
  const code = typeof d.code === "string" ? d.code : d.detail?.code;
  return typeof code === "string" ? code : null;
}

/**
 * Detect the backend's storage-quota 413 and return the used/quota bytes.
 *
 * Both storage gates emit a 413 whose lifted problem body carries
 * ``storage_quota_exceeded`` — storage_quota.py uses ``code`` +
 * ``bytes_used``/``bytes_quota``; quota.py uses ``error`` +
 * ``used_bytes``/``quota_bytes``. We accept either shape. This lets the UI
 * say "X of Y GiB used — free space or raise the limit" instead of the
 * generic RFC 7807 title "Payload too large", which misleads the user into
 * thinking a single file is oversized when in fact their storage cap is full.
 * Returns null for any non-quota error.
 */
export function storageQuotaExceeded(e: unknown): { usedBytes: number; quotaBytes: number } | null {
  if (!(e instanceof ApiError) || e.status !== 413) return null;
  const d = e.detail as Record<string, unknown> | null | undefined;
  if (!d || typeof d !== "object") return null;
  if (d.code !== "storage_quota_exceeded" && d.error !== "storage_quota_exceeded") return null;
  const num = (...keys: string[]): number => {
    for (const k of keys) {
      const v = d[k];
      if (typeof v === "number") return v;
    }
    return 0;
  };
  return {
    usedBytes: num("bytes_used", "used_bytes"),
    quotaBytes: num("bytes_quota", "quota_bytes"),
  };
}

/**
 * Produce a string suitable for ``Error.message`` from a FastAPI /
 * Starlette response body. Three shapes are common:
 *
 * 1. ``"plain string"`` — usually a Starlette HTTPException with a
 *    string detail (most of our handlers).
 * 2. ``{ detail: "string" }`` — same as above, parsed by JSON.parse.
 * 3. ``{ detail: [{loc, msg, type}, ...] }`` — Pydantic 422 validation
 *    errors. Without the explicit branch the array gets coerced via
 *    ``String([...])`` and the user sees ``[object Object]`` from
 *    every validation 422.
 * 4. ``{ detail: {code, message} }`` — our own structured detail
 *    (visual search "study_not_indexed", future error codes). We
 *    surface the message; the code is preserved on ``ApiError.detail``.
 */
function _apiErrorMessage(status: number, detail: unknown): string {
  if (typeof detail === "string") return detail;
  if (detail && typeof detail === "object") {
    const d = (detail as { detail?: unknown }).detail;
    if (typeof d === "string") return d;
    if (Array.isArray(d)) {
      // Pydantic validation error array — pull the human-readable
      // ``msg`` plus a hint at the offending location for debuggers.
      const parts = d
        .map((e: unknown) => {
          if (e && typeof e === "object") {
            const ee = e as { msg?: string; loc?: unknown };
            const where = Array.isArray(ee.loc) ? ee.loc.join(".") : "";
            return where ? `${ee.msg ?? "invalid"} (at ${where})` : ee.msg;
          }
          return String(e);
        })
        .filter(Boolean);
      if (parts.length > 0) return parts.join("; ");
    }
    if (d && typeof d === "object") {
      const dd = d as { message?: string; code?: string };
      if (typeof dd.message === "string") return dd.message;
      if (typeof dd.code === "string") return dd.code;
    }
  }
  return `HTTP ${status}`;
}

// Centralised 401 handler. Call wherever a server response signals
// "your token is no longer valid" so the auth context can flush
// ``user`` to null and re-render the unauthenticated chrome. Without
// this the SiteHeader kept showing "Log out" while pages rendered
// "authentication required" — confusing the operator into thinking
// they were still signed in.
export function _markAuthExpired(): void {
  // Cookie path: nothing to clear on the JS side — the HttpOnly
  // cookie is either already invalidated server-side or harmless
  // until expiry. Just notify auth-context so the chrome re-renders.
  if (typeof window !== "undefined") {
    window.dispatchEvent(new CustomEvent("bv:auth-401"));
  }
}

/**
 * Trigger an authenticated file download. The browser native
 * ``<a href={url} download>`` flow can't be used here because the
 * auth token lives in ``localStorage`` (set by the SPA on login) and
 * is therefore not attached to plain anchor navigations: the request
 * goes out without ``Authorization`` and the API answers 401. This
 * helper does a regular fetch, materialises the response body as a
 * blob, and triggers a programmatic download.
 *
 * Use it everywhere a download link points at an API endpoint
 * (event attachments, care-timeline SVG/PDF/ICS exports, document
 * downloads). For unauthenticated public URLs (e.g. a future
 * subscription-token ICS feed) keep the plain anchor.
 */
export async function authedDownload(url: string, filename?: string): Promise<void> {
  const resp = await fetch(url, {
    // Send the HttpOnly session cookie so the backend can authorise
    // the download. No Authorization header — the legacy
    // localStorage path was removed in the 2026-05-21 hardening.
    credentials: "include",
    cache: "no-store",
  });
  if (!resp.ok) {
    if (resp.status === 401) _markAuthExpired();
    let detail: unknown = await resp.text();
    try {
      detail = JSON.parse(detail as string);
    } catch {
      /* keep raw text */
    }
    throw new ApiError(resp.status, detail);
  }
  // Derive a filename from Content-Disposition if the caller didn't
  // supply one. Falls back to the last URL segment as a final guess.
  let name = filename;
  if (!name) {
    const cd = resp.headers.get("content-disposition") ?? "";
    const m = /filename\*?=(?:UTF-8'')?["']?([^"';\n]+)["']?/i.exec(cd);
    name = m ? decodeURIComponent(m[1]) : url.split("/").pop() || "download";
  }
  const blob = await resp.blob();
  const blobUrl = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = blobUrl;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  // ``revokeObjectURL`` immediately can race with the download in
  // some browsers; deferring to a microtask is enough.
  setTimeout(() => URL.revokeObjectURL(blobUrl), 1000);
}

export async function request<T>(
  path: string,
  init: RequestInit & { json?: unknown } = {},
): Promise<T> {
  const headers = new Headers(init.headers);
  let body = init.body;
  if (init.json !== undefined) {
    headers.set("content-type", "application/json");
    body = JSON.stringify(init.json);
  }
  const resp = await fetch(`${API_BASE_URL}${path}`, {
    ...init,
    headers,
    body,
    cache: "no-store",
    // HttpOnly session cookie travels via this flag; the auth-model
    // note at the top of the file has the migration rationale.
    credentials: "include",
  });
  if (!resp.ok) {
    if (resp.status === 401) _markAuthExpired();
    // The body stream can only be read once. Read it as text first,
    // then try to parse as JSON; if it isn't JSON we keep the raw
    // text. The previous ``resp.json() || resp.text()`` pattern hit
    // the browser's "body stream already read" guard whenever the
    // first read partially consumed the stream (typical on
    // validation 422 responses with a structured detail).
    const raw = await resp.text();
    let detail: unknown = raw;
    if (raw) {
      try {
        detail = JSON.parse(raw);
      } catch {
        // not JSON; keep the raw text
      }
    }
    throw new ApiError(resp.status, detail);
  }
  if (resp.status === 204) return undefined as T;
  const ct = resp.headers.get("content-type") ?? "";
  if (ct.includes("application/json")) return (await resp.json()) as T;
  // Defensive: when the server omits Content-Type but the body looks
  // like JSON, parse it anyway. Rationale: every caller of ``request``
  // is typed against a JSON shape (``request<Patient>``,
  // ``request<PatientDocument[]>``, ...), and silently returning an
  // ArrayBuffer cast as T causes downstream "is not iterable" /
  // ".filter is not a function" crashes deep inside React renders that
  // surface as the bare Next.js error boundary. Falling back to JSON
  // parsing is harmless for true JSON payloads with a missing header
  // and only fails fast on real binary responses (which no typed
  // ``request<T>`` caller asks for — those use ``fetch`` directly).
  const text = await resp.text();
  const trimmed = text.trimStart();
  if (trimmed.startsWith("{") || trimmed.startsWith("[") || trimmed.startsWith('"')) {
    try {
      return JSON.parse(text) as T;
    } catch {
      /* fall through to arrayBuffer */
    }
  }
  return new TextEncoder().encode(text).buffer as unknown as T;
}

export interface Paginated<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
  // Populated by /api/search?facets=true. Shape documented on
  // SearchFacets. Other paginated endpoints leave this undefined.
  facets?: SearchFacets | null;
}

export interface StudyListParams {
  limit?: number;
  offset?: number;
  modality?: string;
  body_part?: string;
}

export interface SearchParams extends StudyListParams {
  q?: string;
  date_from?: string;
  date_to?: string;
  tag?: string[];
  // 'public' = OpenData library + studies marked is_public.
  // 'mine'   = studies owned by the caller.
  // 'all' or undefined = everything the caller can read.
  scope?: "all" | "public" | "mine";
  // When true the response includes facets (counts per
  // modality / body_part / year / top_tags). Used to populate
  // the SearchSidebar chip palette.
  facets?: boolean;
  limit?: number;
  offset?: number;
}

export interface SearchFacets {
  modality?: Record<string, number>;
  body_part?: Record<string, number>;
  year?: Record<string, number>;
  top_tags?: { namespace: string; value: string; count: number }[];
}

type QSValue = string | number | boolean | string[] | undefined | null;

function qs(params: Record<string, QSValue>): string {
  const s = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === "") continue;
    if (Array.isArray(v)) for (const x of v) s.append(k, String(x));
    else s.set(k, String(v));
  }
  return s.toString() ? `?${s.toString()}` : "";
}

export type { QSValue };
export { qs };
