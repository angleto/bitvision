"use client";

// N-pane world-coordinate crosshair sync for the multiphase viewer.
//
// Generalises the followup viewer's 2-pane pushSync to an arbitrary grid:
// each pane registers its MPRLayoutHandle and an optional transform to the
// reference frame (pane 0). A crosshair change in one pane broadcasts the
// mapped world point to every other pane, guarded against the resulting
// echo (each setCrosshairWorld would otherwise re-fire onCrosshairChange).

import type { MPRLayoutHandle } from "@/components/MPRLayoutTypes";
import { type PaneTransform, mapWorldAcrossPanes } from "@/lib/worldSync";
import { useCallback, useMemo, useRef, useState } from "react";

export interface WorldSyncGrid {
  /** Ref callback for pane ``i``'s CornerstoneMPRLayout. */
  registerPane: (i: number) => (h: MPRLayoutHandle | null) => void;
  /** Crosshair-change handler to pass to pane ``i``'s layout. */
  onCrosshairChange: (i: number, pos: [number, number, number]) => void;
  /** Record pane ``i``'s transform (reference-world -> pane-world); null =
   *  identity (same FoR as the reference pane). */
  setTransform: (i: number, m: PaneTransform) => void;
  sync: boolean;
  setSync: (v: boolean) => void;
}

export function useWorldSyncGrid(): WorldSyncGrid {
  const handles = useRef<(MPRLayoutHandle | null)[]>([]);
  const transforms = useRef<PaneTransform[]>([]);
  const guard = useRef(false);
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

  const onCrosshairChange = useCallback(
    (i: number, pos: [number, number, number]) => {
      if (!sync || guard.current) return;
      // ``undefined`` = pane not syncable (different FoR, not yet aligned);
      // ``null`` = identity (shares the reference frame); Mat4 = aligned.
      const ti = transforms.current[i];
      if (ti === undefined) return;
      const from = handles.current[i];
      const worldI = from?.getCrosshairWorld?.(pos);
      if (!worldI) return;
      guard.current = true;
      try {
        for (let j = 0; j < handles.current.length; j++) {
          if (j === i) continue;
          const tj = transforms.current[j];
          if (tj === undefined) continue;
          const to = handles.current[j];
          if (!to?.setCrosshairWorld) continue;
          const worldJ = mapWorldAcrossPanes(worldI, ti, tj);
          to.setCrosshairWorld(worldJ);
        }
      } finally {
        // Release the echo guard on the next frame, after the
        // setCrosshairWorld-driven re-renders have settled.
        requestAnimationFrame(() => {
          guard.current = false;
        });
      }
    },
    [sync],
  );

  return useMemo(
    () => ({ registerPane, onCrosshairChange, setTransform, sync, setSync }),
    [registerPane, onCrosshairChange, setTransform, sync],
  );
}
