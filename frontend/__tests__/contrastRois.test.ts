import {
  type Group,
  type Meas,
  type Vec3,
  circleRadiusMm,
  newestFreshCircle,
  ownMeasurements,
  paneForUid,
  phaseRoisFromGroup,
  propagateZWorld,
  samePoints,
} from "@/lib/contrastRois";
import { describe, expect, it } from "vitest";

// Four phases of the real study, each a distinct synthetic FrameOfReference.
const BASALE = "s-basale";
const ARTERIOSA = "s-arteriosa";
const VENOSA = "s-venosa";
const TARDIVA = "s-tardiva";
const PANES = [
  { series_id: BASALE },
  { series_id: ARTERIOSA },
  { series_id: VENOSA },
  { series_id: TARDIVA },
];

function circle(markerId: string, seriesId: string, center: Vec3, edge: Vec3): Meas {
  return {
    markerId,
    csToolName: "CircleROI",
    worldPoints: [center, edge],
    frameOfReferenceUID: paneForUid(seriesId),
  };
}

describe("ownMeasurements — per-phase attribution", () => {
  it("a box drawn on the delayed phase is NOT attributed to the unenhanced one", () => {
    // Cornerstone fires on the GLOBAL event target, so every pane's callback
    // gets the full stream. The box lives on the delayed (tardiva) pane.
    const stream: Meas[] = [circle("m-tardiva", TARDIVA, [10, 10, -300], [13, 10, -300])];

    // The unenhanced (basale) pane must see NONE of it — the exact bug the
    // radiologist hit ("drawn on tardiva, counted on basale").
    expect(ownMeasurements(stream, BASALE)).toHaveLength(0);
    // The delayed pane owns it.
    const own = ownMeasurements(stream, TARDIVA);
    expect(own).toHaveLength(1);
    expect(own[0].markerId).toBe("m-tardiva");
  });

  it("splits a mixed global stream to the right panes", () => {
    const stream: Meas[] = [
      circle("m-b", BASALE, [5, 5, -300], [8, 5, -300]),
      circle("m-v", VENOSA, [5, 5, -260], [8, 5, -260]),
      circle("m-t", TARDIVA, [5, 5, -280], [8, 5, -280]),
    ];
    expect(ownMeasurements(stream, BASALE).map((m) => m.markerId)).toEqual(["m-b"]);
    expect(ownMeasurements(stream, ARTERIOSA)).toHaveLength(0);
    expect(ownMeasurements(stream, VENOSA).map((m) => m.markerId)).toEqual(["m-v"]);
    expect(ownMeasurements(stream, TARDIVA).map((m) => m.markerId)).toEqual(["m-t"]);
  });

  it("ignores annotations with no / foreign frame of reference", () => {
    const stream: Meas[] = [
      {
        markerId: "x",
        csToolName: "CircleROI",
        worldPoints: [
          [0, 0, 0],
          [1, 0, 0],
        ],
      },
      { ...circle("y", "other-study", [0, 0, 0], [1, 0, 0]) },
    ];
    expect(ownMeasurements(stream, BASALE)).toHaveLength(0);
  });
});

describe("newestFreshCircle", () => {
  it("returns the newest unseen circle on this pane", () => {
    const own: Meas[] = [
      circle("old", BASALE, [0, 0, 0], [2, 0, 0]),
      circle("new", BASALE, [1, 1, 0], [4, 1, 0]),
    ];
    expect(newestFreshCircle(own, new Set(["old"]))?.markerId).toBe("new");
  });

  it("skips already-seen markers (no re-capture of a placed box)", () => {
    const own: Meas[] = [circle("placed", BASALE, [0, 0, 0], [2, 0, 0])];
    expect(newestFreshCircle(own, new Set(["placed"]))).toBeNull();
  });

  it("ignores non-circle and zero-point measurements", () => {
    const own: Meas[] = [
      {
        markerId: "len",
        csToolName: "Length",
        worldPoints: [
          [0, 0, 0],
          [1, 0, 0],
        ],
      },
      { markerId: "empty", csToolName: "CircleROI", worldPoints: [[0, 0, 0]] },
    ];
    expect(newestFreshCircle(own, new Set())).toBeNull();
  });
});

