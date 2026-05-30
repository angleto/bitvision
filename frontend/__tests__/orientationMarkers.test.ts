// Unit tests for the anatomical orientation markers.
//
// Laterality is a patient-safety control: a wrong L/R letter can drive a
// wrong-side report. These pin the LPS letter mapping and the camera ->
// edge-letter derivation (incl. flips) against the radiological axial
// convention (patient Left on screen right, Anterior up).

import { describe, expect, test } from "vitest";

import {
  type Vec3,
  cameraEdgeLetters,
  cameraTransformFlags,
  lpsDirectionToString,
} from "@/lib/orientationMarkers";

describe("lpsDirectionToString", () => {
  test("maps the cardinal LPS axes", () => {
    expect(lpsDirectionToString([1, 0, 0])).toBe("L");
    expect(lpsDirectionToString([-1, 0, 0])).toBe("R");
    expect(lpsDirectionToString([0, 1, 0])).toBe("P");
    expect(lpsDirectionToString([0, -1, 0])).toBe("A");
    expect(lpsDirectionToString([0, 0, 1])).toBe("S");
    expect(lpsDirectionToString([0, 0, -1])).toBe("I");
  });

  test("concatenates oblique directions most-dominant first", () => {
    // Mostly anterior, a bit to the left.
    const v: Vec3 = [0.3, -0.95, 0];
    expect(lpsDirectionToString(v)).toBe("AL");
  });

  test("emits at least the dominant axis below threshold", () => {
    expect(lpsDirectionToString([0.05, 0, 0])).toBe("L");
  });
});

describe("cameraEdgeLetters", () => {
  test("standard Cornerstone AXIAL camera reads A top / P bottom / R left / L right", () => {
    // CS3D axial default: looking along -Z, screen-up = -Y.
    const letters = cameraEdgeLetters({
      viewUp: [0, -1, 0],
      viewPlaneNormal: [0, 0, -1],
    });
    expect(letters).toEqual({ top: "A", bottom: "P", left: "R", right: "L" });
  });

  test("horizontal flip swaps left/right (R<->L)", () => {
    const letters = cameraEdgeLetters({
      viewUp: [0, -1, 0],
      viewPlaneNormal: [0, 0, -1],
      flipHorizontal: true,
    });
    expect(letters).toEqual({ top: "A", bottom: "P", left: "L", right: "R" });
  });

  test("vertical flip swaps top/bottom (A<->P)", () => {
    const letters = cameraEdgeLetters({
      viewUp: [0, -1, 0],
      viewPlaneNormal: [0, 0, -1],
      flipVertical: true,
    });
    expect(letters).toEqual({ top: "P", bottom: "A", left: "R", right: "L" });
  });

  test("sagittal camera reads S/I vertical, A/P horizontal", () => {
    // Looking along +X (from the patient's left), up = +Z (superior).
    const letters = cameraEdgeLetters({
      viewUp: [0, 0, 1],
      viewPlaneNormal: [1, 0, 0],
    });
    expect(letters?.top).toBe("S");
    expect(letters?.bottom).toBe("I");
    // screen-right = viewUp x normal = [0,0,1]x[1,0,0] = [0,1,0] = P
    expect(letters?.right).toBe("P");
    expect(letters?.left).toBe("A");
  });

  test("returns null without camera vectors", () => {
    expect(cameraEdgeLetters({})).toBeNull();
  });
});

describe("cameraTransformFlags", () => {
  test("default camera is neither flipped nor rotated", () => {
    expect(cameraTransformFlags({ rotation: 0 })).toEqual({ flipped: false, rotated: false });
  });

  test("flags flips and rotation", () => {
    expect(cameraTransformFlags({ flipHorizontal: true }).flipped).toBe(true);
    expect(cameraTransformFlags({ flipVertical: true }).flipped).toBe(true);
    expect(cameraTransformFlags({ rotation: 90 }).rotated).toBe(true);
    expect(cameraTransformFlags({ rotation: 270 }).rotated).toBe(true);
    expect(cameraTransformFlags({ rotation: 0.4 }).rotated).toBe(false);
    // Wraparound: 359.5° is 0.5° off the default → within tolerance.
    expect(cameraTransformFlags({ rotation: 359.5 }).rotated).toBe(false);
  });
});
