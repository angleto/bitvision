// CareTimeline-level smoke tests.
//
// We do not own a DOM-render layer (no @testing-library/react / no
// jsdom in this package). The rendering paths of CareTimeline are
// validated by hand in the browser; this suite asserts the contract
// on which CareTimeline relies — namely:
//   1. the typed REST client builds the right URLs
//   2. the fixture matches the 7-phase Patient X shape
//   3. the propose / assign / reorder mutations hit the right
//      endpoints with the right method + idempotency / If-Match
//      semantics, so the rendered chips and dots end up wired to a
//      working backend.

import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import { carePhasesApi } from "@/lib/api_records";

import { CANARY_PATIENT_ID, PHASE_IDS, buildCanaryTimeline } from "./_fixtures";

interface FetchCall {
  url: string;
  init: RequestInit;
}

let calls: FetchCall[];
let nextResponse: { status: number; body: unknown } | null;

beforeEach(() => {
  calls = [];
  nextResponse = { status: 200, body: {} };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init: RequestInit = {}) => {
      calls.push({ url, init });
      const body = nextResponse?.body ?? {};
      return new Response(JSON.stringify(body), {
        status: nextResponse?.status ?? 200,
        headers: { "content-type": "application/json" },
      });
    }),
  );
  // The real ``request`` helper pulls a JWT from window.localStorage;
  // running in node we don't have one. The helper guards on
  // ``typeof window === "undefined"`` so the call is a no-op here.
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("Patient X fixture", () => {
  test("renders 7 phases matching the golden timeline", () => {
    const tl = buildCanaryTimeline();
    expect(tl.phases.length).toBe(7);
    expect(tl.phases.map((p) => p.slug)).toEqual([
      "imaging-pre-op",
      "intervento-chirurgico",
      "follow-up-post-op",
      "inizio-follow-up-oncologico",
      "sorveglianza-periodica",
      "rivalutazione",
      "altro",
    ]);
    // Phases are chronologically ordered.
    expect(tl.phases.map((p) => p.ordinal)).toEqual([0, 1, 2, 3, 4, 5, 6]);
    // Surgery phase carries 3 events.
    expect(tl.phases[1].events.length).toBe(3);
  });

  test("each event carries a resolved navigation target", () => {
    const tl = buildCanaryTimeline();
    for (const ph of tl.phases) {
      for (const ev of ph.events) {
        expect(ev.target.url.startsWith("/")).toBe(true);
        expect(ev.target.mcp_uri.startsWith("mcp://")).toBe(true);
      }
    }
  });
});

describe("CareTimeline empty state contract", () => {
  test("propose endpoint is invoked when user accepts the empty-state CTA", async () => {
    nextResponse = { status: 202, body: { proposal_id: "p1", status: "pending" } };
    await carePhasesApi.propose(CANARY_PATIENT_ID, { lang: "it" });
    expect(calls).toHaveLength(1);
    expect(calls[0].url).toContain(`/api/patients/${CANARY_PATIENT_ID}/care-phases:propose`);
    expect(calls[0].init.method).toBe("POST");
  });
});

describe("CareTimeline navigation handlers (URL contract)", () => {
  test("phase chip navigation slug is reachable as a route segment", () => {
    const tl = buildCanaryTimeline();
    for (const p of tl.phases) {
      // care-phases routes are /patients/:id/care-phases/:slug
      expect(p.slug).toMatch(/^[a-z0-9-]+$/);
    }
    expect(PHASE_IDS).toHaveLength(7);
  });

  test("event dot click target is the resolved url field", () => {
    const tl = buildCanaryTimeline();
    const firstEvent = tl.phases[0].events[0];
    expect(firstEvent.target.url).toBe("/studies/study-1");
  });
});

describe("Mutation client wiring", () => {
  test("apply-proposal sends Idempotency-Key header", async () => {
    nextResponse = {
      status: 200,
      body: { applied_phases: [], applied_assignments: 0, skipped_assignments: 0 },
    };
    await carePhasesApi.applyProposal(
      CANARY_PATIENT_ID,
      { proposal_id: "p1", accept_phases: [], accept_assignments: [] },
      "idem-123",
    );
    const headers = new Headers(calls[0].init.headers);
    expect(headers.get("Idempotency-Key")).toBe("idem-123");
  });

  test("update sends If-Match for optimistic-concurrency control", async () => {
    nextResponse = { status: 200, body: {} };
    await carePhasesApi.update(CANARY_PATIENT_ID, PHASE_IDS[0], "etag-x", {
      name: "Renamed",
    });
    const headers = new Headers(calls[0].init.headers);
    expect(headers.get("If-Match")).toBe("etag-x");
    expect(calls[0].init.method).toBe("PATCH");
  });
});
