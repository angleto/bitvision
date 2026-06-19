import type { Series } from "@/lib/api";
import {
  type PrimaryPlane,
  bestMatchSeries,
  descTokens,
  matchConfidence,
  seriesMatchScore,
  seriesOptionLabel,
} from "@/lib/seriesMatch";
import { describe, expect, it } from "vitest";

function mk(p: Partial<Series>): Series {
  return {
    id: p.id ?? "s",
    study_id: "study",
    series_instance_uid: "1.2.3",
    series_number: p.series_number ?? null,
    modality: p.modality ?? null,
    body_part_examined: p.body_part_examined ?? null,
    series_description: p.series_description ?? null,
    expected_instance_count: null,
    received_instance_count: p.received_instance_count ?? 0,
    ingestion_complete: true,
  } as Series;
}

describe("descTokens", () => {
  it("lowercases, strips accents/punctuation, keeps >=3-char tokens", () => {
    expect(descTokens("TC addome — fase portale/venosa")).toEqual(
      new Set(["addome", "fase", "portale", "venosa"]),
    );
    expect(descTokens(null)).toEqual(new Set());
    expect(descTokens("a di un")).toEqual(new Set()); // all <3 chars
  });
});

describe("seriesMatchScore", () => {
  const ref = mk({
    modality: "CT",
    series_description: "TC addome fase portale venosa",
    received_instance_count: 250,
  });

  it("rewards same modality dominantly", () => {
    const sameMod = mk({ modality: "CT", series_description: "x", received_instance_count: 100 });
    const diffMod = mk({ modality: "MR", series_description: "x", received_instance_count: 100 });
    expect(seriesMatchScore(sameMod, ref)).toBeGreaterThan(seriesMatchScore(diffMod, ref) + 50);
  });

  it("rewards shared description tokens (contrast phase phrasing)", () => {
    const portal = mk({
      modality: "CT",
      series_description: "addome portale venosa",
      received_instance_count: 250,
    });
    const arterial = mk({
      modality: "CT",
      series_description: "addome arteriosa",
      received_instance_count: 250,
    });
    expect(seriesMatchScore(portal, ref)).toBeGreaterThan(seriesMatchScore(arterial, ref));
  });

  it("rewards matching acquisition plane", () => {
    const a = mk({ modality: "CT", series_description: "x", received_instance_count: 250 });
    const withAxial = seriesMatchScore(a, ref, {
      candidatePlane: "axial",
      referencePlane: "axial",
    });
    const withCoronal = seriesMatchScore(a, ref, {
      candidatePlane: "coronal",
      referencePlane: "axial",
    });
    expect(withAxial).toBe(withCoronal + 25);
  });
});

describe("bestMatchSeries", () => {
  it("picks the portal-venous CT for a portal-venous reference, not arterial/basal", () => {
    const ref = mk({ modality: "CT", series_description: "TC addome portale venosa" });
    const candidates = [
      mk({ id: "basal", modality: "CT", series_description: "addome basale" }),
      mk({ id: "art", modality: "CT", series_description: "addome arteriosa" }),
      mk({ id: "pv", modality: "CT", series_description: "addome portale venosa" }),
      mk({ id: "mr", modality: "MR", series_description: "addome portale venosa" }),
    ];
    expect(bestMatchSeries(candidates, ref)?.id).toBe("pv");
  });

  it("uses plane to break ties between same-phase series", () => {
    const ref = mk({ id: "r", modality: "CT", series_description: "addome venosa" });
    const cand = [
      mk({ id: "cor", modality: "CT", series_description: "addome venosa" }),
      mk({ id: "ax", modality: "CT", series_description: "addome venosa" }),
    ];
    const planeOf = (id: string): PrimaryPlane => (id === "ax" || id === "r" ? "axial" : "coronal");
    expect(bestMatchSeries(cand, ref, planeOf)?.id).toBe("ax");
  });

  it("returns null for no candidates", () => {
    expect(bestMatchSeries([], mk({}))).toBeNull();
  });
});

