"use client";

// Single dot on the right side of the vertical timeline. Pixel-styled
// after ``timeline_fascicolo_canary_patient.svg``: a coloured disc
// followed by date + title (two lines). Click navigates to
// ``event.target.url`` (resolved server-side, see EventTarget). The
// component is keyboard-accessible: Tab focuses, Enter / Space invoke
// the navigation.

import { useTranslations } from "next-intl";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback } from "react";

import type { EventStatus, TimelineEvent } from "@/lib/api_records";

interface Props {
  event: TimelineEvent;
  /** Phase colour. The dot itself takes this colour; the title row
   *  inherits theme foreground for legibility. */
  color: string;
  /** When true, the component does NOT navigate (used by the editor
   *  drag-drop overlay). The dot is still focusable but Enter/Space
   *  delegate to the parent's drag handle. */
  inert?: boolean;
  /** When provided, replaces the default navigation. Used by
   *  CareTimeline to open the same EventDrawer the calendar view
   *  uses, so click-to-edit is consistent across timeline and
   *  calendar (no duplicated dialog code). */
  onActivate?: (event: TimelineEvent) => void;
  /**
   * Layout variant. ``vertical`` (default, pre-A2 behaviour): one
   * dot per row, date+title stacked. ``horizontal``: compact pill
   * suitable for the TimelineDaySwimlane same-date row — the date
   * is hoisted to the swimlane header, the pill carries just the
   * time-of-day + status glyph + truncated title.
   */
  variant?: "vertical" | "horizontal";
}

const ITALIAN_MONTH = [
  "gen",
  "feb",
  "mar",
  "apr",
  "mag",
  "giu",
  "lug",
  "ago",
  "set",
  "ott",
  "nov",
  "dic",
];

function withFromTimeline(rawUrl: string): string {
  const [pathPart, hashPart] = rawUrl.split("#");
  const sep = pathPart.includes("?") ? "&" : "?";
  const next = `${pathPart}${sep}from=timeline`;
  return hashPart ? `${next}#${hashPart}` : next;
}

function formatDate(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return `${d.getDate()} ${ITALIAN_MONTH[d.getMonth()]} ${d.getFullYear()}`;
}

/** Format HH:MM in the user's locale. Returns ``null`` when the input
 * is a DATE-only string (no time component), so the horizontal pill
 * knows to omit the time slot. */
function formatTime(iso: string | null): string | null {
  if (!iso) return null;
  // Heuristic: a 10-char ``YYYY-MM-DD`` is a DATE; anything longer is
  // an ISO datetime with at least an hour component.
  if (iso.length <= 10) return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  return `${hh}:${mm}`;
}

