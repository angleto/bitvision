// World-coordinate crosshair sync across N viewer panes.
//
// The followup viewer (2 panes) maps a world point from one pane to the
// other through a single registration matrix. This generalises it to N
// panes via a shared REFERENCE frame (pane 0 by convention):
//
//   - each pane stores a transform M_i that maps REFERENCE-world -> pane_i-world
//     (the rigid registration matrix with fixed=reference, moving=pane_i);
//   - a pane sharing the reference FrameOfReferenceUID stores null (identity) —
//     the common intra-study contrast-CT case: all phases share one FoR, so
//     every pane is identity and sync is exact with zero registration.
//
// To map a point picked in pane i into pane j: lift to the reference frame
// (w_ref = M_i^{-1} . w_i) then drop into pane j (w_j = M_j . w_ref).

import { type Mat4, type Vec3, applyAffine, invertAffine } from "@/lib/affine";

/** Transform mapping reference-world -> this pane's world. ``null`` =
 *  identity (pane shares the reference frame of reference). */
export type PaneTransform = Mat4 | null;

/**
 * Map a world (LPS) point picked in pane ``i`` into pane ``j``'s world,
 * routing through the reference frame.
 *
 * Degrades safely: if a transform is non-invertible it is treated as
 * identity for that hop rather than throwing, so a bad matrix never wedges
 * the whole grid's sync.
 */
export function mapWorldAcrossPanes(
  worldI: Vec3,
  transformI: PaneTransform,
  transformJ: PaneTransform,
): Vec3 {
  let wRef: Vec3 = worldI;
  if (transformI) {
    const inv = invertAffine(transformI);
    wRef = inv ? applyAffine(inv, worldI) : worldI;
  }
  return transformJ ? applyAffine(transformJ, wRef) : wRef;
}

/**
 * True when ``next`` is essentially the same world point ``current`` already
 * sits at — within ``eps`` mm on every axis. Used to skip a redundant
 * ``setCrosshairWorld`` push that would re-issue a camera write (and its
 * CAMERA_MODIFIED echo) for no visible change. ``eps`` is deliberately tiny:
 * a genuine single-slice move is at least one slice-spacing apart, far larger
 * than this, so only true no-ops / echoes are skipped.
 */
export function shouldSkipSync(current: Vec3 | null | undefined, next: Vec3, eps = 0.01): boolean {
  if (!current) return false;
  return (
    Math.abs(current[0] - next[0]) < eps &&
    Math.abs(current[1] - next[1]) < eps &&
    Math.abs(current[2] - next[2]) < eps
  );
}
