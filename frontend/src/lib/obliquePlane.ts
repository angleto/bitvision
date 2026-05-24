// Math helpers for reformatting a Float32 volume along an arbitrary plane.
//
// Conventions
// - Volume scalars are stored as Float32Array with voxel index =
//     z * (nx * ny) + y * nx + x (matches MPRViewport).
// - "normal" is a unit vector (expressed in voxel axes) that defines the
//   plane orientation. "up" is the in-plane vertical axis (also unit).
// - The plane passes through `origin` (voxel coordinates, may be fractional).
// - Output samples are laid out row-major, row length = outW, rows = outH,
//   with outPixelSize in millimetres. This gives a physically-correct image.

export type Vec3 = [number, number, number];

export function v3(x: number, y: number, z: number): Vec3 {
  return [x, y, z];
}

export function normalize(v: Vec3): Vec3 {
  const n = Math.hypot(v[0], v[1], v[2]) || 1;
  return [v[0] / n, v[1] / n, v[2] / n];
}

export function cross(a: Vec3, b: Vec3): Vec3 {
  return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
}

export function dot(a: Vec3, b: Vec3): number {
  return a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
}

export function add(a: Vec3, b: Vec3): Vec3 {
  return [a[0] + b[0], a[1] + b[1], a[2] + b[2]];
}

export function scale(a: Vec3, s: number): Vec3 {
  return [a[0] * s, a[1] * s, a[2] * s];
}

// Rotate vector `v` around unit axis `axis` by angle (radians).
export function rotateAround(v: Vec3, axis: Vec3, angle: number): Vec3 {
  const a = normalize(axis);
  const cosA = Math.cos(angle);
  const sinA = Math.sin(angle);
  const k = dot(a, v);
  // Rodrigues' rotation formula.
  return [
    v[0] * cosA + (a[1] * v[2] - a[2] * v[1]) * sinA + a[0] * k * (1 - cosA),
    v[1] * cosA + (a[2] * v[0] - a[0] * v[2]) * sinA + a[1] * k * (1 - cosA),
    v[2] * cosA + (a[0] * v[1] - a[1] * v[0]) * sinA + a[2] * k * (1 - cosA),
  ];
}

// Build an orthonormal plane basis (u-right, v-down, normal) from a desired
// plane normal and a desired "up" direction. The up vector is re-projected
// onto the plane so the basis stays orthonormal even if caller is sloppy.
export function planeBasis(normal: Vec3, upHint: Vec3): { u: Vec3; v: Vec3; n: Vec3 } {
  const n = normalize(normal);
  // If upHint is near-parallel to n, pick a safe fallback.
  let up = upHint;
  if (Math.abs(dot(n, normalize(up))) > 0.99) {
    up = Math.abs(n[2]) < 0.99 ? [0, 0, 1] : [0, 1, 0];
  }
  // u = up x n, then v = n x u (so +v points "down" in screen space).
  const u = normalize(cross(up, n));
  const v = normalize(cross(n, u));
  return { u, v, n };
}

// Trilinear interpolation in voxel space. Returns `fallback` when out of
// bounds (one voxel border, so the boundary samples still interpolate cleanly).
export function trilinear(
  scalars: Float32Array,
  nx: number,
  ny: number,
  nz: number,
  x: number,
  y: number,
  z: number,
  fallback = 0,
): number {
  if (x < 0 || y < 0 || z < 0 || x > nx - 1 || y > ny - 1 || z > nz - 1) return fallback;
  const x0 = Math.floor(x);
  const y0 = Math.floor(y);
  const z0 = Math.floor(z);
  const x1 = Math.min(x0 + 1, nx - 1);
  const y1 = Math.min(y0 + 1, ny - 1);
  const z1 = Math.min(z0 + 1, nz - 1);
  const fx = x - x0;
  const fy = y - y0;
  const fz = z - z0;
  const nxy = nx * ny;
  const c000 = scalars[z0 * nxy + y0 * nx + x0];
  const c100 = scalars[z0 * nxy + y0 * nx + x1];
  const c010 = scalars[z0 * nxy + y1 * nx + x0];
  const c110 = scalars[z0 * nxy + y1 * nx + x1];
  const c001 = scalars[z1 * nxy + y0 * nx + x0];
  const c101 = scalars[z1 * nxy + y0 * nx + x1];
  const c011 = scalars[z1 * nxy + y1 * nx + x0];
  const c111 = scalars[z1 * nxy + y1 * nx + x1];
  const c00 = c000 * (1 - fx) + c100 * fx;
  const c01 = c001 * (1 - fx) + c101 * fx;
  const c10 = c010 * (1 - fx) + c110 * fx;
  const c11 = c011 * (1 - fx) + c111 * fx;
  const c0 = c00 * (1 - fy) + c10 * fy;
  const c1 = c01 * (1 - fy) + c11 * fy;
  return c0 * (1 - fz) + c1 * fz;
}

