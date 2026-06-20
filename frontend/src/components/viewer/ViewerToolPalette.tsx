"use client";

// Shared measurement/tool palette for the Cornerstone viewers. Lifted from the
// inline JSX in viewer/series/[id]/page.tsx so the single-series viewer and the
// multiphase contrast grid expose the SAME tools wired to the same
// ``activeTool`` contract that CornerstoneMPRLayout already understands.

import { useTranslations } from "next-intl";
import { TOOL_BUTTONS, TOOL_HINT_KEYS, type Tool } from "./toolTypes";

export interface ViewerToolPaletteProps {
  activeTool: Tool | null;
  /** Toggle a tool; null = back to the default crosshair-only mode. */
  onChange: (tool: Tool | null) => void;
  /** Optional "Clear all annotations" action. */
  onClearAll?: () => void;
  /** Restrict to a subset of tools (e.g. the contrast grid's compact set). */
  tools?: readonly Tool[];
  /** Tighter buttons + no hint line, for a per-pane strip. */
  compact?: boolean;
}

export default function ViewerToolPalette({
  activeTool,
  onChange,
  onClearAll,
  tools,
  compact,
}: ViewerToolPaletteProps) {
  const tv = useTranslations("viewer");
  const buttons = tools ? TOOL_BUTTONS.filter(([t]) => tools.includes(t)) : TOOL_BUTTONS;
  // Larger than the old 0.12rem/0.2rem padding so the tool buttons are
  // legible/clickable (the worst tap-target offenders in the audit). Touch
  // devices additionally get the 44px min via the ``@media (pointer: coarse)``
  // rule on ``.viewer-btn`` in globals.css.
  const btnStyle = compact
    ? { fontSize: "0.72rem", padding: "0.28rem 0.45rem" }
    : { fontSize: "0.76rem", padding: "0.34rem 0.55rem" };

  return (
    <div>
      <div style={{ display: "flex", gap: "0.3rem", flexWrap: "wrap" }}>
        {buttons.map(([t, labelKey]) => (
          <button
            key={t}
            type="button"
            style={btnStyle}
            className={activeTool === t ? "viewer-btn viewer-btn--active" : "viewer-btn"}
            aria-pressed={activeTool === t}
            // Click the active button = deselect (default crosshair-only mode).
            onClick={() => onChange(activeTool === t ? null : t)}
          >
            {tv(labelKey)}
          </button>
        ))}
        {onClearAll && (
          <button
            type="button"
            className="viewer-btn"
            style={{ ...btnStyle, color: "#f66" }}
            onClick={onClearAll}
            title={tv("clearAllTitle")}
          >
            {tv("clearAll")}
          </button>
        )}
      </div>
      {!compact && (
        <p className="meta" style={{ marginTop: "0.3rem", fontSize: "0.65rem" }}>
          {tv(TOOL_HINT_KEYS[activeTool ?? "none"] ?? "hintText")}
        </p>
      )}
    </div>
  );
}
