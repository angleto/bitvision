// Unit tests for the shared voxel sampler.
//
// The samplers are reused by the voxel HUD (single-voxel readout under
// the cursor) and by the Lens probe (mean/std/min/max over a disc with
// configurable radius). Each axis projects the disc onto a different
// pair of in-plane indices, so the test exercises all three.

import { describe, expect, test } from "vitest";

import {
  type Sampler,
  type SamplingVolume,
  makeSampler,
  sampleDisk,
  sampleVoxel,
} from "@/lib/cornerstoneTools/volumeSampling";

// Synthetic volume: 16 × 16 × 8 with a known intensity field. Voxel at
// (i, j, k) carries the value ``i * 100 + j``, independent of k — that
// makes mean / std / min / max predictable analytically without having
// to materialise the array elsewhere.
function makeSyntheticVolume(): SamplingVolume {
  const dims: [number, number, number] = [16, 16, 8];
  const [nx, ny, nz] = dims;
  const data = new Float32Array(nx * ny * nz);
  for (let k = 0; k < nz; k++) {
    for (let j = 0; j < ny; j++) {
      for (let i = 0; i < nx; i++) {
        data[i + j * nx + k * nx * ny] = i * 100 + j;
      }
    }
  }
  const spacing: [number, number, number] = [1, 1, 1];

  return {
    imageData: {
      worldToIndex: (w) => [w[0], w[1], w[2]],
      getDimensions: () => dims,
      getSpacing: () => spacing,
    },
    voxelManager: {
      getAtIJK: (i, j, k) => data[i + j * nx + k * nx * ny] as number,
    },
    dimensions: dims,
    spacing,
  };
}

// Helper: build a sampler and fail loudly if the factory returns null
// — that always indicates the test setup is broken, not the sampler.
function buildSampler(): Sampler {
  const s = makeSampler(makeSyntheticVolume());
  if (!s) throw new Error("test setup: makeSampler returned null");
  return s;
}

describe("sampleVoxel", () => {
  test("returns the value at the rounded IJK index", () => {
    const sampler = buildSampler();
    const res = sampleVoxel(sampler, [7, 3, 2]);
    expect(res).not.toBeNull();
    expect(res?.value).toBe(7 * 100 + 3);
    expect(res?.i).toBe(7);
    expect(res?.j).toBe(3);
    expect(res?.k).toBe(2);
  });

  test("returns null outside the volume", () => {
    const sampler = buildSampler();
    expect(sampleVoxel(sampler, [-1, 0, 0])).toBeNull();
    expect(sampleVoxel(sampler, [16, 0, 0])).toBeNull();
    expect(sampleVoxel(sampler, [0, 0, 8])).toBeNull();
  });
});

describe("sampleDisk — axial plane", () => {
  test("radius 1 mm picks up exactly the 5-voxel cross (centre + N/E/S/W)", () => {
    // Centre at (8, 8, 4). Voxels at offset (0,0), (±1,0), (0,±1) — 5
    // voxels with x²+y² ≤ 1.
    const stats = sampleDisk(buildSampler(), [8, 8, 4], "axial", 1.0);
    expect(stats).not.toBeNull();
    expect(stats?.count).toBe(5);
    // Values: (8,8)=808, (7,8)=708, (9,8)=908, (8,7)=807, (8,9)=809
    // Sum = 808 + 708 + 908 + 807 + 809 = 4040, mean = 808.
    expect(stats?.mean).toBeCloseTo(808, 5);
    expect(stats?.min).toBe(708);
    expect(stats?.max).toBe(908);
  });

  test("radius 5 mm — count matches the inscribed disc", () => {
    // ⌈5/1⌉ = 5 → 11×11 candidate grid; voxels with i²+j² ≤ 25 keep
    // (the disc area is ~π·25 ≈ 78.5; the integer grid count is
    // computed below for exactness).
    const expectedCount = countDiscVoxels(5);
    const stats = sampleDisk(buildSampler(), [8, 8, 4], "axial", 5.0);
    expect(stats?.count).toBe(expectedCount);
  });

  test("returns null when the centre is outside the volume", () => {
    const stats = sampleDisk(buildSampler(), [-1, 0, 0], "axial", 5.0);
    expect(stats).toBeNull();
  });

  test("clips at volume bounds when the disc straddles an edge", () => {
    // Centre at (0, 0, 4) — only the SE quadrant of the disc is inside.
    const stats = sampleDisk(buildSampler(), [0, 0, 4], "axial", 2.0);
    expect(stats).not.toBeNull();
    // Voxels included: (0,0), (1,0), (0,1), (1,1), (2,0), (0,2)
    // — i.e. the inside-bounds subset of a r=2 disc.
    expect(stats?.count).toBe(6);
  });
});

describe("sampleDisk — sagittal plane", () => {
  test("varies j,k at fixed i; centre (4, 8, 4) r=1 picks 5 voxels", () => {
    const stats = sampleDisk(buildSampler(), [4, 8, 4], "sagittal", 1.0);
    expect(stats?.count).toBe(5);
    // All five voxels have i=4 → value = 400 + j. Means j ∈ {8, 7, 9}.
    // (4,8,4)=408, (4,7,4)=407, (4,9,4)=409, (4,8,3)=408, (4,8,5)=408
    // Sum=2040 → mean=408.
    expect(stats?.mean).toBeCloseTo(408, 5);
    expect(stats?.min).toBe(407);
    expect(stats?.max).toBe(409);
  });
});

describe("sampleDisk — coronal plane", () => {
  test("varies i,k at fixed j; centre (8, 4, 4) r=1 picks 5 voxels", () => {
    const stats = sampleDisk(buildSampler(), [8, 4, 4], "coronal", 1.0);
    expect(stats?.count).toBe(5);
    // All five voxels have j=4 → value = i*100 + 4.
    // (8,4,4)=804, (7,4,4)=704, (9,4,4)=904, (8,4,3)=804, (8,4,5)=804.
    // Sum = 4020 → mean = 804.
    expect(stats?.mean).toBeCloseTo(804, 5);
    expect(stats?.min).toBe(704);
    expect(stats?.max).toBe(904);
  });
});

describe("makeSampler — legacy fallback", () => {
  test("falls back to getScalarData when voxelManager is missing", () => {
    const dims: [number, number, number] = [4, 4, 2];
    const data = new Float32Array([
      // k=0
      1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16,
      // k=1
      17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32,
    ]);
    const vol: SamplingVolume = {
      imageData: {
        worldToIndex: (w) => [w[0], w[1], w[2]],
        getDimensions: () => dims,
        getSpacing: () => [1, 1, 1],
      },
      // No voxelManager → fall back to getScalarData.
      getScalarData: () => data,
      dimensions: dims,
    };
    const sampler = makeSampler(vol);
    expect(sampler).not.toBeNull();
    expect(sampler?.sample(0, 0, 0)).toBe(1);
    expect(sampler?.sample(3, 3, 1)).toBe(32);
  });
});

// Helper: count integer (i, j) pairs with i² + j² ≤ r².
function countDiscVoxels(radius: number): number {
  let n = 0;
  const half = Math.ceil(radius);
  for (let dj = -half; dj <= half; dj++) {
    for (let di = -half; di <= half; di++) {
      if (di * di + dj * dj <= radius * radius) n += 1;
    }
  }
  return n;
}
