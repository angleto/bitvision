// Group + sort a flat list of TimelineRailItem into day buckets.
//
// Why a dedicated helper: the swimlane layout (one row per date,
// dots horizontal inside the row) needs items grouped by local
// calendar date, ordered chronologically, with undated items sinking
// to the bottom (mirrors the backend's ``ORDER BY ... NULLS LAST``).
// Pulling the grouping out of the rail component keeps the rail
// pure-render and makes the sort behaviour unit-testable in
// isolation.

import type { TimelineRailItem } from "./types";

export const UNDATED_BUCKET = "__undated__";

export type SortDir = "asc" | "desc";

export interface TimelineDayBucket<T = unknown> {
  /** ``YYYY-MM-DD`` for dated buckets, the literal sentinel
   *  ``UNDATED_BUCKET`` for undated. */
  dateKey: string;
  /** ``true`` when this bucket is the undated tail. */
  undated: boolean;
  /**
   * Items in this bucket, already sorted by ``sortKey`` ASC (within
   * a single calendar day "earlier in the day" reads naturally as
   * "left-to-right"). The direction of the outer bucket sequence
   * is controlled by ``groupByDate(dir)``; intra-day stays ASC so
   * the visual reading "09:00 then 14:30" is preserved regardless
   * of whether the outer days are newest-first or oldest-first.
   */
  items: TimelineRailItem<T>[];
}

/**
 * Group items by ``dateKey`` and return the buckets ordered by
 * date according to ``dir``. The undated bucket (items with
 * ``dateKey === null``) is always appended last.
 *
 * Stability: items inside a bucket are sorted by ``sortKey`` ASC
 * with ``null`` sortKeys sinking last (timed items first, undated-
 * within-day next). Ties keep insertion order.
 */
export function groupByDate<T>(
  items: TimelineRailItem<T>[],
  dir: SortDir = "desc",
): TimelineDayBucket<T>[] {
  const buckets = new Map<string, TimelineRailItem<T>[]>();
  for (const item of items) {
    const key = item.dateKey ?? UNDATED_BUCKET;
    const existing = buckets.get(key);
    if (existing) existing.push(item);
    else buckets.set(key, [item]);
  }

  const dated: TimelineDayBucket<T>[] = [];
  let undated: TimelineDayBucket<T> | null = null;
  for (const [dateKey, bucketItems] of buckets) {
    const sorted = bucketItems.slice().sort((a, b) => {
      const sa = a.sortKey;
      const sb = b.sortKey;
      if (sa === null && sb === null) return 0;
      if (sa === null) return 1;
      if (sb === null) return -1;
      return sa.localeCompare(sb);
    });
    const bucket: TimelineDayBucket<T> = {
      dateKey,
      undated: dateKey === UNDATED_BUCKET,
      items: sorted,
    };
    if (bucket.undated) undated = bucket;
    else dated.push(bucket);
  }

  dated.sort((a, b) =>
    dir === "asc" ? a.dateKey.localeCompare(b.dateKey) : b.dateKey.localeCompare(a.dateKey),
  );

  return undated ? [...dated, undated] : dated;
}
