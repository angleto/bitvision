// Finding-class → colour map (task cde63ced).
//
// Annotation colour is bound to the finding's CLASS (the FindingType
// ``category`` enum) instead of being arbitrary, so a spiculated nodule and a
// pleural effusion read differently at a glance both on the Cornerstone canvas
// (per-annotation style + the SVG MarkerOverlay stroke) and in the side panels
// (a colour chip).
//
// Why concrete hex and not ``--bv-*`` theme variables: these are SEMANTIC
// category colours (a fixed legend), and both consumers need a concrete colour
// string — Cornerstone's annotation style API and an SVG ``stroke`` cannot read
// CSS custom properties. The palette is chosen to be saturated mid-tones that
// hold contrast against BOTH a grayscale medical image and a light/dark panel
// background (the constraint is "readable in both modes", which a saturated
// hue satisfies on grey far better than a theme chrome colour would).

/** The FindingType.category enum the backend ships (findings.py). */
export const FINDING_CATEGORIES = [
  "lesion",
  "mass",
  "fluid",
  "fracture",
  "inflammation",
  "vascular",
  "calcification",
  "hemorrhage",
  "other",
] as const;

export type FindingCategory = (typeof FINDING_CATEGORIES)[number];

/** Distinct, saturated hues — legible on grey imaging and either panel theme. */
export const CLASS_COLOR: Record<FindingCategory, string> = {
  lesion: "#fb923c", // amber-400 (keeps the legacy bbox.lesion colour)
  mass: "#f472b6", // pink-400
  fluid: "#38bdf8", // sky-400
  fracture: "#a78bfa", // violet-400
  inflammation: "#f87171", // red-400
  vascular: "#f43f5e", // rose-500
  calcification: "#e5e7eb", // gray-200 (bright, reads on dark imaging)
  hemorrhage: "#ef4444", // red-500
  other: "#facc15", // yellow-400
};

/** Colour for a finding category, falling back to ``other`` for an unknown /
 *  missing category so a caller never has to null-check. */
export function colorForCategory(category: string | null | undefined): string {
  if (category && category in CLASS_COLOR) {
    return CLASS_COLOR[category as FindingCategory];
  }
  return CLASS_COLOR.other;
}

/** Stroke colour for the SVG MarkerOverlay: the finding CLASS colour when the
 *  marker is linked to a finding, else the legacy geometry-kind colour so
 *  un-classified markers (raw bbox / fiducial / text-overlay) keep their look.
 *  Pure — shared by the overlay and its unit test. */
export function overlayStroke(opts: {
  category?: string | null;
  kind?: string | null;
}): string {
  if (opts.category) return colorForCategory(opts.category);
  switch (opts.kind) {
    case "bbox.lesion":
      return "#fb923c"; // amber-400 (== lesion class colour)
    case "fiducial":
      return "#22d3ee"; // cyan-400
    default:
      return "#facc15"; // yellow-400 (text-overlay et al.)
  }
}
