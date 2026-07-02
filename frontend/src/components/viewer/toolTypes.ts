// Shared viewer tool vocabulary. Extracted from viewer/series/[id]/page.tsx so
// the single-series viewer and the multiphase contrast grid drive the SAME
// tool palette (no duplication). The string values map 1:1 to the Cornerstone
// tool dispatch in CornerstoneMPRLayout (``measureToolByActive``).

export type Tool =
  | "wl"
  | "pan"
  | "measure-dist"
  | "measure-angle"
  | "measure-area"
  | "measure-ellipse"
  | "measure-rect"
  | "measure-sphere"
  | "measure-bidirectional"
  | "measure-freehand"
  | "measure-arrow"
  | "measure-text"
  | "measure-probe"
  | "measure-lens"
  | "segment"
  | "bbox"
  | "text";

// Ordered [tool, i18n-label-key] pairs for the palette buttons. Keys live in
// the ``viewer`` namespace (messages/*.json) — already present, no new strings.
export const TOOL_BUTTONS: readonly [Tool, string][] = [
  ["wl", "toolWl"],
  ["pan", "toolPan"],
  ["measure-dist", "toolDistance"],
  ["measure-bidirectional", "toolBidirectional"],
  ["measure-angle", "toolAngle"],
  ["measure-area", "toolArea"],
  ["measure-ellipse", "toolEllipse"],
  ["measure-rect", "toolRect"],
  ["measure-sphere", "toolSphere"],
  ["measure-freehand", "toolFreehand"],
  ["measure-arrow", "toolArrow"],
  ["measure-text", "toolText"],
  ["measure-probe", "toolProbe"],
  ["measure-lens", "toolLens"],
  ["segment", "toolSegment"],
];

// Glyph icons for tools that read better as a symbol than a word — the pan
// "hand" is the universal viewer affordance (matches the in-viewport ✋ toggle
// in CornerstoneMPRLayout), so both viewers show the SAME hand for panning
// instead of a text button. Tools without an entry render their text label.
export const TOOL_ICONS: Partial<Record<Tool, string>> = {
  pan: "✋",
};

// active-tool -> i18n hint key (``viewer`` namespace).
export const TOOL_HINT_KEYS: Record<string, string> = {
  none: "hintNone",
  wl: "hintWl",
  pan: "hintPan",
  "measure-dist": "hintDist",
  "measure-bidirectional": "hintBidirectional",
  "measure-angle": "hintAngle",
  "measure-area": "hintArea",
  "measure-ellipse": "hintEllipse",
  "measure-rect": "hintRect",
  "measure-sphere": "hintSphere",
  "measure-freehand": "hintFreehand",
  "measure-arrow": "hintArrow",
  "measure-text": "hintText",
  "measure-probe": "hintProbe",
  "measure-lens": "hintLens",
  segment: "hintSegment",
};

// The subset a radiologist needs for a multiphase contrast read in a tight
// per-pane grid: window/level, pan, the HU/enhancement ROIs, linear + angle,
// and the live probe. Excludes the rarely-used freehand/text/arrow to keep the
// compact palette legible. The single-series viewer passes no subset (all 14).
export const CONTRAST_TOOLS: readonly Tool[] = [
  "wl",
  "pan",
  "measure-sphere",
  "measure-ellipse",
  "measure-rect",
  "measure-dist",
  "measure-angle",
  "measure-probe",
];
