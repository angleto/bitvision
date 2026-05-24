/**
 * Shared voxel-sampling helpers.
 *
 * Two callers today:
 * 1. The voxel HUD in CornerstoneMPRLayout (single-voxel readout under
 *    the cursor for every tool).
 * 2. The Lens probe (live mean / std / min / max over a disc of
 *    configurable radius, plus optional SUV scaling).
 *
 * Cornerstone3D 4.x exposes scalars via ``volume.voxelManager.getAtIJK``
 * with a ``getScalarData`` legacy fallback. The samplers below adapt to
 * whichever surface is available, so consumers never see the API split.
 *
 * All math runs on the main thread. A 50 mm disc on 0.5 mm-spaced PET
 * (the worst case) is ~31k voxels per frame; even at 60 fps the cost
 * stays in microseconds.
 */

import type * as cs from "@cornerstonejs/core";

export type Axis = "axial" | "sagittal" | "coronal";

export type SamplingVolume = {
  imageData?: {
    worldToIndex: (w: cs.Types.Point3) => cs.Types.Point3;
    getDimensions: () => [number, number, number];
    getSpacing?: () => [number, number, number];
  };
  voxelManager?: {
    getAtIJK?: (i: number, j: number, k: number) => number;
  };
  getScalarData?: () => ArrayLike<number>;
  dimensions?: [number, number, number];
  spacing?: [number, number, number];
};

export type Sampler = {
  imageData: NonNullable<SamplingVolume["imageData"]>;
  dims: [number, number, number];
  spacing: [number, number, number];
  sample: (i: number, j: number, k: number) => number;
};

export type DiskStats = {
  count: number;
  mean: number;
  std: number;
  min: number;
  max: number;
};

/** Adapt either ``voxelManager`` (CS3D 4.x) or ``getScalarData`` (legacy)
 *  into a uniform ``Sampler``. Returns ``null`` when the volume has no
 *  usable scalar surface. */
export function makeSampler(vol: SamplingVolume | null | undefined): Sampler | null {
  if (!vol) return null;
  const imageData = vol.imageData;
  if (!imageData) return null;
  const dims = imageData.getDimensions();
  const spacing = imageData.getSpacing?.() ?? vol.spacing ?? [1, 1, 1];
  const getAtIJK = vol.voxelManager?.getAtIJK?.bind(vol.voxelManager);
  if (getAtIJK) {
    return { imageData, dims, spacing, sample: getAtIJK };
  }
  const scalars = vol.getScalarData?.();
  if (!scalars) return null;
  const stride0 = dims[0];
  const stride1 = dims[0] * dims[1];
  return {
    imageData,
    dims,
    spacing,
    sample: (i, j, k) => scalars[i + j * stride0 + k * stride1] as number,
  };
}

/** Read a single voxel value at the world point. ``null`` when outside
 *  the volume or unreadable. */
export function sampleVoxel(
  sampler: Sampler,
  world: cs.Types.Point3,
): { i: number; j: number; k: number; value: number } | null {
  let idx: cs.Types.Point3;
  try {
    idx = sampler.imageData.worldToIndex(world);
  } catch {
    return null;
  }
  const i = Math.round(idx[0]);
  const j = Math.round(idx[1]);
  const k = Math.round(idx[2]);
  if (
    i < 0 ||
    j < 0 ||
    k < 0 ||
    i >= sampler.dims[0] ||
    j >= sampler.dims[1] ||
    k >= sampler.dims[2]
  ) {
    return null;
  }
  const v = sampler.sample(i, j, k);
  if (typeof v !== "number" || !Number.isFinite(v)) return null;
  return { i, j, k, value: v };
}

/** Iterate the in-plane voxel indices inside a disc of ``radiusMm``
 *  centered on world point ``world``, perpendicular to ``axis``, and
 *  return the aggregate stats.
 *
 *  Dispatch on ``axis``:
 *  - ``axial``: vary i,j at fixed k
 *  - ``sagittal``: vary j,k at fixed i
 *  - ``coronal``: vary i,k at fixed j
 *
 *  Returns ``null`` when the center lies outside the volume or no
 *  voxel inside the disc is sampleable (every read produced NaN or
 *  fell outside bounds). */