describe("matchConfidence", () => {
  const axial = (id: string): PrimaryPlane =>
    id.startsWith("ax") || id === "r" ? "axial" : "coronal";

  it("is not confident with no candidates", () => {
    expect(matchConfidence([], mk({ id: "r", modality: "CT" }))).toMatchObject({
      best: null,
      confident: false,
      reason: "empty",
    });
  });

  it("is not confident when nothing shares the reference modality (CT vs MR)", () => {
    const ref = mk({ id: "r", modality: "CT", series_description: "addome venosa" });
    const cand = [
      mk({
        id: "m1",
        modality: "MR",
        series_description: "addome venosa",
        received_instance_count: 200,
      }),
      mk({ id: "m2", modality: "MR", series_description: "addome", received_instance_count: 200 }),
    ];
    expect(matchConfidence(cand, ref)).toMatchObject({
      confident: false,
      reason: "no-modality-match",
    });
  });

  it("auto-selects the only comparable (same-modality) series", () => {
    const ref = mk({ id: "r", modality: "CT", series_description: "addome venosa" });
    const cand = [
      mk({ id: "ct", modality: "CT", series_description: "x", received_instance_count: 200 }),
      mk({
        id: "mr",
        modality: "MR",
        series_description: "addome venosa",
        received_instance_count: 200,
      }),
    ];
    expect(matchConfidence(cand, ref)).toMatchObject({
      confident: true,
      reason: "single",
      best: expect.objectContaining({ id: "ct" }),
    });
  });

  it("auto-selects a clear plane-margin winner", () => {
    const ref = mk({
      id: "r",
      modality: "CT",
      series_description: "addome venosa",
      received_instance_count: 250,
    });
    const cand = [
      mk({
        id: "ax",
        modality: "CT",
        series_description: "addome venosa",
        received_instance_count: 250,
      }),
      mk({
        id: "cor",
        modality: "CT",
        series_description: "addome venosa",
        received_instance_count: 250,
      }),
    ];
    // ax uniquely matches the reference plane (+25) -> margin >= 25 -> confident.
    expect(matchConfidence(cand, ref, axial)).toMatchObject({
      confident: true,
      reason: "clear-margin",
      best: expect.objectContaining({ id: "ax" }),
    });
  });

  it("asks (ambiguous) when comparable series differ only by contrast phase", () => {
    // Same plane, same region; only the phase phrasing differs -> a clinical
    // choice, not a guess. Margin (~one phase token) stays under the threshold.
    const ref = mk({
      id: "r",
      modality: "CT",
      series_description: "addome portale venosa",
      received_instance_count: 250,
    });
    const cand = [
      mk({
        id: "axpv",
        modality: "CT",
        series_description: "addome portale venosa",
        received_instance_count: 250,
      }),
      mk({
        id: "axart",
        modality: "CT",
        series_description: "addome arteriosa",
        received_instance_count: 250,
      }),
    ];
    const res = matchConfidence(cand, ref, axial);
    expect(res.confident).toBe(false);
    expect(res.reason).toBe("ambiguous");
    expect(res.best?.id).toBe("axpv"); // still the best guess, just not auto-loaded
  });
});

describe("seriesOptionLabel", () => {
  it("formats #num · MOD · plane · N img — description", () => {
    const s = mk({
      series_number: 4,
      modality: "ct",
      series_description: "TC addome venosa",
      received_instance_count: 273,
    });
    expect(seriesOptionLabel(s, "assiale")).toBe("#4 · CT · assiale · 273 img — TC addome venosa");
  });
  it("omits plane when unknown and description when absent", () => {
    const s = mk({ series_number: 1, modality: "SR", received_instance_count: 1 });
    expect(seriesOptionLabel(s, null)).toBe("#1 · SR · 1 img");
  });
});
