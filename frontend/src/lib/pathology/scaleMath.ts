/*
 * Pure scale / magnification math for the whole-slide-image viewer.
 *
 * Kept free of OpenSeadragon (and any DOM) so it is unit-testable in the
 * node vitest environment — the diagnostically load-bearing arithmetic
 * (a wrong µm reading is a wrong tumour measurement) lives here, the
 * component just feeds it ``screenPxPerImagePx`` from the live viewport.
 *
 * Conventions:
 * - ``mppX`` = micrometres per pixel at the slide's BASE (level-0)
 *   resolution. Null ⇒ physical scale unknown (e.g. an uncalibrated
 *   gross photo); the scale bar and µm readouts hide until calibration.
 * - ``screenPxPerImagePx`` = how many CSS pixels one base-level image
 *   pixel occupies on screen at the current zoom (1.0 ⇒ the slide is
 *   shown at its scanned resolution).
 */

/** A physical length rounded to a "nice" 1 / 2 / 5 × 10ⁿ value. */
export function niceLength(raw: number): number {
  if (!(raw > 0) || !Number.isFinite(raw)) return 0;
  const exp = Math.floor(Math.log10(raw));
  const base = 10 ** exp;
  const frac = raw / base;
  const snapped = frac < 1.5 ? 1 : frac < 3.5 ? 2 : frac < 7.5 ? 5 : 10;
  return snapped * base;
}

export interface ScaleBar {
  /** The bar's physical length in micrometres (a "nice" number). */
  micrometers: number;
  /** The bar's width in CSS pixels at the current zoom. */
  screenPx: number;
  /** Human label, switching to mm at ≥ 1000 µm. */
  label: string;
}

/**
 * Compute a scale bar close to ``targetScreenPx`` wide.
 *
 * Returns null when the physical scale is unknown (``mppX`` null/≤0) or
 * the viewport scale is not yet known, so the caller hides the bar.
 */
export function computeScaleBar(args: {
  mppX: number | null;
  screenPxPerImagePx: number;
  targetScreenPx: number;
}): ScaleBar | null {
  const { mppX, screenPxPerImagePx, targetScreenPx } = args;
  if (!mppX || mppX <= 0 || !(screenPxPerImagePx > 0)) return null;
  // µm represented by one screen pixel = mpp / (screen px per image px).
  const umPerScreenPx = mppX / screenPxPerImagePx;
  const rawUm = targetScreenPx * umPerScreenPx;
  const micrometers = niceLength(rawUm);
  if (micrometers <= 0) return null;
  const screenPx = micrometers / umPerScreenPx;
  return { micrometers, screenPx, label: formatMicrometers(micrometers) };
}

export function formatMicrometers(um: number): string {
  if (um >= 1000) {
    const mm = um / 1000;
    return `${Number.isInteger(mm) ? mm : mm.toFixed(mm < 10 ? 1 : 0)} mm`;
  }
  return `${Number.isInteger(um) ? um : um.toFixed(um < 10 ? 1 : 0)} µm`;
}

/**
 * Nominal objective magnification at base resolution. Uses the scanner's
 * reported ``magnification`` when present, else derives it from mpp
 * (40× ≈ 0.25 µm/px ⇒ ``10 / mppX``). Null when neither is known.
 */
export function baseMagnification(
  magnification: number | null,
  mppX: number | null,
): number | null {
  if (magnification && magnification > 0) return magnification;
  if (mppX && mppX > 0) return Math.round(10 / mppX);
  return null;
}

/**
 * On-screen magnification = base magnification × current display scale.
 * ``screenPxPerImagePx`` of 1.0 ⇒ the slide is shown at its scanned
 * magnification; 0.5 ⇒ half (e.g. 20× on a 40× scan). Null when the base
 * magnification is unknown.
 */
export function currentMagnification(
  baseMag: number | null,
  screenPxPerImagePx: number,
): number | null {
  if (!baseMag || baseMag <= 0 || !(screenPxPerImagePx > 0)) return null;
  return baseMag * screenPxPerImagePx;
}

/** Format a live magnification readout, e.g. ``18.4×`` / ``40×``. A clean
 *  objective power renders as an integer; an intermediate zoom keeps one
 *  decimal so the pathologist sees they are between standard powers. */
export function formatMagnification(mag: number | null): string {
  if (mag == null) return "—";
  const rounded = Math.round(mag);
  if (Math.abs(mag - rounded) < 0.05) return `${rounded}×`;
  return `${mag.toFixed(1)}×`;
}

/**
 * Magnification presets the viewer should offer, clamped to ≤ the slide's
 * base magnification: over-zooming past the scanned resolution shows
 * interpolated mush and falsely implies detail, so those presets are
 * dropped (diagnostic-safety, not cosmetics). When the base magnification
 * is unknown, all presets are offered.
 */
export function availablePresets(
  baseMag: number | null,
  presets: readonly number[] = [1, 2, 4, 10, 20, 40],
): number[] {
  if (!baseMag || baseMag <= 0) return [...presets];
  return presets.filter((p) => p <= baseMag + 1e-6);
}

/**
 * The display scale (``screenPxPerImagePx``) that renders a target
 * objective magnification, given the slide's base magnification.
 */
export function scaleForMagnification(targetMag: number, baseMag: number): number {
  if (!(baseMag > 0)) return 1;
  return targetMag / baseMag;
}

/**
 * Derive mpp from a manual calibration: the user drew a line spanning
 * ``imagePixels`` base-level pixels that is ``physicalLength`` long in the
 * given unit. Returns micrometres-per-pixel, or null on bad input.
 */
export function calibrationMpp(
  imagePixels: number,
  physicalLength: number,
  unit: "um" | "mm",
): number | null {
  if (!(imagePixels > 0) || !(physicalLength > 0)) return null;
  const um = unit === "mm" ? physicalLength * 1000 : physicalLength;
  return um / imagePixels;
}
