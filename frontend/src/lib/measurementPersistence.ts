// Debounced backend sync for 2D viewer measurements. The viewer keeps
// measurements in local React state for responsiveness; this hook
// mirrors them to POST /api/series/{id}/measurements so they survive
// reloads and can be exported as DICOM SR.
//
// Usage:
//   const { loaded, error, saving, exportSr, refresh } =
//     useMeasurementPersistence(seriesId, measurements, {
//       enabled: !!user, debounceMs: 800,
//     });
//
// The hook:
//   1. On mount (or when seriesId changes) loads existing measurements
//      and hands them back via `onLoad`. The caller merges them into
//      local state.
//   2. Whenever the `measurements` reference changes it schedules a
//      debounced POST. Subsequent changes within `debounceMs` collapse
//      into a single request.
//   3. `client_id` on each measurement is what the backend keys its
//      upsert on — stable ids across reloads are a caller
//      responsibility. Remote rows missing from the client set are
//      deleted (`replace: true`).

import { useCallback, useEffect, useRef, useState } from "react";

import { type MeasurementPayload, type MeasurementRow, measurementsApi } from "./api";

export interface UseMeasurementPersistenceOptions {
  enabled?: boolean;
  debounceMs?: number;
  onLoad?: (rows: MeasurementRow[]) => void;
  onError?: (err: unknown) => void;
}

export interface MeasurementPersistenceState {
  loaded: boolean;
  saving: boolean;
  error: string | null;
  remote: MeasurementRow[];
  refresh: () => Promise<void>;
  exportSr: () => Promise<Record<string, unknown>>;
  flush: () => Promise<void>;
}

export function useMeasurementPersistence(
  seriesId: string | null | undefined,
  measurements: MeasurementPayload[],
  options: UseMeasurementPersistenceOptions = {},
): MeasurementPersistenceState {
  const { enabled = true, debounceMs = 800, onLoad, onError } = options;
  const [loaded, setLoaded] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [remote, setRemote] = useState<MeasurementRow[]>([]);

  const latestRef = useRef<MeasurementPayload[]>(measurements);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const onLoadRef = useRef(onLoad);
  const onErrorRef = useRef(onError);
  onLoadRef.current = onLoad;
  onErrorRef.current = onError;

  useEffect(() => {
    latestRef.current = measurements;
  }, [measurements]);

  const refresh = useCallback(async () => {
    if (!seriesId || !enabled) return;
    try {
      const rows = await measurementsApi.list(seriesId);
      setRemote(rows);
      setLoaded(true);
      setError(null);
      onLoadRef.current?.(rows);
    } catch (e) {
      const msg = e instanceof Error ? e.message : "load failed";
      setError(msg);
      onErrorRef.current?.(e);
    }
  }, [seriesId, enabled]);

  useEffect(() => {
    setLoaded(false);
    setRemote([]);
    if (!seriesId || !enabled) return;
    void refresh();
  }, [seriesId, enabled, refresh]);

  const pushNow = useCallback(async () => {
    if (!seriesId || !enabled) return;
    const payload = latestRef.current;
    setSaving(true);
    try {
      const rows = await measurementsApi.upsert(seriesId, payload, true);
      setRemote(rows);
      setError(null);
    } catch (e) {
      const msg = e instanceof Error ? e.message : "save failed";
      setError(msg);
      onErrorRef.current?.(e);
    } finally {
      setSaving(false);
    }
  }, [seriesId, enabled]);

  // Debounced push whenever `measurements` reference changes (but only
  // after the initial load completes, to avoid clobbering existing
  // server rows with an empty client state on first mount).
  // biome-ignore lint/correctness/useExhaustiveDependencies: ``measurements`` is the change trigger; pushNow internally reads the freshest reference. Biome can't see the chain.
  useEffect(() => {
    if (!seriesId || !enabled || !loaded) return;
    if (timerRef.current) clearTimeout(timerRef.current);
    timerRef.current = setTimeout(() => {
      void pushNow();
    }, debounceMs);
    return () => {
      if (timerRef.current) clearTimeout(timerRef.current);
    };
  }, [measurements, seriesId, enabled, loaded, debounceMs, pushNow]);

  const flush = useCallback(async () => {
    if (timerRef.current) {
      clearTimeout(timerRef.current);
      timerRef.current = null;
    }
    await pushNow();
  }, [pushNow]);

  const exportSr = useCallback(async () => {
    if (!seriesId) throw new Error("no series id");
    // Flush pending changes first so the SR reflects the latest state.
    await flush();
    return measurementsApi.exportSr(seriesId);
  }, [seriesId, flush]);

  return { loaded, saving, error, remote, refresh, exportSr, flush };
}
