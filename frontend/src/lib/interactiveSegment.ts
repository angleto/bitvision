// Pure coordinate mapping for the interactive (MedSAM-2) click-to-segment
// tool (task 3af7a33d). Kept dependency-free so it is exhaustively
// unit-testable — the whole correctness risk of the feature lives here:
// a wrong axis / slice_idx / point produces a silently-misaligned mask.
//
// Contract (from backend api/segmentations.py + worker segment_interactive.py):
//   - axis: 0 = sagittal (X-fixed), 1 = coronal (Y-fixed), 2 = axial (Z-fixed).
//   - The worker slices the packed (nz, ny, nx) volume as:
//       axis=2 (axial)    -> slice shape (ny, nx)
//       axis=1 (coronal)  -> slice shape (nz, nx)
//       axis=0 (sagittal) -> slice shape (nz, ny)
//   - points are (x, y) in SAM image convention: x = column, y = row of the
//     picked slice.
//
// Given a clicked voxel index ijk = [i, j, k] (i = x-voxel, j = y-voxel,
// k = z-voxel) and the pane the click landed in, derive (axis, slice_idx,
// point):
//   axial  : fixed Z -> axis 2, slice_idx = k, slice cols=x rows=y -> point [i, j]
//   coronal: fixed Y -> axis 1, slice_idx = j, slice cols=x rows=z -> point [i, k]
//   sagittal:fixed X -> axis 0, slice_idx = i, slice cols=y rows=z -> point [j, k]

export type PaneAxis = "axial" | "coronal" | "sagittal";

export interface PredictArgs {
  /** 0 = sagittal, 1 = coronal, 2 = axial (matches the worker). */
  axis: 0 | 1 | 2;
  /** Voxel index along ``axis``. */
  slice_idx: number;
  /** (x, y) = (column, row) in the picked slice. */
  point: [number, number];
}

/** Map a clicked voxel + the pane it was clicked in to the interactive-predict
 *  request geometry. Rounds to integer voxel indices. */
export function ijkClickToPredict(paneAxis: PaneAxis, ijk: [number, number, number]): PredictArgs {
  const i = Math.round(ijk[0]);
  const j = Math.round(ijk[1]);
  const k = Math.round(ijk[2]);
  switch (paneAxis) {
    case "axial":
      return { axis: 2, slice_idx: k, point: [i, j] };
    case "coronal":
      return { axis: 1, slice_idx: j, point: [i, k] };
    case "sagittal":
      return { axis: 0, slice_idx: i, point: [j, k] };
  }
}

/** True when the voxel index is inside a volume of the given dimensions. A
 *  click outside the rendered volume (e.g. on the black margin) rounds to an
 *  out-of-range index and must be rejected before calling the backend. */
export function ijkInBounds(
  ijk: [number, number, number],
  dims: [number, number, number],
): boolean {
  return (
    ijk[0] >= 0 &&
    ijk[1] >= 0 &&
    ijk[2] >= 0 &&
    ijk[0] < dims[0] &&
    ijk[1] < dims[1] &&
    ijk[2] < dims[2]
  );
}

/** Decode the base64 raw-uint8 mask the predict endpoint returns (the 2D
 *  slice mask, row-major shape [h, w]) into a flat Uint8Array. Display today
 *  uses the persisted full-volume mask via ``fetchMask``; this helper exists
 *  for a future in-slice Cornerstone overlay and to keep the transport
 *  decoding unit-tested. Works in both browser (atob) and Node/test (Buffer). */
export function decodeMaskB64(b64: string): Uint8Array {
  if (typeof atob === "function") {
    const bin = atob(b64);
    const out = new Uint8Array(bin.length);
    for (let n = 0; n < bin.length; n++) out[n] = bin.charCodeAt(n);
    return out;
  }
  // Node / vitest fallback.
  return new Uint8Array(Buffer.from(b64, "base64"));
}
