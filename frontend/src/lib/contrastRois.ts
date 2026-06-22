// Pure ROI bookkeeping for the multiphase contrast wash-out table. Extracted
// from the viewer page so the per-phase attribution logic is unit-testable
// WITHOUT a browser / Cornerstone GL context (the part that kept breaking:
// "I drew a ROI on the delayed phase and it was counted on the unenhanced one").
//
// The root cause it fixes: Cornerstone annotation events fire on the GLOBAL
// event target, so every pane's measurement callback receives EVERY pane's
// annotations. Each pane must therefore filter the stream to the annotations
// that belong to it — keyed by the pane's distinct synthetic FrameOfReference
// (one per phase, ``bvp-phase:<seriesId>``).

export type Vec3 = [number, number, number];

export type Meas = {
  markerId?: string;
  csToolName?: string;
  tool?: string;
  worldPoints?: Vec3[];
  frameOfReferenceUID?: string;
};

export type GroupMember = { markerId: string; worldPoints: Vec3[] };
export type Group = Record<number, GroupMember>;

/** The distinct synthetic FrameOfReferenceUID a phase pane builds its volume
 *  with, so its annotations are independent from the other phases'. */
export const paneForUid = (seriesId: string): string => `bvp-phase:${seriesId}`;

/** The subset of a (global) measurement stream that was drawn on THIS pane —
 *  the annotations whose FrameOfReferenceUID is the pane's synthetic one. This
 *  is what stops a draw on one phase being captured by another. */
export function ownMeasurements(ms: Meas[], seriesId: string): Meas[] {
  const f = paneForUid(seriesId);
  return ms.filter((m) => m.frameOfReferenceUID === f);
}

/** The newest circle ROI in a pane's own stream that has not been processed
 *  yet (markerId not in ``seen``) — i.e. a fresh hand-drawn ROI. */
export function newestFreshCircle(ownMs: Meas[], seen: Set<string>): Meas | null {
  for (let i = ownMs.length - 1; i >= 0; i--) {
    const m = ownMs[i];
    if (
      (m.csToolName === "CircleROI" || m.tool === "sphere") &&
      (m.worldPoints?.length ?? 0) >= 2 &&
      m.markerId != null &&
      !seen.has(m.markerId)
    ) {
      return m;
    }
  }
  return null;
}

export function circleRadiusMm(wp: Vec3[]): number {
  const c = wp[0];
  const e = wp[1];
  return Math.hypot(e[0] - c[0], e[1] - c[1], e[2] - c[2]);
}

export function samePoints(a: Vec3[], b: Vec3[], tol = 1e-3): boolean {
  if (a.length !== b.length) return false;
  for (let i = 0; i < a.length; i++) {
    for (let j = 0; j < 3; j++) if (Math.abs(a[i][j] - b[i][j]) > tol) return false;
  }
  return true;
}

export type PaneZGeom = {
  /** Volume origin Z (cornerstone imageData getOrigin()[2]). */
  originZ?: number | null;
  /** Volume slice spacing Z (getSpacing()[2]). */
  spacingZ?: number | null;
  /** Fallback: the pane's synced crosshair world Z. */
  crossZ?: number | null;
};

/** Map a box's world handle points from a source phase into a target phase by
 *  the SAME slice index: keep the in-plane (x, y), shift Z to the target's
 *  world-Z at that index (z_q = origin_q + k * spacing_q, k from the source).
 *  Falls back to the target's synced crosshair Z, then the drawn Z. */
export function propagateZWorld(src: PaneZGeom, tgt: PaneZGeom, worldPoints: Vec3[]): Vec3[] {
  const cz = worldPoints[0]?.[2] ?? 0;
  const k =
    src.originZ != null && src.spacingZ != null && src.spacingZ !== 0
      ? (cz - src.originZ) / src.spacingZ
      : null;
  let zq = cz;
  if (k != null && tgt.originZ != null && tgt.spacingZ != null) zq = tgt.originZ + k * tgt.spacingZ;
  else if (tgt.crossZ != null) zq = tgt.crossZ;
  return worldPoints.map((wp) => [wp[0], wp[1], zq] as Vec3);
}

export type PhaseRoi = { series_id: string; center_lps: Vec3; radius_mm: number };

/** Per-phase ROIs for the wash-out, from each phase's CURRENT box position:
 *  the live drag (``liveByPane``) if the operator re-centred it, else the
 *  stored group point. One entry per pane that has a box of this group. */
export function phaseRoisFromGroup(
  group: Group,
  panes: { series_id: string }[],
  liveByPane: Record<number, Meas[]>,
): PhaseRoi[] {
  const out: PhaseRoi[] = [];
  for (let i = 0; i < panes.length; i++) {
    const m = group[i];
    if (!m) continue;
    const live = (liveByPane[i] ?? []).find(
      (d) => d.markerId === m.markerId && (d.worldPoints?.length ?? 0) >= 2,
    );
    const pts = (live?.worldPoints ?? m.worldPoints) as Vec3[];
    if (!pts || pts.length < 2) continue;
    const r = circleRadiusMm(pts);
    if (!(r > 0)) continue;
    out.push({
      series_id: panes[i].series_id,
      center_lps: [pts[0][0], pts[0][1], pts[0][2]],
      radius_mm: r,
    });
  }
  return out;
}
