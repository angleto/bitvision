// Unit tests for the per-resource job binding helpers in lib/jobs.ts.
//
// These pin the cross-session recovery contract: a study card that
// enqueues an export and then survives a tab reload / logout / fresh
// session must rebind to the same Job id when ``findByScope`` is
// called with the matching ``(scope_kind, scope_id)`` tuple. Without
// this localStorage hint the FE has no way to reconnect because the
// ``GET /api/jobs`` list does not surface scope.

import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import { jobsStorage } from "@/lib/jobs";

class _MemStorage {
  private store = new Map<string, string>();
  getItem(k: string): string | null {
    return this.store.has(k) ? (this.store.get(k) as string) : null;
  }
  setItem(k: string, v: string): void {
    this.store.set(k, v);
  }
  removeItem(k: string): void {
    this.store.delete(k);
  }
  clear(): void {
    this.store.clear();
  }
  key(_i: number): string | null {
    return null;
  }
  get length(): number {
    return this.store.size;
  }
}

beforeEach(() => {
  vi.stubGlobal("window", { localStorage: new _MemStorage() });
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("jobsStorage scope binding", () => {
  test("findByScope returns null when nothing is tracked", () => {
    expect(jobsStorage.findByScope("study", "abc")).toBeNull();
  });

  test("add stores scope and findByScope resolves the most recent match", () => {
    jobsStorage.add({
      id: "job-1",
      kind: "study_export",
      scope_kind: "study",
      scope_id: "study-A",
    });
    jobsStorage.add({
      id: "job-2",
      kind: "study_export",
      scope_kind: "study",
      scope_id: "study-B",
    });
    expect(jobsStorage.findByScope("study", "study-A")?.id).toBe("job-1");
    expect(jobsStorage.findByScope("study", "study-B")?.id).toBe("job-2");
    // Unrelated scopes don't leak across resources.
    expect(jobsStorage.findByScope("document", "study-A")).toBeNull();
  });

  test("findByScope picks the newest entry on duplicate scope", () => {
    // ``add`` prepends, so the most-recently-enqueued job for a given
    // scope wins — required when a user retries after a failure
    // without explicitly removing the failed entry.
    jobsStorage.add({
      id: "old-job",
      kind: "study_export",
      scope_kind: "study",
      scope_id: "study-A",
    });
    jobsStorage.add({
      id: "new-job",
      kind: "study_export",
      scope_kind: "study",
      scope_id: "study-A",
    });
    expect(jobsStorage.findByScope("study", "study-A")?.id).toBe("new-job");
  });

  test("remove drops the entry and findByScope falls back", () => {
    jobsStorage.add({
      id: "job-1",
      kind: "study_export",
      scope_kind: "study",
      scope_id: "study-A",
    });
    jobsStorage.remove("job-1");
    expect(jobsStorage.findByScope("study", "study-A")).toBeNull();
  });

  test("entries without scope_kind/scope_id are ignored by findByScope", () => {
    // Backwards-compatible: legacy localStorage rows (pre-scope) must
    // not match an arbitrary scope_kind/id pair.
    jobsStorage.add({ id: "legacy", kind: "fascicolo_export" });
    expect(jobsStorage.findByScope("study", "anything")).toBeNull();
  });

  test("scope survives a JSON round-trip", () => {
    // The localStorage write/read goes through JSON.stringify/parse;
    // pin that scope_kind / scope_id come back as strings.
    jobsStorage.add({
      id: "job-r",
      kind: "study_export",
      scope_kind: "study",
      scope_id: "study-R",
    });
    const list = jobsStorage.list();
    expect(list).toHaveLength(1);
    expect(list[0]).toMatchObject({
      id: "job-r",
      kind: "study_export",
      scope_kind: "study",
      scope_id: "study-R",
    });
  });
});