export function sampleDisk(
  sampler: Sampler,
  world: cs.Types.Point3,
  axis: Axis,
  radiusMm: number,
): DiskStats | null {
  let center: cs.Types.Point3;
  try {
    center = sampler.imageData.worldToIndex(world);
  } catch {
    return null;
  }
  const ci = Math.round(center[0]);
  const cj = Math.round(center[1]);
  const ck = Math.round(center[2]);
  const [nx, ny, nz] = sampler.dims;
  if (ci < 0 || cj < 0 || ck < 0 || ci >= nx || cj >= ny || ck >= nz) {
    return null;
  }

  const [dx, dy, dz] = sampler.spacing;
  const r2 = radiusMm * radiusMm;
  let count = 0;
  let sum = 0;
  let sum2 = 0;
  let vMin = Number.POSITIVE_INFINITY;
  let vMax = Number.NEGATIVE_INFINITY;

  const accumulate = (i: number, j: number, k: number) => {
    if (i < 0 || j < 0 || k < 0 || i >= nx || j >= ny || k >= nz) return;
    const v = sampler.sample(i, j, k);
    if (typeof v !== "number" || !Number.isFinite(v)) return;
    count += 1;
    sum += v;
    sum2 += v * v;
    if (v < vMin) vMin = v;
    if (v > vMax) vMax = v;
  };

  if (axis === "axial") {
    const halfI = Math.max(1, Math.ceil(radiusMm / dx));
    const halfJ = Math.max(1, Math.ceil(radiusMm / dy));
    for (let dj = -halfJ; dj <= halfJ; dj++) {
      const yMm = dj * dy;
      const y2 = yMm * yMm;
      for (let di = -halfI; di <= halfI; di++) {
        const xMm = di * dx;
        if (xMm * xMm + y2 > r2) continue;
        accumulate(ci + di, cj + dj, ck);
      }
    }
  } else if (axis === "sagittal") {
    const halfJ = Math.max(1, Math.ceil(radiusMm / dy));
    const halfK = Math.max(1, Math.ceil(radiusMm / dz));
    for (let dk = -halfK; dk <= halfK; dk++) {
      const zMm = dk * dz;
      const z2 = zMm * zMm;
      for (let dj = -halfJ; dj <= halfJ; dj++) {
        const yMm = dj * dy;
        if (yMm * yMm + z2 > r2) continue;
        accumulate(ci, cj + dj, ck + dk);
      }
    }
  } else {
    const halfI = Math.max(1, Math.ceil(radiusMm / dx));
    const halfK = Math.max(1, Math.ceil(radiusMm / dz));
    for (let dk = -halfK; dk <= halfK; dk++) {
      const zMm = dk * dz;
      const z2 = zMm * zMm;
      for (let di = -halfI; di <= halfI; di++) {
        const xMm = di * dx;
        if (xMm * xMm + z2 > r2) continue;
        accumulate(ci + di, cj, ck + dk);
      }
    }
  }

  if (count === 0) return null;
  const mean = sum / count;
  const variance = sum2 / count - mean * mean;
  return {
    count,
    mean,
    std: Math.sqrt(Math.max(0, variance)),
    min: vMin,
    max: vMax,
  };
}

/** Return the world-coordinate IJK of a world point as a ``[i,j,k]``
 *  triple of integers (rounded). Useful when pinning a sphere ROI on
 *  the backend — the API takes voxel indices, not world coords. */
export function worldToIjk(
  sampler: Sampler,
  world: cs.Types.Point3,
): [number, number, number] | null {
  try {
    const idx = sampler.imageData.worldToIndex(world);
    return [Math.round(idx[0]), Math.round(idx[1]), Math.round(idx[2])];
  } catch {
    return null;
  }
}
