/**
 * Minimal 4x4 affine helpers for mapping a world (LPS) point through a
 * registration transform between two studies' frames. Pure + unit-tested.
 */

export type Mat4 = number[][]; // 4x4 row-major
export type Vec3 = [number, number, number];

/** Apply a 4x4 affine to a 3D point: ``out = M @ [p; 1]``. */
export function applyAffine(m: Mat4, p: Vec3): Vec3 {
  return [
    m[0][0] * p[0] + m[0][1] * p[1] + m[0][2] * p[2] + m[0][3],
    m[1][0] * p[0] + m[1][1] * p[1] + m[1][2] * p[2] + m[1][3],
    m[2][0] * p[0] + m[2][1] * p[1] + m[2][2] * p[2] + m[2][3],
  ];
}

/** Invert a 4x4 affine (rotation/scale/shear + translation). Returns null
 * when the linear part is singular. */
export function invertAffine(m: Mat4): Mat4 | null {
  const a = [
    [m[0][0], m[0][1], m[0][2]],
    [m[1][0], m[1][1], m[1][2]],
    [m[2][0], m[2][1], m[2][2]],
  ];
  const det =
    a[0][0] * (a[1][1] * a[2][2] - a[1][2] * a[2][1]) -
    a[0][1] * (a[1][0] * a[2][2] - a[1][2] * a[2][0]) +
    a[0][2] * (a[1][0] * a[2][1] - a[1][1] * a[2][0]);
  if (Math.abs(det) < 1e-12) return null;
  const inv = [
    [
      (a[1][1] * a[2][2] - a[1][2] * a[2][1]) / det,
      (a[0][2] * a[2][1] - a[0][1] * a[2][2]) / det,
      (a[0][1] * a[1][2] - a[0][2] * a[1][1]) / det,
    ],
    [
      (a[1][2] * a[2][0] - a[1][0] * a[2][2]) / det,
      (a[0][0] * a[2][2] - a[0][2] * a[2][0]) / det,
      (a[0][2] * a[1][0] - a[0][0] * a[1][2]) / det,
    ],
    [
      (a[1][0] * a[2][1] - a[1][1] * a[2][0]) / det,
      (a[0][1] * a[2][0] - a[0][0] * a[2][1]) / det,
      (a[0][0] * a[1][1] - a[0][1] * a[1][0]) / det,
    ],
  ];
  const b = [m[0][3], m[1][3], m[2][3]];
  const tb: Vec3 = [
    -(inv[0][0] * b[0] + inv[0][1] * b[1] + inv[0][2] * b[2]),
    -(inv[1][0] * b[0] + inv[1][1] * b[1] + inv[1][2] * b[2]),
    -(inv[2][0] * b[0] + inv[2][1] * b[1] + inv[2][2] * b[2]),
  ];
  return [
    [inv[0][0], inv[0][1], inv[0][2], tb[0]],
    [inv[1][0], inv[1][1], inv[1][2], tb[1]],
    [inv[2][0], inv[2][1], inv[2][2], tb[2]],
    [0, 0, 0, 1],
  ];
}

/** Shape-check a value as a 4x4 numeric matrix (e.g. from a registration's
 * ``result_meta.lps_matrix``). */
export function isMat4(v: unknown): v is Mat4 {
  return (
    Array.isArray(v) &&
    v.length === 4 &&
    v.every(
      (row) => Array.isArray(row) && row.length === 4 && row.every((n) => typeof n === "number"),
    )
  );
}
