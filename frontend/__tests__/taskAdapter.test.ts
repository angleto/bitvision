// taskAdapter — PatientTask → TimelineRailItem priority + edge cases.
//
// The dateKey / sortKey picking rules drive the entire merged-view
// ordering (events + tasks on one rail). Pin them here so a future
// refactor that "improves" the picking heuristic can't quietly break
// the chronological reading users rely on.

import { describe, expect, test } from "vitest";

import { groupByDate } from "@/components/timeline/groupByDate";
import { taskToRailItem, tasksToRailItems } from "@/components/timeline/taskAdapter";
import type { PatientTask, TaskStatus } from "@/lib/api_records";

function makeTask(overrides: Partial<PatientTask> = {}): PatientTask {
  return {
    id: overrides.id ?? "task-1",
    patient_id: "p-1",
    title: overrides.title ?? "Prenotare TAC",
    description: null,
    category: overrides.category ?? "admin",
    priority: overrides.priority ?? "normal",
    status: (overrides.status ?? "pending") as TaskStatus,
    due_at: overrides.due_at ?? null,
    snooze_until: null,
    completed_at: overrides.completed_at ?? null,
    timezone: null,
    phase_id: null,
    phase_assigned_by: null,
    phase_assigned_at: null,
    recurrence_rule: null,
    parent_task_id: null,
    assigned_to_contact_id: null,
    related_event_id: null,
    related_document_id: null,
    labels: null,
    links: null,
    reminder_offsets_minutes: null,
    etag: "etag-1",
    author_kind: overrides.author_kind ?? "human",
    status_changed_at: null,
    status_changed_by_kind: null,
    status_change_reason: null,
    deleted_at: null,
    created_at: "2026-05-13T10:00:00Z",
    updated_at: "2026-05-13T10:00:00Z",
    ...overrides,
  };
}

describe("taskToRailItem", () => {
  test("task with due_at uses due_at for both dateKey and sortKey", () => {
    const task = makeTask({ due_at: "2026-06-15T09:00:00Z" });
    const item = taskToRailItem(task, "#abc");
    expect(item.dateKey).toBe("2026-06-15");
    expect(item.sortKey).toBe("2026-06-15T09:00:00Z");
    expect(item.source).toBe("task");
    expect(item.color).toBe("#abc");
  });

  test("done task without due_at falls back to completed_at", () => {
    const task = makeTask({
      status: "done",
      due_at: null,
      completed_at: "2026-05-13T16:30:00Z",
    });
    const item = taskToRailItem(task, "#000");
    expect(item.dateKey).toBe("2026-05-13");
    expect(item.sortKey).toBe("2026-05-13T16:30:00Z");
  });

  test("pending task without due_at lands in the undated bucket", () => {
    const task = makeTask({ status: "pending", due_at: null });
    const item = taskToRailItem(task, "#000");
    expect(item.dateKey).toBeNull();
    expect(item.sortKey).toBeNull();
  });

  test("non-done task with completed_at but no due_at stays undated (completed_at is for done only)", () => {
    // Edge case: a pending task with a stale completed_at from a
    // previous done/reopen cycle. We don't pick completed_at unless
    // the task is currently done — otherwise reopened tasks would
    // anchor on the old completion timestamp.
    const task = makeTask({
      status: "pending",
      due_at: null,
      completed_at: "2025-01-01T00:00:00Z",
    });
    const item = taskToRailItem(task, "#000");
    expect(item.dateKey).toBeNull();
    expect(item.sortKey).toBeNull();
  });

  test("tasksToRailItems preserves input order", () => {
    const tasks = [makeTask({ id: "a" }), makeTask({ id: "b" }), makeTask({ id: "c" })];
    const items = tasksToRailItems(tasks, "#fff");
    expect(items.map((i) => i.id)).toEqual(["a", "b", "c"]);
  });
});

describe("taskAdapter + groupByDate pipeline (merged view ordering)", () => {
  test("multiple tasks on the same due day collapse into one swimlane, ordered by time", () => {
    const tasks = [
      makeTask({ id: "afternoon", due_at: "2026-06-15T15:00:00Z" }),
      makeTask({ id: "morning", due_at: "2026-06-15T09:30:00Z" }),
    ];
    const items = tasksToRailItems(tasks, "#000");
    const buckets = groupByDate(items);
    expect(buckets).toHaveLength(1);
    expect(buckets[0].dateKey).toBe("2026-06-15");
    expect(buckets[0].items.map((i) => i.id)).toEqual(["morning", "afternoon"]);
  });

  test("tasks without due_at appear in the trailing undated bucket", () => {
    const tasks = [
      makeTask({ id: "dated", due_at: "2026-06-15T09:00:00Z" }),
      makeTask({ id: "undated" }),
    ];
    const items = tasksToRailItems(tasks, "#000");
    const buckets = groupByDate(items);
    expect(buckets[buckets.length - 1].undated).toBe(true);
    expect(buckets[buckets.length - 1].items.map((i) => i.id)).toEqual(["undated"]);
  });
});
