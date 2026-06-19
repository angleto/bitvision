import type { SeriesPhase } from "@/lib/api";
import {
  defaultPhasePanes,
  isReviewablePhase,
  nonReviewableSeries,
  reviewableSeries,
} from "@/lib/contrastPhases";
import { describe, expect, it } from "vitest";

// Build a minimal SeriesPhase. ``is_reviewable_phase`` is intentionally left
// undefined in most cases to exercise the FE heuristic fallback (older
// backend); pass it explicitly to test trusting the backend flag.
function sp(p: Partial<SeriesPhase> & { series_id: string }): SeriesPhase {
  return {
    series_id: p.series_id,
    series_number: p.series_number ?? null,
    modality: p.modality ?? "CT",
    series_description: p.series_description ?? null,
    body_part_examined: p.body_part_examined ?? "CHEST",
    acquisition_phase: p.acquisition_phase ?? null,
    phase_confidence: p.phase_confidence ?? null,
    phase_source: p.phase_source ?? null,
    needs_confirmation: p.needs_confirmation ?? false,
    acquisition_time_of_day: p.acquisition_time_of_day ?? null,
    contrast_bolus_agent: p.contrast_bolus_agent ?? null,
    frame_of_reference_uid: p.frame_of_reference_uid ?? null,
    instance_count: p.instance_count ?? 200,
    series_plane: p.series_plane,
    is_reviewable_phase: p.is_reviewable_phase,
  };
}

// The real 70ce04b1 study that exposed the bug: 2 axial phases buried in
// scouts, reformats, a recon kernel, a prep loop and dose reports.
function realStudy(): SeriesPhase[] {
  return [
    sp({ series_id: "scout", series_number: 1, series_description: "Scout", instance_count: 2 }),
    sp({
      series_id: "basale",
      series_number: 2,
      series_description: "Basale",
      instance_count: 258,
      series_plane: "axial",
      acquisition_phase: "unenhanced",
      is_reviewable_phase: true,
    }),
    sp({
      series_id: "polmone",
      series_number: 3,
      series_description: "Polmone 1.25",
      instance_count: 277,
      series_plane: "axial",
      is_reviewable_phase: true,
    }),
    sp({
      series_id: "tardiva",
      series_number: 9,
      series_description: "tardiva dopo portale",
      instance_count: 195,
      series_plane: "axial",
      acquisition_phase: "delayed",
      is_reviewable_phase: true,
    }),
    sp({
      series_id: "screen",
      series_number: 99,
      series_description: "Screen Save",
      instance_count: 1,
      is_reviewable_phase: false,
    }),
    sp({
      series_id: "prep",
      series_number: 200,
      series_description: "Serie Prep Smart",
      instance_count: 6,
      is_reviewable_phase: false,
    }),
    sp({
      series_id: "sag",
      series_number: 300,
      series_description: "SAG",
      instance_count: 152,
      series_plane: "sagittal",
      is_reviewable_phase: false,
    }),
    sp({
      series_id: "cor",
      series_number: 301,
      series_description: "COR",
      instance_count: 148,
      series_plane: "sagittal",
      is_reviewable_phase: false,
    }),
    sp({
      series_id: "dose",
      series_number: 999,
      series_description: "Rapporto dose",
      instance_count: 1,
      is_reviewable_phase: false,
    }),
  ];
}

describe("contrastPhases", () => {
  it("opens exactly the two acquired phases, not six", () => {
    const panes = defaultPhasePanes(realStudy());
    expect(panes.map((p) => p.series_id)).toEqual(["basale", "tardiva"]);
  });

  it("orders panes by clinical phase (unenhanced before delayed)", () => {
    const panes = defaultPhasePanes(realStudy());
    expect(panes.map((p) => p.acquisition_phase)).toEqual(["unenhanced", "delayed"]);
  });

  it("picker offers reviewable axial volumes only, hides clutter", () => {
    const study = realStudy();
    const ok = reviewableSeries(study).map((p) => p.series_id);
    expect(ok.sort()).toEqual(["basale", "polmone", "tardiva"]);
    const hidden = nonReviewableSeries(study).map((p) => p.series_id);
    expect(hidden.sort()).toEqual(["cor", "dose", "prep", "sag", "scout", "screen"]);
  });

  it("trusts the backend is_reviewable_phase flag when present", () => {
    expect(isReviewablePhase(sp({ series_id: "x", is_reviewable_phase: false }))).toBe(false);
    expect(isReviewablePhase(sp({ series_id: "x", is_reviewable_phase: true }))).toBe(true);
  });

  it("falls back to the heuristic when the flag is absent (older backend)", () => {
    // No flag, no geometry: description + size carry it.
    expect(
      isReviewablePhase(sp({ series_id: "x", series_description: "Basale", instance_count: 200 })),
    ).toBe(true);
    expect(
      isReviewablePhase(sp({ series_id: "x", series_description: "SAG", instance_count: 200 })),
    ).toBe(false);
    expect(
      isReviewablePhase(sp({ series_id: "x", series_description: "Scout", instance_count: 3 })),
    ).toBe(false);
    // Geometry wins over a stray "SAG" token in an axial series.
    expect(
      isReviewablePhase(
        sp({ series_id: "x", series_description: "SAG recon", series_plane: "axial" }),
      ),
    ).toBe(true);
  });

  it("excludes a stale-labelled reformat from the default panes", () => {
    // A coronal reformat that somehow carries a portal label must not open.
    const study = [
      sp({
        series_id: "ax",
        series_description: "Portale",
        acquisition_phase: "portal_venous",
        series_plane: "axial",
        is_reviewable_phase: true,
      }),
      sp({
        series_id: "cor",
        series_description: "Portale COR",
        acquisition_phase: "portal_venous",
        series_plane: "coronal",
        is_reviewable_phase: false,
      }),
    ];
    expect(defaultPhasePanes(study).map((p) => p.series_id)).toEqual(["ax"]);
  });
});
