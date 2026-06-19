// Unit tests for the RECIST target-lesion persistence orchestration and the
// pure display/threshold helpers.

import { describe, expect, test, vi } from "vitest";

import {
  type RecistPersistenceApi,
  isBelowTargetThreshold,
  persistTargetTimepoint,
  readRecistBasis,
  recistRationale,
} from "@/lib/recist";

function mockApi(overrides: Partial<{ findingThrows: boolean }> = {}): {
  api: RecistPersistenceApi;
  markerCreate: ReturnType<typeof vi.fn>;
  markerRemove: ReturnType<typeof vi.fn>;
  findingCreate: ReturnType<typeof vi.fn>;
} {
  const markerCreate = vi.fn(async (_p: string, _input: unknown) => ({ id: "marker-1" }));
  const markerRemove = vi.fn(async (_id: string) => undefined);
  const findingCreate = vi.fn(async (_p: string, _input: unknown, _opts?: unknown) => {
    if (overrides.findingThrows) throw new Error("422 finding rejected");
    return { id: "finding-1" };
  });
  const api = {
    markers: { create: markerCreate, remove: markerRemove },
    findings: { create: findingCreate },
  } as unknown as RecistPersistenceApi;
  return { api, markerCreate, markerRemove, findingCreate };
}

describe("persistTargetTimepoint", () => {
  test("creates a bidirectional marker then a measured, typed finding linked to it", async () => {
    const { api, markerCreate, findingCreate } = mockApi();
    const res = await persistTargetTimepoint(api, {
      patientId: "p1",
      studyId: "s-base",
      seriesId: "ser-1",
      frameOfReferenceUID: "FOR-1",
      measurement: {
        longAxisMm: 24.0,
        shortAxisMm: 16.0,
        worldPoints: [
          [0, 0, 0],
          [24, 0, 0],
          [12, -8, 0],
          [12, 8, 0],
        ],
      },
      lesionType: "lymph_node",
      anatomy: "mediastinum",
      idempotencyKey: "idem-1",
    });

    expect(res).toEqual({ markerId: "marker-1", findingId: "finding-1" });

    // Marker carries both axes + world geometry.
    const markerInput = markerCreate.mock.calls[0][1] as Record<string, unknown>;
    const computed = markerInput.computed as Record<string, unknown>;
    const geometry = markerInput.geometry as Record<string, unknown>;
    expect(markerInput.kind).toBe("measurement.distance");
    expect(markerInput.target_id).toBe("s-base");
    expect(computed.long_axis_mm).toBe(24.0);
    expect(computed.short_axis_mm).toBe(16.0);
    expect(geometry.world_points).toHaveLength(4);

    // Finding carries the measurements, the node type, and the marker link.
    const findingInput = findingCreate.mock.calls[0][1] as Record<string, unknown>;
    const opts = findingCreate.mock.calls[0][2] as Record<string, unknown>;
    expect(findingInput.type).toBe("lymph_node");
    expect(findingInput.longest_diameter_mm).toBe(24.0);
    expect(findingInput.short_axis_mm).toBe(16.0);
    expect(findingInput.status).toBe("confirmed");
    expect(findingInput.geometry_refs).toEqual([{ marker_id: "marker-1", role: "measurement" }]);
    expect(opts.idempotencyKey).toBe("idem-1");
  });

  test("rolls back the orphan marker when the finding create fails", async () => {
    const { api, markerRemove, findingCreate } = mockApi({ findingThrows: true });
    await expect(
      persistTargetTimepoint(api, {
        patientId: "p1",
        studyId: "s-base",
        measurement: { longAxisMm: 30, shortAxisMm: 20 },
        lesionType: "lesion",
      }),
    ).rejects.toThrow("422 finding rejected");
    expect(findingCreate).toHaveBeenCalledOnce();
    expect(markerRemove).toHaveBeenCalledWith("marker-1");
  });
});

describe("readRecistBasis", () => {
  test("parses lesions, caps and ne_reason defensively", () => {
    const b = readRecistBasis({
      n_target_lesions: 2,
      ne_reason: null,
      has_baseline: true,
      has_current: true,
      caps: {
        n_targets: 2,
        max_targets: 5,
        over_limit: false,
        per_organ: { liver: 2 },
        per_organ_over_limit: [],
      },
      lesions: [
        {
          track_id: "t1",
          label: "RUL",
          baseline_mm: 50,
          current_mm: 35,
          delta_mm: -15,
          is_nodal: false,
          anatomy: "lung",
        },
        {
          track_id: "t2",
          label: "Node",
          baseline_mm: 16,
          current_mm: null,
          delta_mm: null,
          is_nodal: true,
          anatomy: null,
        },
      ],
    });
    expect(b.n_target_lesions).toBe(2);
    expect(b.lesions[0].delta_mm).toBe(-15);
    expect(b.lesions[1].is_nodal).toBe(true);
    expect(b.lesions[1].current_mm).toBeNull();
    expect(b.caps?.per_organ.liver).toBe(2);
  });

  test("tolerates a null/empty basis", () => {
    const b = readRecistBasis(null);
    expect(b.n_target_lesions).toBe(0);
    expect(b.lesions).toEqual([]);
    expect(b.caps).toBeNull();
  });
});

describe("recistRationale", () => {
  test("PR/SD report the percent vs baseline", () => {
    const ctx = {
      targetSumMm: 55,
      baselineSumMm: 80,
      nadirSumMm: 55,
      pctChange: -31.25,
      newLesions: false,
    };
    expect(recistRationale("PR", ctx)).toEqual({ key: "PR", pct: -31.25 });
    expect(recistRationale("SD", { ...ctx, pctChange: -10 })).toEqual({ key: "SD", pct: -10 });
  });

  test("PD reports percent vs nadir, or a new-lesion key", () => {
    const grew = recistRationale("PD", {
      targetSumMm: 60,
      baselineSumMm: 100,
      nadirSumMm: 50,
      pctChange: -40,
      newLesions: false,
    });
    expect(grew.key).toBe("PD");
    expect(grew.pct).toBeCloseTo(20, 5);

    expect(
      recistRationale("PD", {
        targetSumMm: 30,
        baselineSumMm: 50,
        nadirSumMm: 30,
        pctChange: -40,
        newLesions: true,
      }),
    ).toEqual({ key: "PD_new", pct: null });
  });
});

describe("isBelowTargetThreshold", () => {
  test("parenchymal lesion needs long axis >= 10 mm", () => {
    expect(isBelowTargetThreshold("lesion", 12, 6)).toBe(false);
    expect(isBelowTargetThreshold("lesion", 8, 6)).toBe(true);
    expect(isBelowTargetThreshold("lesion", undefined, undefined)).toBe(true);
  });

  test("lymph node needs short axis >= 15 mm", () => {
    expect(isBelowTargetThreshold("lymph_node", 30, 16)).toBe(false);
    expect(isBelowTargetThreshold("lymph_node", 30, 12)).toBe(true);
  });
});