describe("propagateZWorld — same-slice copy across phases", () => {
  it("lands the copy on the target's DISPLAYED slice (crossZ) so it renders", () => {
    // The target pane shows slice z=-240 (its crosshair). The copy must sit on
    // that exact plane to draw — a fractional index-mapped Z would fall between
    // slices and the CircleROI would be invisible (the copy-to-all bug).
    const src = { originZ: -660, spacingZ: 2.5, crossZ: -300 };
    const tgt = { originZ: -600, spacingZ: 2.5, crossZ: -240 };
    const wp: Vec3[] = [
      [10, 20, -300],
      [13, 20, -300],
    ];
    const out = propagateZWorld(src, tgt, wp);
    expect(out[0][0]).toBe(10);
    expect(out[0][1]).toBe(20);
    expect(out[0][2]).toBe(-240); // the displayed slice, not a between-slices Z
    expect(out[1][2]).toBe(-240);
    expect(circleRadiusMm(out)).toBeCloseTo(3, 6);
  });

  it("falls back to the index-mapped Z when the target crosshair is unknown", () => {
    // No crossZ -> origin/spacing map: slice k=144 in the source maps to
    // world z = −600 + 144*2.5 = −240 in the target.
    const out = propagateZWorld(
      { originZ: -660, spacingZ: 2.5 },
      { originZ: -600, spacingZ: 2.5 },
      [
        [10, 20, -300],
        [13, 20, -300],
      ],
    );
    expect(out[0][2]).toBeCloseTo(-240, 6);
  });

  it("falls back to the drawn Z when nothing about the target is known", () => {
    const out = propagateZWorld({}, {}, [
      [1, 2, -300],
      [4, 2, -300],
    ]);
    expect(out[0][2]).toBe(-300);
  });
});

describe("phaseRoisFromGroup — wash-out reads each phase's own box", () => {
  it("emits one ROI per phase that has a box, at its own position", () => {
    const group: Group = {
      0: {
        markerId: "m0",
        worldPoints: [
          [5, 5, -300],
          [8, 5, -300],
        ],
      },
      2: {
        markerId: "m2",
        worldPoints: [
          [5, 5, -260],
          [8, 5, -260],
        ],
      },
    };
    const rois = phaseRoisFromGroup(group, PANES, {});
    expect(rois.map((r) => r.series_id)).toEqual([BASALE, VENOSA]);
    expect(rois[0].center_lps).toEqual([5, 5, -300]);
    expect(rois[1].center_lps).toEqual([5, 5, -260]);
    expect(rois[0].radius_mm).toBeCloseTo(3, 6);
  });

  it("prefers the LIVE dragged position over the stored one", () => {
    const group: Group = {
      0: {
        markerId: "m0",
        worldPoints: [
          [5, 5, -300],
          [8, 5, -300],
        ],
      },
    };
    // Operator dragged the box: live stream has new points for the same marker.
    const live = { 0: [circle("m0", BASALE, [7, 7, -300], [12, 7, -300])] };
    const rois = phaseRoisFromGroup(group, PANES, live);
    expect(rois[0].center_lps).toEqual([7, 7, -300]);
    expect(rois[0].radius_mm).toBeCloseTo(5, 6);
  });
});

describe("samePoints", () => {
  it("detects an unchanged box vs a dragged one", () => {
    const a: Vec3[] = [
      [1, 1, 1],
      [2, 2, 2],
    ];
    expect(
      samePoints(a, [
        [1, 1, 1],
        [2, 2, 2],
      ]),
    ).toBe(true);
    expect(
      samePoints(a, [
        [1.5, 1, 1],
        [2, 2, 2],
      ]),
    ).toBe(false);
  });
});
