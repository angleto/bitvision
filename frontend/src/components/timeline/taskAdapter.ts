// Adapter: PatientTask → TimelineRailItem<PatientTask>.
//
// Sibling of ``eventAdapter.ts``. Decisions on dateKey / sortKey:
//
// * ``due_at`` is the natural "when does this need to happen by"
//   anchor — that's how a user will scan their checklist. We use
//   ``due_at`` for both the day bucket and the intra-day sort.
// * ``completed_at`` is preferred only for already-done tasks that
//   have no due_at (e.g. an LLM-drafted task the user just ticked
//   off without ever setting a deadline). The done timestamp is at
//   least an honest signal for "this row belongs here on this day".
// * Otherwise the task lands in the undated bucket (rendered at the
//   tail of the rail with reduced opacity per the swimlane styling).
//
// The colour passed in is the phase colour when the task is attached
// to a CarePhase, or the neutral muted token otherwise.

import type { PatientTask } from "@/lib/api_records";

import type { TimelineRailItem } from "./types";

export type TimelineTaskItem = TimelineRailItem<PatientTask> & { source: "task" };

function pickSortKey(task: PatientTask): string | null {
  if (task.due_at) return task.due_at;
  if (task.status === "done" && task.completed_at) return task.completed_at;
  return null;
}

function pickDateKey(task: PatientTask): string | null {
  const sort = pickSortKey(task);
  if (sort) return sort.slice(0, 10);
  return null;
}

export function taskToRailItem(task: PatientTask, color: string): TimelineTaskItem {
  return {
    id: task.id,
    source: "task",
    dateKey: pickDateKey(task),
    sortKey: pickSortKey(task),
    color,
    payload: task,
  };
}

export function tasksToRailItems(tasks: PatientTask[], color: string): TimelineTaskItem[] {
  return tasks.map((t) => taskToRailItem(t, color));
}
