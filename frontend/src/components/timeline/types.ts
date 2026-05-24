// Types shared by TimelineRail, TimelineDaySwimlane and the per-source
// dot renderers. The rail itself stays source-agnostic: it consumes a
// flat list of ``TimelineRailItem`` already merged + sorted by the
// caller, and dispatches the dot rendering via the ``renderDot``
// callback. This keeps tasks and events decoupled at the rail level
// (sprint A2 only wires events; sprint A3 will add the task source).

import type { TimelineEvent } from "@/lib/api_records";

/**
 * Source discriminator. Used by the consumer to decide which dot
 * component to render. Tasks are added in sprint A3.
 */
export type TimelineSource = "event" | "task";

/**
 * Generic envelope around one renderable item on the rail. Carries
 * just enough metadata for grouping (date + sort key) and routing
 * (id + source); the full domain object travels in ``payload`` so
 * the dot renderer can read whichever fields it needs.
 */
export interface TimelineRailItem<T = unknown> {
  /** Stable identity for keying. */
  id: string;
  /** Which dot renderer to use. */
  source: TimelineSource;
  /**
   * Day bucket for the swimlane, in ``YYYY-MM-DD`` form. Items
   * without a known date land in a special trailing bucket (see
   * ``UNDATED_BUCKET`` in groupByDate).
   */
  dateKey: string | null;
  /**
   * Intra-day sort key. ISO datetime when available
   * (``planned_start_at`` / ``actual_start_at`` / ``due_at``), otherwise
   * ``null``. Items with a null sortKey are rendered after the timed
   * ones with reduced opacity.
   */
  sortKey: string | null;
  /** Phase colour used by the legacy dot — passed through for
   *  visual coherence with the rail layout. */
  color: string;
  /** The domain object the renderer needs. */
  payload: T;
}

/**
 * Convenience for sprint A2: a rail item whose payload is a
 * ``TimelineEvent``. Sprint A3 will add a ``TimelineTaskItem`` sibling
 * carrying a ``PatientTask`` payload.
 */
export type TimelineEventItem = TimelineRailItem<TimelineEvent> & {
  source: "event";
};
