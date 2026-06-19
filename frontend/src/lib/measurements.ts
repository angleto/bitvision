// Measurement primitives for the 2D DICOM viewer.
// Points are stored as [x, y] tuples in canvas (screen) pixel space so they
// can be rendered on the SVG overlay directly. Physical-unit conversions are
// applied at compute time using the per-axis pixel spacing supplied by the
// caller (from series metadata).
//
// NOTE: persistence lives in T2; this module only owns the in-memory shape
// and tool state machine.

export type MeasurementKind =
  | "distance"
  | "angle"
  | "area"
  | "ellipse"
  | "freehand"
  | "arrow"
  | "text"
  | "probe";

export type Pt = [number, number];

export interface Measurement {
  id: string;
  kind: MeasurementKind;
  points: Pt[];
  label?: string;
  color?: string;
}

/** Per-axis physical pixel spacing in mm. */
export interface Spacing {
  spU: number;
  spV: number;
}

// ---------------------------------------------------------------------------
// ID generation (deterministic per spec: crypto.randomUUID()).
// We fall back to a Math.random-based v4-shaped string in non-browser test
// environments that lack `crypto.randomUUID`.
// ---------------------------------------------------------------------------

export function newId(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  // RFC4122-ish fallback (test environments only).
  return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0;
    const v = c === "x" ? r : (r & 0x3) | 0x8;
    return v.toString(16);
  });
}

// ---------------------------------------------------------------------------
// Geometry helpers (screen-space points + mm conversion).
// ---------------------------------------------------------------------------

export function distanceMm(a: Pt, b: Pt, sp: Spacing): number {
  const dx = (b[0] - a[0]) * sp.spU;
  const dy = (b[1] - a[1]) * sp.spV;
  return Math.sqrt(dx * dx + dy * dy);
}

export function angleDeg(a: Pt, vertex: Pt, c: Pt): number {
  const v1 = [a[0] - vertex[0], a[1] - vertex[1]];
  const v2 = [c[0] - vertex[0], c[1] - vertex[1]];
  const dot = v1[0] * v2[0] + v1[1] * v2[1];
  const m1 = Math.hypot(v1[0], v1[1]);
  const m2 = Math.hypot(v2[0], v2[1]);
  if (m1 === 0 || m2 === 0) return 0;
  return (Math.acos(Math.max(-1, Math.min(1, dot / (m1 * m2)))) * 180) / Math.PI;
}

/** Shoelace polygon area in mm^2. */
export function polygonAreaMm2(pts: Pt[], sp: Spacing): number {
  if (pts.length < 3) return 0;
  let a = 0;
  for (let i = 0; i < pts.length; i++) {
    const j = (i + 1) % pts.length;
    a += pts[i][0] * sp.spU * (pts[j][1] * sp.spV);
    a -= pts[j][0] * sp.spU * (pts[i][1] * sp.spV);
  }
  return Math.abs(a / 2);
}

export function polygonPerimeterMm(pts: Pt[], sp: Spacing, closed = false): number {
  if (pts.length < 2) return 0;
  let p = 0;
  for (let i = 0; i < pts.length - 1; i++) p += distanceMm(pts[i], pts[i + 1], sp);
  if (closed) p += distanceMm(pts[pts.length - 1], pts[0], sp);
  return p;
}

/** Ellipse geometry from two opposite corners of its bounding box. */
export interface EllipseGeom {
  cx: number;
  cy: number;
  rx: number;
  ry: number;
  majorMm: number;
  minorMm: number;
  areaMm2: number;
}

export function ellipseFromBBox(p0: Pt, p1: Pt, sp: Spacing): EllipseGeom {
  const cx = (p0[0] + p1[0]) / 2;
  const cy = (p0[1] + p1[1]) / 2;
  const rx = Math.abs(p1[0] - p0[0]) / 2;
  const ry = Math.abs(p1[1] - p0[1]) / 2;
  const axisXMm = rx * 2 * sp.spU;
  const axisYMm = ry * 2 * sp.spV;
  const majorMm = Math.max(axisXMm, axisYMm);
  const minorMm = Math.min(axisXMm, axisYMm);
  const areaMm2 = Math.PI * (axisXMm / 2) * (axisYMm / 2);
  return { cx, cy, rx, ry, majorMm, minorMm, areaMm2 };
}

// ---------------------------------------------------------------------------
// Tool state machine.
//
// The viewer accumulates a list of "pending" points as the user clicks; each
// tool decides whether the current click completes a measurement or merely
// extends it. `appendPoint` returns either:
//   - { status: "pending", points }       → keep collecting
//   - { status: "complete", measurement } → push to the measurement list
// ---------------------------------------------------------------------------

export type ToolStep =
  | { status: "pending"; points: Pt[] }
  | { status: "complete"; measurement: Measurement };

