import { sparklinePoints } from "@/components/Sparkline";
import { describe, expect, it } from "vitest";

describe("sparklinePoints", () => {
  it("maps an increasing series left-to-right, lowest value at the bottom", () => {
    const pts = sparklinePoints([1, 2, 3], 100, 30);
    expect(pts).toHaveLength(3);
    expect(pts[0].x).toBeCloseTo(0);
    expect(pts[2].x).toBeCloseTo(100);
    // higher value -> smaller y (SVG y grows downward)
    expect(pts[2].y).toBeLessThan(pts[0].y);
  });

  it("centres a single point and drops non-finite values", () => {
    expect(sparklinePoints([5], 100, 30)[0].x).toBeCloseTo(50);
    expect(sparklinePoints([1, Number.NaN, 3], 100, 30)).toHaveLength(2);
    expect(sparklinePoints([], 100, 30)).toEqual([]);
  });

  it("keeps points within the padded box", () => {
    for (const p of sparklinePoints([3, 1, 4, 1, 5], 120, 40)) {
      expect(p.y).toBeGreaterThanOrEqual(2);
      expect(p.y).toBeLessThanOrEqual(38);
    }
  });
});
