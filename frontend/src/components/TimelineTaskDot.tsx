"use client";

// Operational-task dot on the timeline. Sibling of TimelineEventDot
// for the PatientTask source. Different glyphs (☐ ▶ ☑ 💤 ⊘) and
// different FSM (pending/in_progress/snoozed/done/dropped), same
// vertical / horizontal variant contract so TimelineRail can mix
// events and tasks in the merged view without knowing which is which.
//
// WCAG-AA: status is communicated through three redundant channels
// (colour, glyph, aria-label) so a user with deuteranopia can still
// tell pending from done.

import { useTranslations } from "next-intl";
import { useCallback } from "react";

import type { PatientTask, TaskStatus } from "@/lib/api_records";

interface Props {
  task: PatientTask;
  /** Phase colour when the task is attached to a CarePhase, neutral
   *  muted token otherwise. */
  color: string;
  /** Click handler — typically opens the TaskDrawer for in-place
   *  inspection + status transitions. When omitted the component is
   *  navigation-only (no router push, since tasks have no dedicated
   *  page yet — the drawer is the canonical surface). */
  onActivate?: (task: PatientTask) => void;
  /** Disables interactions (used by editor overlays for drag-drop). */
  inert?: boolean;
  /** Layout variant. ``vertical`` (default): full-row chip with
   *  due-date + title. ``horizontal``: compact pill suitable for the
   *  TimelineDaySwimlane same-date row. */
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

function formatDate(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return `${d.getDate()} ${ITALIAN_MONTH[d.getMonth()]} ${d.getFullYear()}`;
}

function formatTime(iso: string | null): string | null {
  if (!iso) return null;
  if (iso.length <= 10) return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  return `${hh}:${mm}`;
}

function statusStyle(status: TaskStatus): {
  containerStyle: React.CSSProperties;
  titleStrike: boolean;
  badgeBg: string;
  badgeFg: string;
  glyph: string;
} {
  switch (status) {
    case "pending":
      return {
        containerStyle: {
          border: "1px solid var(--bv-status-planned-border, #5b8def)",
          background: "var(--bv-status-planned-bg, rgba(91,141,239,0.06))",
        },
        titleStrike: false,
        badgeBg: "var(--bv-status-planned-bg, rgba(91,141,239,0.12))",
        badgeFg: "var(--bv-status-planned-border, #2557d6)",
        glyph: "☐",
      };
    case "in_progress":
      return {
        containerStyle: {
          border: "1px solid var(--bv-status-confirmed-border, #1e8e3e)",
          background: "var(--bv-status-confirmed-bg, rgba(30,142,62,0.08))",
        },
        titleStrike: false,
        badgeBg: "var(--bv-status-confirmed-bg, rgba(30,142,62,0.14))",
        badgeFg: "var(--bv-status-confirmed-border, #146b2d)",
        glyph: "▶",
      };
    case "snoozed":
      return {
        containerStyle: {
          border: "1px dashed var(--bv-fg-muted, #8b8b8b)",
          background: "transparent",
          opacity: 0.78,
        },
        titleStrike: false,
        badgeBg: "transparent",
        badgeFg: "var(--bv-fg-soft)",
        glyph: "💤",
      };
    case "done":
      return {
        containerStyle: {
          border: "1px solid var(--bv-status-cancelled-border, #9ca3af)",
          background: "var(--bv-status-cancelled-bg, rgba(156,163,175,0.10))",
          opacity: 0.78,
        },
        titleStrike: true,
        badgeBg: "var(--bv-status-confirmed-bg, rgba(30,142,62,0.12))",
        badgeFg: "var(--bv-status-confirmed-border, #146b2d)",
        glyph: "☑",
      };
    case "dropped":
      return {
        containerStyle: {
          border: "1px dashed var(--bv-status-cancelled-border, #9ca3af)",
          background: "transparent",
          opacity: 0.72,
        },
        titleStrike: true,
        badgeBg: "transparent",
        badgeFg: "var(--bv-fg-soft)",
        glyph: "⊘",
      };
    default:
      return {
        containerStyle: {},
        titleStrike: false,
        badgeBg: "transparent",
        badgeFg: "var(--bv-fg-soft)",
        glyph: "",
      };
  }
}

export default function TimelineTaskDot({
  task,
  color,
  onActivate,
  inert = false,
  variant = "vertical",
}: Props) {
  const tStatus = useTranslations("taskStatus");
  const handleActivate = useCallback(() => {
    if (inert) return;
    onActivate?.(task);
  }, [inert, onActivate, task]);

  const status = task.status;
  const sStyle = statusStyle(status);
  const displayIso = task.due_at ?? task.completed_at;
  const isAgent = task.author_kind === "agent";
  const accessibleLabel = `${formatDate(displayIso)} — ${task.title} — ${tStatus(`${status}_aria`)}`;

  if (variant === "horizontal") {
    const timeLabel = formatTime(displayIso);
    return (
      <span
        // biome-ignore lint/a11y/useSemanticElements: parent rail row already provides focusable container; nesting a button would double-tab.
        role="button"
        tabIndex={inert ? -1 : 0}
        aria-label={accessibleLabel}
        title={task.title}
        data-task-id={task.id}
        data-task-status={status}
        data-variant="horizontal"
        className="timeline-task-dot"
        onKeyDown={(e) => {
          if (inert) return;
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            handleActivate();
          }
        }}
        onClick={handleActivate}
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
                fontSize: "0.85rem",
                color: sStyle.badgeFg,
                flex: "0 0 auto",
              }}
            >
              {sStyle.glyph}
            </span>
          )}
          <span
            className="timeline-task-dot__title"
            style={{
              fontSize: "0.82rem",
              color: "var(--bv-fg)",
              textDecoration: sStyle.titleStrike ? "line-through" : undefined,
            }}
          >
            {task.title}
          </span>
          {isAgent && (
            <span
              title="Task drafted by AI"
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
      </span>
    );
  }

  // Vertical variant: full-row chip used in flat / mobile layouts.
  return (
    <div
      className="timeline-task-dot"
      data-variant="vertical"
      data-task-status={status}
      // biome-ignore lint/a11y/useSemanticElements: row hosts focusable button semantics; nesting <button> would conflict with the parent click handler.
      role="button"
      tabIndex={inert ? -1 : 0}
      aria-label={accessibleLabel}
      data-task-id={task.id}
      onKeyDown={(e) => {
        if (inert) return;
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          handleActivate();
        }
      }}
      onClick={handleActivate}
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
          {isAgent && (
            <span
              title="Task drafted by AI"
              aria-label="AI"
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
        <span
          className="timeline-task-dot__title"
          style={{
            fontSize: "0.88rem",
            color: "var(--bv-fg)",
            overflow: "hidden",
            textOverflow: "ellipsis",
            textDecoration: sStyle.titleStrike ? "line-through" : undefined,
          }}
        >
          {task.title}
        </span>
        {task.description && (
          <span
            style={{
              fontSize: "0.72rem",
              color: "var(--bv-fg-muted, #888)",
              overflow: "hidden",
              textOverflow: "ellipsis",
              whiteSpace: "nowrap",
            }}
          >
            {task.description}
          </span>
        )}
      </span>
    </div>
  );
}
