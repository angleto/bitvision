"use client";

// Shared rendering primitive for any timeline-style list. Groups a
// flat ``TimelineRailItem[]`` into day buckets and renders one
// ``TimelineDaySwimlane`` per bucket. Pure-render: it owns no state,
// no fetch, no URL syncing. The caller (CareTimeline today, TaskTimeline
// + MergedTimelineView in sprint A3) drives data + filters.
//
// The rail accepts a single ``items`` list of already-merged
// ``TimelineRailItem`` envelopes plus a ``renderItem`` callback that
// dispatches to the right dot component based on ``item.source``. This
// keeps tasks and events decoupled at the rail level: sprint A2 wires
// only ``source === "event"``; sprint A3 will hand the same component
// a list that interleaves tasks with events for the merged view.

import type { ReactNode } from "react";

import TimelineDaySwimlane from "./TimelineDaySwimlane";
import { type SortDir, groupByDate } from "./groupByDate";
import type { TimelineRailItem } from "./types";

interface Props<T> {
  /** Already-filtered, source-agnostic list of items to render. */
  items: TimelineRailItem<T>[];
  /** Outer bucket ordering. Intra-bucket items stay ASC (time-of-day). */
  sort?: SortDir;
  /** Locale for date labels. */
  locale: string;
  /** Per-item render callback (typically dispatches on item.source). */
  renderItem: (item: TimelineRailItem<T>, index: number) => ReactNode;
  /** Label for the undated tail bucket, e.g. "Senza data". */
  undatedLabel?: string;
  /** Optional empty-state placeholder when no items are present. */
  emptyState?: ReactNode;
}

export default function TimelineRail<T>({
  items,
  sort = "desc",
  locale,
  renderItem,
  undatedLabel,
  emptyState,
}: Props<T>) {
  if (items.length === 0) {
    return <>{emptyState ?? null}</>;
  }
  const buckets = groupByDate(items, sort);
  return (
    <ol
      className="timeline-rail"
      style={{
        listStyle: "none",
        margin: 0,
        padding: 0,
        display: "flex",
        flexDirection: "column",
        gap: "0.35rem",
      }}
    >
      {buckets.map((bucket) => (
        <TimelineDaySwimlane
          key={bucket.dateKey}
          dateKey={bucket.dateKey}
          undated={bucket.undated}
          items={bucket.items}
          locale={locale}
          renderItem={renderItem}
          undatedLabel={undatedLabel}
        />
      ))}
    </ol>
  );
}
