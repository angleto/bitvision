import { applyAffine, invertAffine, isMat4 } from "@/lib/affine";
import { describe, expect, it } from "vitest";

const ROT_Z_90: number[][] = [
  [0, -1, 0, 5],
  [1, 0, 0, -2],
  [0, 0, 1, 3],
  [0, 0, 0, 1],
];

describe("affine", () => {
  it("applies a rotation + translation", () => {
    // rotate (1,0,0) by +90° about z -> (0,1,0), then translate (5,-2,3)
    expect(applyAffine(ROT_Z_90, [1, 0, 0])).toEqual([5, -1, 3]);
  });

  it("inverts so that M then M^-1 is identity", () => {
    const inv = invertAffine(ROT_Z_90);
    expect(inv).not.toBeNull();
    const p: [number, number, number] = [3.5, -1.2, 7];
    const back = applyAffine(inv as number[][], applyAffine(ROT_Z_90, p));
    for (let i = 0; i < 3; i++) expect(back[i]).toBeCloseTo(p[i], 9);
  });

  it("returns null for a singular linear part", () => {
    expect(
      invertAffine([
        [0, 0, 0, 1],
        [0, 0, 0, 2],
        [0, 0, 0, 3],
        [0, 0, 0, 1],
      ]),
    ).toBeNull();
  });

  it("validates 4x4 shape", () => {
    expect(isMat4(ROT_Z_90)).toBe(true);
    expect(isMat4([[1, 2, 3]])).toBe(false);
    expect(isMat4(null)).toBe(false);
  });
});
