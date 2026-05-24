"use client";

// One swimlane on the timeline = one calendar day. Header (date label)
// on the left, a row of dots on the right, ordered by time-of-day.
//
// Visual contract
// ---------------
// Above ``min-width: 640px`` the swimlane is a 2-column grid with a
// fixed-width date header and a flex-wrap row of dots. Below the
// breakpoint the swimlane stacks vertically (header above, dots below
// in a column) so a 360px viewport doesn't force a horizontal scroll.
// The breakpoint is enforced via the ``.timeline-day-swimlane`` CSS
// class in ``app/globals.css``; inline styles handle theme tokens
// and per-item layout, the class handles responsive behaviour.
//
// Wrap policy: when a single day has many dots they wrap to a second
// row inside the same swimlane (``flex-wrap: wrap``). The logical
// grouping stays one swimlane = one date — we never split a day into
// two swimlanes.
//
// A11y: the header is a ``<h3>``-equivalent role, the dot row is a
// ``<ul>`` with no list bullets. The aria-label on the swimlane root
// announces the date so a screen reader hears "lunedì 15 giugno 2026"
// before the dots inside.

import type { ReactNode } from "react";

import type { TimelineRailItem } from "./types";

const ITALIAN_MONTHS_FULL = [
  "gennaio",
  "febbraio",
  "marzo",
  "aprile",
  "maggio",
  "giugno",
  "luglio",
  "agosto",
  "settembre",
  "ottobre",
  "novembre",
  "dicembre",
];

const ITALIAN_WEEKDAYS_SHORT = ["dom", "lun", "mar", "mer", "gio", "ven", "sab"];

/**
 * Format ``YYYY-MM-DD`` into a locale-aware human label.
 * IT: "lun 15 giu 2026" — short weekday + day + abbreviated month + year.
 * EN: ``Intl.DateTimeFormat(en)`` rendering — "Mon Jun 15, 2026".
 *
 * Pure function (no React deps) so it stays testable in isolation.
 */
export function formatDateLabel(dateKey: string, locale: string): string {
  if (locale.startsWith("it")) {
    const [y, m, d] = dateKey.split("-").map((s) => Number.parseInt(s, 10));
    if (!y || !m || !d) return dateKey;
    const date = new Date(Date.UTC(y, m - 1, d));
    const weekday = ITALIAN_WEEKDAYS_SHORT[date.getUTCDay()];
    const monthShort = ITALIAN_MONTHS_FULL[m - 1]?.slice(0, 3) ?? "";
    return `${weekday} ${d} ${monthShort} ${y}`;
  }
  // Fallback for non-Italian locales: ISO date through Intl.
  try {
    const [y, m, d] = dateKey.split("-").map((s) => Number.parseInt(s, 10));
    if (!y || !m || !d) return dateKey;
    return new Intl.DateTimeFormat(locale, {
      weekday: "short",
      day: "numeric",
      month: "short",
      year: "numeric",
    }).format(new Date(Date.UTC(y, m - 1, d)));
  } catch {
    return dateKey;
  }
}

interface Props<T> {
  /** ``YYYY-MM-DD`` or the literal ``UNDATED_BUCKET`` sentinel. */
  dateKey: string;
  /** ``true`` when the swimlane represents the undated tail bucket. */
  undated: boolean;
  /** Items in this bucket, already sorted by sortKey ASC. */
  items: TimelineRailItem<T>[];
  /** Locale string for date formatting (``"it"`` / ``"en"``). */
  locale: string;
  /**
   * Per-item render callback. Returns the dot/chip for the given
   * rail item. The swimlane positions the result inside the dot row.
   * Keeping rendering external means we don't bind the swimlane to a
   * specific dot component — events get ``TimelineEventDot``, tasks
   * (sprint A3) get ``TimelineTaskDot``.
   */
  renderItem: (item: TimelineRailItem<T>, index: number) => ReactNode;
  /** Label for the undated bucket, e.g. "Senza data". */
  undatedLabel?: string;
}

export default function TimelineDaySwimlane<T>({
  dateKey,
  undated,
  items,
  locale,
  renderItem,
  undatedLabel = "Senza data",
}: Props<T>) {
  const label = undated ? undatedLabel : formatDateLabel(dateKey, locale);
  return (
    <li
      className="timeline-day-swimlane"
      data-date-key={dateKey}
      data-undated={undated || undefined}
      aria-label={label}
    >
      <header className="timeline-day-swimlane__header">
        <span
          className="timeline-day-swimlane__date"
          style={{ fontVariantNumeric: "tabular-nums" }}
        >
          {label}
        </span>
        {items.length > 1 && (
          <span className="timeline-day-swimlane__count" aria-hidden>
            ×{items.length}
          </span>
        )}
      </header>
      <ul
        className="timeline-day-swimlane__items"
        style={{
          listStyle: "none",
          margin: 0,
          padding: 0,
          display: "flex",
          flexWrap: "wrap",
          gap: "0.4rem",
          alignItems: "stretch",
        }}
      >
        {items.map((item, index) => (
          <li
            key={item.id}
            data-source={item.source}
            data-undated-item={item.sortKey === null && !undated ? "true" : undefined}
            style={{
              // Items without a precise time within an otherwise dated
              // day are rendered with reduced opacity so the eye sees
              // the timed ones first. The undated *bucket* (different
              // case) uses the same hint via data-undated on the parent.
              opacity: item.sortKey === null && !undated ? 0.78 : 1,
              minWidth: 0,
            }}
          >
            {renderItem(item, index)}
          </li>
        ))}
      </ul>
    </li>
  );
}
