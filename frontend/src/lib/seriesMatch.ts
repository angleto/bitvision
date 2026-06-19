// Medical series matching + labeling for the follow-up comparison viewer.
//
// When comparing two studies (e.g. a prior and a current contrast CT), the
// user must be able to pick WHICH series to compare on each side on medical
// criteria — e.g. the portal-venous axial series of both. The system must not
// silently guess "the first CT". These pure helpers (a) build a human label
// from the metadata actually available on a Series (modality, verbatim
// description, instance count) plus the acquisition plane resolved separately,
// and (b) score how well a candidate series matches a reference series so the
// UI can pre-select a transparent, overridable default.
//
// Phase words (portale/venosa/arteriosa/basale/tardiva) are NOT inferred —
// the verbatim series_description carries them and is shown as-is; matching
// rewards shared description tokens so the same phrasing lines up across
// studies without a brittle keyword table.

import type { Series } from "@/lib/api";

// Mirrors DisplayMetadata.primary_plane (backend-derived acquisition plane).
export type PrimaryPlane = "axial" | "sagittal" | "coronal" | "oblique" | "unknown";

/** Lowercase, accent-stripped, punctuation-split tokens (>=3 chars) of a
 * free-text series description, for medical-similarity overlap. */
export function descTokens(desc: string | null | undefined): Set<string> {
  if (!desc) return new Set();
  // Non-alphanumerics (punctuation AND accented chars, which are outside
  // [a-z0-9]) collapse to token separators. The medical phase words we match
  // on \u2014 portale / venosa / arteriosa / basale / tardiva / assiale \u2014 are
  // ASCII, so this is sufficient without combining-mark normalisation.
  const norm = desc.toLowerCase().replace(/[^a-z0-9]+/g, " ");
  return new Set(norm.split(" ").filter((w) => w.length >= 3));
}

export interface MatchPlanes {
  candidatePlane?: PrimaryPlane | null;
  referencePlane?: PrimaryPlane | null;
}

/** Medical match score between a candidate and a reference series. Same
 * modality dominates; then shared description tokens (contrast phase / region
 * phrasing); then same acquisition plane; then same body part; a small bonus
 * for multi-slice volumes over single-image SR/scout. Higher = better match. */
export function seriesMatchScore(
  candidate: Series,
  reference: Series,
  planes: MatchPlanes = {},
): number {
  let score = 0;

  const cm = (candidate.modality ?? "").toUpperCase();
  const rm = (reference.modality ?? "").toUpperCase();
  if (cm && cm === rm) score += 100;

  const ct = descTokens(candidate.series_description);
  const rt = descTokens(reference.series_description);
  let shared = 0;
  for (const w of ct) if (rt.has(w)) shared += 1;
  score += shared * 12;

  const cp = planes.candidatePlane;
  const rp = planes.referencePlane;
  if (cp && rp && cp !== "unknown" && cp === rp) score += 25;

  const cb = (candidate.body_part_examined ?? "").toLowerCase();
  const rb = (reference.body_part_examined ?? "").toLowerCase();
  if (cb && cb === rb) score += 8;

  if ((candidate.received_instance_count ?? 0) > 3) score += 2;
  return score;
}

/** Pick the candidate series that best matches ``reference`` on medical
 * criteria. Returns null for an empty candidate list. The result is a
 * transparent default for the UI; the user can always override it. */
export function bestMatchSeries(
  candidates: Series[],
  reference: Series,
  planeOf?: (seriesId: string) => PrimaryPlane | null | undefined,
): Series | null {
  let best: Series | null = null;
  let bestScore = Number.NEGATIVE_INFINITY;
  for (const c of candidates) {
    const s = seriesMatchScore(c, reference, {
      candidatePlane: planeOf?.(c.id) ?? null,
      referencePlane: planeOf?.(reference.id) ?? null,
    });
    if (s > bestScore) {
      bestScore = s;
      best = c;
    }
  }
  return best;
}

// Why a default was (or was not) auto-selected, so the picker can explain
// itself instead of silently loading something:
//  - "single"           one comparable (same-modality) series — the obvious pick
//  - "clear-margin"     one comparable series clearly beats the rest (plane/phrasing)
//  - "ambiguous"        several comparable series, no clear winner (e.g. same plane,
//                       different contrast phase) — a medical choice the user must make
//  - "no-modality-match" no candidate shares the reference modality — don't auto-compare
//  - "empty"            no candidates at all
export type MatchReason = "single" | "clear-margin" | "ambiguous" | "no-modality-match" | "empty";

export interface MatchConfidence {
  best: Series | null;
  confident: boolean;
  reason: MatchReason;
}

// A candidate must at least share the reference modality to be "comparable"
// (== the +100 from seriesMatchScore). Comparing a CT against an MR series by
// default is never sensible.
const MODALITY_FLOOR = 100;
// The winner must beat the runner-up by at least this much to auto-load. 25 ==
// one acquisition-plane match: a series that UNIQUELY lines up with the
// reference plane (axial↔axial) is a confident default. A margin below this
// (e.g. two comparable axial series differing only in contrast phase) is a
// clinical decision — surface the picker rather than guess the phase.
const CONFIDENT_MARGIN = 25;

/** Decide whether ``candidates`` contain a confident medical default to compare
 * against ``reference``, or whether the choice is ambiguous and must be left to
 * the user. Pure: drives whether the comparison viewer auto-loads or asks. */
export function matchConfidence(
  candidates: Series[],
  reference: Series,
  planeOf?: (seriesId: string) => PrimaryPlane | null | undefined,
): MatchConfidence {
  if (candidates.length === 0) return { best: null, confident: false, reason: "empty" };
  const referencePlane = planeOf?.(reference.id) ?? null;
  const scored = candidates
    .map((c) => ({
      c,
      s: seriesMatchScore(c, reference, {
        candidatePlane: planeOf?.(c.id) ?? null,
        referencePlane,
      }),
    }))
    .sort((a, b) => b.s - a.s);

  const top = scored[0];
  // Nothing shares the modality: comparing apples to oranges — make the user choose.
  if (top.s < MODALITY_FLOOR) {
    return { best: top.c, confident: false, reason: "no-modality-match" };
  }
  const comparable = scored.filter((x) => x.s >= MODALITY_FLOOR);
  if (comparable.length === 1) {
    return { best: top.c, confident: true, reason: "single" };
  }
  const margin = top.s - comparable[1].s;
  if (margin >= CONFIDENT_MARGIN) {
    return { best: top.c, confident: true, reason: "clear-margin" };
  }
  return { best: top.c, confident: false, reason: "ambiguous" };
}

/** Human label for a series option in the comparison selector:
 * ``#<num> · <MOD> · <plane> · <N> img — <description>``. ``planeLabel`` is the
 * already-localized plane word (or null/undefined to omit). The verbatim
 * description is the medical source of truth and is always shown when present. */
export function seriesOptionLabel(series: Series, planeLabel?: string | null): string {
  const parts: string[] = [];
  if (series.series_number != null) parts.push(`#${series.series_number}`);
  if (series.modality) parts.push(series.modality.toUpperCase());
  if (planeLabel) parts.push(planeLabel);
  parts.push(`${series.received_instance_count} img`);
  const head = parts.join(" · ");
  const desc = series.series_description?.trim();
  return desc ? `${head} — ${desc}` : head;
}