// Visual style per event_status. We pin both colour and shape (dashed
// border, strikethrough, opacity) because WCAG-AA forbids relying on
// colour alone — a user with deuteranopia must still tell a planned
// event from a completed one. The status badge (label + icon glyph
// below) provides the third redundant channel.
function statusStyle(status: EventStatus): {
  containerStyle: React.CSSProperties;
  titleStrike: boolean;
  badgeBg: string;
  badgeFg: string;
  glyph: string;
} {
  switch (status) {
    case "planned":
      return {
        containerStyle: {
          border: "1px dashed var(--bv-status-planned-border, #5b8def)",
          background: "var(--bv-status-planned-bg, rgba(91,141,239,0.06))",
          opacity: 0.85,
        },
        titleStrike: false,
        badgeBg: "var(--bv-status-planned-bg, rgba(91,141,239,0.12))",
        badgeFg: "var(--bv-status-planned-border, #2557d6)",
        glyph: "⏳",
      };
    case "confirmed":
      return {
        containerStyle: {
          border: "1px solid var(--bv-status-confirmed-border, #1e8e3e)",
          background: "var(--bv-status-confirmed-bg, rgba(30,142,62,0.06))",
        },
        titleStrike: false,
        badgeBg: "var(--bv-status-confirmed-bg, rgba(30,142,62,0.12))",
        badgeFg: "var(--bv-status-confirmed-border, #146b2d)",
        glyph: "✓",
      };
    case "cancelled":
      return {
        containerStyle: {
          border: "1px solid var(--bv-status-cancelled-border, #9ca3af)",
          background: "var(--bv-status-cancelled-bg, rgba(156,163,175,0.10))",
          opacity: 0.75,
        },
        titleStrike: true,
        badgeBg: "var(--bv-status-cancelled-bg, rgba(156,163,175,0.20))",
        badgeFg: "var(--bv-status-cancelled-border, #4b5563)",
        glyph: "⊘",
      };
    case "missed":
      return {
        containerStyle: {
          border: "1px solid var(--bv-status-missed-border, #d97706)",
          background: "var(--bv-status-missed-bg, rgba(217,119,6,0.08))",
        },
        titleStrike: false,
        badgeBg: "var(--bv-status-missed-bg, rgba(217,119,6,0.18))",
        badgeFg: "var(--bv-status-missed-border, #9a5b04)",
        glyph: "△",
      };
    case "rescheduled":
      return {
        containerStyle: {
          border: "1px dashed var(--bv-status-cancelled-border, #9ca3af)",
          background: "transparent",
          opacity: 0.7,
        },
        titleStrike: true,
        badgeBg: "transparent",
        badgeFg: "var(--bv-fg-soft)",
        glyph: "↻",
      };
    default:
      // ``completed`` — neutral; matches the historical render so
      // pre-0098 timelines look unchanged.
      return {
        containerStyle: {},
        titleStrike: false,
        badgeBg: "transparent",
        badgeFg: "var(--bv-fg-soft)",
        glyph: "",
      };
  }
}

