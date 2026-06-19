import { describe, expect, test } from "vitest";

import {
  availablePresets,
  baseMagnification,
  calibrationMpp,
  computeScaleBar,
  currentMagnification,
  formatMagnification,
  formatMicrometers,
  niceLength,
  scaleForMagnification,
} from "@/lib/pathology/scaleMath";

describe("niceLength", () => {
  test("snaps to 1/2/5×10ⁿ", () => {
    expect(niceLength(1.2)).toBe(1);
    expect(niceLength(1.6)).toBe(2);
    expect(niceLength(4)).toBe(5);
    expect(niceLength(8)).toBe(10);
    expect(niceLength(230)).toBe(200);
    expect(niceLength(0)).toBe(0);
  });
});

describe("computeScaleBar", () => {
  test("a 40× slide (0.25 µm/px) at full resolution", () => {
    // 1 image px = 1 screen px ⇒ 1 screen px = 0.25 µm.
    const bar = computeScaleBar({ mppX: 0.25, screenPxPerImagePx: 1, targetScreenPx: 100 });
    // ~25 µm target → nice 20 µm; bar width = 20 / 0.25 = 80 px.
    expect(bar).not.toBeNull();
    expect(bar?.micrometers).toBe(20);
    expect(bar?.screenPx).toBeCloseTo(80, 5);
    expect(bar?.label).toBe("20 µm");
  });

  test("switches to mm for large bars and hides when mpp unknown", () => {
    const bar = computeScaleBar({ mppX: 0.5, screenPxPerImagePx: 0.01, targetScreenPx: 100 });
    // 1 screen px = 0.5/0.01 = 50 µm; 100 px → 5000 µm → 5 mm.
    expect(bar?.micrometers).toBe(5000);
    expect(bar?.label).toBe("5 mm");
    expect(computeScaleBar({ mppX: null, screenPxPerImagePx: 1, targetScreenPx: 100 })).toBeNull();
  });
});

describe("formatMicrometers", () => {
  test("µm under 1000, mm above", () => {
    expect(formatMicrometers(250)).toBe("250 µm");
    expect(formatMicrometers(5)).toBe("5 µm");
    expect(formatMicrometers(2000)).toBe("2 mm");
  });
});

describe("magnification", () => {
  test("uses reported magnification, else derives from mpp", () => {
    expect(baseMagnification(40, 0.25)).toBe(40);
    expect(baseMagnification(null, 0.25)).toBe(40);
    expect(baseMagnification(null, 0.5)).toBe(20);
    expect(baseMagnification(null, null)).toBeNull();
  });

  test("current magnification scales with the display", () => {
    expect(currentMagnification(40, 1)).toBe(40);
    expect(currentMagnification(40, 0.5)).toBe(20);
    expect(currentMagnification(null, 1)).toBeNull();
    expect(formatMagnification(18.37)).toBe("18.4×");
    expect(formatMagnification(40)).toBe("40×");
    expect(formatMagnification(null)).toBe("—");
  });

  test("presets clamp to the base magnification (no false over-zoom)", () => {
    expect(availablePresets(20)).toEqual([1, 2, 4, 10, 20]);
    expect(availablePresets(40)).toEqual([1, 2, 4, 10, 20, 40]);
    expect(availablePresets(null)).toEqual([1, 2, 4, 10, 20, 40]);
  });

  test("scaleForMagnification inverts currentMagnification", () => {
    expect(scaleForMagnification(20, 40)).toBe(0.5);
    expect(scaleForMagnification(40, 40)).toBe(1);
  });
});

describe("calibrationMpp", () => {
  test("known length over N pixels → µm/px", () => {
    expect(calibrationMpp(200, 1000, "um")).toBe(5);
    expect(calibrationMpp(100, 1, "mm")).toBe(10); // 1 mm = 1000 µm / 100 px
    expect(calibrationMpp(0, 100, "um")).toBeNull();
  });
});
