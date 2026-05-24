"use client";

import type { ReactNode } from "react";

interface Props {
  /** Slug used by ``SidebarSectionNav`` chips (``data-section`` attribute). */
  sectionId: string;
  /** Visible heading text shown in the ``<summary>`` row. */
  title: string;
  /** Initial open/closed state. Default ``false``. */
  defaultOpen?: boolean;
  /** Skip rendering entirely when ``false`` — cleaner than wrapping in an ``if``. */
  visible?: boolean;
  /** Optional chip rendered to the right of the title (e.g. "PT", count). */
  badge?: ReactNode;
  /** Optional one-line hint shown muted below the summary when the
   *  section is closed. Keeps essential context visible without opening. */
  hint?: ReactNode;
  /** Section body. */
  children: ReactNode;
}

/**
 * Collapsible right-rail section. Replaces the previous flat ``<h2>``
 * stack so the rail goes from ~21 visible blocks to ~7 grouped chips
 * that the user opens on demand (progressive disclosure).
 *
 * Keeps the existing ``SidebarSectionNav`` contract: each section
 * exposes its slug via ``data-section`` so a chip click can scroll AND
 * open the section in one gesture.
 */
export default function SidebarSection({
  sectionId,
  title,
  defaultOpen = false,
  visible = true,
  badge,
  hint,
  children,
}: Props) {
  if (!visible) return null;
  return (
    <details data-section={sectionId} open={defaultOpen} className="viewer-rail-section">
      <summary className="viewer-rail-section__summary">
        <span className="viewer-rail-section__title">{title}</span>
        {badge ? <span className="viewer-rail-section__badge">{badge}</span> : null}
      </summary>
      {hint ? <div className="viewer-rail-section__hint">{hint}</div> : null}
      <div className="viewer-rail-section__body">{children}</div>
    </details>
  );
}
