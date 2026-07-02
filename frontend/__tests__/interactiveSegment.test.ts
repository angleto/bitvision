import { describe, expect, it } from "vitest";

import {
  type PredictArgs,
  decodeMaskB64,
  ijkClickToPredict,
  ijkInBounds,
} from "@/lib/interactiveSegment";

// The mapping is the whole correctness risk of the click-to-segment tool
// (task 3af7a33d). Assert every pane against the backend/worker contract:
//   axial(Z)   -> axis 2, slice_idx=k, point [i,j]
//   coronal(Y) -> axis 1, slice_idx=j, point [i,k]
//   sagittal(X)-> axis 0, slice_idx=i, point [j,k]
describe("ijkClickToPredict", () => {
  const ijk: [number, number, number] = [10, 20, 30]; // i=10 (x), j=20 (y), k=30 (z)

  it("axial: fixed Z, in-plane (x,y)", () => {
    expect(ijkClickToPredict("axial", ijk)).toEqual<PredictArgs>({
      axis: 2,
      slice_idx: 30,
      point: [10, 20],
    });
  });

  it("coronal: fixed Y, in-plane (x,z)", () => {
    expect(ijkClickToPredict("coronal", ijk)).toEqual<PredictArgs>({
      axis: 1,
      slice_idx: 20,
      point: [10, 30],
    });
  });

  it("sagittal: fixed X, in-plane (y,z)", () => {
    expect(ijkClickToPredict("sagittal", ijk)).toEqual<PredictArgs>({
      axis: 0,
      slice_idx: 10,
      point: [20, 30],
    });
  });

  it("rounds fractional voxel indices to integers", () => {
    const r = ijkClickToPredict("axial", [10.4, 19.6, 30.5]);
    expect(r).toEqual<PredictArgs>({ axis: 2, slice_idx: 31, point: [10, 20] });
    expect(Number.isInteger(r.slice_idx)).toBe(true);
    expect(r.point.every(Number.isInteger)).toBe(true);
  });

  it("every pane's point/slice_idx are drawn from the fixed-axis contract", () => {
    // A distinct value per axis catches any transposition bug.
    const p: [number, number, number] = [1, 2, 3];
    expect(ijkClickToPredict("axial", p)).toMatchObject({ axis: 2, slice_idx: 3, point: [1, 2] });
    expect(ijkClickToPredict("coronal", p)).toMatchObject({ axis: 1, slice_idx: 2, point: [1, 3] });
    expect(ijkClickToPredict("sagittal", p)).toMatchObject({
      axis: 0,
      slice_idx: 1,
      point: [2, 3],
    });
  });
});

describe("ijkInBounds", () => {
  const dims: [number, number, number] = [512, 512, 200];
  it("accepts an in-volume voxel", () => {
    expect(ijkInBounds([10, 20, 30], dims)).toBe(true);
    expect(ijkInBounds([0, 0, 0], dims)).toBe(true);
    expect(ijkInBounds([511, 511, 199], dims)).toBe(true);
  });
  it("rejects negative or out-of-range voxels (click on the black margin)", () => {
    expect(ijkInBounds([-1, 20, 30], dims)).toBe(false);
    expect(ijkInBounds([10, 512, 30], dims)).toBe(false);
    expect(ijkInBounds([10, 20, 200], dims)).toBe(false);
  });
});

describe("decodeMaskB64", () => {
  it("round-trips raw uint8 bytes", () => {
    const raw = new Uint8Array([0, 1, 1, 0, 255, 128]);
    const b64 = Buffer.from(raw).toString("base64");
    expect(Array.from(decodeMaskB64(b64))).toEqual(Array.from(raw));
  });
});
