// CarePhaseEditor — drag-drop endpoint wiring.
//
// As with the CareTimeline tests, the rendering layer is not exercised
// here (no jsdom in this package). What this suite does test is the
// CONTRACT the editor relies on: when the user drops an event onto a
// phase chip, the right REST endpoint is hit; when the user reorders
// phase chips, the reorder endpoint is hit with the right ordinals
// payload. The editor's drop handler is a thin wrapper around these
// calls — keeping the contract tested means a regression in the
// client surfaces here, not silently in the browser.

import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import { type ReorderItem, carePhasesApi } from "@/lib/api_records";

import { CANARY_PATIENT_ID, PHASE_IDS, buildCanaryTimeline } from "./_fixtures";

interface FetchCall {
  url: string;
  init: RequestInit;
}

let calls: FetchCall[];

beforeEach(() => {
  calls = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init: RequestInit = {}) => {
      calls.push({ url, init });
      return new Response(JSON.stringify({}), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("Drag-drop event reassign", () => {
  test("dropping an event onto a phase invokes PUT assignEvent", async () => {
    const tl = buildCanaryTimeline();
    const sourcePhase = tl.phases[0];
    const targetPhase = tl.phases[1];
    const evtId = sourcePhase.events[0].id;

    await carePhasesApi.assignEvent(CANARY_PATIENT_ID, targetPhase.id, evtId, {
      confidence: 1.0,
    });

    expect(calls).toHaveLength(1);
    expect(calls[0].url).toContain(
      `/api/patients/${CANARY_PATIENT_ID}/care-phases/${targetPhase.id}/events/${evtId}`,
    );
    expect(calls[0].init.method).toBe("PUT");
    const body = JSON.parse(calls[0].init.body as string);
    expect(body.confidence).toBe(1.0);
  });

  test("removing the trash button invokes DELETE unassign", async () => {
    const tl = buildCanaryTimeline();
    const phase = tl.phases[1];
    const evtId = phase.events[0].id;

    await carePhasesApi.unassignEvent(CANARY_PATIENT_ID, phase.id, evtId);

    expect(calls[0].url).toContain(
      `/api/patients/${CANARY_PATIENT_ID}/care-phases/${phase.id}/events/${evtId}`,
    );
    expect(calls[0].init.method).toBe("DELETE");
  });
});

describe("Drag-drop phase reorder", () => {
  test("moving phase 0 to position 2 sends matching ordinals batch", async () => {
    const tl = buildCanaryTimeline();
    const ordered = [...tl.phases].sort((a, b) => a.ordinal - b.ordinal);
    const [moved] = ordered.splice(0, 1);
    ordered.splice(2, 0, moved);
    const ordinals: ReorderItem[] = ordered.map((p, i) => ({
      phase_id: p.id,
      ordinal: i,
    }));

    await carePhasesApi.reorder(CANARY_PATIENT_ID, { ordinals });

    expect(calls[0].url).toContain(`/api/patients/${CANARY_PATIENT_ID}/care-phases:reorder`);
    expect(calls[0].init.method).toBe("POST");
    const body = JSON.parse(calls[0].init.body as string);
    expect(body.ordinals).toHaveLength(7);
    // The phase that was at ordinal 0 is now at ordinal 2.
    const movedEntry = (body.ordinals as ReorderItem[]).find((o) => o.phase_id === PHASE_IDS[0]);
    expect(movedEntry?.ordinal).toBe(2);
  });
});

describe("Revisions", () => {
  test("restore endpoint takes revision_no in body", async () => {
    await carePhasesApi.restoreRevision(CANARY_PATIENT_ID, PHASE_IDS[0], 3);
    expect(calls[0].url).toContain(
      `/api/patients/${CANARY_PATIENT_ID}/care-phases/${PHASE_IDS[0]}/restore`,
    );
    const body = JSON.parse(calls[0].init.body as string);
    expect(body.revision_no).toBe(3);
  });
});
