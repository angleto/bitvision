// Viewport state persistence — per-user, per-series UI state.
// The server treats the JSON blob as opaque; only this module and the
// viewer page agree on its shape.

import { useEffect, useRef, useState } from "react";

import { API_BASE_URL, ApiError, getStoredToken } from "./api";

export type ViewportStateBlob = Record<string, unknown>;

export interface ViewportStateResponse {
  series_id: string;
  state: ViewportStateBlob;
  updated_at: string;
}

async function viewportFetch<T>(
  path: string,
  init: RequestInit & { json?: unknown } = {},
): Promise<T | null> {
  const headers = new Headers(init.headers);
  const token = getStoredToken();
  if (token) headers.set("authorization", `Bearer ${token}`);
  let body = init.body;
  if (init.json !== undefined) {
    headers.set("content-type", "application/json");
    body = JSON.stringify(init.json);
  }
  const resp = await fetch(`${API_BASE_URL}${path}`, {
    credentials: "include",
    ...init,
    headers,
    body,
    cache: "no-store",
  });
  if (resp.status === 404) return null;
  if (!resp.ok) {
    let detail: unknown;
    try {
      detail = await resp.json();
    } catch {
      detail = await resp.text();
    }
    throw new ApiError(resp.status, detail);
  }
  return (await resp.json()) as T;
}

export const viewportStateApi = {
  get: (seriesId: string) =>
    viewportFetch<ViewportStateResponse>(`/api/viewport-state/${seriesId}`),
  put: (seriesId: string, state: ViewportStateBlob) =>
    viewportFetch<ViewportStateResponse>(`/api/viewport-state/${seriesId}`, {
      method: "PUT",
      json: { state },
    }),
};

export interface UseViewportStateOptions {
  debounceMs?: number;
  enabled?: boolean;
  onRestore?: (state: ViewportStateBlob) => void;
}

/**
 * Persist viewer state per (user, series).
 *
 * Fetches saved state on mount and calls `onRestore` once, then
 * debounces PUTs as `current` changes. Saves are skipped until the
 * initial fetch completes — otherwise the viewer's default `current`
 * would overwrite real saved state before `onRestore` applies it.
 */
export function useViewportState(
  seriesId: string | null | undefined,
  current: ViewportStateBlob,
  options: UseViewportStateOptions = {},
): void {
  const { debounceMs = 1000, enabled = true, onRestore } = options;
  const [loaded, setLoaded] = useState(false);

  const onRestoreRef = useRef(onRestore);
  onRestoreRef.current = onRestore;

  useEffect(() => {
    if (!enabled || !seriesId) {
      setLoaded(false);
      return;
    }
    let cancelled = false;
    setLoaded(false);
    viewportStateApi
      .get(seriesId)
      .then((resp) => {
        if (cancelled) return;
        if (resp?.state && onRestoreRef.current) onRestoreRef.current(resp.state);
        setLoaded(true);
      })
      .catch(() => {
        if (!cancelled) setLoaded(true);
      });
    return () => {
      cancelled = true;
    };
  }, [seriesId, enabled]);

  useEffect(() => {
    if (!enabled || !seriesId || !loaded) return;
    const handle = window.setTimeout(() => {
      viewportStateApi.put(seriesId, current).catch(() => {
        // Network hiccup — next change re-triggers a save.
      });
    }, debounceMs);
    return () => window.clearTimeout(handle);
  }, [seriesId, current, loaded, debounceMs, enabled]);
}
