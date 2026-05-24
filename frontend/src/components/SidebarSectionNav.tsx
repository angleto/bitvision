"use client";

import { type RefObject, useCallback, useEffect, useState } from "react";

interface Section {
  /** Label rendered on the chip. */
  label: string;
  /**
   * Substring to match against ``<h2>`` text content inside the
   * sidebar — first match wins, scroll target. Lowercase compared.
   */
  match: string;
  title?: string;
}

interface Props {
  /** Ref to the scrollable sidebar element ``<aside>``. */
  containerRef: RefObject<HTMLElement | null>;
  /** Sections to expose, in the order they should appear in the chip bar. */
  sections: Section[];
}

/**
 * Sticky chip nav for the viewer right sidebar.
 *
 * The sidebar carries 10+ stacked sections (presets, MPR nav, lighting,
 * tools, ...) which is too many to scroll through visually each time.
 * Rather than collapse every section into an accordion (high-risk
 * refactor), we drop a horizontal chip strip at the top with quick
 * jumps. Clicking a chip smoothly scrolls the sidebar to the matching
 * ``<h2>``; an IntersectionObserver highlights the chip whose section
 * is currently in view, so the bar doubles as a live position map.
 */
export default function SidebarSectionNav({ containerRef, sections }: Props) {
  const [active, setActive] = useState<string | null>(null);

  const findHeading = useCallback(
    (match: string): HTMLElement | null => {
      const root = containerRef.current;
      if (!root) return null;
      // Prefer a stable ``data-section="<match>"`` attribute on any
      // element — this survives i18n of the heading text. Fall back to
      // substring match on h2 text content for legacy sections that
      // haven't been tagged yet.
      const tagged = root.querySelector(
        `[data-section="${CSS.escape(match)}"]`,
      ) as HTMLElement | null;
      if (tagged) return tagged;
      const lower = match.toLowerCase();
      const headings = Array.from(root.querySelectorAll("h2"));
      return (
        (headings.find((h) =>
          (h.textContent || "").toLowerCase().includes(lower),
        ) as HTMLElement | null) || null
      );
    },
    [containerRef],
  );

  // Set up the IntersectionObserver — re-runs when the sidebar mounts
  // or sections change.
  useEffect(() => {
    const root = containerRef.current;
    if (!root) return;
    const targets = sections
      .map((s) => ({ section: s, el: findHeading(s.match) }))
      .filter((t): t is { section: Section; el: HTMLElement } => t.el !== null);
    if (targets.length === 0) return;

    const obs = new IntersectionObserver(
      (entries) => {
        // Pick the topmost entry currently intersecting; falls back to
        // the previously-active one when nothing intersects (between
        // long sections), so the chip bar never "jumps" to nothing.
        const visible = entries
          .filter((e) => e.isIntersecting)
          .sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top);
        if (visible.length > 0) {
          const m = targets.find((t) => t.el === visible[0].target);
          if (m) setActive(m.section.match);
        }
      },
      {
        root,
        rootMargin: "0px 0px -60% 0px",
        threshold: [0, 1],
      },
    );
    for (const t of targets) obs.observe(t.el);
    return () => obs.disconnect();
  }, [containerRef, sections, findHeading]);

  const jump = useCallback(
    (match: string) => {
      const el = findHeading(match);
      if (!el) return;
      // When the target is (or sits inside) a collapsible
      // ``<details data-section="…">``, open it before scrolling so
      // the user lands on visible content, not on a closed summary
      // row. Walking up via ``closest('details')`` lets us also open
      // a parent details that *wraps* a nested ``data-section`` div.
      const details = el.closest("details") as HTMLDetailsElement | null;
      if (details && !details.open) details.open = true;
      el.scrollIntoView({ behavior: "smooth", block: "start" });
      setActive(match);
    },
    [findHeading],
  );

  return (
    <nav
      aria-label="sidebar sections"
      style={{
        // The sidebar layout (``.viewer-layout__sidebar``) is a flex
        // column; this nav sits as the non-scrolling header. No
        // ``position: sticky`` needed — the layout itself keeps the
        // nav anchored, and the underlying content scrolls below.
        display: "flex",
        gap: 4,
        flexWrap: "wrap",
        padding: "0.4rem 0.5rem",
        background: "#0e1118",
      }}
    >
      {sections.map((s) => {
        const isActive = s.match === active;
        return (
          <button
            key={s.match}
            type="button"
            onClick={() => jump(s.match)}
            title={s.title || s.label}
            style={{
              font: "inherit",
              fontSize: "0.72rem",
              padding: "0.2rem 0.55rem",
              borderRadius: 999,
              border: `1px solid ${isActive ? "#e96b1f" : "#2a2f3b"}`,
              background: isActive ? "#e96b1f" : "transparent",
              color: isActive ? "#fff" : "#c0c8d8",
              cursor: "pointer",
              letterSpacing: "0.02em",
              whiteSpace: "nowrap",
            }}
          >
            {s.label}
          </button>
        );
      })}
    </nav>
  );
}
