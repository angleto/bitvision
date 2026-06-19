// Unit tests for the Cornerstone BidirectionalTool → RECIST axis extraction.

import { describe, expect, test } from "vitest";

import { extractBidirectionalMm, formatBidirectionalLabel } from "@/lib/measurements";

describe("extractBidirectionalMm", () => {
  test("pulls long axis from length and short axis from width (mm, verbatim)", () => {
    expect(extractBidirectionalMm({ length: 24.3, width: 16.1 })).toEqual({
      longAxisMm: 24.3,
      shortAxisMm: 16.1,
    });
  });

  test("a length-only annotation yields long axis, no short axis", () => {
    expect(extractBidirectionalMm({ length: 18 })).toEqual({
      longAxisMm: 18,
      shortAxisMm: undefined,
    });
  });

  test("non-finite and missing values become undefined (not 0)", () => {
    expect(extractBidirectionalMm({ length: Number.NaN, width: Number.POSITIVE_INFINITY })).toEqual(
      {
        longAxisMm: undefined,
        shortAxisMm: undefined,
      },
    );
    expect(extractBidirectionalMm(undefined)).toEqual({
      longAxisMm: undefined,
      shortAxisMm: undefined,
    });
  });

  test("a genuine zero measurement is preserved", () => {
    expect(extractBidirectionalMm({ length: 0, width: 0 })).toEqual({
      longAxisMm: 0,
      shortAxisMm: 0,
    });
  });
});

describe("formatBidirectionalLabel", () => {
  test("formats both axes as 'long × short mm'", () => {
    expect(formatBidirectionalLabel(24.34, 16.07)).toBe("24.3 × 16.1 mm");
  });

  test("falls back to long-axis-only when no short axis", () => {
    expect(formatBidirectionalLabel(18.2)).toBe("18.2 mm");
  });

  test("returns null when there is no long axis", () => {
    expect(formatBidirectionalLabel(undefined, 12)).toBeNull();
  });
});
