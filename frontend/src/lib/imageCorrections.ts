// Small helpers that encode the DICOM pixel fidelity rules used by the
// viewer: photometric auto-invert for MONOCHROME1, and non-square pixel
// spacing (anisotropic voxels) correction via CSS/canvas transforms.
//
// Kept as pure functions so the MPR / slice viewer components can wire
// them in without changing their render loop structure.

export interface DisplayMetadata {
  series_id: string;
  photometric_interpretation: string | null;
  invert: boolean;
  pixel_spacing: [number, number]; // [sx, sy] mm (column, row)
  rows: number;
  columns: number;
}

/**
 * Apply the photometric invert rule to an already-windowed 8-bit intensity.
 *
 * For MONOCHROME1 DICOMs the stored convention is "high pixel = black"
 * (film-negative look). To get a conventional "high = bright" display
 * we flip the 8-bit value. ``invertFlag`` is the per-series toggle
 * coming from the display-metadata endpoint (or a user override).
 */
export function applyPhotometric(invertFlag: boolean, pixel: number): number {
  if (!invertFlag) return pixel;
  // Clamp to the 8-bit display range so callers can pass the raw
  // pre-clamp value and still get a valid result.
  const p = Math.max(0, Math.min(255, pixel));
  return 255 - p;
}

export interface AspectCorrection {
  /** Horizontal scale for CSS ``transform: scale(scaleX, scaleY)``. */
  scaleX: number;
  /** Vertical scale for CSS ``transform: scale(scaleX, scaleY)``. */
  scaleY: number;
  /** Ready-made ``transform`` string; 1 for the axis already at unit spacing. */
  transform: string;
}

/**
 * Compute a CSS/canvas scale that makes anisotropic pixels (``sx != sy``)
 * render as squares on screen. The axis with smaller spacing keeps
 * scale = 1; the other axis scales by the ratio so 1 mm of tissue
 * occupies the same number of CSS pixels in both directions.
 *
 * Example: ``spacing = [0.5, 1.0]`` → ``{ scaleX: 1, scaleY: 2 }``:
 * vertical stretch by 2× so each row represents the same physical
 * distance as each column.
 */
export function correctAspectRatio(spacing: [number, number]): AspectCorrection {
  const [sx, sy] = spacing;
  if (!Number.isFinite(sx) || !Number.isFinite(sy) || sx <= 0 || sy <= 0) {
    return { scaleX: 1, scaleY: 1, transform: "none" };
  }
  if (Math.abs(sx - sy) < 1e-6) {
    return { scaleX: 1, scaleY: 1, transform: "none" };
  }
  // Normalise against the smaller spacing so neither axis shrinks
  // (shrinking would crop the image; stretching just fills more canvas).
  const base = Math.min(sx, sy);
  const scaleX = sx / base;
  const scaleY = sy / base;
  return {
    scaleX,
    scaleY,
    transform: `scale(${scaleX}, ${scaleY})`,
  };
}