export function appendPoint(kind: MeasurementKind, pending: Pt[], next: Pt, sp: Spacing): ToolStep {
  const pts = [...pending, next];

  switch (kind) {
    case "distance":
      if (pts.length >= 2) {
        return {
          status: "complete",
          measurement: createDistance(pts[0], pts[1], sp),
        };
      }
      return { status: "pending", points: pts };

    case "angle":
      if (pts.length >= 3) {
        return {
          status: "complete",
          measurement: createAngle(pts[0], pts[1], pts[2]),
        };
      }
      return { status: "pending", points: pts };

    case "ellipse":
      if (pts.length >= 2) {
        return {
          status: "complete",
          measurement: createEllipse(pts[0], pts[1], sp),
        };
      }
      return { status: "pending", points: pts };

    case "arrow":
      if (pts.length >= 2) {
        return {
          status: "complete",
          measurement: createArrow(pts[0], pts[1]),
        };
      }
      return { status: "pending", points: pts };

    case "text":
      return { status: "complete", measurement: createText(next) };

    case "probe":
      return { status: "complete", measurement: createProbe(next) };

    // Area + freehand are closed by the caller (double-click / pointer-up).
    case "area":
    case "freehand":
      return { status: "pending", points: pts };
  }
}

/** Called by the viewer when the user closes a polygon/freehand path. */
export function finalize(kind: MeasurementKind, pending: Pt[], sp: Spacing): Measurement | null {
  if (kind === "area") {
    if (pending.length < 3) return null;
    return createArea(pending, sp);
  }
  if (kind === "freehand") {
    if (pending.length < 2) return null;
    return createFreehand(pending, sp);
  }
  return null;
}

// ---------------------------------------------------------------------------
// Per-tool constructors.
// ---------------------------------------------------------------------------

export function createDistance(a: Pt, b: Pt, sp: Spacing): Measurement {
  const value = distanceMm(a, b, sp);
  return {
    id: newId(),
    kind: "distance",
    points: [a, b],
    label: `${value.toFixed(1)} mm`,
    color: "#0f0",
  };
}

export function createAngle(a: Pt, vertex: Pt, c: Pt): Measurement {
  const deg = angleDeg(a, vertex, c);
  return {
    id: newId(),
    kind: "angle",
    points: [a, vertex, c],
    label: `${deg.toFixed(1)}°`,
    color: "#ff0",
  };
}

export function createArea(pts: Pt[], sp: Spacing): Measurement {
  const area = polygonAreaMm2(pts, sp);
  return {
    id: newId(),
    kind: "area",
    points: pts.slice(),
    label: `${area.toFixed(1)} mm²`,
    color: "#0cf",
  };
}

export function createEllipse(p0: Pt, p1: Pt, sp: Spacing): Measurement {
  const g = ellipseFromBBox(p0, p1, sp);
  return {
    id: newId(),
    kind: "ellipse",
    points: [p0, p1],
    label: `${g.majorMm.toFixed(1)}×${g.minorMm.toFixed(1)} mm · ${g.areaMm2.toFixed(1)} mm²`,
    color: "#f6c",
  };
}

export function createFreehand(pts: Pt[], sp: Spacing): Measurement {
  const perim = polygonPerimeterMm(pts, sp, false);
  return {
    id: newId(),
    kind: "freehand",
    points: pts.slice(),
    label: `${perim.toFixed(1)} mm`,
    color: "#fa0",
  };
}

export function createArrow(from: Pt, to: Pt, text?: string): Measurement {
  return {
    id: newId(),
    kind: "arrow",
    points: [from, to],
    label: text,
    color: "#f44",
  };
}

export function createText(at: Pt, text = "Note"): Measurement {
  return {
    id: newId(),
    kind: "text",
    points: [at],
    label: text,
    color: "#fff",
  };
}

export function createProbe(at: Pt, huValue?: number): Measurement {
  return {
    id: newId(),
    kind: "probe",
    points: [at],
    label: huValue === undefined ? "probe" : `${huValue.toFixed(0)} HU`,
    color: "#9ef",
  };
}

// ---------------------------------------------------------------------------
// Cornerstone3D BidirectionalTool extraction (MPR viewer / RECIST).
//
// The Cornerstone-native MPR layout reports a bidirectional annotation's two
// axes in patient mm under ``cachedStats`` (``length`` = long axis,
// ``width`` = short axis). These pure helpers turn that into the long/short
// values our RECIST persistence stores; kept free of any Cornerstone import
// so they unit-test in jsdom.
// ---------------------------------------------------------------------------

/** A BidirectionalTool ``cachedStats`` entry (only the fields we read). */
export interface BidirectionalStats {
  length?: number;
  width?: number;
}

/** Long/short axes (mm) of a bidirectional measurement. Cornerstone already
 *  reports both in patient space, so we pass them through verbatim; non-finite
 *  or missing values become ``undefined`` so callers can distinguish "not a
 *  bidirectional" from "measured 0". */
export function extractBidirectionalMm(stats: BidirectionalStats | undefined): {
  longAxisMm?: number;
  shortAxisMm?: number;
} {
  const long = stats?.length;
  const short = stats?.width;
  return {
    longAxisMm: typeof long === "number" && Number.isFinite(long) ? long : undefined,
    shortAxisMm: typeof short === "number" && Number.isFinite(short) ? short : undefined,
  };
}

/** Human label for a bidirectional measurement, e.g. ``24.0 × 16.0 mm``;
 *  falls back to the long-axis-only form (``24.0 mm``) when there is no short
 *  axis (a plain Length tool routed through the same path). */
export function formatBidirectionalLabel(longAxisMm?: number, shortAxisMm?: number): string | null {
  if (longAxisMm === undefined) return null;
  if (shortAxisMm === undefined) return `${longAxisMm.toFixed(1)} mm`;
  return `${longAxisMm.toFixed(1)} × ${shortAxisMm.toFixed(1)} mm`;
}
