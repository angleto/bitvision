// Unit tests for presetForPhase: mapping a classified contrast/acquisition
// phase to the matching W/L preset (region-aware liver vs kidney).

import { describe, expect, test } from "vitest";

import { presetForPhase } from "@/lib/windowing";

describe("presetForPhase", () => {
  test("liver phases map to their dedicated CT windows", () => {
    expect(presetForPhase("unenhanced", "LIVER")?.label).toBe("CT Liver non-contrast");
    expect(presetForPhase("arterial", "LIVER")?.label).toBe("CT Liver arterial");
    expect(presetForPhase("portal_venous", "LIVER")?.label).toBe("CT Liver portal venous");
    expect(presetForPhase("delayed", "LIVER")?.label).toBe("CT Liver delayed");
  });

  test("renal phases map to kidney windows when body part is renal", () => {
    expect(presetForPhase("corticomedullary", "KIDNEY")?.label).toBe("CT Kidney corticomedullary");
    expect(presetForPhase("nephrographic", "kidney")?.label).toBe("CT Kidney nephrographic");
    expect(presetForPhase("excretory", "renal mass")?.label).toBe("CT Kidney excretory");
    expect(presetForPhase("unenhanced", "KIDNEY")?.label).toBe("CT Kidney");
  });

  test("default region is hepatic when body part is absent/abdomen", () => {
    expect(presetForPhase("portal_venous", undefined)?.label).toBe("CT Liver portal venous");
    expect(presetForPhase("arterial", "ABDOMEN")?.label).toBe("CT Liver arterial");
  });

  test("returns a usable wc/ww, not just a label", () => {
    const p = presetForPhase("arterial", "LIVER");
    expect(p).not.toBeNull();
    expect(typeof p?.wc).toBe("number");
    expect(p?.ww).toBeGreaterThan(0);
  });

  test("phases without a dedicated CT window return null", () => {
    // hepatobiliary = MR Gd-EOB; dynamic / other have no CT preset.
    expect(presetForPhase("hepatobiliary", "LIVER")).toBeNull();
    expect(presetForPhase("dynamic", "LIVER")).toBeNull();
    expect(presetForPhase("other", "LIVER")).toBeNull();
  });

  test("null / unknown phase returns null", () => {
    expect(presetForPhase(null, "LIVER")).toBeNull();
    expect(presetForPhase(undefined, "LIVER")).toBeNull();
    expect(presetForPhase("not_a_phase", "LIVER")).toBeNull();
  });
});