export default function TimelineEventDot({
  event,
  color,
  inert = false,
  onActivate,
  variant = "vertical",
}: Props) {
  const router = useRouter();
  const tStatus = useTranslations("eventStatus");
  // ``?from=timeline`` lets the destination page (study, document,
  // clinical-event, ...) render a back-link to the events tab so the
  // user can return without losing their place. Preserves any
  // existing query string on the resolved target URL.
  const url = withFromTimeline(event.target.url);
  const handleActivate = useCallback(() => {
    if (inert) return;
    if (onActivate) {
      onActivate(event);
      return;
    }
    router.push(url);
  }, [inert, onActivate, event, router, url]);

  const status: EventStatus = event.event_status ?? "completed";
  const sStyle = statusStyle(status);
  // Use the planning timestamp for planned/confirmed (the event_date
  // trigger keeps DATE in sync, but we want hour granularity for the
  // upcoming-event chips). Fall back to the canonical event_date.
  const displayIso =
    (status === "planned" || status === "confirmed") && event.planned_start_at
      ? event.planned_start_at
      : (event.actual_start_at ?? event.event_date);

  if (variant === "horizontal") {
    return renderHorizontalPill({
      event,
      color,
      inert,
      url,
      onActivate,
      handleActivate,
      status,
      sStyle,
      displayIso,
      tStatus,
    });
  }

  // We render the row as a single focusable region. The visible <Link>
  // owns the accessible name; the surrounding row catches keyboard
  // events so the entire chip is invokable, not only the title text.
  return (
    <div
      className="timeline-event-dot"
      data-variant="vertical"
      data-event-status={status}
      // biome-ignore lint/a11y/useSemanticElements: row hosts a nested <Link>; wrapping the row itself in <a> would nest interactive elements (HTML invalid).
      role="link"
      tabIndex={inert ? -1 : 0}
      aria-label={`${formatDate(displayIso)} — ${event.title} — ${tStatus(`${status}_aria`)}`}
      data-event-id={event.id}
      onKeyDown={(e) => {
        if (inert) return;
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          handleActivate();
        }
      }}
      style={{
        display: "grid",
        gridTemplateColumns: "16px 1fr",
        alignItems: "start",
        gap: "0.6rem",
        padding: "0.25rem 0.4rem",
        borderRadius: 6,
        cursor: inert ? "default" : "pointer",
        outline: "none",
        ...sStyle.containerStyle,
      }}
      onMouseDown={(e) => {
        // Allow native link click to win when user clicks the title;
        // when the user clicks the dot itself we intercept here.
        if ((e.target as HTMLElement).closest("a")) return;
      }}
      onClick={(e) => {
        if (inert) return;
        if ((e.target as HTMLElement).closest("a")) return;
        handleActivate();
      }}
    >
      <span
        aria-hidden
        style={{
          width: 10,
          height: 10,
          borderRadius: "50%",
          background: color,
          marginTop: 6,
          boxShadow: "0 0 0 2px var(--bv-card-bg, #fff)",
        }}
      />
      <span style={{ display: "flex", flexDirection: "column", minWidth: 0 }}>
        <span
          style={{
            fontSize: "0.78rem",
            color: "var(--bv-fg-soft)",
            fontVariantNumeric: "tabular-nums",
          }}
        >
          {formatDate(displayIso)}
          {status !== "completed" && (
            <span
              aria-hidden
              title={tStatus(status)}
              style={{
                marginLeft: 6,
                padding: "0 0.35rem",
                borderRadius: 4,
                background: sStyle.badgeBg,
                color: sStyle.badgeFg,
                fontSize: "0.65rem",
                fontWeight: 600,
                textTransform: "uppercase",
                letterSpacing: "0.04em",
                border: "1px solid currentColor",
              }}
            >
              {sStyle.glyph && <span style={{ marginRight: 3 }}>{sStyle.glyph}</span>}
              {tStatus(status)}
            </span>
          )}
          {event.phase_assigned_by === "agent" && (
            <span
              title="Assegnazione proposta da agente"
              style={{
                marginLeft: 6,
                padding: "0 0.3rem",
                borderRadius: 4,
                background: "var(--bv-accent-soft, #eef2ff)",
                color: "var(--bv-accent, #4f46e5)",
                fontSize: "0.65rem",
                fontWeight: 600,
                textTransform: "uppercase",
                letterSpacing: "0.04em",
              }}
            >
              AI
            </span>
          )}
        </span>
        {inert || onActivate ? (
          // When ``onActivate`` is provided we render a plain span so
          // the row's outer click handler routes through the parent's
          // drawer logic instead of letting the nested <Link> navigate
          // (which would bypass the drawer entirely).
          <span
            className="timeline-event-dot__title"
            style={{
              fontSize: "0.88rem",
              color: "var(--bv-fg)",
              overflow: "hidden",
              textOverflow: "ellipsis",
              textDecoration: sStyle.titleStrike ? "line-through" : undefined,
            }}
          >
            {event.title}
          </span>
        ) : (
          <Link
            href={url}
            className="timeline-event-dot__title"
            style={{
              fontSize: "0.88rem",
              color: "var(--bv-fg)",
              textDecoration: sStyle.titleStrike ? "line-through" : "none",
              overflow: "hidden",
              textOverflow: "ellipsis",
            }}
          >
            {event.title}
          </Link>
        )}
        {event.body_part && (
          <span
            style={{
              fontSize: "0.72rem",
              color: "var(--bv-fg-muted, #888)",
            }}
          >
            {event.body_part}
          </span>
        )}
      </span>
    </div>
  );
}

// ---------------------------------------------------------------------
// Horizontal pill (variant="horizontal") — compact rendering for the
// TimelineDaySwimlane same-date row. Date is hoisted to the swimlane
// header; the pill carries time-of-day + status glyph + truncated
// title. The status pin and AI badge stay so the WCAG-AA contract
// (colour + glyph + label) is preserved.
// ---------------------------------------------------------------------

interface HorizontalArgs {
  event: TimelineEvent;
  color: string;
  inert: boolean;
  url: string;
  onActivate?: (event: TimelineEvent) => void;
  handleActivate: () => void;
  status: EventStatus;
  sStyle: ReturnType<typeof statusStyle>;
  displayIso: string | null;
  tStatus: (key: string) => string;
}

