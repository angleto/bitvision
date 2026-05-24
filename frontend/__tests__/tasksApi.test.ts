// tasksApi REST client contract — URL composition + headers.
//
// Mirrors backend/src/bvphoenix/api/patient_tasks.py endpoints. The
// pattern is identical to the existing CareTimeline.test.tsx wire-
// level checks: stub global fetch, exercise each method, assert URL
// + headers. Done without DOM (node environment) so it can stay in
// the existing harness.

import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import { tasksApi } from "@/lib/api_records";

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
      // The Response constructor refuses a non-null body on 204/205/304
      // (null-body status, per the spec). Match the wire by passing a
      // null body for those.
      const bodyIsAllowed = status !== 204 && status !== 205 && status !== 304;
      const body = bodyIsAllowed ? JSON.stringify(nextResponse?.body ?? {}) : null;
      return new Response(body, {
        status,
        headers: { "content-type": "application/json" },
      });
    }),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("tasksApi.list", () => {
  test("base URL has no query string when no filters supplied", async () => {
    nextResponse = { status: 200, body: [] };
    await tasksApi.list("p-1");
    expect(calls[0].url).toContain("/api/patients/p-1/tasks");
    expect(calls[0].url).not.toContain("?");
  });

  test("statuses are encoded as repeated keys (FastAPI list[str] semantics)", async () => {
    nextResponse = { status: 200, body: [] };
    await tasksApi.list("p-1", { statuses: ["pending", "in_progress"] });
    expect(calls[0].url).toMatch(/statuses=pending/);
    expect(calls[0].url).toMatch(/statuses=in_progress/);
  });

  test("category + priority + date range survive the URL composition", async () => {
    nextResponse = { status: 200, body: [] };
    await tasksApi.list("p-1", {
      category: "admin",
      priority: "high",
      due_from: "2026-06-01T00:00:00Z",
      due_to: "2026-06-30T23:59:59Z",
    });
    expect(calls[0].url).toContain("category=admin");
    expect(calls[0].url).toContain("priority=high");
    expect(calls[0].url).toContain("due_from=");
    expect(calls[0].url).toContain("due_to=");
  });

  test("include_deleted=true surfaces tombstones", async () => {
    nextResponse = { status: 200, body: [] };
    await tasksApi.list("p-1", { include_deleted: true });
    expect(calls[0].url).toContain("include_deleted=true");
  });
});

describe("tasksApi mutations", () => {
  test("create sends Idempotency-Key header", async () => {
    nextResponse = { status: 201, body: { id: "t-1" } };
    await tasksApi.create({ patient_id: "p-1", title: "Test" }, "idem-abc");
    const headers = new Headers(calls[0].init.headers);
    expect(headers.get("Idempotency-Key")).toBe("idem-abc");
    expect(calls[0].init.method).toBe("POST");
    expect(calls[0].url).toContain("/api/patient-tasks");
  });

  test("update sends If-Match for optimistic-concurrency control", async () => {
    nextResponse = { status: 200, body: { id: "t-1" } };
    await tasksApi.update("t-1", { title: "Renamed" }, "etag-x");
    const headers = new Headers(calls[0].init.headers);
    expect(headers.get("If-Match")).toBe("etag-x");
    expect(calls[0].init.method).toBe("PATCH");
  });

  test("remove sends If-Match and uses DELETE method", async () => {
    nextResponse = { status: 204, body: {} };
    await tasksApi.remove("t-1", "etag-x");
    expect(calls[0].init.method).toBe("DELETE");
    expect(new Headers(calls[0].init.headers).get("If-Match")).toBe("etag-x");
  });

  test("restore POSTs to /restore with If-Match", async () => {
    nextResponse = { status: 200, body: { id: "t-1" } };
    await tasksApi.restore("t-1", "etag-x");
    expect(calls[0].url).toContain("/api/patient-tasks/t-1/restore");
    expect(calls[0].init.method).toBe("POST");
    expect(new Headers(calls[0].init.headers).get("If-Match")).toBe("etag-x");
  });
});

describe("tasksApi.transition", () => {
  test("snooze POSTs to /snooze with Idempotency-Key + If-Match + body", async () => {
    nextResponse = { status: 200, body: { id: "t-1" } };
    await tasksApi.transition(
      "t-1",
      "snooze",
      { snooze_until: "2026-06-15T09:00:00Z", reason: "later" },
      "etag-x",
      "idem-1",
    );
    expect(calls[0].url).toContain("/api/patient-tasks/t-1/snooze");
    expect(calls[0].init.method).toBe("POST");
    const headers = new Headers(calls[0].init.headers);
    expect(headers.get("If-Match")).toBe("etag-x");
    expect(headers.get("Idempotency-Key")).toBe("idem-1");
    const body = JSON.parse(calls[0].init.body as string);
    expect(body.snooze_until).toBe("2026-06-15T09:00:00Z");
    expect(body.reason).toBe("later");
  });

  test("start requires no body parameters (empty JSON {})", async () => {
    nextResponse = { status: 200, body: { id: "t-1" } };
    await tasksApi.transition("t-1", "start", {} as never, "etag-x", "idem-1");
    expect(calls[0].url).toContain("/api/patient-tasks/t-1/start");
    expect(calls[0].init.body).toBe("{}");
  });

  test("dry_run appends ?dry_run=true to the URL", async () => {
    nextResponse = { status: 200, body: { id: "t-1" } };
    await tasksApi.transition(
      "t-1",
      "complete",
      { completed_at: null, note: null },
      "etag-x",
      "idem-1",
      { dry_run: true },
    );
    expect(calls[0].url).toContain("/api/patient-tasks/t-1/complete?dry_run=true");
  });

  test("drop requires a reason in the body", async () => {
    nextResponse = { status: 200, body: { id: "t-1" } };
    await tasksApi.transition("t-1", "drop", { reason: "not needed anymore" }, "etag-x", "idem-1");
    const body = JSON.parse(calls[0].init.body as string);
    expect(body.reason).toBe("not needed anymore");
  });
});
