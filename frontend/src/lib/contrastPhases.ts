// Shared policy for the multiphase contrast-CT viewer: which of a study's
// series are *reviewable phase volumes* (the axial source acquisitions a
// radiologist opens side by side) versus clutter (scouts, screenshots, dose
// reports, bolus-prep loops, MPR reformats).
//
// The backend computes ``is_reviewable_phase`` authoritatively (geometry +
// description); this module trusts that flag and falls back to a description
// heuristic only when the field is absent (older backend) so the viewer is
// never wrong-by-default on a fresh deploy mismatch. Keeping the policy in one
// place stops the picker and the default-pane logic from drifting apart.

import type { SeriesPhase } from "@/lib/api";

// Mirror of backend ``series_kind.MIN_VOLUME_INSTANCES``: below this a CT
// "series" is a scout / screenshot / dose report / bolus-prep loop.
export const MIN_VOLUME_INSTANCES = 16;

// Clinical left-to-right ordering of phases in the grid.
export const PHASE_ORDER: Record<string, number> = {
  unenhanced: 0,
  arterial: 1,
  portal_venous: 2,
  delayed: 3,
  hepatobiliary: 4,
  corticomedullary: 5,
  nephrographic: 6,
  excretory: 7,
  dynamic: 8,
  other: 9,
};

const NON_REVIEWABLE_DESC =
  /\b(scout|topogram|topogramma|scanogram|localiz\w*|localizz\w*|surview|pilot|screen\s*save|screensave|secondary\s*capture|dose\s*record|dose\s*report|rapporto\s*dose|smart\s*prep|bolus\s*track\w*|test\s*bolus|monitoring|prep\s*smart|serie\s*prep)\b/i;

const REFORMAT_DESC =
  /\b(sag|sagittal|sagittale|cor|coronal|coronale|mpr|mip|minip|reformat\w*|reform\w*|ricostr\w*|rimformat\w*|curved|cpr|vr|vrt|3d|ssd)\b/i;

/** FE fallback when the backend did not send ``is_reviewable_phase``. */
function reviewableHeuristic(p: SeriesPhase): boolean {
  if ((p.modality || "").toUpperCase() !== "CT") return false;
  if ((p.instance_count ?? 0) < MIN_VOLUME_INSTANCES) return false;
  const desc = p.series_description || "";
  if (NON_REVIEWABLE_DESC.test(desc)) return false;
  // Geometry wins over a stray token: an axial plane is a source even if the
  // description happens to contain a reformat word.
  if (p.series_plane === "axial") return true;
  if (p.series_plane && p.series_plane !== "axial") return false;
  if (REFORMAT_DESC.test(desc)) return false;
  return true;
}

/** Is this series a reviewable axial contrast-phase volume? */
export function isReviewablePhase(p: SeriesPhase): boolean {
  return typeof p.is_reviewable_phase === "boolean"
    ? p.is_reviewable_phase
    : reviewableHeuristic(p);
}

/** The study's reviewable phase volumes (axial sources), in clinical phase
 *  order then acquisition order — the candidates the picker offers first. */
export function reviewableSeries(phases: SeriesPhase[]): SeriesPhase[] {
  return phases.filter(isReviewablePhase).sort(comparePhaseSeries);
}

/** Everything the picker hides by default (reformats, scouts, captures, dose
 *  reports, prep loops) — revealed by the "show all series" escape hatch. */
export function nonReviewableSeries(phases: SeriesPhase[]): SeriesPhase[] {
  return phases.filter((p) => !isReviewablePhase(p));
}

function comparePhaseSeries(a: SeriesPhase, b: SeriesPhase): number {
  const pa = PHASE_ORDER[a.acquisition_phase ?? "other"] ?? 9;
  const pb = PHASE_ORDER[b.acquisition_phase ?? "other"] ?? 9;
  if (pa !== pb) return pa - pb;
  const ta = a.acquisition_time_of_day ?? "";
  const tb = b.acquisition_time_of_day ?? "";
  if (ta !== tb) return ta < tb ? -1 : 1;
  return (a.series_number ?? 0) - (b.series_number ?? 0);
}

/** The default phase panes: one reviewable axial series per classified phase,
 *  in clinical order. Reformats / recon-kernel duplicates / junk are excluded
 *  even if a stale label sits on them, so the viewer never opens six panes
 *  when two phases were acquired. When several reviewable series share a
 *  phase, the axial source (and earliest acquisition) wins. */
export function defaultPhasePanes(phases: SeriesPhase[]): SeriesPhase[] {
  const candidates = phases
    .filter((p) => p.acquisition_phase && isReviewablePhase(p))
    .sort(comparePhaseSeries);
  const byPhase = new Map<string, SeriesPhase>();
  for (const p of candidates) {
    const key = p.acquisition_phase as string;
    const cur = byPhase.get(key);
    if (!cur || preferAsSource(p, cur)) byPhase.set(key, p);
  }
  return [...byPhase.values()].sort(comparePhaseSeries);
}

/** Prefer ``cand`` over the incumbent ``cur`` as a phase's representative
 *  series: an explicitly axial plane beats unknown/other; otherwise keep the
 *  earlier (already-sorted) incumbent. */
function preferAsSource(cand: SeriesPhase, cur: SeriesPhase): boolean {
  const candAxial = cand.series_plane === "axial";
  const curAxial = cur.series_plane === "axial";
  if (candAxial !== curAxial) return candAxial;
  return false;
}
