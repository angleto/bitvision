"use client";

// Shared DICOM-viewer instrumentation probe.
//
// Goal: let an automated UX/usability audit (Playwright) drive every
// viewer surface and read its live, NON-PHI state from a single
// ``window.__viewer`` global — without shipping that surface to real
// users by default. The probe is dormant unless an admin flips the
// ``viewer.debug.instrumentation`` public app-setting at runtime (no
// redeploy): see /admin/settings. When off, ``updateViewerProbe`` is a
// no-op and ``window.__viewer`` is never written.
//
// What it MUST NOT expose: pixel data, patient identifiers beyond the
// opaque UUIDs already present in the URL, or anything a non-admin can't
// already see. Only technical state: geometry/VOI/camera/layout/tool/
// timing/error — the signals an audit needs to reason about clarity and
// correctness.

import { useEffect, useState } from "react";

import { settingsApi } from "@/lib/api";

export const VIEWER_DEBUG_KEY = "viewer.debug.instrumentation";

/** Live per-viewport (pane) state an audit can assert on. */
export interface ViewerPaneProbe {
  visible: boolean;
  /** Current window/level as a raw value range, or null if unset. A
   *  collapsed/absent VOI is the signature of the "all-black pane" bug. */
  voi?: { lower: number; upper: number } | null;
  invert?: boolean;
  /** Cornerstone camera essentials — enough to detect a stale/unfit
   *  camera (parallelScale not matching the volume extent). */
  camera?: {
    parallelScale?: number;
    position?: number[];
    focalPoint?: number[];
  } | null;
  sliceIndex?: number | null;
  /** Canvas dimensions in device pixels — 0×0 signals a pane that never
   *  laid out (another black-screen cause). */
  canvas?: { width: number; height: number } | null;
  /** Crosshair voxel index (IJK) of this pane. */
  crosshairIjk?: [number, number, number] | null;
  /** Crosshair world position (LPS). All synced panes should report the SAME
   *  crosshairLps — a radiological-sync test asserts they coincide. */
  crosshairLps?: [number, number, number] | null;
  /** True when the synced world point fell OUTSIDE this phase's coverage and
   *  was snapped to the nearest valid slice (multiphase phases with different
   *  z-extents). A radiological test asserts this is false on the liver
   *  overlap (no black panes) and true only outside it. */
  outOfCoverage?: boolean | null;
}

/** The whole-viewer snapshot exposed at ``window.__viewer``. */
export interface ViewerProbe {
  /** Which surface populated it: 'series' | 'contrast' | 'followup' | … */
  surface: string;
  updatedAt: number;
  identity?: {
    seriesId?: string;
    studyId?: string;
    patientId?: string;
    modality?: string;
  };
  volume?: {
    dims?: [number, number, number];
    spacing?: [number, number, number];
    hasGeometry?: boolean;
  } | null;
  panes?: Record<string, ViewerPaneProbe>;
  activeTool?: string | null;
  layout?: string | null;
  measurementCount?: number;
  /** Surface-level error currently shown to the user (the red card), or
   *  null. An audit treats a non-null value as a functional failure. */
  error?: string | null;
  /** Coarse timings the surface chose to record (ms): volume load,
   *  first paint, wash-out round-trip, … */
  timings?: Record<string, number>;
  /** Free-form, append-only breadcrumbs the surface emits ("auto-WL
   *  applied", "fusion fetch failed", …) so an audit can explain a
   *  finding without guessing. */
  notes?: string[];
  /** Last wash-out computation result (contrast surface) so a radiological
   *  test can assert sensible per-phase HU + the liver relative curve without
   *  scraping the panel DOM. */
  washoutResult?: {
    region?: string | null;
    apw?: number | null;
    rpw?: number | null;
    curve?: Array<{ acquisition_phase: string | null; hu_mean: number | null }>;
    parenchymaCurve?: Array<{ acquisition_phase: string | null; hu_mean: number | null }>;
    relativeCurve?: Array<{ acquisition_phase: string | null; delta_hu: number | null }>;
    samples?: Array<{ acquisition_phase: string | null; hu_mean: number; hu_std: number }>;
    skipped?: Array<{ acquisition_phase: string | null; reason: string }>;
  } | null;
}

declare global {
  interface Window {
    __viewer?: ViewerProbe;
  }
}

// Module-level mirror of the flag so non-React call sites (e.g. inside
// CornerstoneMPRLayout render callbacks) can cheaply gate themselves
// without prop-drilling or re-reading the setting.
let _enabled = false;

/** True once an admin has enabled the instrumentation flag this session. */
export function viewerDebugEnabled(): boolean {
  return _enabled;
}

/** Merge a partial snapshot into ``window.__viewer``. No-op unless the
 *  flag is on, so the probe never materialises for ordinary users.
 *  ``panes`` and ``notes`` merge instead of replacing so independent
 *  call sites (page vs. CornerstoneMPRLayout) can each contribute. */
export function updateViewerProbe(partial: Partial<ViewerProbe>): void {
  if (typeof window === "undefined" || !_enabled) return;
  const cur = window.__viewer ?? { surface: partial.surface ?? "unknown", updatedAt: 0 };
  const notes = partial.notes?.length
    ? [...(cur.notes ?? []), ...partial.notes].slice(-50)
    : cur.notes;
  window.__viewer = {
    ...cur,
    ...partial,
    panes: { ...(cur.panes ?? {}), ...(partial.panes ?? {}) },
    timings: { ...(cur.timings ?? {}), ...(partial.timings ?? {}) },
    notes,
    updatedAt: Date.now(),
  };
}

/** Reset the probe for a fresh surface mount (clears stale panes/notes
 *  from a previous study so an audit never reads cross-study leftovers). */
export function resetViewerProbe(surface: string): void {
  if (typeof window === "undefined" || !_enabled) return;
  window.__viewer = { surface, updatedAt: Date.now(), panes: {}, notes: [] };
}

// Cache the flag across mounts within a session: the value only changes
// when an admin toggles it, and the audit re-loads the page after
// toggling, so a per-session fetch is enough.
let _cache: boolean | null = null;

/** React hook: resolves the ``viewer.debug.instrumentation`` flag once
 *  and returns whether instrumentation is enabled. Also syncs the
 *  module-level mirror so ``updateViewerProbe`` works from any call
 *  site. Fails closed (disabled) on any error. */
export function useViewerDebug(): boolean {
  const [enabled, setEnabled] = useState<boolean>(_cache ?? false);

  useEffect(() => {
    if (_cache !== null) {
      _enabled = _cache;
      setEnabled(_cache);
      return;
    }
    let alive = true;
    settingsApi
      .listPublic()
      .then((rows) => {
        const on = rows.some((r) => r.key === VIEWER_DEBUG_KEY && r.value === true);
        _cache = on;
        _enabled = on;
        if (alive) setEnabled(on);
      })
      .catch(() => {
        _cache = false;
        _enabled = false;
      });
    return () => {
      alive = false;
    };
  }, []);

  return enabled;
}
