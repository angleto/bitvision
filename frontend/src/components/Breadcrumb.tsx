"use client";

import { useTranslations } from "next-intl";
import { useState } from "react";

import type { BreadcrumbSegment } from "@/lib/api";

interface Props {
  /**
   * Ordered from root to leaf. The root home segment (folder icon) is always
   * rendered, so a caller passing `[]` still gets a clickable home.
   */
  segments: BreadcrumbSegment[];
  onNavigate: (path: string) => void;
  /**
   * Optional drop handler. When set, every clickable crumb (the home icon
   * + every parent segment, but NOT the current "tail" crumb) becomes a
   * drop target. Dropping a node id onto a crumb fires this callback so
   * the parent can issue a ``tree/move`` against that segment's
   * ``folder_id`` (or root when ``folder_id`` is null). The drop visual
   * uses the same dashed-orange outline as the folder card target.
   *
   * ``copyMode`` is true when Cmd (mac) / Alt (linux+windows) was held
   * during the drop — the parent issues ``add_item_to_folder``
   * (hardlink) instead of ``tree/move``.
   */
  onDropOnSegment?: (
    segment: BreadcrumbSegment,
    draggedId: string | null,
    copyMode: boolean,
  ) => void;
  /**
   * The id currently being dragged (passed through from ContentPane via
   * the parent layout). Used for the drop-target highlight; when null we
   * are not in a drag, so crumbs render normally.
   */
  draggingId?: string | null;
}

/**
 * Drive-style breadcrumb bar. Every segment is clickable (including the home
 * icon) and the tail (current folder) is rendered muted + non-clickable.
 */
export default function Breadcrumb({
  segments,
  onNavigate,
  onDropOnSegment,
  draggingId = null,
}: Props) {
  const tFasc = useTranslations("fascicolo");
  const [hoverIdx, setHoverIdx] = useState<number | null>(null);
  // The patient-tree endpoint already emits the synthetic root as the
  // first segment (``BreadcrumbEntry(id=None, name="Fascicolo")``); we
  // just override its name with the locale-specific label so the
  // breadcrumb reads "Fascicolo / Studi" in IT and "Health record /
  // Studi" in EN. Falling back to a synthetic root when the caller
  // passes ``[]`` keeps the home icon present in edge cases (older
  // API versions, manual ``segments={[]}`` consumers).
  const full: BreadcrumbSegment[] =
    segments.length > 0
      ? [{ ...segments[0], name: tFasc("treeRootLabel") }, ...segments.slice(1)]
      : [{ name: tFasc("treeRootLabel"), path: "/" }];
  return (
    <nav
      aria-label="breadcrumb"
      style={{
        display: "flex",
        flexWrap: "wrap",
        alignItems: "center",
        gap: "0.25rem",
        fontSize: "0.9rem",
        padding: "0.35rem 0",
        color: "var(--bv-muted, #667085)",
      }}
    >
      {full.map((seg, i) => {
        const isLast = i === full.length - 1;
        return (
          <span
            key={`${seg.path}-${i}`}
            style={{ display: "inline-flex", alignItems: "center", gap: "0.25rem" }}
          >
            {i > 0 && <span style={{ color: "#c0c8d8" }}>/</span>}
            {isLast ? (
              <span style={{ color: "var(--bv-fg, #111)", fontWeight: 500 }}>
                {i === 0 ? <HomeIcon /> : null}
                {seg.name}
              </span>
            ) : (
              <button
                type="button"
                onClick={() => onNavigate(seg.path)}
                className="ghost"
                onDragOver={(e) => {
                  if (!onDropOnSegment || !draggingId) return;
                  e.preventDefault();
                  e.dataTransfer.dropEffect = "move";
                  setHoverIdx(i);
                }}
                onDragLeave={() => {
                  if (hoverIdx === i) setHoverIdx(null);
                }}
                onDrop={(e) => {
                  if (!onDropOnSegment) return;
                  e.preventDefault();
                  const id = e.dataTransfer.getData("text/plain") || draggingId || null;
                  setHoverIdx(null);
                  onDropOnSegment(seg, id, e.altKey || e.metaKey);
                }}
                style={{
                  background: "transparent",
                  border: "none",
                  padding: "0.15rem 0.35rem",
                  color: "inherit",
                  cursor: "pointer",
                  borderRadius: 4,
                  display: "inline-flex",
                  alignItems: "center",
                  gap: "0.25rem",
                  outline: hoverIdx === i ? "2px dashed #e96b1f" : undefined,
                  outlineOffset: hoverIdx === i ? 2 : undefined,
                }}
              >
                {i === 0 ? <HomeIcon /> : null}
                {seg.name}
              </button>
            )}
          </span>
        );
      })}
    </nav>
  );
}

function HomeIcon() {
  return (
    <svg
      width="14"
      height="14"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M3 9.5 12 3l9 6.5V21a1 1 0 0 1-1 1h-5v-7H9v7H4a1 1 0 0 1-1-1V9.5z" />
    </svg>
  );
}
