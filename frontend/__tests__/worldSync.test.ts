// Unit tests for the N-pane world-coordinate sync math.

import { describe, expect, test } from "vitest";

import type { Mat4, Vec3 } from "@/lib/affine";
import { mapWorldAcrossPanes } from "@/lib/worldSync";

// Pure translation matrix (reference-world -> pane-world): +[10,20,30].
const TRANSLATE: Mat4 = [
  [1, 0, 0, 10],
  [0, 1, 0, 20],
  [0, 0, 1, 30],
  [0, 0, 0, 1],
];

describe("mapWorldAcrossPanes", () => {
  test("identity <-> identity is a no-op (same-FoR common case)", () => {
    const p: Vec3 = [5, 6, 7];
    expect(mapWorldAcrossPanes(p, null, null)).toEqual(p);
  });

  test("identity source -> translated target applies the target transform", () => {
    // pane i = reference (identity); pane j has M_j = +[10,20,30].
    expect(mapWorldAcrossPanes([0, 0, 0], null, TRANSLATE)).toEqual([10, 20, 30]);
  });

  test("translated source -> identity target lifts back to reference", () => {
    // pane i has M_i = +[10,20,30]; a point at [10,20,30] in pane i is the
    // reference origin, which lands at [0,0,0] in the identity pane j.
    expect(mapWorldAcrossPanes([10, 20, 30], TRANSLATE, null)).toEqual([0, 0, 0]);
  });

  test("translated source -> translated target round-trips through reference", () => {
    // Both panes share the same transform: i->ref->j is the identity.
    const p: Vec3 = [3, 4, 5];
    const out = mapWorldAcrossPanes(p, TRANSLATE, TRANSLATE);
    expect(out[0]).toBeCloseTo(3);
    expect(out[1]).toBeCloseTo(4);
    expect(out[2]).toBeCloseTo(5);
  });
});