export interface ReformatParams {
  scalars: Float32Array;
  dims: [number, number, number]; // nx, ny, nz
  spacing: [number, number, number]; // sx, sy, sz in mm
  origin: Vec3; // plane origin in voxel coords
  u: Vec3; // in-plane "right" unit axis (voxel space)
  v: Vec3; // in-plane "down" unit axis (voxel space)
  outW: number; // output image width (pixels)
  outH: number; // output image height (pixels)
  outPixelSize: number; // mm per output pixel (isotropic)
  fallback?: number; // value for out-of-volume samples
}

// Resample the volume along the plane into a Float32Array of size outW*outH.
// Each output pixel covers `outPixelSize` mm. The plane is sampled so the
// origin maps to the image centre.
export function reformatSlice(p: ReformatParams): Float32Array {
  const { scalars, dims, spacing, origin, u, v, outW, outH, outPixelSize } = p;
  const [nx, ny, nz] = dims;
  const [sx, sy, sz] = spacing;
  const fallback = p.fallback ?? 0;
  const out = new Float32Array(outW * outH);
  // Convert plane axes (mm) to voxel steps per output pixel.
  // Moving 1 mm along axis u changes voxel coords by (u[0]/sx, u[1]/sy, u[2]/sz).
  const stepU: Vec3 = [
    (u[0] / sx) * outPixelSize,
    (u[1] / sy) * outPixelSize,
    (u[2] / sz) * outPixelSize,
  ];
  const stepV: Vec3 = [
    (v[0] / sx) * outPixelSize,
    (v[1] / sy) * outPixelSize,
    (v[2] / sz) * outPixelSize,
  ];
  const halfW = (outW - 1) / 2;
  const halfH = (outH - 1) / 2;
  // Start at top-left corner of the reformatted image (in voxel coords).
  const start: Vec3 = [
    origin[0] - halfW * stepU[0] - halfH * stepV[0],
    origin[1] - halfW * stepU[1] - halfH * stepV[1],
    origin[2] - halfW * stepU[2] - halfH * stepV[2],
  ];
  for (let j = 0; j < outH; j++) {
    const rowX = start[0] + j * stepV[0];
    const rowY = start[1] + j * stepV[1];
    const rowZ = start[2] + j * stepV[2];
    for (let i = 0; i < outW; i++) {
      const x = rowX + i * stepU[0];
      const y = rowY + i * stepU[1];
      const z = rowZ + i * stepU[2];
      out[j * outW + i] = trilinear(scalars, nx, ny, nz, x, y, z, fallback);
    }
  }
  return out;
}

// Convenience: build the world-space diagonal (in mm) of the volume so the
// reformat image can always cover the whole dataset regardless of orientation.
export function volumeDiagonalMm(
  dims: [number, number, number],
  spacing: [number, number, number],
): number {
  const dx = dims[0] * spacing[0];
  const dy = dims[1] * spacing[1];
  const dz = dims[2] * spacing[2];
  return Math.sqrt(dx * dx + dy * dy + dz * dz);
}
