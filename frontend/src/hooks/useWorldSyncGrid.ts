"use client";

// N-pane crosshair sync for the multiphase viewer.
//
// Two propagation modes:
//
//  * INDEX sync (``setIndexSync(true)``): used when every pane shares the same
//    acquisition geometry (same FrameOfReferenceUID + origin + spacing + slice
//    direction — the normal intra-study multiphase contrast-CT case). A scroll
//    to slice ``k`` in one pane drives every other pane to slice ``k`` too
//    (clamped to its own z-extent). This is exactly what a radiologist does by
//    hand ("bring them all to slice 160") and is robust: it does NOT go through
//    the world<->index round-trip, which in practice mis-mapped the synced panes
//    to the wrong slice (the "phases move to the wrong level / two pairs" bug).
//
//  * WORLD sync (default): maps the picked world (LPS) point from one pane to
//    every other through a per-pane registration transform. Needed when panes
//    live in different frames (the followup viewer with a rigid registration).
//
// Both modes are guarded against the echo each programmatic crosshair write
// would otherwise re-fire.

import type { MPRLayoutHandle } from "@/components/MPRLayoutTypes";
import { type PaneTransform, mapWorldAcrossPanes, shouldSkipSync } from "@/lib/worldSync";
import { useCallback, useMemo, useRef, useState } from "react";

export interface WorldSyncGrid {
  /** Ref callback for pane ``i``'s CornerstoneMPRLayout. */
  registerPane: (i: number) => (h: MPRLayoutHandle | null) => void;
  /** Crosshair-change handler to pass to pane ``i``'s layout. */
  onCrosshairChange: (i: number, pos: [number, number, number]) => void;
  /** Record pane ``i``'s transform (reference-world -> pane-world); null =
   *  identity (same FoR as the reference pane). */
  setTransform: (i: number, m: PaneTransform) => void;
  /** Switch to slice-INDEX propagation (panes share acquisition geometry). */
  setIndexSync: (v: boolean) => void;
  sync: boolean;
  setSync: (v: boolean) => void;
}

export function useWorldSyncGrid(): WorldSyncGrid {
  const handles = useRef<(MPRLayoutHandle | null)[]>([]);
  const transforms = useRef<PaneTransform[]>([]);
  const guard = useRef(false);
  const indexSync = useRef(false);
  const [sync, setSync] = useState(true);

  const registerPane = useCallback(
    (i: number) => (h: MPRLayoutHandle | null) => {
      handles.current[i] = h;
    },
    [],
  );

  const setTransform = useCallback((i: number, m: PaneTransform) => {
    transforms.current[i] = m;
  }, []);

  const setIndexSync = useCallback((v: boolean) => {
    indexSync.current = v;
  }, []);

  const onCrosshairChange = useCallback(
    (i: number, pos: [number, number, number]) => {
      if (!sync || guard.current) return;
      guard.current = true;
      try {
        // INDEX sync: drive every other pane to the SAME slice index (clamped),
        // keeping its own in-plane crosshair. This is what works when the phases
        // share geometry; the world round-trip below mis-maps them in practice.
        if (indexSync.current) {
          const k = Math.round(pos[2]);
          for (let j = 0; j < handles.current.length; j++) {
            if (j === i) continue;
            const to = handles.current[j];
            if (!to?.setCrosshair) continue;
            const nz = to.dims?.[2] ?? 1;
            const kj = Math.max(0, Math.min(nz - 1, k));
            const cur = to.crosshair;
            if (cur && cur[2] === kj) continue; // already there — skip the echo
            to.setCrosshair([cur?.[0] ?? pos[0], cur?.[1] ?? pos[1], kj]);
          }
          return;
        }

        // WORLD sync: route the picked point through the reference frame.
        // ``undefined`` transform = pane not syncable (different FoR, not yet
        // aligned); ``null`` = identity (shares the reference frame); Mat4 = aligned.
        const ti = transforms.current[i];
        if (ti === undefined) return;
        const from = handles.current[i];
        const worldI = from?.getCrosshairWorld?.(pos);
        if (!worldI) return;
        for (let j = 0; j < handles.current.length; j++) {
          if (j === i) continue;
          const tj = transforms.current[j];
          if (tj === undefined) continue;
          const to = handles.current[j];
          if (!to?.setCrosshairWorld) continue;
          const worldJ = mapWorldAcrossPanes(worldI, ti, tj);
          if (shouldSkipSync(to.getCrosshairWorld?.(), worldJ)) continue;
          to.setCrosshairWorld(worldJ);
        }
      } finally {
        // Release the echo guard on the next frame, after the synced re-renders
        // have settled.
        requestAnimationFrame(() => {
          guard.current = false;
        });
      }
    },
    [sync],
  );

  return useMemo(
    () => ({ registerPane, onCrosshairChange, setTransform, setIndexSync, sync, setSync }),
    [registerPane, onCrosshairChange, setTransform, setIndexSync, sync],
  );
}
