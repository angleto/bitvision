// Unit tests for the deterministic fusion volume ordering.
//
// Pins the regression: opening a PET as ``primary`` and adding a
// CT as ``fusion`` used to put the CT at pos[1] (with the PET
// colormap callback applied), inverting the visual z-order until
// the user reloaded. ``resolveFusionOrder`` swaps in that case so
// the anatomical volume is always the base.

import { describe, expect, test } from "vitest";

import { isFunctionalModality, resolveFusionOrder } from "@/lib/fusionVolumeOrder";

describe("isFunctionalModality", () => {
  test("returns true for PT and NM (case-insensitive)", () => {
    expect(isFunctionalModality("PT")).toBe(true);
    expect(isFunctionalModality("pt")).toBe(true);
    expect(isFunctionalModality("NM")).toBe(true);
    expect(isFunctionalModality("nm")).toBe(true);
  });

  test("returns false for anatomical modalities", () => {
    for (const m of ["CT", "MR", "CR", "DX", "XA", "US", "OT"]) {
      expect(isFunctionalModality(m)).toBe(false);
    }
  });

  test("treats null/undefined/empty as anatomical (safer default)", () => {
    expect(isFunctionalModality(null)).toBe(false);
    expect(isFunctionalModality(undefined)).toBe(false);
    expect(isFunctionalModality("")).toBe(false);
  });
});

describe("resolveFusionOrder", () => {
  const ct = { volumeId: "vol-ct", modality: "CT" };
  const pt = { volumeId: "vol-pt", modality: "PT" };
  const mr = { volumeId: "vol-mr", modality: "MR" };

  test("no fusion → primary is the only volume, no overlay", () => {
    const r = resolveFusionOrder(ct, null);
    expect(r.baseVolumeId).toBe("vol-ct");
    expect(r.overlayVolumeId).toBeNull();
    expect(r.swapped).toBe(false);
  });

  test("canonical CT primary + PT fusion → no swap", () => {
    const r = resolveFusionOrder(ct, pt);
    expect(r.baseVolumeId).toBe("vol-ct");
    expect(r.overlayVolumeId).toBe("vol-pt");
    expect(r.swapped).toBe(false);
  });

  test("PT primary + CT fusion → SWAP (regression of the original bug)", () => {
    // The user opened the PET first and added the CT as fusion. The
    // resolver must put the CT as the base so the colormap callback
    // applies to the PT (which is now at pos[1]).
    const r = resolveFusionOrder(pt, ct);
    expect(r.baseVolumeId).toBe("vol-ct");
    expect(r.overlayVolumeId).toBe("vol-pt");
    expect(r.swapped).toBe(true);
  });

  test("PT primary + MR fusion → SWAP (MR is anatomical)", () => {
    const r = resolveFusionOrder(pt, mr);
    expect(r.baseVolumeId).toBe("vol-mr");
    expect(r.overlayVolumeId).toBe("vol-pt");
    expect(r.swapped).toBe(true);
  });

  test("two anatomical (CT + MR) → keep user-chosen order", () => {
    const r = resolveFusionOrder(ct, mr);
    expect(r.baseVolumeId).toBe("vol-ct");
    expect(r.overlayVolumeId).toBe("vol-mr");
    expect(r.swapped).toBe(false);
  });

  test("two functional (PT + NM) → keep user-chosen order", () => {
    const nm = { volumeId: "vol-nm", modality: "NM" };
    const r = resolveFusionOrder(pt, nm);
    expect(r.baseVolumeId).toBe("vol-pt");
    expect(r.overlayVolumeId).toBe("vol-nm");
    expect(r.swapped).toBe(false);
  });

  test("missing modality on primary → treated as anatomical", () => {
    // The primary side often resolves modality before fusion does
    // (the user picks the primary in the study list, which carries
    // the modality already; the fusion arrives later via a separate
    // fetch). Treating ``null`` as anatomical means the canonical
    // CT-primary case still works during the brief window when the
    // primary's modality string hasn't propagated yet.
    const primary = { volumeId: "vol-x", modality: null };
    const r = resolveFusionOrder(primary, pt);
    expect(r.baseVolumeId).toBe("vol-x");
    expect(r.overlayVolumeId).toBe("vol-pt");
    expect(r.swapped).toBe(false);
  });

  test("case-insensitive modality match", () => {
    const lower = { volumeId: "vol-pt-lower", modality: "pt" };
    const r = resolveFusionOrder(lower, ct);
    expect(r.swapped).toBe(true);
    expect(r.baseVolumeId).toBe("vol-ct");
    expect(r.overlayVolumeId).toBe("vol-pt-lower");
  });
});
