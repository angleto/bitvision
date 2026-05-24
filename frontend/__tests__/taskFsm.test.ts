// PatientTask FSM mirror — client-side gating logic.
//
// Mirrors backend/src/bvphoenix/services/patient_tasks_fsm.py. Any
// drift between the two raises 422 invalid_transition at click-time;
// these tests pin the contract so the UI doesn't grey out buttons the
// backend would have allowed (silent feature loss is worse than a
// 422 the user can retry).

import { describe, expect, test } from "vitest";

import type { TaskStatus, TaskTransitionVerb } from "@/lib/api_records";
import { VERB_TARGET_STATUS, allowedNext, allowedVerbs, isTransitionAllowed } from "@/lib/task_fsm";

const MATRIX: ReadonlyArray<readonly [TaskStatus, TaskStatus, boolean]> = [
  ["pending", "in_progress", true],
  ["pending", "snoozed", true],
  ["pending", "done", true],
  ["pending", "dropped", true],
  ["pending", "pending", false],
  ["in_progress", "snoozed", true],
  ["in_progress", "done", true],
  ["in_progress", "dropped", true],
  ["in_progress", "pending", false],
  ["in_progress", "in_progress", false],
  ["snoozed", "pending", true],
  ["snoozed", "in_progress", true],
  ["snoozed", "done", false],
  ["snoozed", "dropped", false],
  ["snoozed", "snoozed", false],
  ["done", "pending", true],
  ["done", "in_progress", false],
  ["done", "snoozed", false],
  ["done", "dropped", false],
  ["done", "done", false],
  ["dropped", "pending", true],
  ["dropped", "in_progress", false],
  ["dropped", "snoozed", false],
  ["dropped", "done", false],
  ["dropped", "dropped", false],
];

describe("isTransitionAllowed", () => {
  test.each(MATRIX)("%s -> %s = %s", (from, to, allowed) => {
    expect(isTransitionAllowed(from, to)).toBe(allowed);
  });
});

describe("allowedNext", () => {
  test("pending fans out to four states", () => {
    expect(allowedNext("pending")).toEqual(
      new Set<TaskStatus>(["in_progress", "snoozed", "done", "dropped"]),
    );
  });

  test("done reopens only to pending (no in_progress, no snoozed)", () => {
    expect(allowedNext("done")).toEqual(new Set<TaskStatus>(["pending"]));
  });

  test("dropped reopens only to pending", () => {
    expect(allowedNext("dropped")).toEqual(new Set<TaskStatus>(["pending"]));
  });

  test("snoozed wakes to pending or in_progress (and nothing else)", () => {
    expect(allowedNext("snoozed")).toEqual(new Set<TaskStatus>(["pending", "in_progress"]));
  });
});

describe("VERB_TARGET_STATUS", () => {
  test("each verb has a defined target status", () => {
    const verbs: TaskTransitionVerb[] = ["start", "snooze", "wake", "complete", "drop", "reopen"];
    for (const v of verbs) {
      expect(VERB_TARGET_STATUS[v]).toBeDefined();
    }
  });

  test("start targets in_progress (matches backend ACTION_TO_TARGET_STATUS)", () => {
    expect(VERB_TARGET_STATUS.start).toBe("in_progress");
  });

  test("reopen always targets pending (preserves completed_at audit)", () => {
    expect(VERB_TARGET_STATUS.reopen).toBe("pending");
  });
});

describe("allowedVerbs (UI gating)", () => {
  test("pending offers start / snooze / complete / drop", () => {
    expect(allowedVerbs("pending").sort()).toEqual(["complete", "drop", "snooze", "start"].sort());
  });

  test("in_progress offers snooze / complete / drop (no restart)", () => {
    expect(allowedVerbs("in_progress").sort()).toEqual(["complete", "drop", "snooze"].sort());
  });

  test("snoozed offers only wake (lifts to pending or in_progress)", () => {
    expect(allowedVerbs("snoozed")).toEqual(["wake"]);
  });

  test("done offers only reopen", () => {
    expect(allowedVerbs("done")).toEqual(["reopen"]);
  });

  test("dropped offers only reopen", () => {
    expect(allowedVerbs("dropped")).toEqual(["reopen"]);
  });
});