function renderHorizontalPill({
  event,
  color,
  inert,
  url,
  onActivate,
  handleActivate,
  status,
  sStyle,
  displayIso,
  tStatus,
}: HorizontalArgs) {
  const timeLabel = formatTime(displayIso);
  // ``planned`` / ``confirmed`` already carry the time in ``displayIso``;
  // for ``completed`` / ``missed`` we fall back to ``actual_start_at``
  // when present (handled by the caller setting ``displayIso``).
  const inner = (
    <>
      <span
        aria-hidden
        style={{
          width: 8,
          height: 8,
          borderRadius: "50%",
          background: color,
          boxShadow: "0 0 0 2px var(--bv-card-bg, #fff)",
          flex: "0 0 auto",
        }}
      />
      <span
        style={{
          display: "inline-flex",
          alignItems: "center",
          gap: "0.35rem",
          minWidth: 0,
        }}
      >
        {timeLabel && (
          <span
            style={{
              fontSize: "0.72rem",
              color: "var(--bv-fg-soft)",
              fontVariantNumeric: "tabular-nums",
              flex: "0 0 auto",
            }}
          >
            {timeLabel}
          </span>
        )}
        {sStyle.glyph && (
          <span
            aria-hidden
            title={tStatus(status)}
            style={{
              fontSize: "0.78rem",
              color: sStyle.badgeFg,
              flex: "0 0 auto",
            }}
          >
            {sStyle.glyph}
          </span>
        )}
        <span
          className="timeline-event-dot__title"
          style={{
            fontSize: "0.82rem",
            color: "var(--bv-fg)",
            textDecoration: sStyle.titleStrike ? "line-through" : undefined,
          }}
        >
          {event.title}
        </span>
        {event.phase_assigned_by === "agent" && (
          <span
            title="Assegnazione proposta da agente"
            aria-label="AI"
            style={{
              padding: "0 0.3rem",
              borderRadius: 4,
              background: "var(--bv-accent-soft, #eef2ff)",
              color: "var(--bv-accent, #4f46e5)",
              fontSize: "0.6rem",
              fontWeight: 600,
              letterSpacing: "0.04em",
              flex: "0 0 auto",
            }}
          >
            AI
          </span>
        )}
      </span>
    </>
  );

  const accessibleLabel = `${timeLabel ?? ""} ${event.title} — ${tStatus(`${status}_aria`)}`.trim();

  // Use a real <Link> when no onActivate handler is supplied (the row
  // can be a plain navigation target). When onActivate is provided
  // (CareTimeline drawer pattern), use a button-styled span so the
  // outer parent owns the click semantics.
  if (!inert && !onActivate) {
    return (
      <Link
        href={url}
        className="timeline-event-dot"
        data-variant="horizontal"
        data-event-status={status}
        data-event-id={event.id}
        aria-label={accessibleLabel}
        title={event.title}
        style={{
          display: "inline-grid",
          gridTemplateColumns: "8px auto",
          alignItems: "center",
          gap: "0.4rem",
          padding: "0.2rem 0.55rem",
          borderRadius: 999,
          cursor: "pointer",
          textDecoration: "none",
          ...sStyle.containerStyle,
        }}
      >
        {inner}
      </Link>
    );
  }
  return (
    <span
      // biome-ignore lint/a11y/useSemanticElements: parent <li> hosts a focusable button; nesting a button here would be redundant + double-tab.
      role="button"
      tabIndex={inert ? -1 : 0}
      aria-label={accessibleLabel}
      title={event.title}
      data-event-id={event.id}
      data-event-status={status}
      data-variant="horizontal"
      className="timeline-event-dot"
      onKeyDown={(e) => {
        if (inert) return;
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          handleActivate();
        }
      }}
      onClick={() => {
        if (inert) return;
        handleActivate();
      }}
      style={{
        display: "inline-grid",
        gridTemplateColumns: "8px auto",
        alignItems: "center",
        gap: "0.4rem",
        padding: "0.2rem 0.55rem",
        borderRadius: 999,
        cursor: inert ? "default" : "pointer",
        outline: "none",
        ...sStyle.containerStyle,
      }}
    >
      {inner}
    </span>
  );
}
