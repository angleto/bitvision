// transparencyApi REST client contract — which endpoint each method
// hits. Mirrors backend/src/bvphoenix/api/transparency.py: the public
// slice vs the admin superset are two distinct URLs, and the client
// must route to the right one. Wire-level check (stub global fetch,
// assert URL + method), no DOM, so it stays in the node harness.

import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import { transparencyApi } from "@/lib/api";

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
      const status = nextResponse?.status ?? 200;
      return new Response(JSON.stringify(nextResponse?.body ?? {}), {
        status,
        headers: { "content-type": "application/json" },
      });
    }),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("transparencyApi", () => {
  test("get() hits the public endpoint", async () => {
    nextResponse = {
      status: 200,
      body: { generated_at: "x", version: "1", studies: {}, governance: {} },
    };
    await transparencyApi.get();
    expect(calls[0].url).toContain("/api/transparency");
    // Must be the public slice, not the admin one.
    expect(calls[0].url).not.toContain("/api/transparency/admin");
    expect((calls[0].init.method ?? "GET").toUpperCase()).toBe("GET");
  });

  test("getAdmin() hits the admin endpoint", async () => {
    nextResponse = {
      status: 200,
      body: {
        generated_at: "x",
        version: "1",
        studies: {},
        governance: {},
        users: { total: 0 },
        sharing: {},
        llm: {},
      },
    };
    await transparencyApi.getAdmin();
    expect(calls[0].url).toContain("/api/transparency/admin");
    expect((calls[0].init.method ?? "GET").toUpperCase()).toBe("GET");
  });
});
