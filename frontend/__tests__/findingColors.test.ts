import {
  CLASS_COLOR,
  FINDING_CATEGORIES,
  colorForCategory,
  overlayStroke,
} from "@/lib/findingColors";
import { describe, expect, it } from "vitest";

describe("findingColors", () => {
  it("maps every one of the 9 backend categories to a distinct colour", () => {
    const colors = FINDING_CATEGORIES.map((c) => CLASS_COLOR[c]);
    expect(colors).toHaveLength(9);
    expect(new Set(colors).size).toBe(9); // all distinct
    for (const c of FINDING_CATEGORIES) {
      expect(colorForCategory(c)).toBe(CLASS_COLOR[c]);
    }
  });

  it("falls back to the 'other' colour for unknown / missing category", () => {
    expect(colorForCategory(undefined)).toBe(CLASS_COLOR.other);
    expect(colorForCategory(null)).toBe(CLASS_COLOR.other);
    expect(colorForCategory("")).toBe(CLASS_COLOR.other);
    expect(colorForCategory("not-a-category")).toBe(CLASS_COLOR.other);
  });

  it("overlayStroke prefers the class colour when a category is present", () => {
    expect(overlayStroke({ category: "mass", kind: "bbox.lesion" })).toBe(CLASS_COLOR.mass);
    expect(overlayStroke({ category: "fluid" })).toBe(CLASS_COLOR.fluid);
  });

  it("overlayStroke falls back to the legacy geometry-kind colour without a category", () => {
    expect(overlayStroke({ kind: "bbox.lesion" })).toBe("#fb923c");
    expect(overlayStroke({ kind: "fiducial" })).toBe("#22d3ee");
    expect(overlayStroke({ kind: "text-overlay" })).toBe("#facc15");
    expect(overlayStroke({})).toBe("#facc15");
  });
});
